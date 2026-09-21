"""From a transcript and a desktop snapshot to one `Decision`. Never raises, never hangs.

Order, and why (docs/PLAN.md sections 3 and 5):

1. Lock first. A locked session refuses before a single byte is normalized or sent.
2. Normalize, then the exact grammar, also over the normalizer's variants. The easy 80
   percent never touches the network: a Jev round trip measured about 315 ms p50 from
   this machine, against well under a millisecond for a grammar parse.
3. Entities resolve locally against trusted lexicon fields (`resolve.py`).
4. Only when the grammar has no parse, or an entity will not resolve, the Jev fan-out
   runs (`requests.py`): concurrent small requests, one round trip.
5. Whatever Jev says is a proposal. Agreement, corroboration and the tier function
   (`tiers.py`) decide what actually happens.

The measured facts that became policy here:

- Jev is not deterministic: on a near-tie P(top) ranged 0.45 to 0.67 and the pick flipped
  4 times in 30 identical requests. So nothing gates on an absolute probability. The
  signal is R2's margin plus agreement between two formulations of the same decision:
  R2 (one forced choice) and R2b (one absolute Boolean per window).
- A forced choice crowns a winner even when the right window is not open. When every
  R2b Boolean is low the answer is "no such window", and the offer is to launch the app,
  not to focus whatever was nearest.
- The service's own `confidence` cannot be recomputed for Score answers (the formula
  failed on 7 of 7 live answers), so it is never read here. Command confidence is the
  minimum over the answers the chosen intent actually read.
- About one small request in two hundred returns 503, and tails reached seconds. A
  failed fan-out degrades to the local scorer's top three as numbered hints.

Privacy invariants (PLAN section 7), all enforced here rather than trusted to callers:
dictated text never enters a request or `last_exchange`; window titles travel only in
R2t and only under the three conditions in `_title_windows`.

Picking. HINTS, ACT_SWAP and a launch offer all carry `Decision.action` as a TEMPLATE:
the pending operation with every slot filled except, for hints, the target. The
understander remembers the template of the last decision that had candidates
(`pending`). The caller passes those candidates back as `picking`; a spoken number n
then re-targets the template at `picking[n - 1]` and is judged by the tier function like
any other action, so a picked tier 0 or 1 action is ACT and a picked close is still a
cancellable COUNTDOWN. A number chooses a target; it does not waive a tier.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from hyprsay.config import Config
from hyprsay.jev.client import JevAuthError, JevTimeout
from hyprsay.jev.types import BooleanAnswer, ChoiceAnswer, Evaluation, ScoreAnswer
from hyprsay.model import (
    Action,
    App,
    Candidate,
    Decision,
    DesktopState,
    Direction,
    Evaluator,
    Intent,
    Parse,
    Slots,
    Transcript,
    Verdict,
    Window,
)
from hyprsay.nlu import bank, requests, resolve, tiers
from hyprsay.nlu.resolve import MAX_HINTS, LexiconLike, Resolution
from hyprsay.nlu.tiers import Evidence

# "did you mean" floor for intents (PLAN section 6), reused as the floor under which a
# window or app is not a plausible alternative worth a swap badge
PLAUSIBLE = 0.15

WINDOW_OPS = frozenset(
    {
        Intent.CLOSE_WINDOW,
        Intent.MOVE_TO_WORKSPACE,
        Intent.FULLSCREEN,
        Intent.TOGGLE_FLOATING,
        Intent.RESIZE_WINDOW,
        Intent.MOVE_WINDOW,
    }
)
NO_TARGET = frozenset(
    {Intent.SWITCH_WORKSPACE, Intent.FOCUS_DIRECTION, Intent.VOLUME, Intent.MEDIA}
)
BARE = frozenset({Intent.HELP, Intent.UNDO, Intent.AGAIN, Intent.CANCEL})
WHILE_PICKING = frozenset({Intent.PICK, Intent.CANCEL, Intent.UNDO})
REQUIRED: dict[Intent, str] = {
    Intent.SWITCH_WORKSPACE: "workspace",
    Intent.MOVE_TO_WORKSPACE: "workspace",
    Intent.FOCUS_DIRECTION: "direction",
    Intent.MOVE_WINDOW: "direction",
    Intent.VOLUME: "verb",
    Intent.MEDIA: "verb",
}
# an utterance that opens with one of these is dictation. If the grammar could not parse
# it, it still never goes to Jev: what follows the carrier is the user's text.
TYPING_CARRIERS = frozenset({"type", "say", "dictate", "write"})
# every way a recognizer writes those verbs. A closed list, not a fuzzy match: similarity
# scored "rid" at 0.83 against "write", which swallowed "get rid of the music", while the
# case that actually leaks, "typed" for "type", scored only 0.68. Code calculates.
DICTATION_OPENERS = frozenset(
    {
        "type", "typed", "types", "typing", "typo",
        "say", "says", "said", "saying", "sey",
        "write", "writes", "writing", "written", "wrote",
        # "right" is deliberately absent although it is how a recognizer often writes
        # "write": it is also the direction word, and treating "move this right" as
        # dictation would break a core command to guard a rarer phrasing of a rarer one
        "dictate", "dictated", "dictates", "dictating",
    }
)  # fmt: skip
# how far in to look. "I'll type ..." and "let me say ..." put the verb second or third;
# past that a carrier is far more likely to be an ordinary word.
DICTATION_LOOKAHEAD = 3


def _sounds_like_dictation(literal: tuple[str, ...] | list[str]) -> bool:
    """Does this utterance open with a carrier verb, however it came out of the recognizer?

    An exact match on the first token is not enough. "Typed my password is hunter2" and
    "I'll type my password" both carry text the speaker never meant to send anywhere, and
    both slip past `literal[0] in TYPING_CARRIERS`.
    """
    return any(word in DICTATION_OPENERS for word in list(literal)[:DICTATION_LOOKAHEAD])


STARTERS = (Intent.FOCUS_WINDOW, Intent.SWITCH_WORKSPACE, Intent.LAUNCH_APP)


@dataclass(frozen=True)
class _Need:
    """The grammar settled the intent and the slots; one entity would not resolve."""

    parse: Parse
    wants: str  # "window" | "app"
    resolution: Resolution = field(default_factory=Resolution)


@dataclass
class _Answers:
    """One fan-out's replies, merged by request family."""

    r1: dict[str, Any] = field(default_factory=dict)
    relative: ChoiceAnswer | None = None
    absolute: dict[str, float] = field(default_factory=dict)
    titled: ChoiceAnswer | None = None
    apps: list[ChoiceAnswer] = field(default_factory=list)
    failed: dict[str, Exception] = field(default_factory=dict)
    late: bool = False

    def failure(self, *families: str) -> Exception | None:
        return next((self.failed[f] for f in families if f in self.failed), None)


class Understander:
    def __init__(
        self,
        cfg: Config,
        lexicon: LexiconLike,
        grammar: Any,
        evaluator: Evaluator | None,
        *,
        normalizer: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self.lexicon = lexicon
        self.grammar = grammar
        self.evaluator = evaluator
        self._clock = clock
        if normalizer is None:
            from hyprsay.nlu.normalize import normalize as normalizer
        self._normalizer = normalizer
        # the operation waiting for a number; see "Picking" in the module docstring
        self.pending: Action | None = None
        # what was sent to Jev and what came back, for `inspect --last`. Dictated text
        # never appears here: no request is ever built for an utterance that carries it.
        self.last_exchange: dict[str, Any] = {}

    # ----------------------------------------------------------------------- entry

    async def understand(
        self,
        transcript: Transcript,
        state: DesktopState,
        *,
        pinned_address: str = "",
        picking: tuple[Candidate, ...] = (),
    ) -> Decision:
        self.last_exchange = {
            "bank": bank.VERSION,
            "path": "",
            "requests": [],
            "answers": {},
            "errors": {},
        }
        try:
            decision = await self._decide(transcript, state, pinned_address, tuple(picking))
        except Exception as exc:  # the engine must never die on one utterance
            self.last_exchange["path"] = "error"
            self.last_exchange["errors"]["internal"] = type(exc).__name__
            decision = Decision(
                Verdict.REFUSE,
                reason=f"internal error while understanding ({type(exc).__name__}); nothing done",
            )
        waits = decision.verdict in (Verdict.HINTS, Verdict.ACT_SWAP, Verdict.SUGGEST)
        self.pending = decision.action if waits and decision.candidates else None
        return decision

    async def _decide(
        self,
        transcript: Transcript,
        state: DesktopState,
        pinned_address: str,
        picking: tuple[Candidate, ...],
    ) -> Decision:
        if state.locked:
            self.last_exchange["path"] = "locked"
            return Decision(Verdict.REFUSE, reason="session is locked")
        norm = self._normalize(transcript.text)
        if not norm.text.strip():
            self.last_exchange["path"] = "empty"
            return Decision(Verdict.NOTHING, reason="nothing was heard")
        if picking:
            self.last_exchange["path"] = "picking"
            return self._pick(norm, _heard(norm, None), picking, state)

        # the grammar tries the utterance and then each variant itself (grammar.py)
        parse = self.grammar.parse(norm, picking=False)
        heard = _heard(norm, parse)
        need: _Need | None = None
        if parse is not None and parse.intent is not Intent.NONE:
            self.last_exchange["path"] = "grammar"
            outcome = self._from_grammar(parse, heard, state, pinned_address)
            if isinstance(outcome, Decision):
                return outcome
            need = outcome
        return await self._semantic(heard, state, pinned_address, need)

    # ----------------------------------------------------------------------- language

    def _normalize(self, text: str) -> Any:
        return self._normalizer(text, vocabulary=self.lexicon.words(), aliases=self.cfg.aliases)

    def _examples(self, *intents: Intent, each: int = 2) -> tuple[str, ...]:
        out: list[str] = []
        for intent in intents:
            try:
                out.extend(tuple(self.grammar.examples(intent))[:each])
            except Exception:  # examples are garnish; a grammar bug must not cost the decision
                continue
        return tuple(out)

    # ----------------------------------------------------------------------- picking

    def _pick(
        self, norm: Any, heard: _Heard, picking: tuple[Candidate, ...], state: DesktopState
    ) -> Decision:
        template = self.pending
        parse = self.grammar.parse(norm, picking=True)

        def again(reason: str) -> Decision:
            return Decision(
                Verdict.HINTS, template, picking, reason=reason, heard=heard.said, tier=0
            )

        if parse is None or parse.intent not in WHILE_PICKING:
            return again("say a number, or cancel")
        if parse.intent is not Intent.PICK:
            return Decision(Verdict.ACT, Action(parse.intent), tier=0, heard=heard.said)
        number = parse.slots.number or 0
        if not 1 <= number <= len(picking):
            return again(f"there is no number {number}")
        if template is None:
            return Decision(
                Verdict.REFUSE, reason="nothing is waiting for a number", heard=heard.said
            )
        chosen = picking[number - 1]
        if template.intent is Intent.LAUNCH_APP:
            if chosen.app is None or not chosen.app.trusted:
                return Decision(Verdict.REFUSE, reason="that is not an app that can be launched")
            action = replace(template, app=chosen.app, window=None)
        else:
            live = state.by_address(chosen.window.address) if chosen.window else None
            if live is None:
                return Decision(Verdict.REFUSE, reason="that window is gone", heard=heard.said)
            action = replace(template, window=live, app=None)
        # a hinted tier 2 action only exists if its verb was said: `_hints` refuses otherwise
        evidence = Evidence(source="pick", corroborated=True, verb_said=True, explicit_target=True)
        return self._finish(action, evidence, (replace(chosen, corroborated=True),), heard, state)

    # ----------------------------------------------------------------------- grammar path

    def _from_grammar(
        self, parse: Parse, heard: _Heard, state: DesktopState, pinned_address: str
    ) -> Decision | _Need:
        intent, slots = parse.intent, parse.slots
        if intent is Intent.TYPE_TEXT or slots.text is not None:
            return self._typing(parse, state, pinned_address)
        if intent is Intent.PICK:
            return Decision(
                Verdict.REFUSE, reason="there is nothing to pick from", heard=heard.said
            )
        if intent in BARE or intent not in bank.INTENTS:
            # lock and anything session-level land here: tier 3, grammar only
            evidence = Evidence(source="grammar", corroborated=True, explicit_target=True)
            return self._finish(Action(intent), evidence, (), heard, state)
        missing = self._missing(intent, slots)
        if missing:
            return self._suggest(heard, f"say which {missing}", intent)
        template = _template(intent, slots)
        if intent in NO_TARGET:
            evidence = Evidence(source="grammar", corroborated=True, explicit_target=True)
            return self._finish(template, evidence, (), heard, state)
        phrase = slots.window_ref or slots.app_ref or ""
        if intent is Intent.LAUNCH_APP:
            return self._launch_locally(parse, phrase, template, heard, state)
        if not phrase:
            if intent is Intent.FOCUS_WINDOW:
                return self._suggest(heard, "say which window", intent)
            return self._pointed(template, heard, state, pinned_address, said=True)
        return self._window_locally(parse, phrase, template, heard, state)

    def _missing(self, intent: Intent, slots: Slots) -> str:
        name = REQUIRED.get(intent, "")
        return name if name and getattr(slots, name) is None else ""

    def _launch_locally(
        self, parse: Parse, phrase: str, template: Action, heard: _Heard, state: Any
    ) -> Decision | _Need:
        if not phrase:
            return _Need(parse, "app")
        found = resolve.resolve_app(phrase, heard.text, self.lexicon, self.cfg.gates)
        if found.status == "none":
            return _Need(parse, "app", found)
        if found.status == "ambiguous":
            return self._hints(template, found.candidates, "several apps match", heard, state)
        return self._launch(found.app, found.candidates, Evidence(source="grammar"), heard, state)

    def _launch(
        self,
        app: App,
        candidates: tuple[Candidate, ...],
        evidence: Evidence,
        heard: _Heard,
        state: DesktopState,
    ) -> Decision:
        if not app.trusted:
            return Decision(
                Verdict.REFUSE,
                reason=f"{app.name} comes from a user-writable desktop file that has not been "
                "approved; run hyprsay doctor",
                heard=heard.said,
            )
        chosen = next((c for c in candidates if c.app is app), None)
        evidence = replace(
            evidence,
            corroborated=bool(chosen and chosen.corroborated),
            explicit_target=True,
            plausible=len(candidates),
        )
        return self._finish(Action(Intent.LAUNCH_APP, app=app), evidence, candidates, heard, state)

    def _window_locally(
        self, parse: Parse, phrase: str, template: Action, heard: _Heard, state: Any
    ) -> Decision | _Need:
        gates = self.cfg.gates
        found = resolve.resolve_window(phrase, heard.text, heard.tokens, state, self.lexicon, gates)
        if found.exact:
            evidence = Evidence(
                source="grammar",
                corroborated=found.candidates[0].corroborated,
                verb_said=tiers.verb_said(parse.intent, heard.literal),
                explicit_target=True,
                by_title=found.by_title,
                plausible=len(found.candidates),
            )
            action = replace(template, window=found.window)
            return self._finish(action, evidence, found.candidates, heard, state)
        if found.status == "ambiguous":
            windows = tuple(c.window for c in found.candidates if c.window)
            narrowed = _movable(template, list(windows), state)
            if narrowed is not None and len(narrowed) == 1:
                only = next(c for c in found.candidates if c.window is narrowed[0])
                evidence = Evidence(
                    source="grammar",
                    corroborated=only.corroborated,
                    verb_said=tiers.verb_said(parse.intent, heard.literal),
                    explicit_target=True,
                    plausible=1,
                )
                return self._finish(
                    replace(template, window=only.window), evidence, (only,), heard, state
                )
            arbitrable = (
                found.same_app
                and found.residual
                and self._titles_allowed(template, heard, state)
                and self._jev_on()
            )
            if arbitrable:
                return _Need(parse, "window", found)
            # "focus kitty" with four kitty windows: no model can know (PLAN 5.4)
            reason = f"{len(windows)} windows match, so pick a number"
            return self._hints(template, found.candidates, reason, heard, state)
        if parse.intent is Intent.FOCUS_WINDOW:
            offer = self._launch_offer(phrase, heard)
            if offer is not None:
                return offer
        return _Need(parse, "window", found)

    def _pointed(
        self,
        template: Action,
        heard: _Heard,
        state: DesktopState,
        pinned_address: str,
        *,
        said: bool,
        evidence: Evidence | None = None,
    ) -> Decision:
        """The target is the window focused at key DOWN, never whatever is focused now."""
        window = _pinned(state, pinned_address)
        if window is None:
            return Decision(
                Verdict.REFUSE, reason="the window you were pointing at is gone", heard=heard.said
            )
        evidence = replace(
            evidence or Evidence(source="grammar"),
            corroborated=True,
            explicit_target=said,
            verb_said=tiers.verb_said(template.intent, heard.literal),
        )
        candidate = resolve.window_candidate(self.lexicon, heard.text, window, 1.0)
        action = replace(template, window=window)
        return self._finish(action, evidence, (candidate,), heard, state)

    def _launch_offer(self, phrase: str, heard: _Heard) -> Decision | None:
        found = resolve.resolve_app(phrase, heard.text, self.lexicon, self.cfg.gates)
        if not found.exact or not found.app.trusted:
            return None
        return self._offer(found.app, found.candidates[0], heard)

    def _offer(self, app: App, candidate: Candidate, heard: _Heard) -> Decision:
        """All R2b low, or no live match: offer to launch rather than focus the nearest."""
        return Decision(
            Verdict.SUGGEST,
            Action(Intent.LAUNCH_APP, app=app),
            (candidate,),
            tier=1,
            reason=f"no {app.name} window is open; say 1 to launch it",
            suggestions=(f"open {app.name.lower()}",),
            heard=heard.said,
        )

    # ----------------------------------------------------------------------- typing

    def _typing(self, parse: Parse, state: DesktopState, pinned_address: str) -> Decision:
        """PLAN 5.6. The text is never normalized into a request, a reason or `heard`."""
        raw = parse.slots.text or ""
        text = tiers.clean_typed_text(raw, self.cfg.safety.type_max_chars)
        heard = f"type text ({len(text)} characters)"
        if parse.intent is not Intent.TYPE_TEXT:
            return Decision(Verdict.REFUSE, reason="only a typing command may carry text")
        window = _pinned(state, pinned_address)
        kind = self.lexicon.kind_of(window) if window else ""
        refusal = tiers.typing_refusal(window, kind, state, self.cfg)
        if refusal:
            return Decision(Verdict.REFUSE, tier=2, reason=refusal, heard=heard)
        if not text:
            return Decision(Verdict.REFUSE, tier=2, reason="there was nothing to type", heard=heard)
        action = Action(Intent.TYPE_TEXT, window=window, text=text)
        level = tiers.tier(Intent.TYPE_TEXT, action, state, self.cfg)
        # the carrier phrase the grammar matched IS the literally spoken verb
        evidence = Evidence(
            source="grammar", corroborated=True, verb_said=True, explicit_target=True
        )
        verdict, reason = tiers.verdict(level, evidence)
        return Decision(verdict, action, tier=level, reason=reason, heard=heard)

    # ----------------------------------------------------------------------- semantic path

    def _jev_on(self) -> bool:
        return self.cfg.jev.enabled and self.evaluator is not None

    async def _semantic(
        self, heard: _Heard, state: DesktopState, pinned_address: str, need: _Need | None
    ) -> Decision:
        if need is None and _sounds_like_dictation(heard.literal):
            self.last_exchange["path"] = "local"
            return self._dictation(heard)
        if len(heard.said) > requests.MAX_UTTERANCE_CHARS:
            self.last_exchange["path"] = "local"
            # far too long to be a command: hearing it again changes nothing
            return self._suggest(
                heard, bank.unsupported_message("long_dictation"), rehearable=False
            )
        if not self._jev_on():
            return self._degrade(heard, state, need, "Jev is turned off")

        self.last_exchange["path"] = "jev"
        fan, local_app = self._fan_out(heard, state, need)
        answers = await self._ask(fan)
        if need is not None:
            reading = _Reading(need.parse.intent, need.parse.slots, [], grammar=True)
        else:
            reading = self._read_utterance(answers, heard)
            if isinstance(reading, Decision):
                return reading
            if isinstance(reading, Exception):
                return self._degrade(heard, state, None, _plain(reading), reading)
        return self._resolve_remote(
            reading, fan, answers, local_app, heard, state, pinned_address, need
        )

    def _fan_out(
        self, heard: _Heard, state: DesktopState, need: _Need | None
    ) -> tuple[requests.FanOut, App | None]:
        cap = self.cfg.jev.token_cap
        base = requests.utterance_state(heard.said, heard.variants)
        built: list[requests.Request] = []
        windows: dict[str, Window] = {}
        titled: dict[str, Window] = {}
        apps: dict[str, App] = {}
        dropped_windows = dropped_apps = 0
        if need is None:
            built += requests.split("r1", base, bank.utterance_questions(), cap)
        if need is None or need.wants == "window":
            made, windows, dropped_windows = requests.window_requests(
                base, state.windows, self._describe, cap
            )
            built += made
            shared = self._title_windows(heard, state, need)
            if shared:
                one, titled = requests.title_request(
                    base, shared, self._describe, self.lexicon.kind_of, self.cfg.privacy, cap
                )
                built += [one] if one else []
        local_app: App | None = None
        if need is None or need.wants == "app":
            phrase = (need.parse.slots.app_ref or "") if need else ""
            found = resolve.resolve_app(
                phrase or heard.text, heard.text, self.lexicon, self.cfg.gates
            )
            if found.exact:
                local_app = found.app  # the lexicon already knows: R3 is skipped
            else:
                made, apps, dropped_apps = requests.app_requests(base, self._installed(heard), cap)
                built += made
        fan = requests.FanOut(tuple(built), windows, titled, apps, dropped_windows, dropped_apps)
        self.last_exchange["requests"] = [r.as_sent() for r in fan.requests]
        self.last_exchange["dropped"] = {"windows": dropped_windows, "apps": dropped_apps}
        return fan, local_app

    def _describe(self, window: Window) -> dict[str, Any]:
        app = self.lexicon.app_for_window(window)
        return requests.describe_window(
            window, app.name if app else "", self.lexicon.kind_of(window)
        )

    def _installed(self, heard: _Heard) -> list[App]:
        """Every installed app, when the lexicon exposes them; else its nearest matches."""
        listing = getattr(self.lexicon, "apps", None)
        listing = listing() if callable(listing) else listing
        if isinstance(listing, dict):
            listing = listing.values()
        if listing is None:
            listing = [app for app, _ in self.lexicon.match_apps(heard.text, limit=40)]
        return [app for app in listing if app.trusted]

    def _titles_allowed(self, template: Action | None, heard: _Heard, state: DesktopState) -> bool:
        if self.cfg.privacy.titles != "when_needed":
            return False
        # titles never help choose a tier 2 target (PLAN 5.6 and 7), so they are not sent
        # for one: neither when the grammar says close, nor when the speaker said the verb
        if set(heard.literal) & tiers.DESTRUCTIVE_VERBS:
            return False
        if template is None:
            return True
        return tiers.tier(template.intent, template, state, self.cfg) < 2

    def _title_windows(
        self, heard: _Heard, state: DesktopState, need: _Need | None
    ) -> tuple[Window, ...]:
        """The only door a title can leave through. All three conditions, or nothing:
        titles are enabled, two or more live windows share the app that a TRUSTED field
        selected, and the utterance has content words left to tell them apart with."""
        template = _template(need.parse.intent, need.parse.slots) if need else None
        if not self._titles_allowed(template, heard, state):
            return ()
        if need is not None and need.resolution.status == "ambiguous":
            shared = tuple(c.window for c in need.resolution.candidates if c.window)
            shared = shared if need.resolution.same_app else ()
        else:
            shared = resolve.spoken_app_windows(heard.text, state, self.lexicon)
        if len(shared) < 2:
            return ()
        residual = resolve.residual_words(heard.tokens, self.lexicon.words())
        return shared if residual else ()

    async def _ask(self, fan: requests.FanOut) -> _Answers:
        async def one(request: requests.Request) -> Evaluation | Exception:
            try:
                return await self.evaluator.evaluate(request.state, request.questions)
            except Exception as exc:
                return exc

        started = self._clock()
        try:
            # the client has its own deadline; this one only guarantees we never hang
            async with asyncio.timeout(self.cfg.jev.late_answer_s):
                replies = await asyncio.gather(*(one(r) for r in fan.requests))
        except TimeoutError:
            late = JevTimeout(f"no answer within {self.cfg.jev.late_answer_s:.1f} s")
            replies = [late for _ in fan.requests]
        elapsed = self._clock() - started
        answers = _Answers(late=elapsed > self.cfg.jev.deadline_s)
        self.last_exchange["elapsed_ms"] = round(elapsed * 1000, 1)
        for request, reply in zip(fan.requests, replies, strict=True):
            family = request.name.split(".")[0]
            if isinstance(reply, Exception):
                answers.failed[family] = reply
                self.last_exchange["errors"][request.name] = _plain(reply)
                continue
            self.last_exchange["answers"][request.name] = _loggable(reply)
            _merge(answers, family, reply)
        return answers

    def _read_utterance(self, answers: _Answers, heard: _Heard) -> _Reading | Decision | Exception:
        failure = answers.failure("r1")
        if failure is not None:
            return failure
        gates, r1 = self.cfg.gates, answers.r1
        addressed = r1["addressed"].probability
        if addressed < gates.addressed:
            return Decision(Verdict.NOTHING, reason="that was not meant for the computer")
        unsupported: ChoiceAnswer = r1["unsupported_kind"]
        if unsupported.choice != bank.NONE and unsupported.margin >= gates.margin:
            # understood perfectly, and it is something hyprsay does not do
            return self._suggest(
                heard, bank.unsupported_message(unsupported.choice), rehearable=False
            )
        intent: ChoiceAnswer = r1["intent"]
        close = [Intent(k) for k, p in intent.ranked if k != bank.NONE and p >= PLAUSIBLE][:3]
        if intent.choice == bank.NONE:
            if not close:
                return Decision(Verdict.NOTHING, reason="no command was recognized")
            return self._suggest(heard, "not understood; did you mean one of these", *close)
        if intent.margin < gates.margin:
            return self._suggest(heard, "not sure what you meant; did you mean", *close)
        chosen = Intent(intent.choice)
        slots, reads, doubt = self._read_slots(chosen, r1, heard)
        if doubt:
            return self._suggest(heard, doubt, chosen)
        return _Reading(chosen, slots, [addressed, intent.p1, *reads])

    def _read_slots(
        self, intent: Intent, r1: dict[str, Any], heard: _Heard
    ) -> tuple[Slots, list[float], str]:
        """Only the slots this intent reads. Returns (slots, probabilities read, doubt)."""
        gate = self.cfg.gates.margin
        fields: dict[str, Any] = {}
        reads: list[float] = []
        if REQUIRED.get(intent) == "workspace":
            answer: ChoiceAnswer = r1["workspace"]
            if answer.choice == bank.NONE_MENTIONED or answer.margin < gate:
                return Slots(), [], "say which workspace"
            # code calculates (PLAN section 3): a number Jev reports must have been said
            if answer.choice.isdigit() and answer.choice not in heard.every_word:
                return Slots(), [], "say the workspace number"
            fields["workspace"] = answer.choice
            reads.append(answer.p1)
        direction: ChoiceAnswer = r1["direction"]
        has_direction = direction.choice != bank.NONE_MENTIONED and direction.margin >= gate
        if REQUIRED.get(intent) == "direction" or (
            intent is Intent.RESIZE_WINDOW and has_direction
        ):
            if not has_direction:
                return Slots(), [], "say which direction"
            fields["direction"] = Direction(direction.choice)
            reads.append(direction.p1)
        allowed = {
            Intent.VOLUME: bank.VOLUME_VERBS,
            Intent.MEDIA: bank.MEDIA_VERBS,
            Intent.RESIZE_WINDOW: bank.RESIZE_VERBS,
        }.get(intent)
        if allowed:
            verb: ChoiceAnswer = r1["verb"]
            if verb.choice in allowed and verb.margin >= gate:
                fields["verb"] = verb.choice
                reads.append(verb.p1)
            elif intent is not Intent.RESIZE_WINDOW or "direction" not in fields:
                return Slots(), [], "say what to do"
        if intent is Intent.VOLUME:
            fields["number"] = next(
                (int(t) for t in heard.literal if t.isdigit() and int(t) <= 100), None
            )
            if fields["verb"] == "set" and fields["number"] is None:
                return Slots(), [], "say the volume as a number"
        if intent in (Intent.RESIZE_WINDOW, Intent.VOLUME):
            # a magnitude, not a decision: it is used, and left out of the confidence
            level = max(0, min(4, round(r1["amount"].score)))
            fields["amount"] = None if level == 2 else level
        return Slots(**fields), reads, ""

    def _resolve_remote(
        self,
        reading: _Reading,
        fan: requests.FanOut,
        answers: _Answers,
        local_app: App | None,
        heard: _Heard,
        state: DesktopState,
        pinned_address: str,
        need: _Need | None,
    ) -> Decision:
        intent = reading.intent
        template = _template(intent, reading.slots)
        evidence = Evidence(source="grammar" if reading.grammar else "jev", late=answers.late)
        if intent in NO_TARGET:
            self._note(reading)
            evidence = replace(evidence, corroborated=True, explicit_target=True)
            return self._finish(template, evidence, (), heard, state)
        if intent is Intent.LAUNCH_APP:
            return self._launch_remote(
                reading, fan, answers, local_app, evidence, heard, state, need
            )
        if intent in WINDOW_OPS and not reading.grammar:
            names = answers.r1["names_window"].probability
            deictic = answers.r1["deictic"].probability
            present = self.cfg.gates.spoken  # about the utterance, not about a window
            if names < present or deictic > names:
                # "this" and "it" are names_window = false, combined in code with the
                # window pinned at key down: deixis is never an option beside real windows
                reading.reads.append(max(deictic, 1.0 - names))
                self._note(reading)
                return self._pointed(
                    template,
                    heard,
                    state,
                    pinned_address,
                    said=deictic >= present,
                    evidence=evidence,
                )
            reading.reads.append(names)
        failure = answers.failure("r2", "r2b")
        if failure is not None:
            return self._degrade(heard, state, need, _plain(failure), failure, reading)
        if answers.relative is None or not answers.absolute:
            # nothing is open at all, so nothing was asked: the same "no such window"
            return self._no_such_window(reading, fan, answers, local_app, heard)
        return self._window_remote(
            reading, template, fan, answers, local_app, evidence, heard, state, need
        )

    def _window_remote(
        self,
        reading: _Reading,
        template: Action,
        fan: requests.FanOut,
        answers: _Answers,
        local_app: App | None,
        evidence: Evidence,
        heard: _Heard,
        state: DesktopState,
        need: _Need | None,
    ) -> Decision:
        gates = self.cfg.gates
        relative, absolute = answers.relative, answers.absolute
        top_rel = relative.ranked[0][0]
        by_abs = sorted(absolute, key=lambda k: absolute[k], reverse=True)
        top_abs = by_abs[0]
        # the runner-up that matters belongs to a DIFFERENT app. Two Firefox windows both
        # answer to "the firefox with kittens", so both score high and neither stands out
        # from the other; that is a tie to break below, not an absent window.
        owner = resolve._owner(self.lexicon, fan.windows[top_abs])
        runner_up = max(
            (
                absolute[key]
                for key in by_abs[1:]
                if resolve._owner(self.lexicon, fan.windows[key]) != owner
            ),
            default=0.0,
        )
        # a Choice always crowns a winner, even when the right window is not open. The
        # booleans are the absolute signal, and they run low: presence is a floor plus a
        # standout over the runner-up (see config.Gates for the measured distribution)
        stands_out = absolute[top_abs] - runner_up >= gates.standout
        # when the lexicon itself tied several open windows, they exist by construction:
        # the question left is WHICH, and asking "is one present" would wrongly say no
        established = need is not None and need.resolution.status == "ambiguous"
        if not established and (absolute[top_abs] < gates.present or not stands_out):
            return self._no_such_window(reading, fan, answers, local_app, heard)

        def both(key: str) -> float:
            return (relative.probabilities.get(key, 0.0) + absolute.get(key, 0.0)) / 2

        plausible = [
            key
            for key in sorted(fan.windows, key=both, reverse=True)
            if relative.probabilities.get(key, 0.0) >= PLAUSIBLE
            or absolute.get(key, 0.0) >= gates.present
        ]
        candidates = {
            key: resolve.window_candidate(self.lexicon, heard.text, fan.windows[key], both(key))
            for key in plausible
        }
        agree = top_rel == top_abs
        wide = relative.margin >= gates.margin
        chosen, by_title = top_rel, False
        contenders = [key for key in plausible if candidates[key].corroborated]
        ambiguous = False
        if need is not None and need.resolution.status == "ambiguous":
            tied = {c.window.address for c in need.resolution.candidates if c.window}
            contenders = [key for key in fan.windows if fan.windows[key].address in tied]
            for key in contenders:
                candidates.setdefault(
                    key,
                    resolve.window_candidate(self.lexicon, heard.text, fan.windows[key], both(key)),
                )
            agree = wide = False  # the lexicon already called it a tie; only a title can break it
            ambiguous = True
        if not ambiguous:
            # every live window the utterance corroborates through a TRUSTED field, not only
            # the ones Jev found plausible. Two Firefox windows both answer to "the browser",
            # and Jev will still sound sure of one of them, from recency alone. That is a
            # guess, not evidence: when two windows share the corroborating identity nothing
            # trusted separates them, however confident the model is (PLAN 5.6).
            for key in fan.windows:
                candidates.setdefault(
                    key,
                    resolve.window_candidate(self.lexicon, heard.text, fan.windows[key], both(key)),
                )
            rivals = [key for key in fan.windows if candidates[key].corroborated]
            if len(rivals) >= 2:
                contenders = sorted(rivals, key=both, reverse=True)
        narrowed = _movable(template, [fan.windows[k] for k in contenders], state)
        if narrowed is not None:
            contenders = [k for k in contenders if fan.windows[k] in narrowed]
            if len(contenders) == 1:
                # a rule, not a guess: the only rival that is not already there
                chosen, agree, wide = contenders[0], True, True
        tie = len(contenders) >= 2
        if tie:
            # two candidates share the corroborating field and nothing trusted separates
            # them (PLAN 5.6): a discriminating title token may break the tie, else hints
            winner = self._by_title(contenders, fan, answers, heard)
            if winner is None:
                shown = tuple(candidates[k] for k in contenders[:MAX_HINTS])
                return self._hints(
                    template, shown, "several windows fit, so pick a number", heard, state
                )
            chosen, by_title, agree, wide = winner, True, True, True
        reading.reads += [relative.probabilities.get(chosen, 0.0), absolute.get(chosen, 0.0)]
        self._note(reading)
        ordered = [chosen, *(k for k in plausible if k != chosen)][:MAX_HINTS]
        evidence = replace(
            evidence,
            corroborated=candidates[chosen].corroborated,
            agree=agree,
            wide_margin=wide,
            verb_said=tiers.verb_said(reading.intent, heard.literal),
            explicit_target=True,
            by_title=by_title,
            plausible=len(plausible),
        )
        action = replace(template, window=fan.windows[chosen])
        return self._finish(action, evidence, tuple(candidates[k] for k in ordered), heard, state)

    def _by_title(
        self, contenders: list[str], fan: requests.FanOut, answers: _Answers, heard: _Heard
    ) -> str | None:
        windows = [fan.windows[key] for key in contenders]
        residual = resolve.residual_words(heard.tokens, self.lexicon.words())
        local = self.lexicon.title_discriminates(residual, windows) if residual else None
        address = local.address if local is not None else ""
        titled = answers.titled
        if (
            not address
            and titled is not None
            and titled.choice != bank.CANNOT_TELL
            and titled.margin >= self.cfg.gates.margin
        ):
            address = fan.titled[titled.choice].address
        return next((k for k in contenders if fan.windows[k].address == address), None)

    def _no_such_window(
        self,
        reading: _Reading,
        fan: requests.FanOut,
        answers: _Answers,
        local_app: App | None,
        heard: _Heard,
    ) -> Decision:
        self._note(reading)
        if reading.intent is Intent.FOCUS_WINDOW:
            app = local_app
            if app is None:
                ranked = self._ranked_apps(fan, answers)
                if ranked and ranked[0][1] >= self.cfg.gates.app_offer:
                    app = ranked[0][0]
            if app is not None and app.trusted:
                return self._offer(app, resolve.app_candidate(self.lexicon, heard.text, app), heard)
        return self._suggest(heard, "no open window matches that", reading.intent)

    def _ranked_apps(self, fan: requests.FanOut, answers: _Answers) -> list[tuple[App, float]]:
        scored = [
            (fan.apps[key], p)
            for shard in answers.apps
            for key, p in shard.probabilities.items()
            if key in fan.apps
        ]
        return sorted(scored, key=lambda ap: -ap[1])

    def _launch_remote(
        self,
        reading: _Reading,
        fan: requests.FanOut,
        answers: _Answers,
        local_app: App | None,
        evidence: Evidence,
        heard: _Heard,
        state: DesktopState,
        need: _Need | None,
    ) -> Decision:
        if local_app is not None:
            self._note(reading)
            candidate = resolve.app_candidate(self.lexicon, heard.text, local_app, 1.0)
            return self._launch(local_app, (candidate,), evidence, heard, state)
        failure = answers.failure("r3")
        if failure is not None:
            return self._degrade(heard, state, need, _plain(failure), failure, reading)
        ranked = [(a, p) for a, p in self._ranked_apps(fan, answers) if p >= PLAUSIBLE]
        if not ranked:
            return self._suggest(heard, "no installed app matches that", Intent.LAUNCH_APP)
        runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
        reading.reads.append(ranked[0][1])
        self._note(reading)
        candidates = tuple(
            resolve.app_candidate(self.lexicon, heard.text, a, p) for a, p in ranked[:MAX_HINTS]
        )
        evidence = replace(evidence, wide_margin=ranked[0][1] - runner_up >= self.cfg.gates.margin)
        return self._launch(ranked[0][0], candidates, evidence, heard, state)

    # ----------------------------------------------------------------------- outcomes

    def _note(self, reading: _Reading) -> None:
        self.last_exchange["decided"] = {
            "intent": reading.intent.value,
            "source": "grammar" if reading.grammar else "jev",
            # the minimum over the answers this intent read. Never the service's own field.
            "confidence": round(min(reading.reads), 3) if reading.reads else 1.0,
        }

    def _finish(
        self,
        action: Action,
        evidence: Evidence,
        candidates: tuple[Candidate, ...],
        heard: _Heard,
        state: DesktopState,
    ) -> Decision:
        if action.workspace == "here":
            # "bring the browser here": the one place a spoken workspace needs live state
            action = replace(action, workspace=str(state.active_workspace_id))
        level = tiers.tier(action.intent, action, state, self.cfg)
        verdict, reason = tiers.verdict(level, evidence)
        shown = candidates[:MAX_HINTS]
        if verdict is Verdict.HINTS and not shown:
            return self._suggest(heard, reason, action.intent)
        if verdict is Verdict.HINTS:
            # a template, not an order: the target is blank until a number fills it in
            return Decision(
                verdict,
                replace(action, window=None, app=None),
                shown,
                tier=level,
                reason=reason,
                heard=heard.said,
            )
        if verdict is Verdict.REFUSE:
            return Decision(verdict, tier=level, reason=reason, heard=heard.said)
        if verdict is not Verdict.ACT_SWAP:
            shown = shown[:1]
        return Decision(verdict, action, shown, tier=level, reason=reason, heard=heard.said)

    def _hints(
        self,
        template: Action,
        candidates: tuple[Candidate, ...],
        reason: str,
        heard: _Heard,
        state: DesktopState,
    ) -> Decision:
        level = tiers.tier(template.intent, template, state, self.cfg)
        if level >= 2 and not tiers.verb_said(template.intent, heard.literal):
            # even a numbered pick may not turn an unspoken verb into a disruptive action
            verdict, why = tiers.verdict(level, Evidence(source="jev"))
            return Decision(verdict, tier=level, reason=why, heard=heard.said)
        if not candidates:
            return self._suggest(heard, reason, template.intent)
        swap = self._most_recent_rival(template, candidates, level, state)
        if swap is not None:
            return Decision(
                Verdict.ACT_SWAP,
                replace(template, window=swap.window),
                (swap, *(c for c in candidates if c is not swap))[:MAX_HINTS],
                tier=level,
                reason="several windows fit: took the most recent, say a number to switch",
                heard=heard.said,
            )
        blank = replace(template, window=None, app=None)
        return Decision(
            Verdict.HINTS,
            blank,
            candidates[:MAX_HINTS],
            tier=level,
            reason=reason,
            heard=heard.said,
        )

    def _most_recent_rival(
        self,
        template: Action,
        candidates: tuple[Candidate, ...],
        level: int,
        state: DesktopState,
    ) -> Candidate | None:
        """Tier 0 only: which of several equally good windows to take without asking.

        A focus costs nothing to reverse, so making the speaker say everything twice is the
        worse failure. But the choice must not ride on a noisy model: measured, Jev flips a
        near-tie about one time in eight. So it is a rule: the most recently used window
        that is not the one already focused ("go to the browser" said from a browser means
        the other one). Every rival must be corroborated by a trusted field, so a hostile
        title can never put a window on this list. Anything above tier 0 still asks.
        """
        if level != 0 or template.intent is not Intent.FOCUS_WINDOW or len(candidates) < 2:
            return None
        if not all(c.window is not None and c.corroborated for c in candidates):
            return None
        others = [c for c in candidates if c.window.address != state.active_address]
        return min(others, key=lambda c: c.window.focus_rank) if others else None

    def _suggest(
        self, heard: _Heard, reason: str, *intents: Intent, rehearable: bool = True
    ) -> Decision:
        return Decision(
            Verdict.SUGGEST,
            reason=reason,
            suggestions=self._examples(*intents),
            heard=heard.said,
            rehearable=rehearable,
        )

    def _dictation(self, heard: _Heard) -> Decision:
        """Someone is dictating and the grammar could not place it.

        Nothing about this utterance may leave: not the words (the understander stops
        here), not the audio (`rehearable` stays false), and not the text on the overlay,
        which is why `heard` is left empty.
        """
        return Decision(
            Verdict.SUGGEST,
            reason='to type, say "type" and then the words, with the window focused',
            suggestions=self._examples(Intent.TYPE_TEXT),
            dictation=True,
        )

    def _degrade(
        self,
        heard: _Heard,
        state: DesktopState,
        need: _Need | None,
        why: str,
        failure: Exception | None = None,
        reading: _Reading | None = None,
    ) -> Decision:
        """Offline, 503 after retry, timeout, bad key: "pick a number", not silence (5.5)."""
        self.last_exchange["path"] = "degraded"
        if need is not None:
            intent, slots = need.parse.intent, need.parse.slots
        elif reading is not None:
            intent, slots = reading.intent, reading.slots
        else:
            if isinstance(failure, JevAuthError):
                return Decision(
                    Verdict.REFUSE,
                    reason="the AI gateway rejected the key; only exact commands work",
                    heard=heard.said,
                )
            # Jev is off or unreachable: the transcript was never the problem
            return self._suggest(
                heard, f"{why}; only exact commands work right now", *STARTERS, rehearable=False
            )
        phrase = slots.window_ref or slots.app_ref or heard.text
        if intent is Intent.LAUNCH_APP:
            near = [a for a, _ in self.lexicon.match_apps(phrase, limit=5) if a.trusted]
            shown = tuple(
                resolve.app_candidate(self.lexicon, heard.text, a) for a in near[:MAX_HINTS]
            )
        else:
            matches = self.lexicon.match_windows(phrase, state, limit=5)
            shown = tuple(
                resolve.window_candidate(self.lexicon, heard.text, w, s)
                for w, s in matches[:MAX_HINTS]
            )
        return self._hints(_template(intent, slots), shown, f"{why}; pick a number", heard, state)


@dataclass(frozen=True)
class _Heard:
    """One utterance as the rest of this module reads it.

    `text` is what the decision is made from: the normalized utterance, or the variant
    the grammar parsed (it records that in `Parse.utterance`, so a repaired "firefuck"
    corroborates Firefox). `literal` is what the recognizer actually produced, and only
    `literal` may supply a tier 2 verb: a guess never authorizes a disruptive action.
    """

    said: str
    text: str
    tokens: tuple[str, ...]
    literal: tuple[str, ...]
    variants: tuple[str, ...]

    @property
    def every_word(self) -> set[str]:
        return set(self.literal).union(*(v.split() for v in self.variants))


def _heard(norm: Any, parse: Parse | None) -> _Heard:
    # the real normalizer emits Token objects with raw offsets; fakes may emit strings
    literal = tuple(getattr(t, "text", t) for t in norm.tokens)
    text = parse.utterance if parse is not None and parse.utterance else norm.text
    return _Heard(norm.text, text, tuple(text.split()), literal, tuple(norm.variants))


@dataclass
class _Reading:
    intent: Intent
    slots: Slots
    # the probabilities the chosen intent actually read; their minimum is the confidence
    reads: list[float]
    grammar: bool = False


def _movable(template: Action, windows: list[Window], state: DesktopState) -> list[Window] | None:
    """For a move: the rivals that are not already on the destination workspace.

    "Bring the browser here" with two Firefox windows, one of them already here, is not
    ambiguous: only the other can be meant. Returns None when the rule does not apply or
    would remove every candidate.
    """
    if template.intent is not Intent.MOVE_TO_WORKSPACE or len(windows) < 2:
        return None
    spoken = template.workspace or ""
    if spoken == "here":
        destination = state.active_workspace_id
    elif spoken.isdigit():
        destination = int(spoken)
    else:
        return None
    elsewhere = [w for w in windows if w.workspace_id != destination]
    return elsewhere if elsewhere and len(elsewhere) < len(windows) else None


def _template(intent: Intent, slots: Slots) -> Action:
    """The operation with every slot but the target. Dictated text is never copied here."""
    return Action(
        intent,
        workspace=slots.workspace,
        direction=slots.direction,
        amount=slots.amount,
        verb=slots.verb,
        number=slots.number,
    )


def _pinned(state: DesktopState, pinned_address: str) -> Window | None:
    return state.by_address(pinned_address) if pinned_address else state.focused


def _plain(exc: Exception | None) -> str:
    """One short sentence for the HUD. Jev errors never carry the key (client.py)."""
    if exc is None:
        return "Jev did not answer"
    if isinstance(exc, JevTimeout):
        return "Jev took too long"
    if isinstance(exc, JevAuthError):
        return "the AI gateway rejected the key"
    return f"Jev is unavailable ({type(exc).__name__})"


def _merge(answers: _Answers, family: str, reply: Evaluation) -> None:
    for qid, answer in reply.answers.items():
        if family == "r1":
            answers.r1[qid] = answer
        elif family == "r2" and isinstance(answer, ChoiceAnswer):
            answers.relative = answer
        elif family == "r2b" and isinstance(answer, BooleanAnswer):
            answers.absolute[qid.removeprefix("is_")] = answer.probability
        elif family == "r2t" and isinstance(answer, ChoiceAnswer):
            answers.titled = answer
        elif family == "r3" and isinstance(answer, ChoiceAnswer):
            answers.apps.append(answer)


def _loggable(reply: Evaluation) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for qid, answer in reply.answers.items():
        if isinstance(answer, ChoiceAnswer):
            out[qid] = {"choice": answer.choice, "probabilities": answer.probabilities}
        elif isinstance(answer, ScoreAnswer):
            out[qid] = {"score": answer.score}
        else:
            out[qid] = {"probability": answer.probability}
    return out
