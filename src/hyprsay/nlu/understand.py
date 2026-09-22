"""From a transcript and a desktop snapshot to one `Decision`. Never raises, never hangs.

Order, and why (docs/PLAN.md sections 3 and 5):

1. Lock first. A locked session refuses before a single byte is normalized or sent.
2. Normalize, then the exact grammar, also over the normalizer's variants. The easy 80
   percent never touches the network: a Jev round trip measured about 315 ms p50 from
   this machine, against well under a millisecond for a grammar parse.
3. Entities resolve locally against trusted lexicon fields (`resolve.py`), and an
   application the speaker named is grounded against the live desktop (`ground.py`):
   "open chrome" with chrome already open goes to it, at tier 0, with no request at all.
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
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from hyprsay import controls, inapp, recipes
from hyprsay.config import Config
from hyprsay.jev.client import JevAuthError, JevTimeout
from hyprsay.jev.types import BooleanAnswer, ChoiceAnswer, Evaluation, Question, ScoreAnswer
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
from hyprsay.nlu import bank, clauses, ground, requests, resolve, tiers
from hyprsay.nlu.resolve import MAX_HINTS, LexiconLike, Resolution
from hyprsay.nlu.tiers import Evidence

# "did you mean" floor for intents (PLAN section 6), reused as the floor under which a
# window or app is not a plausible alternative worth a swap badge
PLAUSIBLE = 0.15
# question id prefix for the one Boolean per seam that `nlu/clauses.py` proposed
SEPARATES = "separates_"
_WORD = re.compile(r"[^\W_]+(?:['\u2019][^\W_]+)*")

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
# every intent whose Action must carry a window (`ops.Operation.needs_window`). A blank
# one on these means the target is settled against fresh state when the action runs.
NEEDS_WINDOW = WINDOW_OPS | tiers.IN_APP | {Intent.FOCUS_WINDOW, Intent.TYPE_TEXT}
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
# the verdicts that mean this clause will change the desktop, which is what the clause
# after it is allowed to assume
ACTING = frozenset({Verdict.ACT, Verdict.ACT_SWAP, Verdict.COUNTDOWN, Verdict.CONFIRM_KEY})


@dataclass(frozen=True)
class _Seams:
    """Where one utterance might stop being one command, and the text those offsets index.

    `text` is what `nlu/clauses.py` was asked about and what `at` counts characters in.
    `source` is the same string in its own casing, which is what a clause is actually cut
    from and handed to the normalizer.
    """

    at: tuple[int, ...] = ()
    text: str = ""
    source: str = ""
    # whether a fan-out has already carried the questions about them. Nothing is refused
    # for being "more than one command" on a seam nobody was able to judge.
    asked: bool = False

    def said(self, clause: clauses.Clause) -> str:
        return self.source[clause.start : clause.end]


def _swallowed(parse: Parse, seams: _Seams) -> bool:
    """Did one grammar slot eat what might be a second command?

    A referring phrase matches up to six words, so "focus spotify and close it" parses as
    a focus whose target is "spotify and close it"; the lexicon then resolves that on the
    word "spotify" alone and throws the rest away. The parse consumed every character and
    obeyed a quarter of them, which is v1's bug wearing a full match. So when code has
    proposed a seam and a slot holds more than one word, the seams are asked about rather
    than trusted to the fast path.

    One word cannot hide a clause, which is what keeps "Focus, Kitty." (a real recognizer
    output, comma and all) on the path that never touches the network.
    """
    if not seams.at:
        return False
    spoken = parse.slots.window_ref or parse.slots.app_ref or ""
    return len(spoken.split()) > 1 or bool(parse.slots.text)


# the frozen "nothing proposed cutting this" holder, so it is built once
NO_SEAMS = _Seams()


def _seams(norm: Any) -> _Seams:
    """The seams code offers for this utterance, proposed over the RAW transcript.

    Over the raw and not the normalized text, for one measured reason: the normalizer
    drops punctuation, and the comma is the only seam in the owner's own utterance,
    "open chrome, navigate to youtube and look up ltt". Lower cased because a recognizer
    capitalizes the word after a full stop while `clauses.CONNECTIVES` are written in
    lower case, so "Open firefox. Then close kitty." would otherwise read as one command.

    Lowering changes the length of a string in a few scripts, and these offsets have to
    index the raw text, so a text that changed length falls back to the normalized form.
    """
    raw = getattr(norm, "raw", "") or ""
    lowered = raw.lower()
    text, source = (lowered, raw) if len(lowered) == len(raw) else (norm.text, norm.text)
    return _Seams(tuple(clauses.split_candidates(text)), text, source)


@dataclass(frozen=True)
class _Chain:
    """What a clause in a compound utterance inherits from the clause before it.

    `refers_back` is this clause's own word ("it", "that", "there"); the rest is what the
    clause before it acted on. "this" and "here" are deliberately not back references
    (nlu/clauses.py), so a clause that points with them still means the window pinned at
    key down, and clause 0 is never given one of these at all.
    """

    refers_back: bool = False
    window: Window | None = None
    workspace: str | None = None
    # the clause before opened an application. Its window does not exist yet, so nothing
    # here can name it and the target is settled when the clause runs instead.
    launched: bool = False


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
        # Code proposes the seams, Jev judges them (nlu/clauses.py). An utterance with no
        # connective in it proposes none, and then this is v1's path character for
        # character, which is why nothing without an "and" in it can regress.
        return await self._clause(norm, state, pinned_address, seams=_seams(norm))

    async def _clause(
        self,
        norm: Any,
        state: DesktopState,
        pinned_address: str,
        *,
        seams: _Seams = NO_SEAMS,
        chain: _Chain | None = None,
    ) -> Decision:
        """One command's worth of an utterance: the whole of it, or one clause of it."""
        # the grammar tries the utterance and then each variant itself (grammar.py)
        parse = self.grammar.parse(norm, picking=False)
        heard = _heard(norm, parse)
        need: _Need | None = None
        if parse is not None and parse.intent is not Intent.NONE:
            if _swallowed(parse, seams) and self._jev_on():
                # the parse matched every word, but one slot ate a seam, so the words
                # after it would be resolved away rather than obeyed. Ask before acting.
                taken = self._separating(await self._ask(self._seam_fan(heard, seams)), seams)
                seams = replace(seams, asked=True)
                if taken:
                    chained = await self._chained(seams, state, pinned_address, taken)
                    if chained is not None:
                        return chained
            self.last_exchange["path"] = "grammar"
            outcome = await self._from_grammar(parse, heard, state, pinned_address, chain)
            if isinstance(outcome, Decision):
                return self._partial(seams, heard, outcome) or outcome
            need = outcome
        return await self._semantic(heard, state, pinned_address, need, seams=seams, chain=chain)

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

    async def _from_grammar(
        self,
        parse: Parse,
        heard: _Heard,
        state: DesktopState,
        pinned_address: str,
        chain: _Chain | None = None,
    ) -> Decision | _Need:
        intent, slots = parse.intent, parse.slots
        if intent in tiers.IN_APP:
            return await self._in_app(parse, heard, state, pinned_address, chain)
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
        if (
            chain is not None
            and chain.refers_back
            and chain.workspace
            and REQUIRED.get(intent) == "workspace"
            and slots.workspace is None
        ):
            # "there" is the place the clause before this one acted on
            slots = replace(slots, workspace=chain.workspace)
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
            bound = self._bound(template, heard, state, chain)
            return bound or self._pointed(template, heard, state, pinned_address, said=True)
        return self._window_locally(parse, phrase, template, heard, state)

    # ----------------------------------------------------------------------- back references

    def _bound(
        self, template: Action, heard: _Heard, state: DesktopState, chain: _Chain | None
    ) -> Decision | None:
        """Aim a clause at what the clause before it acted on, or None when it did not
        say to.

        "open firefox and move IT to workspace 3" moves firefox, not whatever happened to
        be focused when the key went down. Clause 0 never gets here, and neither does a
        clause that pointed with "this" or "here", so v1's deixis is untouched.
        """
        if chain is None or not chain.refers_back:
            return None
        evidence = Evidence(
            source="grammar",
            corroborated=True,
            explicit_target=True,
            verb_said=tiers.verb_said(template.intent, heard.literal),
        )
        if chain.window is not None:
            window = state.by_address(chain.window.address) or chain.window
            candidate = resolve.window_candidate(self.lexicon, heard.text, window, 1.0)
            return self._finish(
                replace(template, window=window), evidence, (candidate,), heard, state
            )
        if not chain.launched:
            return None
        # The clause before opened an application, so there is no window to name yet. The
        # target is therefore left blank on purpose: the executor resolves a blank target
        # against FRESH state at the moment of the write (executor._check_window), and by
        # then the launch has waited for its window (ops.LAUNCH_WAIT_S).
        return self._finish(template, evidence, (), heard, state)

    # ----------------------------------------------------------------------- in-app reach

    async def _in_app(
        self,
        parse: Parse,
        heard: _Heard,
        state: DesktopState,
        pinned_address: str,
        chain: _Chain | None,
    ) -> Decision:
        """Reaching INSIDE one window: a wheel notch, a chord, a recipe or a click.

        None of these is in `model.JEV_INTENTS`, so none can arrive from a model: each
        was matched by an exact grammar phrase naming the gesture. The target is the
        window the speaker is looking at, or, in a chain, the one the clause before acted
        on, so "open chrome, navigate to youtube" reaches chrome and not the terminal the
        key was held over.
        """
        window, deferred = self._in_app_target(state, pinned_address, chain)
        if window is None and not deferred:
            return Decision(
                Verdict.REFUSE, reason="the window you were pointing at is gone", heard=heard.said
            )
        kind = self.lexicon.kind_of(window) if window is not None else ""
        if window is not None and parse.intent is Intent.RUN_RECIPE:
            # the target has to be settled BEFORE it is authorized: a browser recipe
            # judged against the terminal that held the key refuses every time, and with
            # the typing sentence, which reads as a safety rule rather than a wrong window
            elsewhere = self._recipe_elsewhere(parse.slots.verb or "", window, kind, state)
            if elsewhere is not None:
                window, kind = elsewhere
        if window is not None:
            # the only thing that stops a gesture reaching a launcher, a lock prompt, an
            # authentication dialog or a shell: the inherited pointer call merely NOTES a
            # covering layer, and a wheel notch is not milder than a keystroke where tmux
            # and vim read the wheel as keys
            gesture = inapp.SCROLL if parse.intent is Intent.SCROLL else inapp.CHORD
            chord = parse.slots.verb or "" if parse.intent is Intent.PRESS_CHORD else ""
            refusal = inapp.refusal(window, kind, state, self.cfg, gesture, chord)
            if refusal:
                return Decision(Verdict.REFUSE, tier=2, reason=refusal, heard=heard.said)
        if parse.intent is Intent.CLICK_CONTROL:
            return await self._click(parse, heard, state, window)
        if parse.intent is Intent.RUN_RECIPE:
            return self._recipe(parse, heard, state, window, kind)
        return self._gesture(parse, heard, state, window)

    def _in_app_target(
        self, state: DesktopState, pinned_address: str, chain: _Chain | None
    ) -> tuple[Window | None, bool]:
        """The window to reach into, and whether it is settled at run time instead."""
        if chain is not None:
            if chain.window is not None:
                return state.by_address(chain.window.address) or chain.window, False
            if chain.launched:
                return None, True
        return _pinned(state, pinned_address), False

    def _gesture(
        self, parse: Parse, heard: _Heard, state: DesktopState, window: Window | None
    ) -> Decision:
        address = window.address if window is not None else ""
        try:
            # built here only so a direction or a chord this cannot send is refused in
            # words now rather than failing at delivery; `ops` builds the one it performs
            if parse.intent is Intent.SCROLL:
                inapp.scroll(parse.slots.direction, parse.slots.amount, address)
            else:
                inapp.press(parse.slots.verb or "", address)
        except inapp.GestureError as exc:
            return Decision(Verdict.REFUSE, reason=str(exc), heard=heard.said)
        action = Action(
            parse.intent,
            window=window,
            direction=parse.slots.direction,
            amount=parse.slots.amount,
            verb=parse.slots.verb,
        )
        # the phrase the grammar matched IS the literally spoken gesture, exactly as a
        # carrier phrase is for typing
        evidence = Evidence(
            source="grammar", corroborated=True, verb_said=True, explicit_target=True
        )
        return self._finish(action, evidence, (), heard, state)

    def _recipe(
        self,
        parse: Parse,
        heard: _Heard,
        state: DesktopState,
        window: Window | None,
        kind: str,
    ) -> Decision:
        name = parse.slots.verb or ""
        if window is None:
            return self._deferred_recipe(name, parse.slots.text, heard, state)
        offered = {recipe.name: recipe for recipe in recipes.for_window(window, kind)}
        recipe = offered.get(name)
        if recipe is None:
            what = window.cls or "that window"
            return Decision(
                Verdict.REFUSE,
                reason=f"{what} has no {name.replace('_', ' ')}",
                heard=heard.said,
            )
        refusal = recipes.refusal_for(recipe, window, kind)
        if refusal:
            return Decision(Verdict.REFUSE, tier=2, reason=refusal, heard=heard.said)
        try:
            # rendered now and thrown away, so "that sounded like a phrase rather than an
            # address" is said before any key goes out. `ops` renders the one it performs.
            recipes.render(recipe, parse.slots.text, window)
        except recipes.RecipeError as exc:
            return Decision(Verdict.REFUSE, reason=str(exc), heard=heard.said)
        action = Action(Intent.RUN_RECIPE, window=window, verb=recipe.name, text=parse.slots.text)
        return self._finish(action, _recipe_evidence(recipe, heard), (), heard, state)

    def _recipe_elsewhere(
        self, name: str, focused: Window, kind: str, state: DesktopState
    ) -> tuple[Window, str] | None:
        """The window this recipe belongs to, when it is not the one being looked at.

        "Go to youtube.com" names a browser, not whatever happened to hold the key. Asked
        over a terminal it used to refuse with the typing sentence, which reads as a
        safety rule when really the wrong window had been chosen. So: if the focused
        window cannot do it and exactly one open window can, that is the one meant. More
        than one and nobody can know which, so the caller falls through to the refusal
        that names what is missing.
        """
        if any(recipe.name == name for recipe in recipes.for_window(focused, kind)):
            return None
        able = [
            (w, k)
            for w in state.windows
            if w.address != focused.address
            for k in (self.lexicon.kind_of(w),)
            if any(recipe.name == name for recipe in recipes.for_window(w, k))
        ]
        return able[0] if len(able) == 1 else None

    def _deferred_recipe(
        self, name: str, text: str | None, heard: _Heard, state: DesktopState
    ) -> Decision:
        """A recipe in a clause that follows a launch: the window is not open yet.

        What it may do is still decided here, from the recipe's own tier; which window it
        runs against, and whether that window's kind offers it at all, is settled by
        `ops._run_recipe` against fresh state.
        """
        recipe = next(
            (r for table in recipes.RECIPES.values() for r in table.values() if r.name == name),
            None,
        )
        if recipe is None:
            return Decision(
                Verdict.REFUSE, reason=f"there is no {name.replace('_', ' ')}", heard=heard.said
            )
        action = Action(Intent.RUN_RECIPE, verb=name, text=text)
        return self._finish(action, _recipe_evidence(recipe, heard), (), heard, state)

    async def _click(
        self, parse: Parse, heard: _Heard, state: DesktopState, window: Window | None
    ) -> Decision:
        """The one place the accessibility tree is read, and only because the utterance
        asked to click: a speculative walk cost 16.2 seconds live on a real window."""
        if window is None:
            return Decision(
                Verdict.REFUSE,
                reason="what can be clicked is read from a window that is already open, "
                "so say that on its own once it is",
                heard=heard.said,
            )
        try:
            # a D-Bus walk, so never on the loop: it may take a second and a half before
            # it gives up, and the next key press must still open the microphone on time
            found = await asyncio.to_thread(controls.controls_for, window)
        except controls.ControlsError as exc:
            # a bus that will not start, a Chrome without its flag, a walk still running:
            # each is one sentence naming its own repair, and none means "no such button"
            return Decision(Verdict.REFUSE, reason=str(exc), heard=heard.said)
        ranked = controls.best_match(parse.slots.text or "", found)
        if not ranked:
            return self._suggest(
                heard, "nothing in that window is called that", Intent.CLICK_CONTROL
            )
        control = ranked[0][0]
        action = Action(
            Intent.CLICK_CONTROL, window=window, verb=control.name, text=parse.slots.text
        )
        # the click verb was literally said, and the control was chosen only by words the
        # speaker used: `best_match` scores against nothing else, so the window's own text
        # cannot supply the evidence that selects it (controls.py, docs/PLAN.md 5.6)
        evidence = Evidence(
            source="grammar", corroborated=True, verb_said=True, explicit_target=True
        )
        return self._finish(action, evidence, (), heard, state)

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
        return self._launch(
            found.app,
            found.candidates,
            Evidence(source="grammar"),
            heard,
            state,
            template,
            # the words the rule matched, which is where the launch verb and any novelty
            # word beside it live. A parse built by hand records none and means "launch".
            said=parse.utterance,
        )

    def _launch(
        self,
        app: App,
        candidates: tuple[Candidate, ...],
        evidence: Evidence,
        heard: _Heard,
        state: DesktopState,
        template: Action | None = None,
        *,
        said: str = "",
    ) -> Decision:
        """The one funnel both paths reach once an application has been settled.

        It is also where "open chrome" stops meaning "start chrome": the verb only
        declared a preference, and `ground.reach` decides against the live desktop
        whether that means going to a window that exists or starting another copy.
        """
        if not app.trusted:
            return Decision(
                Verdict.REFUSE,
                reason=f"{app.name} comes from a user-writable desktop file that has not been "
                "approved; run hyprsay doctor",
                heard=heard.said,
            )
        # the template carries the workspace the speaker asked for ("open firefox on
        # workspace 3"). Building a bare Action here threw it away and opened the
        # application where it already was, which is what "open zapzap in a new
        # workspace" did.
        base = template if template is not None else Action(Intent.LAUNCH_APP)
        prefer = ground.preference(said, base.workspace)
        reached = ground.reach(app, prefer, state, self.lexicon, self.cfg.gates)
        if reached.verdict is not ground.Reached.LAUNCH:
            return self._reach(reached, evidence, heard, state)
        chosen = next((c for c in candidates if c.app is app), None)
        evidence = replace(
            evidence,
            corroborated=bool(chosen and chosen.corroborated),
            explicit_target=True,
            plausible=len(candidates),
        )
        action = replace(base, intent=Intent.LAUNCH_APP, app=app, window=None)
        return self._finish(action, evidence, candidates, heard, state)

    def _reach(
        self, reached: ground.Reach, evidence: Evidence, heard: _Heard, state: DesktopState
    ) -> Decision:
        """A launch phrase that resolved to a window that is already open.

        The workspace is deliberately not copied onto the action. A focus follows its
        window to whatever workspace it lives on, which is what "open chrome" with chrome
        on workspace 8 should do; carrying a workspace here would read as "drag it over
        to me", a different command at a different tier. When the speaker did name a
        workspace, `ground.preference` never let the utterance get this far.

        Several windows are handed to `_hints`, the same machinery a tie on "focus
        firefox" uses, so a tier 0 tie still acts on the most recent window and offers
        the others as badges rather than growing a second way to choose.
        """
        template = Action(Intent.FOCUS_WINDOW)
        found = tuple(
            resolve.window_candidate(self.lexicon, heard.text, w, 1.0) for w in reached.windows
        )
        if reached.verdict is ground.Reached.AMBIGUOUS:
            return self._hints(template, found[:MAX_HINTS], reached.reason, heard, state)
        only = found[0]
        evidence = replace(
            evidence,
            corroborated=only.corroborated,
            explicit_target=True,
            verb_said=tiers.verb_said(Intent.FOCUS_WINDOW, heard.literal),
            by_title=False,
            plausible=1,
        )
        return self._finish(replace(template, window=only.window), evidence, found, heard, state)

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
            reason=f'no {app.name} window is open; say "open {app.name.lower()}" to start it',
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
        self,
        heard: _Heard,
        state: DesktopState,
        pinned_address: str,
        need: _Need | None,
        *,
        seams: _Seams = NO_SEAMS,
        chain: _Chain | None = None,
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
            # nobody can judge the seams, so the utterance stays whole: exactly v1
            return self._degrade(heard, state, need, "Jev is turned off")

        self.last_exchange["path"] = "jev"
        fan, local_app = self._fan_out(heard, state, need, seams)
        answers = await self._ask(fan)
        taken = [] if seams.asked else self._separating(answers, seams)
        if seams.at:
            seams = replace(seams, asked=True)
        if taken:
            chained = await self._chained(seams, state, pinned_address, taken)
            if chained is not None:
                return chained
        if need is not None:
            reading = _Reading(need.parse.intent, need.parse.slots, [], grammar=True)
        else:
            reading = self._read_utterance(answers, heard)
            if isinstance(reading, Decision):
                return reading
            if isinstance(reading, Exception):
                return self._degrade(heard, state, None, _plain(reading), reading)
        decision = self._resolve_remote(
            reading, fan, answers, local_app, heard, state, pinned_address, need, chain
        )
        if seams.at and not taken:
            return self._partial(seams, heard, decision) or decision
        return decision

    # -------------------------------------------------------------- compound utterances

    def _seam_questions(self, seams: _Seams) -> dict[str, Question]:
        """One Boolean per seam code offered, for the request that was going out anyway.

        Question count is free and payload is not (PLAN 5.5), so asking costs nothing.
        What the halves may SAY is another matter: `clauses.split_candidates` refuses to
        offer a seam inside a dictated tail, but such a tail can still sit on the far
        side of one it did offer, as in "close this and type my password". Those words
        are the speaker's, not a command, and they never reach a model (PLAN 5.6 and
        section 7), so every half is cut at its carrier verb before it is sent.
        """
        built: dict[str, Question] = {}
        for index, at in enumerate(seams.at):
            cut = clauses.clauses_for(seams.text, [at])
            if len(cut) != 2:
                continue
            left, right = cut
            word = seams.text[left.end : right.start].strip(" ,") or "and"
            built[f"{SEPARATES}{index}"] = bank.separates(
                word, _before_dictation(seams.said(left)), _before_dictation(seams.said(right))
            )
        return built

    def _seam_fan(self, heard: _Heard, seams: _Seams) -> requests.FanOut:
        """A request carrying nothing but the seam Booleans, for a parse the grammar
        already settled: there is nothing else worth asking about it."""
        base = requests.utterance_state(heard.said, heard.variants)
        built = requests.split("r1", base, self._seam_questions(seams), self.cfg.jev.token_cap)
        fan = requests.FanOut(tuple(built))
        self.last_exchange["requests"] = [r.as_sent() for r in fan.requests]
        return fan

    def _separating(self, answers: _Answers, seams: _Seams) -> list[int]:
        """The seams the answers said really separate two commands.

        The gate is `gates.spoken`, the knob for Booleans about the UTTERANCE rather than
        about one window. A seam nobody answered about is not taken: a failed request
        leaves the utterance whole, which is v1's behaviour and the safe one.
        """
        floor = self.cfg.gates.spoken
        taken: list[int] = []
        for index, at in enumerate(seams.at):
            answer = answers.r1.get(f"{SEPARATES}{index}")
            if isinstance(answer, BooleanAnswer) and answer.probability >= floor:
                taken.append(at)
        return taken

    async def _chained(
        self, seams: _Seams, state: DesktopState, pinned_address: str, taken: list[int]
    ) -> Decision | None:
        """Every clause understood on its own, the first one carrying the rest.

        Each clause is judged against the desktop as the clause before it leaves it, and
        its `verb_said` is read from its OWN words, so a launch can never carry a close.
        """
        cut = clauses.clauses_for(seams.text, taken)
        if len(cut) < 2:
            return None
        decisions: list[Decision] = []
        carried = _Chain()
        for clause in cut:
            # each clause is normalized on its own, so its tokens carry offsets into its
            # own words: dictated text and a recipe's spoken span are still cut from what
            # the recognizer wrote, and number repair now sees each clause's real end
            sub = self._normalize(seams.said(clause))
            if not sub.text.strip():
                continue
            chain = None if clause.index == 0 else replace(carried, refers_back=clause.refers_back)
            decision = await self._clause(sub, state, pinned_address, chain=chain)
            decisions.append(decision)
            carried = _carried(decision)
            state = _projected(state, decision)
        if len(decisions) < 2:
            return None
        self.last_exchange["path"] = "chain"
        self.last_exchange["clauses"] = [seams.said(clause) for clause in cut]
        return replace(decisions[0], rest=tuple(decisions[1:]))

    def _partial(self, seams: _Seams, heard: _Heard, decision: Decision) -> Decision | None:
        """Words left over after a seam nobody cut, on an utterance about to be acted on.

        The answers said this is one command. If what follows one of the seams reads as a
        command all by itself, then acting would carry out the front of the utterance and
        drop the rest, which is worse than refusing: doing a third of what was asked is
        the bug this whole file exists to fix.

        Only a seam that was really judged counts. With Jev off nobody could say, and
        guessing the other way would turn ordinary commands into questions.
        """
        if not seams.asked or decision.verdict not in ACTING:
            return None
        for at in seams.at:
            cut = clauses.clauses_for(seams.text, [at])
            if len(cut) != 2:
                continue
            tail = self.grammar.parse(self._normalize(seams.said(cut[1])), picking=False)
            if tail is not None and tail.intent is not Intent.NONE:
                self.last_exchange["path"] = "partial"
                return self._suggest(
                    heard,
                    "that sounded like more than one command, so nothing was done; "
                    "say them one at a time",
                    tail.intent,
                    rehearable=False,
                )
        return None

    def _fan_out(
        self,
        heard: _Heard,
        state: DesktopState,
        need: _Need | None,
        seams: _Seams = NO_SEAMS,
    ) -> tuple[requests.FanOut, App | None]:
        cap = self.cfg.jev.token_cap
        base = requests.utterance_state(heard.said, heard.variants)
        built: list[requests.Request] = []
        windows: dict[str, Window] = {}
        titled: dict[str, Window] = {}
        apps: dict[str, App] = {}
        dropped_windows = dropped_apps = 0
        # the seam questions ride in the R1 family whether or not R1's own questions are
        # being asked, so `_merge` files their answers in the same place
        asked = self._seam_questions(seams) if seams.at and not seams.asked else {}
        if need is None:
            built += requests.split("r1", base, {**bank.utterance_questions(), **asked}, cap)
        elif asked:
            built += requests.split("r1", base, asked, cap)
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
        chain: _Chain | None = None,
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
                reading, fan, answers, local_app, evidence, heard, state, need, template
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
                bound = self._bound(template, heard, state, chain)
                if bound is not None:
                    return bound
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
        template: Action | None = None,
    ) -> Decision:
        # Jev chose the intent, not the action: from bank.VERSION 2026-09-22.1 the
        # LAUNCH_APP description covers an application that is already open, and the
        # branch is `ground.reach`'s to take against live state. The words are the whole
        # utterance, since no rule matched a span of it.
        said = heard.text
        if local_app is not None:
            self._note(reading)
            candidate = resolve.app_candidate(self.lexicon, heard.text, local_app, 1.0)
            return self._launch(
                local_app, (candidate,), evidence, heard, state, template, said=said
            )
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
        return self._launch(ranked[0][0], candidates, evidence, heard, state, template, said=said)

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


def _before_dictation(text: str) -> str:
    """The words up to and including a carrier verb, and nothing that follows it.

    The carrier itself stays: "type" with nothing after it is exactly what tells a reader
    that this half is an instruction of its own. What the speaker meant to have typed is
    what may not go anywhere, so it is cut here and not shortened, redacted or hashed.
    """
    for match in _WORD.finditer(text):
        if match.group().lower() in DICTATION_OPENERS:
            return text[: match.end()]
    return text


def _recipe_evidence(recipe: recipes.Recipe, heard: _Heard) -> Evidence:
    """A recipe never comes from a model, so the tier 2 verb rule reads differently here.

    `recipes.SPOKEN_VERBS` names the word that has to be in the utterance for the recipes
    whose own tier is 2, and that is checked against this clause's own words. Where the
    table names no word, the exact grammar phrase that selected the recipe IS the
    literally spoken verb, the same reading `_typing` makes of a carrier phrase: no model
    was asked, and no other phrase could have reached this recipe.
    """
    said = recipes.verb_said(recipe, heard.literal) or recipe.name not in recipes.SPOKEN_VERBS
    return Evidence(source="grammar", corroborated=True, verb_said=said, explicit_target=True)


def _carried(decision: Decision) -> _Chain:
    """What the next clause may point back at. A clause that did not act leaves nothing."""
    action = decision.action
    if action is None or decision.verdict not in ACTING:
        return _Chain()
    # A launch has no window yet, and neither has a clause that was itself aimed at
    # whatever that launch opened. Either way the chain is working inside a window only
    # the executor will know, so everything after it is settled at run time too.
    pending = action.intent is Intent.LAUNCH_APP or (
        action.window is None and action.intent in NEEDS_WINDOW
    )
    return _Chain(window=action.window, workspace=action.workspace, launched=pending)


def _projected(state: DesktopState, decision: Decision) -> DesktopState:
    """The desktop as the clause before leaves it, as far as code can honestly know.

    Only what the compositor is certain to do is written down: a focus moves the active
    address, a workspace switch moves the active workspace, a move takes the window with
    it. That is what the next clause is tiered against, so "move it to 3 and make it
    fullscreen" is not judged as if the window were still where it started.

    Nothing is invented about a launch. The window it opens does not exist in any
    snapshot, and pretending otherwise would put a made-up address in an Action.
    """
    action = decision.action
    if action is None or decision.verdict not in ACTING:
        return state
    if action.intent is Intent.FOCUS_WINDOW and action.window is not None:
        return replace(state, active_address=action.window.address)
    workspace = action.workspace or ""
    if action.intent is Intent.SWITCH_WORKSPACE and workspace.isdigit():
        return replace(state, active_workspace_id=int(workspace))
    moving = action.intent is Intent.MOVE_TO_WORKSPACE and action.window is not None
    if moving and workspace.isdigit():
        moved = replace(action.window, workspace_id=int(workspace), workspace_name=workspace)
        return replace(
            state,
            windows=tuple(moved if w.address == moved.address else w for w in state.windows),
        )
    return state


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
