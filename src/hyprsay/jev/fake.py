"""A scripted Jev for tests. It never opens a socket.

The real service answers every declared question independently, so the fake is
scripted the same way: per question id, not per request. A test says what Jev
would believe ("window" leans to w2, "addressed" is 0.97) and the fake shapes that
into the real answer types from `types.py`, for whichever concurrent request
happens to carry the question. Anything left unscripted gets the answer a bored
model would give: a Boolean near zero, a Score at its lowest level, a Choice on its
"none" option when it has one. That keeps tests about one rule from having to
script the whole fan-out.

Failures are scripted too, because the policy that matters most is what happens
when Jev is slow, down, or rejects the key. Every call is recorded before it can
fail, so "zero calls" and "this string never left the machine" are assertable.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from .client import JevAuthError, JevBusy, JevError, JevTimeout, JevUnavailable
from .types import (
    Answer,
    Boolean,
    BooleanAnswer,
    Choice,
    ChoiceAnswer,
    Evaluation,
    Question,
    Score,
    ScoreAnswer,
)

# option keys that mean "nothing here"; unscripted mass lands on the first one present
NONE_OPTIONS = ("none", "none_mentioned")

Script = Any  # float | str | dict[str, float] | Callable[[Question, Any], Script]


class FakeEvaluator:
    """Implements `hyprsay.model.Evaluator`.

    Script values, per question id:
    - Boolean: a float probability
    - Score: a float score
    - Choice: an option key (it gets `sure`, the rest share what is left), or a dict
      of option key to probability. Keys the question does not offer are dropped and
      the missing mass goes to the "none" option when there is one, which is how one
      script serves every shard of a sharded question.
    - any of them: a callable `(question, state) -> value`
    """

    def __init__(
        self,
        answers: dict[str, Script] | None = None,
        *,
        fail: JevError | None = None,
        fail_on: dict[str, JevError] | None = None,
        delay: float = 0.0,
        sure: float = 0.9,
    ) -> None:
        self.answers: dict[str, Script] = dict(answers or {})
        self.fail = fail
        self.fail_on: dict[str, JevError] = dict(fail_on or {})
        self.delay = delay
        self.sure = sure
        self.calls: list[tuple[Any, dict[str, Question]]] = []

    # ------------------------------------------------------------------ failures

    @classmethod
    def timing_out(cls, **kwargs: Any) -> FakeEvaluator:
        return cls(fail=JevTimeout("no answer within 0.90 s"), **kwargs)

    @classmethod
    def unavailable(cls, **kwargs: Any) -> FakeEvaluator:
        return cls(fail=JevUnavailable("HTTP 503: upstream unavailable"), **kwargs)

    @classmethod
    def unauthorized(cls, **kwargs: Any) -> FakeEvaluator:
        return cls(fail=JevAuthError("invalid gateway key"), **kwargs)

    @classmethod
    def busy(cls, **kwargs: Any) -> FakeEvaluator:
        return cls(fail=JevBusy("rate limited", retry_after=1.0), **kwargs)

    # ------------------------------------------------------------------ scripting

    def script(self, qid: str, value: Script) -> FakeEvaluator:
        self.answers[qid] = value
        return self

    # ------------------------------------------------------------------ inspection

    @property
    def asked(self) -> set[str]:
        """Every question id that was ever sent."""
        return {qid for _, questions in self.calls for qid in questions}

    def request_with(self, qid: str) -> tuple[Any, dict[str, Question]]:
        """The first recorded request that carried this question."""
        for state, questions in self.calls:
            if qid in questions:
                return state, questions
        raise KeyError(qid)

    def sent_text(self) -> str:
        """Everything that was sent, as one string, for "this never left" assertions."""
        return json.dumps(
            [[state, {q: _plain(v) for q, v in qs.items()}] for state, qs in self.calls],
            ensure_ascii=False,
            default=str,
        )

    # ------------------------------------------------------------------ the seam

    async def evaluate(self, state: Any, questions: dict[str, Question]) -> Evaluation:
        self.calls.append((state, dict(questions)))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail is not None:
            raise self.fail
        for qid, exc in self.fail_on.items():
            if qid in questions:
                raise exc
        answers = {qid: self._answer(qid, q, state) for qid, q in questions.items()}
        return Evaluation(answers=answers, latency_ms=self.delay * 1000, attempts=1, route="fake")

    def _answer(self, qid: str, question: Question, state: Any) -> Answer:
        value = self.answers.get(qid)
        while callable(value):
            value = _call(value, question, state)
        if isinstance(question, Boolean):
            return BooleanAnswer(float(value) if value is not None else 0.02)
        if isinstance(question, Score):
            score = float(value) if value is not None else 0.0
            nearest = str(min(range(len(question.levels)), key=lambda i: abs(i - score)))
            return ScoreAnswer(score, {nearest: 1.0}, confidence=0.5)
        return self._choice(question, value)

    def _choice(self, question: Choice, value: Script) -> ChoiceAnswer:
        options = list(question.options)
        none = next((o for o in NONE_OPTIONS if o in question.options), None)
        if value is None:
            value = {none: 1.0} if none else {o: 1.0 / len(options) for o in options}
        if isinstance(value, str):
            if value not in question.options:
                value = {}
            else:
                rest = [o for o in options if o != value]
                share = (1.0 - self.sure) / len(rest) if rest else 0.0
                value = {value: self.sure if rest else 1.0, **{o: share for o in rest}}
        probabilities = {o: float(value.get(o, 0.0)) for o in options}
        missing = 1.0 - sum(probabilities.values())
        if missing > 1e-9:
            if none is not None:
                probabilities[none] += missing
            else:
                for option in options:
                    probabilities[option] += missing / len(options)
        winner = max(options, key=lambda o: probabilities[o])
        # a deliberately misleading service confidence: nothing may gate on it
        return ChoiceAnswer(winner, probabilities, confidence=0.99)


def _call(fn: Callable[..., Script], question: Question, state: Any) -> Script:
    return fn(question, state)


def _plain(question: Question) -> dict[str, Any]:
    if isinstance(question, Choice):
        return {"instructions": question.instructions, "options": question.options}
    if isinstance(question, Score):
        return {"instructions": question.instructions, "levels": list(question.levels)}
    return {"instructions": question.instructions, "true": question.true, "false": question.false}
