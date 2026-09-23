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

The context and the plan (docs/STRATEGY.md 2.1 and 4.5). Every R1 request now carries
what `nlu/context.py` builds from live state: which applications are open, which
workspace the speaker is on, what they are looking at, and what the last command did.
That is the evidence the question bank was asking about and the request was withholding.
When a harvest (`affordances.harvest`) and a plan reader (`nlu/plan.py`) are both
available, the same fan-out also carries one Choice per step over the options code built
from that state, and the ordered answer is understood one step at a time against a
desktop the step before it has advanced (`_projected`), which is the machinery the
clause chain already uses. The plan is read ONLY when the seam Booleans did not already
cut the utterance and it names two steps or more: a compound with no connective in it is
the one thing `nlu/clauses.py` cannot see, and a single step is better served by the
window agreement above, which a step Choice has no equivalent of.

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
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from hyprsay import affordances, controls, inapp, recipes
from hyprsay.adapters import base as adapters
from hyprsay.adapters.base import Adapter, Affordance, Capability, Trust
from hyprsay.config import Config
from hyprsay.jev import tokens
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
    Service,
    Slots,
    Transcript,
    Verdict,
    Window,
)
from hyprsay.nlu import (
    bank,
    clauses,
    context,
    decide,
    ground,
    interpret,
    plan,
    requests,
    resolve,
    tiers,
    turns,
)
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
# intents whose `text` is an argument the speaker aimed at a target ("find <what>",
# "click <which>") rather than dictation. Only these keep it in a template (`_template`).
ARGUMENT_TEXT = frozenset({Intent.RUN_RECIPE, Intent.CLICK_CONTROL})
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
# after it, and now the utterance after it, is allowed to assume. Defined once, in the
# module that has to agree with this one about what "it happened" means.
ACTING = context.ACTING
# The capabilities a harvested option may name and this module knows how to build a
# complete Action for. `affordances.py` mints the option id from an `Intent`, so this is
# a whitelist over that set: an intent left out of it drops the plan rather than guessing
# at a slot nobody answered. RESIZE_WINDOW and MOVE_WINDOW are the two left out, because
# `REQUIRED` says they need a direction or a size word and a step Choice carries neither.
_CAPABILITIES = frozenset(
    {
        Intent.FOCUS_WINDOW.value,
        Intent.SWITCH_WORKSPACE.value,
        Intent.LAUNCH_APP.value,
        Intent.VOLUME.value,
        Intent.MEDIA.value,
        Intent.FULLSCREEN.value,
        Intent.TOGGLE_FLOATING.value,
        # reaching inside a window. The handle names the window AND what to touch inside
        # it, and `affordances.target` takes it apart so no caller does string surgery.
        Intent.SCROLL.value,
        Intent.PRESS_CHORD.value,
        Intent.RUN_RECIPE.value,
        Intent.CLICK_CONTROL.value,
    }
)
# the harvest is local and measured at 1.4 to 2.2 ms p50, 5.6 ms on the first cold call
# (affordances.py), but it reads adapters, and an adapter reads a bus. So it runs off the
# loop with a deadline: an option list is worth a few milliseconds and never an utterance.
log = logging.getLogger("hyprsay")

HARVEST_S = 0.25

# Words a capability summary can hold that name nothing. A speaker who said "on" has not
# named "play a track on Spotify". Closed and short, for `DICTATION_OPENERS`' reason: this
# decides whether an adapter option is corroborated, and a fuzzy rule would decide it on
# the strength of a preposition.
_FILLER = frozenset(
    {
        "a", "an", "the", "and", "or", "of", "on", "in", "by", "to", "for", "with",
        "from", "it", "its", "this", "that", "one", "up", "is", "are", "be",
    }
)  # fmt: skip

# Why the second turn's Choice named no step, in this module's vocabulary. `plan.Cut` is
# about reading one answer and `turns.Stopped` is about an utterance that ended with
# nothing done, so the two are mapped rather than merged: a cut that has no entry here is
# read as unanswered, which is the strict direction.
_GAVE_UP: dict[plan.Cut, turns.Stopped] = {
    plan.Cut.NONE: turns.Stopped.NO_BETTER,
    plan.Cut.UNKNOWN: turns.Stopped.UNANSWERED,
    plan.Cut.UNSURE: turns.Stopped.NO_BETTER,
    plan.Cut.UNANSWERED: turns.Stopped.UNANSWERED,
}


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
    # Rp: the step Choices, by question id, which is the shape `plan.read` takes
    steps: dict[str, Any] = field(default_factory=dict)
    # Rx: what to do about a phrase the lexicon could not place (`_reach_ballot`)
    reach: ChoiceAnswer | None = None
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
        adapters: tuple[Any, ...] = (),
        interpreter: Any = None,
    ) -> None:
        # stage 1, built once and kept warm by the caller: a cold first call measured
        # about 1.2 s against 395 ms warm (`nlu/interpret.Interpreter`)
        self.interpreter = interpreter
        self.cfg = cfg
        self.lexicon = lexicon
        self.grammar = grammar
        self.evaluator = evaluator
        self._clock = clock
        # built once by the caller and held, never here: building them per utterance
        # would open the session bus on every word and throw away the handles an adapter
        # minted for the utterance before (affordances.Machine)
        self.adapters = tuple(adapters)
        if normalizer is None:
            from hyprsay.nlu.normalize import normalize as normalizer
        self._normalizer = normalizer
        # the operation waiting for a number; see "Picking" in the module docstring
        self.pending: Action | None = None
        # the Rx ballot this utterance offered, so its answer can be read back
        self._reach_plans: dict[str, _Reach] = {}
        # what an earlier clause of the utterance being decided right now already did
        self._chain_did: dict[str, Any] | str = ""
        # what the last few utterances did, and the desktop this one is being decided
        # against. It lives beside `pending` and is cleared on the same paths, because
        # two memories with two lifetimes is the bug factory docs/STRATEGY.md 4.5 names.
        self.context = context.Context()
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
        provisional: bool = False,
    ) -> Decision:
        """`provisional` decides without remembering. While the push-to-talk key is held
        the same sentence is understood six to nine times as it grows (`incremental.py`),
        and every ACT along the way used to be written into the three turn memory, so the
        memory filled with drafts of the sentence being spoken and pushed out the command
        before it. The memory and `pending` are evidence for the NEXT utterance, and a
        sentence still being said is not one yet. The decode on key up remembers it."""
        self.last_exchange = {
            "bank": bank.VERSION,
            "path": "",
            "requests": [],
            "answers": {},
            "errors": {},
        }
        # a note about an earlier clause belongs to ONE utterance. Left standing it
        # would tell the next sentence what the one before it did, twice over.
        self._chain_did = ""
        try:
            transcript = await self._interpret(transcript, provisional)
            decision = await self._decide(transcript, state, pinned_address, tuple(picking))
        except Exception as exc:  # the engine must never die on one utterance
            self.last_exchange["path"] = "error"
            self.last_exchange["errors"]["internal"] = type(exc).__name__
            # The name alone sent me hunting a KeyError by hand for twenty minutes. The
            # traceback goes to the log and NOT into last_exchange, because the journal
            # is written to disk and a frame in this module can hold the speaker's own
            # words (docs/PRIVACY.md); the log is the operator's and is not journalled.
            log.exception("understanding %r failed", type(exc).__name__)
            decision = Decision(
                Verdict.REFUSE,
                reason=f"internal error while understanding ({type(exc).__name__}); nothing done",
            )
        if provisional:
            return decision
        waits = decision.verdict in (Verdict.HINTS, Verdict.ACT_SWAP, Verdict.SUGGEST)
        self.pending = decision.action if waits and decision.candidates else None
        # a chain is remembered by its head: the daemon may stop it part way, and only
        # the head is certain to have been meant exactly as it was understood
        self.context = self.context.remember(
            context.remembered(
                decision,
                said=self.context.utterance,
                now=self.context.now,
                stamp=self.context.fingerprint,
                lexicon=self.lexicon,
            )
        )
        return decision

    async def _interpret(self, transcript: Transcript, provisional: bool) -> Transcript:
        """Stage 1: rambling speech becomes one crisp command, or is left exactly alone.

        Text in, text out. It never chooses an action, and it can only ever hand the next
        stage a sentence; everything that decides anything is still code and Jev.

        NOT on a provisional decode. While the key is held one sentence is understood six
        to nine times (`incremental.py`), and paying about 394 ms of network on each draft
        of a sentence still being spoken would spend the whole point of deciding early.
        The final decode on key up is the one that gets tidied.

        `interpret` never raises and returns its input unchanged on every failure, so a
        model that is slow, down or disabled costs the cleanup and never the command.
        About three quarters of real utterances never reach it at all: the grammar
        already parses them, so there is nothing to tidy (`already_a_command`).
        """
        # inert unless a caller built one and handed it over. Reaching for a shared
        # client here would put a network call inside every unit test that fakes the
        # grammar, where nothing can skip it and nothing wants it.
        if provisional or self.interpreter is None:
            return transcript
        said = await interpret.interpret(
            transcript.text,
            self.context.recent,
            self.cfg,
            interpreter=self.interpreter,
            vocabulary=self.lexicon.words(),
        )
        if said == transcript.text:
            return transcript
        self.last_exchange["interpreted"] = {"from": transcript.text, "to": said}
        return replace(transcript, text=said)

    def acted(self, ok: bool) -> None:
        """What the executor made of the last decision. The caller knows, this does not:
        a verdict of ACT is a permission, and the write can still refuse it."""
        self.context = self.context.did(ok)

    def forget(self) -> None:
        """A lock or a cancel: nothing said before now is in force. Called on the same
        paths that clear the numbered badges, for the same reason."""
        self.pending = None
        self.context = self.context.forgotten()

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
        # the machine as this utterance sees it: what is open, what the last commands
        # did, and the fingerprint of the desktop everything below is decided against
        self.context = self.context.about(
            state,
            now=self._clock(),
            utterance=norm.text,
            tokens=tuple(getattr(token, "text", token) for token in norm.tokens),
            picking=picking,
            pinned_address=pinned_address,
            lexicon=self.lexicon,
        )
        self.last_exchange["fingerprint"] = self.context.fingerprint
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
                if not self._single() or decide.fast(parse, outcome):
                    return self._partial(seams, heard, outcome) or outcome
                # parsed, but not completely enough to answer alone: the grammar's reading
                # is one of the options Jev will see, not the answer (`decide.fast`)
                self.last_exchange["grammar"] = {
                    "intent": parse.intent.value,
                    "verdict": outcome.verdict.value,
                }
            elif not self._single():
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
            self._rivals = ()
            elsewhere = self._recipe_elsewhere(parse.slots.verb or "", window, kind, state)
            if elsewhere is not None:
                window, kind = elsewhere
            elif len(self._rivals) > 1:
                # several windows could run it and nothing said which. Refusing here with
                # the typing sentence blamed the focused terminal for an ambiguity that
                # has nothing to do with it.
                shown = tuple(
                    resolve.window_candidate(self.lexicon, heard.text, w, 1.0)
                    for w in self._rivals[:MAX_HINTS]
                )
                template = _template(parse.intent, parse.slots)
                return self._hints(template, shown, "several windows can do that", heard, state)
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

    _rivals: tuple[Window, ...] = ()

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
        if len(able) == 1:
            return able[0]
        self._rivals = tuple(w for w, _ in able)
        return None

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

    def _single(self) -> bool:
        """One request per command (`nlu/decide.py`), which needs Jev to be reachable."""
        return self._jev_on() and self.cfg.jev.path == "single"

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
        # Only for a whole utterance the grammar settled nothing about. A `need` is a
        # parse that is complete except for one entity, and a plan would be answering a
        # different question; a `chain` is a clause of an utterance something has already
        # cut, and a plan inside it would cut it a second time, by a second mechanism,
        # each obeying its own half.
        # EVERY utterance, not only the ones the grammar settled nothing about. The old
        # condition made the harvest a rescue path: code decided first and the live list
        # of what this machine can actually do was built only after code had failed. That
        # is backwards. A `chain` is still excluded because its clauses were already cut
        # by another mechanism and a plan inside one would cut it a second time.
        offered = await self._harvest(heard) if chain is None else {}
        fan, local_app, offered = self._fan_out(heard, state, need, seams, offered)
        answers = await self._ask(fan)
        taken = [] if seams.asked else self._separating(answers, seams)
        if seams.at:
            seams = replace(seams, asked=True)
        if taken:
            chained = await self._chained(seams, state, pinned_address, taken)
            if chained is not None:
                return chained
        if offered and not taken and self._addressed(answers):
            planned = self._planned(answers, offered, heard, state)
            if planned is not None:
                return planned
            # one step, and it named an adapter option: act on it if it names its own
            # target, and otherwise fetch first and decide again (`nlu/turns.py`)
            second = await self._service_answer(answers, offered, heard, state)
            if second is not None:
                return second
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
        if need is not None and (not _answered(decision) or _mislocated(decision, heard, state)):
            reached = self._reached(answers, heard, state)
            if reached is not None and _answered(reached):
                return self._partial(seams, heard, reached) or reached
            # The narrow question could not place the phrase, which is an empty answer
            # to the wrong question rather than a hard case: nothing here is called
            # "youtube", but a browser sitting right there can go to it. Rx asked the
            # open question in the same parallel gather, so the answer is already paid
            # for.
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
        already settled: there is nothing else worth asking about it.

        No context block either. Whether a word separates two commands is a question
        about the sentence, and what is open on the desktop cannot help answer it.
        """
        base = self._base(heard)
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
            # what THIS utterance has already done, for the clause that follows it. The
            # context block's `previous` is the memory of earlier utterances, so inside
            # one sentence every clause was told the same stale thing: "put the music on
            # and turn it down a bit" decided clause 2 knowing nothing about clause 1 and
            # came back "brightness is not supported yet". Turning something down right
            # after starting music is about the music.
            self._chain_did = _clause_note(decision)
            state = _projected(state, decision)
        if len(decisions) < 2:
            return None
        self.last_exchange["path"] = "chain"
        self.last_exchange["clauses"] = [seams.said(clause) for clause in cut]
        return replace(decisions[0], rest=tuple(decisions[1:]))

    # -------------------------------------------------------------- the ordered plan

    async def _harvest(
        self, heard: _Heard, *, copies: int = plan.MAX_STEPS
    ) -> dict[str, Affordance]:
        """Everything the machine can do right now, as options a model may choose among.

        `copies` is how many times the caller's request will carry this list, and it halves
        the token budget three times over for a plan of three steps (`affordances.harvest`).
        The second turn asks ONE Choice, so it passes 1 and gets the whole budget.

        Off the loop and under a deadline. The harvest itself measured 1.4 to 2.2 ms p50
        and 5.6 ms cold (`affordances.py`), but it asks every adapter and an adapter asks
        a bus, and the next key press has to open the microphone on time whatever a bus
        is doing. A harvest that fails costs the plan and never the utterance.

        The deadline abandons the thread rather than killing it, because nothing can kill
        a thread blocked in a socket read. That is the right trade here: the work is
        read-only by contract (`adapters.base.Adapter.resolve`), so a harvest still
        running when nobody wants it any more costs a thread and changes nothing.
        """
        machine = affordances.Machine(
            state=self.context.state,
            heard=heard.text,
            now=self.context.now,
            lexicon=self.lexicon,
            cfg=self.cfg,
            adapters=self.adapters,
            drift=self._drift(),
        )
        try:
            async with asyncio.timeout(HARVEST_S):
                found = await asyncio.to_thread(affordances.harvest, machine, copies=copies)
        except Exception as exc:
            self.last_exchange["errors"]["harvest"] = type(exc).__name__
            return {}
        self.last_exchange["affordances"] = found.note()
        return plan.offered(found.offered)

    def _drift(self) -> tokens.Drift:
        """How far the token estimator has been running low this session, from the client
        that measures it. A caller with no client has measured nothing, which is what a
        fresh `Drift` says."""
        drift = getattr(self.evaluator, "drift", None)
        return drift if isinstance(drift, tokens.Drift) else tokens.Drift()

    def _plan_request(
        self,
        base: dict[str, Any],
        offered: dict[str, Affordance],
        *,
        name: str = "rp",
        build: Callable[[dict[str, Affordance]], dict[str, Question]] = plan.questions,
    ) -> tuple[requests.Request | None, dict[str, Affordance]]:
        """Rp: the step Choices in ONE request, with the options that fit in it.

        `build` is which questions ask it, and it is a parameter for two reasons that
        arrived together. The second turn asks ONE Choice, over a list that may be fifty
        search results: three Choices carry that list three times and about seven options
        fit, while one carries it once and thirty to forty-three do
        (`requests.plan_request`). And it asks a DIFFERENT question, because a measurement
        said it had to (`bank.chosen_result`). The shrinking loop below is the same either
        way: it is about what fits, not about what is being asked.

        Never split across requests. `requests._wire_one` inlines a Choice's options
        inside each question, so `step_1` and `step_2` in two different requests would
        carry two copies of the option list, which is the one payload this fan-out cannot
        afford (`affordances.OPTION_BUDGET`).

        So the list shrinks instead, from the bottom of the ranking, exactly as
        `window_requests` drops the least recently used window until R2 fits. The harvest
        already priced three copies against its own budget; this re-prices them against
        the request they are actually going into, because the fixed part of a step
        question (`bank.plan_step`'s preamble, repeated verbatim in all three) is a cost
        only the caller can see. The options that survive are returned, because an option
        that was never sent must not be resolvable from an answer either.
        """
        options = dict(offered)
        drift = self._drift()
        cap = requests.headroom(self.cfg.jev.token_cap, drift)
        while options:
            questions = build(options)
            estimated = requests.estimate(base, questions)
            if estimated <= cap:
                built = requests.Request(
                    name, base, questions, estimated, drift.ratio, drift.samples
                )
                return built, options
            options.popitem()  # the lowest ranked option goes first
        return None, {}

    def _addressed(self, answers: _Answers) -> bool:
        """Was this said to the computer at all, and is it something hyprsay does?

        The plan is read BEFORE the intent, because a compound utterance is exactly where
        the intent Choice splits its mass between two commands and `_read_utterance`
        would answer "not sure what you meant" about a sentence that is perfectly clear
        and merely double. These two gates are not skipped with it: `addressed` is what
        keeps a conversation in the room from moving windows, and a plan of two steps is
        no more addressed than a plan of one. A failed R1 answers neither, so it plans
        nothing: nothing has said this was a command.
        """
        gates = self.cfg.gates
        addressed = answers.r1.get("addressed")
        if not isinstance(addressed, BooleanAnswer) or addressed.probability < gates.addressed:
            return False
        unsupported = answers.r1.get("unsupported_kind")
        if isinstance(unsupported, ChoiceAnswer) and unsupported.choice != bank.NONE:
            # understood perfectly, and it is something hyprsay does not do. An option
            # list cannot take a screenshot, so a plan here would name the nearest thing
            # it can do instead, which is the worst possible answer.
            return unsupported.margin < gates.margin
        return True

    def _planned(
        self,
        answers: _Answers,
        offered: dict[str, Affordance],
        heard: _Heard,
        state: DesktopState,
    ) -> Decision | None:
        """The ordered plan as one decision carrying the rest, or None to go on as before.

        Only a plan of two steps or more is taken. One step is what every path below
        already answers, and answers better: a single target goes through R2 and R2b,
        two formulations of one decision that have to agree, and a step Choice has no
        equivalent of that agreement. What a plan adds is the thing nothing else can do,
        which is to see a second command in an utterance with no connective in it.
        """
        read = plan.read(answers.steps, offered)
        self.last_exchange["plan"] = {
            "steps": read.describe(),
            "cut": read.cut.value,
            "complete": read.complete,
        }
        if not read.compound or read.cut is plan.Cut.UNKNOWN:
            # an id code never minted is a broken answer rather than a finished one, so
            # nothing in it is used, not even the steps that came before it
            return None
        decisions: list[Decision] = []
        for step in read.steps:
            built = self._step(step, heard, state, late=answers.late)
            if built is None:
                return None
            decisions.append(built)
            # each step is understood against the desktop the step before it leaves,
            # which is the same projection the clause chain is judged against
            state = _projected(state, built)
        self.last_exchange["path"] = "plan"
        return replace(decisions[0], rest=tuple(decisions[1:]))

    # --------------------------------------------------- an adapter answered on its own

    async def _service_answer(
        self,
        answers: _Answers,
        offered: dict[str, Affordance],
        heard: _Heard,
        state: DesktopState,
    ) -> Decision | None:
        """A one-step plan that named an adapter option, in the two shapes one comes in.

        BOUND, an option that names its own target, is acted on here and nowhere else.
        `_planned` takes only a plan of two steps or more, and its reason is sound for a
        window: "a single target goes through R2 and R2b, two formulations of one decision
        that have to agree". There is no second formulation to agree with about a media
        player. R2 and R2b ask which WINDOW, and nothing else in this file can produce an
        adapter action at all, so for a service the one-step plan is the only route there
        is. Dropping it is what kept "pause the browser, not the music" unreachable.

        UNBOUND is the second turn, and there is exactly one case of it: the answer named
        a capability with NO HANDLE, which is an adapter saying "I can find things by
        name, ask me". Jev is non-generative, so it cannot write the thing to find or the
        id of the thing it finds; only the adapter can, and only after somebody asks it.
        `nlu/turns.py` holds that whole policy and the reasons behind each number.

        WHAT HAPPENS BETWEEN THE TWO TURNS IS A READ. `adapters.base.Adapter.resolve` is
        read only by contract and therefore always tier 0, so nothing is written before
        turn two is judged, and this cannot act twice however badly turn two goes: turn
        one's answer was never something `perform` would accept (`base.Minted`).

        WHY THE SECOND LIST STILL CARRIES THE WHOLE HARVEST when only a fetched candidate
        may be acted on. A Choice always crowns a winner, which this project measured the
        hard way ("a forced choice crowns a winner even when the right window is not
        open"), so a list of nothing but search results would always produce a track. The
        harvest is in the list as somewhere else for the probability mass to go, and code
        reads an answer that lands there as "none of these fitted", stops, and says so.
        None of it is a licence: turn two goes through `_step` and the tier function
        exactly as turn one did.

        None means this was neither case, and the paths below answer the utterance as
        they did before there was a second turn.
        """
        read = plan.read(answers.steps, offered)
        if len(read.steps) != 1:
            return None
        step = read.steps[0]
        asked = step.affordance
        if asked.adapter == affordances.SELF:
            return None
        if not turns.needs_fetching(asked):
            self.last_exchange["path"] = "service"
            return self._step(step, heard, state, late=answers.late)
        note = turns.Note(capability=asked.capability)
        budget = turns.Budget(clock=self._clock).started()
        adapter = adapters.by_name(self.adapters, asked.adapter)
        if adapter is None:
            return self._gave_up(note, turns.Stopped.NO_ADAPTER, heard)
        capability = self._capability_now(adapter, asked.capability, heard, state)
        if not turns.fetchable(capability):
            return self._gave_up(note, turns.Stopped.NO_CAPABILITY, heard)
        slots = turns.slots_for(capability, heard.text, heard.raw.split())
        if slots is None:
            return self._gave_up(note, turns.Stopped.NO_QUERY, heard)
        note = replace(note, query_chars=sum(len(v) for v in slots.values()))
        stop = budget.stop()
        if stop is not None:
            return self._gave_up(note, stop, heard)
        found, why, note = await self._fetch(adapter, capability, slots, heard, state, budget, note)
        if why is not None:
            return self._gave_up(note, why, heard)
        budget = budget.taken()
        return await self._decide_again(step, turns.renumber(found), heard, state, budget, note)

    async def _fetch(
        self,
        adapter: Adapter,
        capability: Capability,
        slots: dict[str, str],
        heard: _Heard,
        state: DesktopState,
        budget: turns.Budget,
        note: turns.Note,
    ) -> tuple[tuple[Affordance, ...], turns.Stopped | None, turns.Note]:
        """Ask the adapter for real candidates, under the fetch half of the budget.

        Off the loop, because an adapter reads a socket or a bus and the next key press
        has to open the microphone on time whatever a socket is doing. The deadline
        abandons the thread rather than killing it, for `_harvest`'s reason and with
        `_harvest`'s justification: `resolve` is read only by contract, so a fetch still
        running when nobody wants it costs a thread and changes nothing.

        Through `base.candidates` and never `adapter.resolve` directly, so that the tier
        floor, the forced `UNTRUSTED`, the unique ids and the cap are all applied to what
        comes back, exactly as they are during a harvest.
        """
        context = adapters.Context(state=state, heard=heard.text, now=self.context.now)
        started = self._clock()

        def ask() -> tuple[Affordance, ...]:
            return adapters.candidates(adapter, capability, slots, context)

        try:
            async with asyncio.timeout(budget.fetch_seconds()):
                found = await asyncio.to_thread(ask)
        except TimeoutError:
            return (), turns.Stopped.OUT_OF_TIME, self._fetched(note, started, 0)
        except adapters.AdapterError as exc:
            # the adapter's own sentence names the repair ("start it with `waytify
            # daemon`"), which is the one thing a generic failure message cannot do
            note = replace(self._fetched(note, started, 0), extra={"why": str(exc)})
            return (), turns.Stopped.FETCH_FAILED, note
        except Exception as exc:
            self.last_exchange["errors"]["fetch"] = type(exc).__name__
            return (), turns.Stopped.FETCH_FAILED, self._fetched(note, started, 0)
        note = self._fetched(note, started, len(found))
        if not found:
            return (), turns.Stopped.NOTHING_FOUND, note
        return found, None, note

    def _fetched(self, note: turns.Note, started: float, count: int) -> turns.Note:
        return replace(note, fetch_ms=(self._clock() - started) * 1000, fetched=count)

    async def _decide_again(
        self,
        asked: plan.Step,
        fetched: list[Affordance],
        heard: _Heard,
        state: DesktopState,
        budget: turns.Budget,
        note: turns.Note,
    ) -> Decision:
        """Turn two: one Choice, over a fresh harvest with the fetched candidates in front.

        The fetched ones go FIRST and keep the adapter's own order, for the reason
        `turns.renumber` gives: "the second live version" is an index into the list
        Spotify ranked, and this file's own ranker would reshuffle it. First also means
        they are the last to be dropped when the list has to shrink to fit the token cap,
        because `_plan_request` pops from the end.
        """
        if budget.left <= 0.0:
            return self._gave_up(note, turns.Stopped.OUT_OF_TIME, heard)
        # the machine may have moved during the fetch, so it is read again rather than
        # remembered; the state the STEP is judged against is still this utterance's one,
        # and the executor re-reads the desktop before it writes, as it does for every
        # other decision in this file
        first_harvest = self.last_exchange.get("affordances")
        again = await self._harvest(heard, copies=1)
        note = replace(note, extra={**note.extra, "harvest": self.last_exchange["affordances"]})
        if first_harvest is not None:
            self.last_exchange["affordances"] = first_harvest
        options = plan.offered([*fetched, *again.values()])
        # "rp.2": the same family as the first plan request, so `_merge` files its answers
        # in `answers.steps` unchanged, and a distinct name, so `inspect --last` shows two
        # requests rather than the second overwriting the first (`requests.Request.name`)
        request, options = self._plan_request(
            self._base(heard), options, name="rp.2", build=plan.result_questions
        )
        if request is None:
            return self._gave_up(note, turns.Stopped.UNANSWERED, heard)
        sent = self.last_exchange.get("requests", [])
        self.last_exchange["requests"] = [*sent, request.as_sent()]
        elapsed = self.last_exchange.get("elapsed_ms")
        answers = await self._ask(requests.FanOut((request,)))
        note = replace(note, asked_ms=self.last_exchange.get("elapsed_ms") or 0.0)
        if elapsed is not None:
            self.last_exchange["elapsed_ms"] = elapsed
        picked = plan.read(answers.steps, options, count=1)
        if not picked.steps:
            return self._gave_up(note, _GAVE_UP.get(picked.cut, turns.Stopped.UNANSWERED), heard)
        chosen = picked.steps[0]
        note = replace(note, chose=chosen.id)
        if chosen.id not in {item.id for item in fetched}:
            # turn two named something turn one had already been offered and passed over,
            # or another capability that would need its own fetch. Either way nothing the
            # fetch produced was chosen, so the fetch answered nothing and the honest
            # move is to stop. Acting on it instead would overrule turn one with evidence
            # turn one already had.
            reason = (
                turns.Stopped.OUT_OF_TURNS
                if turns.needs_fetching(chosen.affordance)
                else turns.Stopped.NO_BETTER
            )
            return self._gave_up(note, reason, heard)
        decision = self._step(chosen, heard, state, late=answers.late)
        if decision is None:
            return self._gave_up(note, turns.Stopped.NO_BETTER, heard)
        self.last_exchange["path"] = "second turn"
        self.last_exchange["turn"] = replace(note, stopped="").describe()
        # `asked` is kept only so the note can say which capability started this; nothing
        # about turn one's answer reaches the action, which was built from turn two alone
        self.last_exchange["turn"]["asked"] = asked.id
        return decision

    def _gave_up(self, note: turns.Note, why: turns.Stopped, heard: _Heard) -> Decision:
        """The second turn ended with nothing to do, and says which of the nine reasons.

        A SUGGEST and never a REFUSE: nothing was forbidden, there was simply nothing to
        act on, and the sentence has to be the one that names the repair rather than one
        that reads as "there is nothing to play" (`base.Adapter.probe`'s rule).
        """
        self.last_exchange["path"] = "second turn"
        self.last_exchange["turn"] = replace(note, stopped=why.value).describe()
        spoken = str(note.extra.get("why") or "") if why is turns.Stopped.FETCH_FAILED else ""
        return self._suggest(
            heard,
            spoken or turns.SENTENCE[why],
            Intent.MEDIA,
            rehearable=why in turns.REHEARABLE,
        )

    def _step(
        self, step: plan.Step, heard: _Heard, state: DesktopState, *, late: bool
    ) -> Decision | None:
        """One step, judged by the tier function exactly as a single command would be.

        None means code cannot carry this step out, and then the whole plan is dropped
        rather than shortened: obeying the front of an utterance and dropping the rest is
        the failure this path exists to remove.
        """
        affordance = step.affordance
        if affordance.adapter != affordances.SELF:
            return self._service_step(step, heard, state, late=late)
        action = self._action_for(affordance, state, first=step.position == 1)
        if action is None:
            return None
        level = tiers.tier(action.intent, action, state, self.cfg)
        if level >= 3:
            # tier 3 is confirmed by a physical key and is never offered to a model. It
            # cannot be built as an affordance either (adapters/base, MAX_OFFERABLE_TIER)
            return None
        if level > 1 and not _reaching(action, heard):
            # A step is chosen from a list, and a list is not evidence from the speaker.
            # That held while the list could only say "close this window": nothing in the
            # utterance had to mention closing for closing to be picked.
            #
            # It does not hold for reaching inside a window, which is the whole of what
            # hypruse is for. "go to youtube" and "click subscribe" ARE the speaker's own
            # words, the option was built from them, and the tier keeps its teeth: above
            # tier 1 the verdict is a countdown that a word cancels. So a disruptive step
            # is allowed exactly when the utterance asked to reach for something.
            return None
        wide = step.margin >= self.cfg.gates.margin
        verb_said = tiers.verb_said(action.intent, heard.literal)
        if action.intent is Intent.LAUNCH_APP and action.app is not None:
            chosen = resolve.app_candidate(self.lexicon, heard.text, action.app, step.probability)
            # `_launch` is the one funnel: whether naming an application means starting
            # one or going to the copy that is already open is `ground.reach`'s decision
            # against live state, here exactly as everywhere else
            return self._launch(
                action.app,
                (chosen,),
                Evidence(source="jev", late=late, wide_margin=wide),
                heard,
                state,
                said=heard.text,
            )
        if affordance.kind == affordances.WINDOW and action.window is not None:
            # a focus target is something the utterance was supposed to NAME, so it is
            # corroborated the same way every other named window is: by a trusted field
            candidate = resolve.window_candidate(
                self.lexicon, heard.text, action.window, step.probability
            )
            evidence = Evidence(
                source="jev",
                corroborated=candidate.corroborated,
                wide_margin=wide,
                verb_said=verb_said,
                explicit_target=candidate.corroborated,
                late=late,
            )
            return self._finish(action, evidence, (candidate,), heard, state)
        # a layout, workspace, volume or media step names no entity to corroborate: the
        # option is hyprsay's own text and its target is the window in front of the
        # speaker, which is pointing rather than naming (`_pointed` reads it the same way)
        evidence = Evidence(
            source="jev",
            corroborated=True,
            wide_margin=wide,
            verb_said=verb_said,
            explicit_target=True,
            late=late,
        )
        return self._finish(action, evidence, (), heard, state)

    # ------------------------------------------------------- an adapter's own capability

    def _service_step(
        self, step: plan.Step, heard: _Heard, state: DesktopState, *, late: bool
    ) -> Decision | None:
        """A chosen adapter option, as an action the executor will actually carry out.

        This used to return None with a comment saying the executor had no route to an
        adapter, which made every MPRIS, Spotify and waytify option a candidate the model
        could be offered, could choose, and could never have obeyed. The route is
        `Intent.SERVICE` and `ops._service`; what is decided here is whether it may run.

        WHAT CORROBORATES ONE, which is the only interesting question. A resolved
        candidate's label is a track name off the internet, and `adapters/base.py`'s rule
        is that untrusted text may RAISE what an act costs and may never be the evidence
        that authorizes it. So the label corroborates nothing. What does is the
        CAPABILITY'S SUMMARY, which is one line written in this repository beside the
        capability, and therefore the one piece of text in an adapter option that a tier
        is allowed to rest on: the speaker said "play", the capability says "play a track
        on Spotify, found by name", and that is the speaker naming what they wanted.

        TIER 2 IS OFFERABLE AND NOT AUTHORIZABLE, deliberately. `base.MAX_OFFERABLE_TIER`
        lets an adapter show a tier 2 capability, and `tiers.verb_said` has no entry for
        `Intent.SERVICE`, so `tiers._disruptive` refuses one. That is the same shape
        `affordances.MAX_INAPP_TIER` already has: an answer to a Choice is not a verb the
        speaker said out loud, and no adapter gets to be the exception.
        """
        affordance = step.affordance
        if turns.needs_fetching(affordance):
            # a capability offered as ITSELF names no target, so there is nothing to
            # perform: it is fetched from, which is `_service_answer`'s job and never a
            # step's. `plan.read`'s rule 5 means this can only be a one-step plan anyway.
            return None
        adapter = adapters.by_name(self.adapters, affordance.adapter)
        capability = self._capability_now(adapter, affordance.capability, heard, state)
        if capability is None:
            # the adapter is gone, or no longer offers this. Dropping the step is the
            # same answer `_action_for` gives a window that closed while Jev was thinking
            return None
        try:
            level = max(affordance.tier, adapters.tier_for(capability, affordance.label))
        except adapters.AdapterError:
            return None
        service = Service(
            adapter=affordance.adapter,
            capability=affordance.capability,
            handle=affordance.handle,
            tier=level,
            label=affordance.label,
        )
        action = Action(Intent.SERVICE, service=service)
        evidence = Evidence(
            source="jev",
            corroborated=self._named_capability(capability, heard),
            wide_margin=step.margin >= self.cfg.gates.margin,
            verb_said=tiers.verb_said(Intent.SERVICE, heard.literal),
            # the handle IS the target and the adapter minted it from what the speaker
            # asked for, so there is no window to have named. Read only at tier 2, where
            # `verb_said` above has already refused.
            explicit_target=True,
            late=late,
        )
        return self._finish(action, evidence, (), heard, state)

    def _capability_now(
        self, adapter: Adapter | None, capability: str, heard: _Heard, state: DesktopState
    ) -> Capability | None:
        """What that adapter says it can do RIGHT NOW, or None.

        Asked again rather than kept from the harvest, which is `base.capability_of`'s
        own reason: what an application can do now is not what it could do when the key
        went down. An adapter that throws costs this option and never the utterance, the
        same bargain `base.offer` makes during the harvest.
        """
        if adapter is None:
            return None
        context = adapters.Context(state=state, heard=heard.text, now=self.context.now)
        try:
            return adapters.capability_of(adapter, capability, context)
        except Exception as exc:  # a broken adapter degrades the option, not the sentence
            self.last_exchange["errors"]["adapter"] = type(exc).__name__
            return None

    def _named_capability(self, capability: Capability, heard: _Heard) -> bool:
        """Did the speaker use one of this capability's own words?

        `affordances.said` and not `claims`, and against the summary rather than against
        the label: `said` asks whether the speaker used THIS option's vocabulary, which is
        the question worth asking of a hand written line, and the summary is the only
        string in an adapter option that this repository wrote.
        """
        words = tuple(w for w in affordances.spoken(capability.summary) if w not in _FILLER)
        if not words:
            return False
        return affordances.said(affordances.spoken(heard.text), (words,)) > 0.0

    def _action_for(
        self, affordance: Affordance, state: DesktopState, *, first: bool
    ) -> Action | None:
        """The operation an option names, dereferenced from its handle by code.

        The model answers an id and nothing else. Everything that turns that id into
        something the executor will run happens here, out of the map code built in the
        same breath as the option, which is why no label can ever become a target.

        A step that is not the first leaves a layout target BLANK on purpose: the handle
        names the window that was focused when the machine was harvested, and by the time
        a later step runs an earlier one has moved the desktop. A blank target is settled
        against FRESH state at the moment of the write (`executor._check_window`), which
        is what a clause after a launch already does (`_bound`).
        """
        intent = Intent(affordance.capability) if affordance.capability in _CAPABILITIES else None
        handle = affordance.handle
        if intent is None:
            return None
        if intent is Intent.FOCUS_WINDOW:
            window = state.by_address(handle)
            return Action(intent, window=window) if window is not None else None
        if intent is Intent.SWITCH_WORKSPACE:
            return Action(intent, workspace=handle) if handle else None
        if intent is Intent.LAUNCH_APP:
            app = self._app_by_id(handle)
            return Action(intent, app=app) if app is not None else None
        if intent is Intent.VOLUME:
            # "set" is the one verb that needs a number, and a step Choice carries none
            return Action(intent, verb=handle) if handle in bank.VOLUME_VERBS - {"set"} else None
        if intent is Intent.MEDIA:
            return Action(intent, verb=handle) if handle in bank.MEDIA_VERBS else None
        if intent in (Intent.FULLSCREEN, Intent.TOGGLE_FLOATING):
            window = state.by_address(handle) if first else None
            return None if first and window is None else Action(intent, window=window)
        if intent in tiers.IN_APP:
            # the handle carries the window and the thing inside it, in that order
            address, inside = affordances.target(handle)
            window = state.by_address(address)
            if window is None or not inside:
                return None
            if intent is Intent.SCROLL:
                if inside not in {d.value for d in Direction}:
                    return None
                return Action(intent, window=window, direction=Direction(inside))
            if intent is Intent.CLICK_CONTROL:
                # `ops._click_control` reads `text` to match the control and `verb` to
                # check the exact name it resolved to, so both carry the same words. The
                # option only exists because the speaker's own words were found in that
                # name (`controls.best_match`), which is what makes it corroborated.
                return Action(intent, window=window, verb=inside, text=inside)
            return Action(intent, window=window, verb=inside)
        # RESIZE_WINDOW and MOVE_WINDOW need a direction or a size word that a step
        # Choice does not carry (`REQUIRED`). Rather than invent one, the plan is dropped
        # and the utterance is understood as one command, exactly as it was before.
        return None

    def _app_by_id(self, ident: str) -> App | None:
        listing = getattr(self.lexicon, "apps", ())
        listing = listing() if callable(listing) else listing
        if isinstance(listing, dict):
            listing = listing.values()
        return next((app for app in listing or () if app.id == ident), None)

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

    def _base(self, heard: _Heard) -> dict[str, Any]:
        """The state every request in a fan-out shares: the utterance, and the desktop.

        The desktop half used to be built and then withheld, which asked the model the
        question and kept the evidence: the intent descriptions say launching is "not for
        going to a window that is already open" while nothing in the request said what
        was open. That is why "open my browser here" was a coin toss. It had no way to
        know the browser was already running one workspace over.

        Only trusted fields go (`context.Context.for_request`): the names of applications
        a root owned `.desktop` file claims, the current workspace, what is focused, and
        what the last command did. A window whose class no trusted file claims is left
        out entirely, because naming it from its own class would let a window name
        itself. No titles, ever, and nothing the speaker dictated.

        Measured when it was added: 113 tokens on each of R1's two parts, against a
        1,800 cap they were using 1,392 and 1,344 of, and 37 us to build against a
        315 ms round trip.
        """
        state = requests.utterance_state(heard.said, heard.variants)
        block = {**self.context.for_request()}
        if self._chain_did:
            # inside one utterance this is the truth and the remembered turn is not
            block["previous"] = self._chain_did
        return {**state, **block}

    def _ballot_label(self, window: Window) -> tuple[str, Any]:
        """How a window is named on the ballot: its trusted name AND what it is for.

        The bare class ("google-chrome") left "open my browser here" answered "none of
        these" at 0.6, because nothing on the ballot said browser. `kind` is the phrase
        the lexicon derives from a trusted `.desktop` file's Categories and GenericName,
        which is the same trusted evidence a window match is allowed to rest on, and it
        is what makes describing instead of naming work at all (bank, section 3).
        """
        label, trust = affordances._window_label(window, self.lexicon)
        try:
            owner = self.lexicon.app_for_window(window)
            kind = self.lexicon.kind_of(window)
        except Exception:
            owner, kind = None, ""
        # the trusted `.desktop` Name, not the class. The class is "google-chrome" and
        # reads like a package; the Name is "Google Chrome" and is what a person calls
        # it. Only when a root owned file claims the window, so a window still cannot
        # name itself.
        if owner is not None and owner.trusted and owner.name:
            label = owner.name
        return (f"{label} ({kind})" if kind else label), trust

    def _reach_ballot(
        self, heard: _Heard, state: DesktopState, need: _Need
    ) -> tuple[dict[str, str], dict[str, _Reach]]:
        """Every whole action the speaker's phrase could name, as options Jev may pick.

        Built from live state and from the phrase itself, never from a table of routes.
        A window is described by the same trusted fields the lexicon matches on, so an
        application cannot write itself onto the ballot through its title.
        """
        phrase = (need.parse.slots.window_ref or need.parse.slots.app_ref or "").strip()
        if not phrase:
            return {}, {}
        labels: dict[str, str] = {}
        plans: dict[str, _Reach] = {}
        ranked = self.lexicon.match_windows(heard.text, state, limit=MAX_HINTS)
        here = state.active_workspace_id
        for i, (window, _score) in enumerate(ranked, 1):
            label, trust = self._ballot_label(window)
            fit = trust is not Trust.UNTRUSTED
            # Rich, and specifically naming both workspaces. The vague wording ("go to
            # the X window, which is already open") lost to "none of these" at 0.53 on
            # "open my browser here"; naming where the window IS and where the speaker
            # is took the same decision to 0.96. Section 3 of the bank says this in
            # general terms and it is worth restating: the option is the question.
            where = f"on workspace {window.workspace_id}"
            labels[f"focus{i}"] = (
                f"leave the {label} window where it is, {where}, and switch to it there"
            )
            plans[f"focus{i}"] = _Reach("focus", window=window, corroborated=fit)
            if window.workspace_id != here:
                labels[f"here{i}"] = (
                    f"bring the {label} window from workspace {window.workspace_id} "
                    f"to workspace {here}, where the speaker is now"
                )
                plans[f"here{i}"] = _Reach("here", window=window, corroborated=fit)
        # the lexicon's best match, not only an exact one. "open my browser here" names
        # no application exactly, so an exact lookup left the ballot with no way to say
        # "start one" at all, and Jev answered "none of these" at 0.84 -- correctly,
        # because the thing being asked about was genuinely not on it.
        matched = self.lexicon.match_apps(phrase, limit=1)
        app = matched[0][0] if matched else None
        if app is not None:
            labels["launch"] = f"start {app.name}, a second copy of it even if one is already open"
            plans["launch"] = _Reach("launch", app=app, corroborated=app.trusted)
        # the browser is the one window that can be sent somewhere by name, and the
        # phrase is where. Offered only when exactly one window can, so that choosing it
        # names a target rather than leaving one to be guessed at run time.
        able = [w for w in state.windows if _can_navigate(w, self.lexicon)]
        if len(able) == 1:
            window = able[0]
            label, trust = self._ballot_label(window)
            fit = trust is not Trust.UNTRUSTED
            # `site` is offered only when the phrase really renders as an address. An
            # option code cannot carry out is not an option, it is a trap: "go to linus
            # tech tips" was answered "navigate" at 0.91 and then died on "that sounded
            # like a phrase rather than an address", which is the ballot's fault and not
            # the answer's. Searching stays on it, so the sentence still has a home.
            if _renders(window, "go_to_url", phrase, self.lexicon):
                labels["site"] = f"point {label} at the website {phrase}"
                plans["site"] = _Reach("site", window=window, text=phrase, corroborated=fit)
            if _renders(window, "search_web", phrase, self.lexicon):
                labels["web"] = f"search the web for the words {phrase}, using {label}"
                plans["web"] = _Reach("web", window=window, text=phrase, corroborated=fit)
        # one real option plus "none of these" is still a question worth asking: it is
        # "is this what you meant", and pre-validating the recipes often leaves exactly
        # one. Requiring two threw the ballot away for "go to linus tech tips", where
        # searching was the only thing that could be done and was the right answer.
        return (labels, plans) if labels else ({}, {})

    def _reached(self, answers: _Answers, heard: _Heard, state: DesktopState) -> Decision | None:
        """The Rx answer as a decision, or None when there is not one to be had."""
        reply, plans = answers.reach, self._reach_plans
        if reply is None or not plans:
            return None
        chosen = plans.get(reply.choice)
        if chosen is None:  # "none of these", or an id from a ballot we no longer hold
            return None
        ranked = reply.ranked
        # against the next real ACTION, not against "none of these". A margin says how
        # much better this action is than the next one; `none` is an abstention and not
        # a rival, and counting it turned "bring Chrome here" at 0.59 against 0.29 of
        # nothing into a close call, so hyprsay fell back on a focus it already knew was
        # wrong. `none` winning is handled above: there is simply no answer then.
        rivals = [p for name, p in ranked[1:] if name != bank.NONE]
        margin = ranked[0][1] - (rivals[0] if rivals else 0.0)
        wide = margin >= self.cfg.gates.margin
        if chosen.kind == "launch" and chosen.app is not None:
            candidate = resolve.app_candidate(self.lexicon, heard.text, chosen.app, ranked[0][1])
            evidence = Evidence(source="jev", wide_margin=wide, late=answers.late)
            return self._launch(chosen.app, (candidate,), evidence, heard, state, said=heard.text)
        window = state.by_address(chosen.window.address) if chosen.window else None
        if window is None:
            return None
        if chosen.kind in ("site", "web"):
            name = "go_to_url" if chosen.kind == "site" else "search_web"
            offered = {r.name: r for r in recipes.for_window(window, self.lexicon.kind_of(window))}
            recipe = offered.get(name)
            if recipe is None:
                return None
            try:
                recipes.render(recipe, chosen.text, window)
            except recipes.RecipeError:
                # "that sounded like a phrase rather than an address" is not a refusal
                # here, it is this option being the wrong one. Searching is on the same
                # ballot, so say nothing and let the narrow reading stand.
                return None
            action = Action(Intent.RUN_RECIPE, window=window, verb=name, text=chosen.text)
            # NOT tiers.verb_said: this recipe was chosen from a list, which is exactly
            # the case `SPOKEN_BY_CONSTRUCTION` excludes (nlu/tiers.py). The speaker has
            # to have asked to go somewhere in their own words.
            said = bool(REACHING_VERBS & set(heard.literal))
            evidence = Evidence(
                source="jev",
                corroborated=chosen.corroborated,
                verb_said=said,
                explicit_target=True,  # offered only when exactly one window can do it
                wide_margin=wide,
                late=answers.late,
            )
            return self._finish(action, evidence, (), heard, state)
        candidate = resolve.window_candidate(self.lexicon, heard.text, window, ranked[0][1])
        evidence = Evidence(
            source="jev",
            corroborated=candidate.corroborated,
            wide_margin=wide,
            explicit_target=candidate.corroborated,
            late=answers.late,
        )
        if chosen.kind == "here":
            action = Action(
                Intent.MOVE_TO_WORKSPACE, window=window, workspace=str(state.active_workspace_id)
            )
        else:
            action = Action(Intent.FOCUS_WINDOW, window=window)
        return self._finish(action, evidence, (candidate,), heard, state)

    def _fan_out(
        self,
        heard: _Heard,
        state: DesktopState,
        need: _Need | None,
        seams: _Seams = NO_SEAMS,
        offered: dict[str, Affordance] | None = None,
    ) -> tuple[requests.FanOut, App | None, dict[str, Affordance]]:
        cap = self.cfg.jev.token_cap
        base = self._base(heard)
        self.last_exchange["context"] = self.context.for_request()
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
        if offered:
            one, offered = self._plan_request(base, offered)
            built += [one] if one is not None else []
        self._reach_plans = {}
        if need is not None:
            # small, and it rides in the same parallel gather, so it costs no latency
            labels, self._reach_plans = self._reach_ballot(heard, state, need)
            if labels:
                built += requests.split("rx", base, {"do": bank.reach(labels)}, cap)
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
        return fan, local_app, offered or {}

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
        if (
            action.intent is Intent.MEDIA
            and action.verb == "play_pause"
            and affordances.names_a_thing_to_play(affordances.spoken(heard.text))
        ):
            # "play the second live version of bohemian rhapsody" with nothing on this
            # machine able to look a track up came back as PLAY/PAUSE, which pauses
            # whatever happens to be open. Naming a particular thing and getting the
            # transport verb is not a near miss, it is a different command, and doing
            # the wrong thing confidently is worse than saying it cannot be done.
            return self._suggest(
                heard,
                "nothing here can look a track up; start the waytify daemon, "
                'or say "play" on its own to resume',
                Intent.MEDIA,
            )
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

    `raw` is the transcript before the normalizer touched it at all, which `literal` is
    NOT: `literal` is `Normalized.tokens`, and those are the normalized spellings carrying
    raw offsets. The difference is exactly one word wide and it matters once, in
    `turns.strip_carrier`: the normalizer writes a spoken "second" as "2", and telling an
    ordinal apart from a band called "2 Unlimited" needs the word the speaker said.
    """

    said: str
    text: str
    tokens: tuple[str, ...]
    literal: tuple[str, ...]
    variants: tuple[str, ...]
    raw: str = ""

    @property
    def every_word(self) -> set[str]:
        return set(self.literal).union(*(v.split() for v in self.variants))


def _heard(norm: Any, parse: Parse | None) -> _Heard:
    # the real normalizer emits Token objects with raw offsets; fakes may emit strings
    literal = tuple(getattr(t, "text", t) for t in norm.tokens)
    text = parse.utterance if parse is not None and parse.utterance else norm.text
    raw = getattr(norm, "raw", "") or norm.text
    return _Heard(norm.text, text, tuple(text.split()), literal, tuple(norm.variants), raw)


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


@dataclass(frozen=True)
class _Reach:
    """One option on the Rx ballot, with everything needed to build its Action."""

    kind: str  # "focus" | "here" | "launch" | "site" | "web"
    window: Window | None = None
    app: App | None = None
    text: str = ""
    corroborated: bool = False


# A recipe reached this way was chosen from a list, so `tiers.SPOKEN_BY_CONSTRUCTION`
# does NOT apply: that shortcut exists because a recipe is normally only reachable by an
# exact phrase, and here it is not. The speaker still has to have asked to go somewhere.
REACHING_VERBS = frozenset(
    {
        "go",
        "open",
        "show",
        "take",
        "bring",
        "pull",
        "navigate",
        "browse",
        "visit",
        "search",
        "look",
        "find",
        "google",
    }  # fmt: skip
)


def _renders(window: Window, name: str, phrase: str, lexicon: LexiconLike) -> bool:
    """Would this recipe actually produce something for these words? Asked of `recipes`,
    which owns the answer, and asked BEFORE the option is offered rather than after it
    is chosen."""
    try:
        recipe = next(
            (r for r in recipes.for_window(window, lexicon.kind_of(window)) if r.name == name),
            None,
        )
        if recipe is None:
            return False
        recipes.render(recipe, phrase, window)
        return True
    except Exception:
        return False


def _can_navigate(window: Window, lexicon: LexiconLike) -> bool:
    """Can this window be pointed at a website? Asked of the recipe table that owns the
    answer, never of a list of class names kept here."""
    try:
        kind = lexicon.kind_of(window)
        return any(r.name == "go_to_url" for r in recipes.for_window(window, kind))
    except Exception:
        return False


# the speaker pointed at a place rather than naming one. Grounding deixis is code's
# job (docs/PLAN.md 5.6); which action it implies is not, and that is what Rx asks.
HERE = frozenset({"here", "this", "current"})


def _mislocated(decision: Decision, heard: _Heard, state: DesktopState) -> bool:
    """Did code settle on a window somewhere else while the speaker said "here"?

    "open my browser here" used to be a coin toss and then, once the desktop rode along
    with the request, a confident focus of a Chrome one workspace away. Confident and
    wrong: the funnel that chooses between starting an application and going to the copy
    already running reads live state, which is right, and has no idea the speaker asked
    for it to be brought over, which is the whole sentence. So the answer is not taken
    at face value here; the ballot, which does carry "bring it to this workspace", is
    asked as well.
    """
    action = decision.action
    if action is None or action.intent is not Intent.FOCUS_WINDOW or action.window is None:
        return False
    if action.window.workspace_id == state.active_workspace_id:
        return False  # already here: focusing IS bringing it over
    return bool(HERE & set(heard.literal))


def _reaching(action: Action, heard: _Heard) -> bool:
    """Did the speaker ask to reach into a window, in their own words?

    The tier 2 guard on a chosen step exists because a list is not evidence. For the
    in-app intents the option was built out of the utterance itself, so the evidence is
    the utterance: a verb of reaching, and an intent that can only touch what is already
    on screen. Closing a window and typing are not on this list and never will be.
    """
    if action.intent not in tiers.IN_APP:
        return False
    return bool(REACHING_VERBS & set(heard.literal))


def _answered(decision: Decision) -> bool:
    """Did this decide something, or is it a shrug?

    An action decided it. So did a refusal, which here is always a safety rule and
    never a failure to understand. So did a tie on something that cannot be taken back,
    where asking IS the answer (docs/PLAN.md 5.6). A suggestion is not an answer, and
    neither is a tie on something free to reverse: those are hyprsay saying it does not
    know, and a caller with another reading to try should try it.
    """
    if decision.verdict in ACTING or decision.verdict in (Verdict.REFUSE, Verdict.NOTHING):
        return True
    if decision.verdict is Verdict.HINTS:
        return decision.tier >= 2
    return False


def _clause_note(decision: Decision) -> dict[str, Any] | str:
    """What one clause of this utterance did, for the clause after it.

    Its intent and its target's kind, never its words: a recipe's spoken span and
    anything dictated are the speaker's own and do not travel (docs/PRIVACY.md). An app
    name only when a trusted `.desktop` file claims the window, the same rule the rest
    of the context block follows.
    """
    action = decision.action
    if action is None or decision.verdict not in ACTING:
        return ""
    note: dict[str, Any] = {"did": action.intent.value}
    if action.verb:
        note["how"] = action.verb
    if action.app is not None and action.app.trusted:
        note["on"] = action.app.name
    return note


def _carried(decision: Decision) -> _Chain:
    """What the next clause may point back at. A clause that did not act leaves nothing.

    Hints are the exception. A hinted clause has not acted, but it is a question rather
    than a failure: it WILL act, on the window the speaker is about to name with a
    number. So what follows it is aimed at that window, not at whatever happened to hold
    the key. That is the same "known only at run time" the launch below describes, and
    `ops._authorize_gesture` is what guards it there.
    """
    action = decision.action
    if decision.verdict is Verdict.HINTS and decision.candidates:
        return _Chain(launched=True)
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
    """The operation with every slot but the target.

    Dictated text is never copied here. A template outlives the utterance it came from,
    waiting in `pending` for a number, and what "type ..." carries is the speaker's own
    words: they last exactly as long as the decision they were cut for.

    What a recipe searches for is not dictation. "Find the ltt youtube channel" does not
    hold a private phrase, it holds the command, so dropping it left a hinted recipe with
    nothing to type: picking a window ran a find for "" and refused. Those keep it.
    """
    return Action(
        intent,
        workspace=slots.workspace,
        direction=slots.direction,
        amount=slots.amount,
        verb=slots.verb,
        number=slots.number,
        text=slots.text if intent in ARGUMENT_TEXT else None,
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
        elif family == "rx" and isinstance(answer, ChoiceAnswer):
            answers.reach = answer
        elif family == "rp":
            # by id, unfiltered: `plan.read` is the one place that decides what a step
            # answer has to look like, and it refuses anything that is not a ChoiceAnswer
            answers.steps[qid] = answer


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
