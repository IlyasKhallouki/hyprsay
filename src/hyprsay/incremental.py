"""When a clause of a half-spoken sentence may be acted on, and when it may not.

The engine used to wait for the key to come up before it decoded anything, so "open
spotify and turn it down" launched Spotify about a second after the speaker had already
finished asking. Acting on the first clause while the second is still being said is worth
most of that second. It is also the most dangerous thing in this codebase, because a
partial transcript is a PREFIX and a prefix is not a shorter version of the sentence: it
is a DIFFERENT sentence that happens to start the same way.

    "close this"        becomes  "close this tab"
    "open chrome"       becomes  "open chrome on workspace three"
    "move it to two"    becomes  "move it to twenty percent"
    "open bits"         becomes  "open bits and bytes"

Every one of those acts on the wrong thing if the prefix is obeyed. So the question this
module answers is never "is this prefix a valid command" (all four are) but "is there
evidence that the speaker has FINISHED this clause". Three rules, and a clause must pass
all three:

1. **Tier 0 or 1, and never the speaker's own words.** A tier 0 action is free to reverse
   (focus, workspace, volume, media) and a tier 1 one is cheap to reverse (a launch
   grounds to the window that is already open). Close, type and lock are tier 2 and 3:
   those are exactly the actions whose prefix reading is unrecoverable, and they wait for
   the whole utterance the way they always have. The tier is read off the clause's own
   Decision, so `nlu/tiers.py` stays the single place that decides what an action may do.

   The tier is not enough on its own, because a recipe can be tier 1 and a recipe carries
   a spoken span: "find lofi" is a perfectly good prefix of "find lofi hip hop radio", and
   acting on it types the wrong thing into a search box and presses enter. So anything
   carrying the speaker's own text waits too, whatever its tier. That is the same cut
   `nlu/context.SPEAKER_TEXT` and `daemon._chip` make, read from the same list.

2. **Evidence the clause ended.** Either a further clause has begun after it, which means
   the speaker has moved on and the words of this one can no longer grow, or the clause's
   own text came back identical from at least two consecutive decodes AND the microphone
   has been quiet since. The first is strong evidence. The second is weak on its own, and
   it was measured to be weak: driven through a real streaming recognizer, "open chrome
   on workspace three" sat at "Open Chrome" for four consecutive decodes while the
   speaker was still saying the rest of it, and the rule as first written focused Chrome
   instead of launching it onto workspace 3. Identical decodes only prove the DECODER has
   not moved. `quiet` is what proves the SPEAKER has not: it is the recorder's own energy
   gate, the one thing in the system that hears the room rather than the model. So the
   weak branch needs both, and `Settler(stable_decodes=...)` makes it stricter still.

3. **A contiguous prefix, never a hole.** Clause n is only offered when every clause
   before it has already been carried out. "open firefox and move it to workspace 3" has
   nothing to move if the launch was refused (daemon._chain says the same thing about a
   whole utterance), and a partial gives a second way to get that wrong: clause 0 could
   come back as a question while clause 1 reads as a clean ACT. Running clause 1 first
   would aim it at whatever happened to be focused. So `look` walks up from the first
   unconsumed clause and stops at the first one that is not ready.

**Consumption.** What has been acted on is a character span of the utterance, a prefix
`[0, consumed_chars)`, and the clause index that span ends at. The index is what actually
prevents a second run: clauses arrive in order, so "the first `n` are spent" is an exact
statement that survives the recognizer changing its mind about the words. The character
span is what that means in the transcript, and it is computed rather than assumed, so
when it cannot be located the caller is told (`Settled.exact`) instead of being handed a
number that looks right.

The span has to be computed because `Decision` carries no offsets: it carries `heard`,
which is the clause as the normalizer wrote it. `spans` locates each clause's `heard` in
the transcript and falls back to counting words when the normalizer repaired one
("firefuck" for "firefox", "work space three" for "workspace 3"). A request for real
offsets on `Decision` is written up in scratchpad/streaming-asks.md; until it lands, the
index is the guarantee and the span is the description.

**Drift.** The partials may come from a different recognizer than the final decode
(`stt/stream.py` explains why), so the final chain's clause 0 can read differently from
the clause 0 that was acted on. `Settled.drifted` names those clauses. They stay consumed
regardless, because running a command twice is the failure this module exists to prevent
and a clause silently skipped is the failure the engine already had before it.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from .model import Decision, Verdict
from .nlu.context import SPEAKER_TEXT

# the highest tier a half-spoken sentence may act on. 2 and 3 are the tiers whose whole
# point is that they are not reversible, and a prefix is exactly the case they guard.
MAX_EARLY_TIER = 1
# how many consecutive decodes must agree before a trailing clause counts as finished
STABLE_DECODES = 2
# the verdicts an early clause may carry. HINTS and SUGGEST are questions and a question
# mid-utterance is answered by the rest of the utterance, not by a badge nobody can see
# yet; COUNTDOWN and CONFIRM_KEY are the tier 2 and 3 ceremonies the tier rule already
# refuses. ACT_SWAP acts and offers a retarget, and the daemon suppresses its badges the
# same way a chained clause does.
EARLY_VERDICTS = frozenset({Verdict.ACT, Verdict.ACT_SWAP})

_WORD = re.compile(r"\S+")


@dataclass(frozen=True)
class Ready:
    """One clause that may be carried out now, and the reason it may be."""

    index: int
    decision: Decision
    span: tuple[int, int]
    why: str


@dataclass(frozen=True)
class Settled:
    """What one decode of a growing utterance means for what may run."""

    spans: tuple[tuple[int, int], ...] = ()
    consumed_index: int = 0
    consumed_chars: int = 0
    ready: tuple[Ready, ...] = ()
    # every clause span was located verbatim in the transcript. False means at least one
    # was placed by counting words, so `consumed_chars` is an estimate and nothing but
    # reporting may lean on it.
    exact: bool = True
    # clauses that were already carried out but whose text has changed since
    drifted: tuple[str, ...] = ()


@dataclass
class Settler:
    """The consumed prefix of one push-to-talk hold, and the settle rule over it.

    One instance per hold. The daemon builds a fresh one on key down, because "the first
    two clauses are spent" is a statement about one utterance and carrying it into the
    next one would skip a command that was never run.
    """

    stable_decodes: int = STABLE_DECODES
    max_tier: int = MAX_EARLY_TIER
    # clause index -> (the text it last showed, how many decodes running it has shown it)
    _runs: dict[int, tuple[str, int]] = field(default_factory=dict)
    # the `heard` of every clause carried out early, in the order they were carried out
    _taken: list[str] = field(default_factory=list)

    @property
    def consumed(self) -> int:
        """How many leading clauses of this utterance have already been carried out."""
        return len(self._taken)

    def reset(self) -> None:
        self._runs.clear()
        self._taken.clear()

    def look(
        self,
        text: str,
        chain: Sequence[Decision],
        *,
        growing: bool = True,
        quiet: bool = True,
    ) -> Settled:
        """What this decode of `text` says about the clauses in `chain`.

        `quiet` is the recorder saying the room has been silent since the last word, and
        it is what the weak branch of the settle rule needs on top of identical decodes.
        It defaults to True so the rule can be reasoned about on its own; the engine
        always passes the real answer.

        `growing` is False for the decode on key up. Nothing settles there: the utterance
        is over, so everything that was not carried out early is carried out in full, and
        the only thing this call is asked for is where the consumed prefix ends. It also
        leaves the stability counters alone, because a final decode is not another vote
        about whether the speaker had stopped talking.
        """
        placed = spans(text, chain)
        if growing:
            self._count(chain)
        consumed = min(len(self._taken), len(chain))
        return Settled(
            spans=placed.spans,
            consumed_index=consumed,
            consumed_chars=placed.spans[consumed - 1][1] if consumed else 0,
            ready=self._ready(chain, placed.spans, consumed, quiet) if growing else (),
            exact=placed.exact,
            drifted=tuple(
                clause.heard
                for index, clause in enumerate(chain[:consumed])
                if clause.heard != self._taken[index]
            ),
        )

    def take(self, ready: Ready) -> None:
        """Record that this clause has been carried out. The next decode skips it."""
        self._taken.append(ready.decision.heard)

    # ----------------------------------------------------------------------- the rule

    def _count(self, chain: Sequence[Decision]) -> None:
        """Bump each clause's run of identical decodes, and cut the ones that changed.

        Keyed by index rather than by text so that a clause which grows ("open chrome"
        into "open chrome on workspace three") restarts its run instead of inheriting the
        run the shorter reading had built up, which is the bug that would let a prefix act
        on the strength of evidence that belonged to a different sentence.
        """
        for index, clause in enumerate(chain):
            seen, run = self._runs.get(index, ("", 0))
            self._runs[index] = (clause.heard, run + 1 if seen == clause.heard else 1)
        for index in [i for i in self._runs if i >= len(chain)]:
            # the utterance got SHORTER, which a re-decode of a growing buffer really does
            # do. A clause that is no longer there has no run to inherit if it comes back.
            del self._runs[index]

    def _ready(
        self,
        chain: Sequence[Decision],
        placed: tuple[tuple[int, int], ...],
        consumed: int,
        quiet: bool,
    ) -> tuple[Ready, ...]:
        out: list[Ready] = []
        for index in range(consumed, len(chain)):
            why = self._why(chain, index, quiet)
            if not why:
                # a hole is never opened: clause n+1 is an order that assumes clause n
                # happened, so the first clause that is not ready ends the offer
                break
            out.append(Ready(index, chain[index], placed[index], why))
        return tuple(out)

    def _why(self, chain: Sequence[Decision], index: int, quiet: bool) -> str:
        """The reason this clause may act now, or "" when it may not."""
        clause = chain[index]
        if clause.verdict not in EARLY_VERDICTS or clause.action is None:
            return ""
        if clause.tier > self.max_tier:
            # close, type and lock. Their prefix reading is the one that cannot be taken
            # back, so they wait for the key to come up.
            return ""
        if _speaker_text(clause):
            # a recipe, a control name or a dictation. Half of what someone meant to type
            # is not a shorter version of it, and a recipe has usually already pressed
            # enter on it by the time the rest of the sentence arrives.
            return ""
        if index < len(chain) - 1:
            return "a further clause has begun"
        run = self._runs.get(index, ("", 0))[1]
        if run >= self.stable_decodes and quiet:
            return f"unchanged across {run} decodes, and the room went quiet"
        return ""


def _speaker_text(clause: Decision) -> bool:
    """Does this clause act on words that are the speaker's own rather than a command?

    The same three questions `nlu/context._speaker_text` asks, off the same list, because
    a second list of "what counts as the speaker's words" is a second thing to forget to
    update.
    """
    action = clause.action
    return (
        clause.dictation
        or action is None
        or action.text is not None
        or action.intent in SPEAKER_TEXT
    )


# --------------------------------------------------------------------------- the spans


@dataclass(frozen=True)
class _Placed:
    spans: tuple[tuple[int, int], ...]
    exact: bool


def spans(text: str, chain: Sequence[Decision]) -> _Placed:
    """Where each clause of `chain` sits in `text`, in order and without overlapping.

    A clause's `heard` is the normalized form of the words it was cut from, so most of the
    time it is a verbatim slice of the lower cased transcript and `find` places it exactly.
    When the normalizer repaired a word it is not, and then the clause is placed by taking
    as many whitespace separated words as it has. That estimate is flagged rather than
    hidden: `consumed_chars` is a description of what ran, never the thing that decides it.
    """
    hay = text.lower()
    out: list[tuple[int, int]] = []
    cursor = 0
    exact = True
    for clause in chain:
        said = (clause.heard or "").strip().lower()
        at = hay.find(said, cursor) if said else -1
        if at < 0:
            at, end = _by_words(text, cursor, len(said.split()))
            exact = exact and not said
        else:
            end = at + len(said)
        out.append((at, end))
        cursor = end
    return _Placed(tuple(out), exact)


def _by_words(text: str, cursor: int, words: int) -> tuple[int, int]:
    """The span of the next `words` whitespace separated words at or after `cursor`."""
    found = [m for m in _WORD.finditer(text) if m.start() >= cursor]
    if not found or words <= 0:
        return cursor, cursor
    return found[0].start(), found[min(words, len(found)) - 1].end()
