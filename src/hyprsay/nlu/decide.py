"""One Jev request per command: what it asks, and how its answer is read.

WHY ONE REQUEST. The first real voice session got about 14 of 44 commands right, and a
replay of the failures against live Jev showed the model was rarely the reason
(docs/superpowers/specs/2026-09-23-browser-voice-design.md). Asked ONE clean question
with the right answer among the options, Jev picked correctly 30 times in 33 at 351 ms
p50. The old path asked seven overlapping requests (`r1` .. `r3.2`), so one slow reply
sank the command and an arbiter had to pick between answers that disagreed. It also
asked "which is the first thing, which is the second", which Jev answers one question at
a time with no idea what the other question chose: in the replay that framing cost 27
points and turned "play hold the line" into a 0.27 against 0.27 tie.

THE THREE QUESTIONS, all in one request, because a question is free and payload is not:

- `action`, a Choice over the harvested options plus `none`;
- `words`, a Choice over every run of up to six words the speaker said, plus `none`. Jev
  cannot write text, but it can point at the words that were spoken, so the song name,
  the search, the address and the number come from here. The replay found "hold the
  line", "ltt", "github com" and "75" in the same request as the action, at 378 ms p50;
- `addressed`, a Boolean: was the speaker talking to the computer at all.

Nothing here touches the desktop or the network. `understand.py` builds the request and
acts on the reading; this module is the part that can be tested without either.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from hyprsay.adapters.base import Affordance
from hyprsay.jev.types import BooleanAnswer, ChoiceAnswer, Question
from hyprsay.nlu import bank, plan

MAX_SPAN_WORDS = 6
# span option ids: "w0_2" is the first two words. Only ever keys of the `words` Choice,
# so they cannot collide with an option id, which lives in a different question.
SPAN_PREFIX = "w"

ACTION = "action"
WORDS = "words"
ADDRESSED = "addressed"


def spans(text: str, max_words: int = MAX_SPAN_WORDS) -> dict[str, str]:
    """Every run of one to `max_words` consecutive words, in the order they were said.

    `text` is the normalized utterance, so punctuation is already gone and numbers are
    already digits: "seventy five" arrives as "75", which is what a volume needs.
    """
    words = text.split()
    out: dict[str, str] = {}
    for start in range(len(words)):
        for end in range(start + 1, min(len(words), start + max_words) + 1):
            out[f"{SPAN_PREFIX}{start}_{end}"] = " ".join(words[start:end])
    return out


def questions(
    options: Mapping[str, Affordance],
    words: Mapping[str, str],
    extra: Mapping[str, Question] | None = None,
) -> dict[str, Question]:
    """The request's questions: `addressed`, `action`, `words`, and whatever rides along.

    `extra` is how the seam Booleans (`understand._seam_questions`) travel in the same
    request instead of a second one. No spans means no `words` question: a Choice whose
    only option is `none` asks nothing and still costs its instructions on the wire.
    """
    built: dict[str, Question] = {
        ADDRESSED: bank.addressed(),
        ACTION: bank.one_action({key: found.as_option() for key, found in options.items()}),
    }
    if words:
        built[WORDS] = bank.which_words(dict(words))
    built.update(extra or {})
    return built


@dataclass(frozen=True)
class Reading:
    """What one answered request says, and nothing that was not in it."""

    addressed: float = 0.0
    step: plan.Step | None = None
    words: str = ""
    words_p: float = 0.0
    # the option the model liked second, for the journal and for "did you mean"
    runner_up: str = ""


def read(
    answers: Mapping[str, Any],
    options: Mapping[str, Affordance],
    words: Mapping[str, str],
) -> Reading:
    """Turn the three answers into a Reading.

    A missing or mistyped answer reads as no answer, never as a guess: a failed request
    and a half answered one come back the same way. An option id that was never offered
    is a broken answer, not a choice.
    """
    addressed = answers.get(ADDRESSED)
    heard = addressed.probability if isinstance(addressed, BooleanAnswer) else 0.0

    step: plan.Step | None = None
    runner_up = ""
    action = answers.get(ACTION)
    if isinstance(action, ChoiceAnswer):
        ranked = action.ranked
        runner_up = next((key for key, _ in ranked if key != action.choice), "")
        chosen = options.get(action.choice) if action.choice != bank.NONE else None
        if chosen is not None:
            step = plan.Step(
                position=1,
                id=action.choice,
                affordance=chosen,
                probability=action.p1,
                margin=action.margin,
            )

    said, said_p = "", 0.0
    span = answers.get(WORDS)
    if isinstance(span, ChoiceAnswer) and span.choice in words:
        said, said_p = words[span.choice], span.p1
    return Reading(addressed=heard, step=step, words=said, words_p=said_p, runner_up=runner_up)


__all__ = [
    "ACTION",
    "ADDRESSED",
    "MAX_SPAN_WORDS",
    "SPAN_PREFIX",
    "WORDS",
    "Reading",
    "questions",
    "read",
    "spans",
]
