"""Questions in, typed answers out.

Jev does not generate text. It reads one shared `state`, evaluates every
declared question against it independently and in parallel, and returns a
probability distribution per question. These types are the whole vocabulary:
three question kinds, three answer kinds.

The answer types carry the two numbers the rest of hyprsay gates on, `p1` and
`margin`, because measurement showed the top probability alone is not enough:
on a near-tie, thirty byte-identical requests moved P(top) between 0.45 and
0.67 and flipped the chosen option four times. A decision is only as good as
its distance from the runner-up.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MAX_CHOICE_OPTIONS = 255
MIN_SCORE_LEVELS = 2
MAX_SCORE_LEVELS = 10

# a JSON value: Jev accepts strings, objects and arrays for instructions and
# for option descriptions, and rich objects ({"app": ..., "kind": ...}) are
# what moved a real window pick from P=0.04 to P=0.96
Json = Any


class QuestionError(ValueError):
    """A question that Jev would reject, caught before a round trip is spent on it."""


@dataclass(frozen=True)
class Choice:
    """Pick exactly one option. Options must be mutually exclusive.

    A Choice is relative: it always picks something. If "none of these" is a
    possible truth, it has to be an option. What must NOT be an option is a
    catch-all that overlaps the real ones: a `focused` entry beside real
    windows absorbed about half the probability mass regardless of the
    utterance, and won outright for "close the terminal" with Firefox focused.
    """

    instructions: Json
    options: dict[str, Json]

    def __post_init__(self) -> None:
        if not self.options:
            raise QuestionError("a choice needs at least one option")
        if len(self.options) > MAX_CHOICE_OPTIONS:
            raise QuestionError(
                f"a choice takes at most {MAX_CHOICE_OPTIONS} options, got {len(self.options)}"
            )
        for key in self.options:
            if not isinstance(key, str) or not key:
                raise QuestionError(f"option keys must be non-empty strings, got {key!r}")


@dataclass(frozen=True)
class Score:
    """Place the state on an ordered scale, lowest level first.

    The returned score is probability weighted, so it can land between levels.
    """

    instructions: Json
    levels: tuple[Json, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "levels", tuple(self.levels))
        if not MIN_SCORE_LEVELS <= len(self.levels) <= MAX_SCORE_LEVELS:
            raise QuestionError(
                f"a score takes {MIN_SCORE_LEVELS} to {MAX_SCORE_LEVELS} levels, "
                f"got {len(self.levels)}"
            )


@dataclass(frozen=True)
class Boolean:
    """The probability that a proposition is true. TypeSafe's own name for this is `noul`."""

    instructions: Json
    true: Json | None = None
    false: Json | None = None


Question = Choice | Score | Boolean


@dataclass(frozen=True)
class ChoiceAnswer:
    choice: str
    probabilities: dict[str, float]
    # the service's own concentration statistic; logged, never gated on
    confidence: float | None = None

    @property
    def ranked(self) -> list[tuple[str, float]]:
        """Options by probability, highest first. Ties keep declaration order."""
        return sorted(self.probabilities.items(), key=lambda kv: -kv[1])

    @property
    def p1(self) -> float:
        return self.ranked[0][1]

    @property
    def margin(self) -> float:
        """Distance from the winner to the runner-up. With one option, p1 itself."""
        ranked = self.ranked
        return ranked[0][1] - ranked[1][1] if len(ranked) > 1 else ranked[0][1]

    def top(self, n: int) -> list[tuple[str, float]]:
        return self.ranked[:n]


@dataclass(frozen=True)
class ScoreAnswer:
    score: float
    probabilities: dict[str, float]
    confidence: float | None = None


@dataclass(frozen=True)
class BooleanAnswer:
    probability: float


Answer = ChoiceAnswer | ScoreAnswer | BooleanAnswer


@dataclass(frozen=True)
class Evaluation:
    """One answered request, plus everything needed to reason about what it cost."""

    answers: dict[str, Answer]
    latency_ms: float
    # how many HTTP attempts ran, counting the hedge and the retry
    attempts: int
    route: str
    input_tokens: int | None = None
    estimated_tokens: int | None = None
    # what the gateway says it charged. Never derive spend from list price:
    # during the launch promotion every call reported cost 0 while the list
    # price said otherwise.
    cost: float | None = None
    market_cost: float | None = None
    model: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def choice(self, qid: str) -> ChoiceAnswer:
        return self._typed(qid, ChoiceAnswer)

    def score(self, qid: str) -> ScoreAnswer:
        return self._typed(qid, ScoreAnswer)

    def boolean(self, qid: str) -> BooleanAnswer:
        return self._typed(qid, BooleanAnswer)

    def _typed(self, qid: str, kind: type) -> Any:
        answer = self.answers[qid]
        if not isinstance(answer, kind):
            raise TypeError(f"{qid!r} is a {type(answer).__name__}, not a {kind.__name__}")
        return answer
