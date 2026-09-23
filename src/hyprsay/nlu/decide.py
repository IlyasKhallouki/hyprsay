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

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from hyprsay.adapters.base import Affordance
from hyprsay.jev.types import BooleanAnswer, ChoiceAnswer, Question
from hyprsay.model import Decision, Intent, Parse, Verdict
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
    # the whole action answer, so `choose` can set aside what cannot run
    probabilities: dict[str, float] = field(default_factory=dict)


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
    probabilities = dict(action.probabilities) if isinstance(action, ChoiceAnswer) else {}
    return Reading(
        addressed=heard,
        step=step,
        words=said,
        words_p=said_p,
        runner_up=runner_up,
        probabilities=probabilities,
    )


# Options that do the same thing to the same place, so they must not split the vote.
# Going to an address in a new tab and in the current one is the case the replay found:
# 0.56 and 0.37 between them, 0.93 together, and a margin rule that read it as unsure.
SAME_THING: dict[str, str] = {"open_url": "go to an address", "go_to_url": "go to an address"}


def family(affordance: Affordance) -> str:
    """Which vote an option counts toward: its own id, unless it is a synonym of another."""
    if affordance.capability == Intent.RUN_RECIPE.value:
        recipe = affordance.handle.rsplit(" ", 1)[-1]
        if recipe in SAME_THING:
            return f"{affordance.handle.rsplit(' ', 1)[0]} {SAME_THING[recipe]}"
    return affordance.id


def choose(
    reading: Reading,
    options: Mapping[str, Affordance],
    viable: Callable[[Affordance], bool] = lambda _: True,
) -> plan.Step | None:
    """The option to act on, with its probability and margin counted by vote.

    Two rules, both about what the answer means rather than what it says:

    - an option that cannot run cannot win. A "found by name" option with no name said,
      or an address that is not one, is set aside, and its probability goes nowhere: it
      is not handed to the runner-up, so nothing gains evidence it did not get.
    - synonyms are one vote (`SAME_THING`). The winner inside the vote is the option the
      model liked best.

    `none` is a vote of its own. None comes back when it wins, or when nothing is left.
    """
    votes: dict[str, float] = {}
    best: dict[str, tuple[float, str]] = {}
    for key, p in reading.probabilities.items():
        if key == bank.NONE:
            votes[bank.NONE] = votes.get(bank.NONE, 0.0) + p
            continue
        found = options.get(key)
        if found is None or not viable(found):
            continue
        vote = family(found)
        votes[vote] = votes.get(vote, 0.0) + p
        if p > best.get(vote, (-1.0, ""))[0]:
            best[vote] = (p, key)
    if not votes:
        return None
    ranked = sorted(votes.items(), key=lambda kv: -kv[1])
    top, total = ranked[0]
    if top == bank.NONE or top not in best:
        return None
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    key = best[top][1]
    return plan.Step(
        position=1, id=key, affordance=options[key], probability=total, margin=total - second
    )


# ------------------------------------------------------------------ the grammar's fast path

# Grammar only by design: a number while badges show, the session verbs, and typing,
# whose words must never reach a model (docs/PLAN.md 5.6).
GRAMMAR_ONLY = frozenset(
    {
        Intent.PICK,
        Intent.CANCEL,
        Intent.UNDO,
        Intent.AGAIN,
        Intent.HELP,
        Intent.LOCK_SCREEN,
        Intent.TYPE_TEXT,
    }
)
# commands about the desktop whose every slot the grammar filled from the speaker's own
# words, where the only reading is the one the grammar found
COMPLETE = frozenset(
    {
        Intent.SWITCH_WORKSPACE,
        Intent.FOCUS_DIRECTION,
        Intent.MOVE_WINDOW,
        Intent.RESIZE_WINDOW,
        Intent.FULLSCREEN,
        Intent.TOGGLE_FLOATING,
        Intent.MOVE_TO_WORKSPACE,
        Intent.VOLUME,
        Intent.MEDIA,
        Intent.SCROLL,
    }
)
_ACTS = frozenset({Verdict.ACT, Verdict.ACT_SWAP})
IN_APP = frozenset({Intent.RUN_RECIPE, Intent.CLICK_CONTROL, Intent.PRESS_CHORD})


def fast(parse: Parse, decision: Decision) -> bool:
    """May the grammar's decision stand without asking Jev?

    The old path let ANY grammar decision stand, including "pick a number" and "did you
    mean", and 9 of the 25 failures in the first voice session never reached Jev at all:
    "search for a depth punk on spotify" parsed as a web search with every word after
    "for" as its text. So the grammar keeps only what it can be sure of: its own intents,
    refusals, complete commands about the desktop, going to a window that is open, and
    closing a window the speaker named. Starting an application is not on the list,
    because "open spotify" can mean a browser tab or a website as well, and only the
    harvest knows which of those exist.
    """
    if parse.intent in GRAMMAR_ONLY:
        return True
    if decision.verdict is Verdict.REFUSE:
        # a refusal stands, except the grammar's own reading of something inside a
        # window: "that sounded like a phrase rather than an address" is the address
        # parser failing on "github. com", not a rule saying no. The harvest applies the
        # real rules (`recipes.refusal_for`) to whatever it offers instead.
        return parse.intent not in IN_APP
    action = decision.action
    if action is None:
        return False
    if decision.verdict in _ACTS:
        return action.intent is Intent.FOCUS_WINDOW or (
            action.intent is parse.intent and parse.intent in COMPLETE
        )
    if decision.verdict is Verdict.COUNTDOWN and action.intent is Intent.CLOSE_WINDOW:
        return bool(parse.slots.window_ref or parse.slots.app_ref)
    return False


__all__ = [
    "ACTION",
    "ADDRESSED",
    "COMPLETE",
    "GRAMMAR_ONLY",
    "MAX_SPAN_WORDS",
    "SPAN_PREFIX",
    "WORDS",
    "SAME_THING",
    "Reading",
    "choose",
    "family",
    "fast",
    "questions",
    "read",
    "spans",
]
