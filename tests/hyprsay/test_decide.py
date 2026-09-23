"""The single request: spans of what was said, the three questions, and reading them back."""

import pytest

from hyprsay.adapters.base import Affordance, Trust
from hyprsay.jev.types import BooleanAnswer, ChoiceAnswer
from hyprsay.nlu import decide


def _aff(ident: str, label: str = "") -> Affordance:
    return Affordance(
        id=ident,
        label=label or f"do {ident}",
        kind="system",
        tier=1,
        handle="h",
        adapter="hyprsay",
        capability="media",
        trust=Trust.TRUSTED,
    )


# --------------------------------------------------------------------------- spans


def test_spans_cover_every_run_of_words():
    got = decide.spans("play hold the line")
    assert got["w1_4"] == "hold the line"
    assert got["w0_1"] == "play"
    assert len(got) == 10  # 4 + 3 + 2 + 1


def test_spans_stop_at_six_words():
    got = decide.spans("a b c d e f g h")
    assert max(len(v.split()) for v in got.values()) == decide.MAX_SPAN_WORDS
    assert "w0_7" not in got


def test_spans_of_nothing_is_nothing():
    assert decide.spans("   ") == {}


def test_spans_keep_the_order_they_were_said_in():
    got = decide.spans("go to github com")
    assert list(got)[:4] == ["w0_1", "w0_2", "w0_3", "w0_4"]


# --------------------------------------------------------------------------- reading


def test_read_returns_the_chosen_option_and_words():
    options = {"x1": _aff("x1"), "x2": _aff("x2")}
    words = decide.spans("play hold the line")
    answers = {
        "addressed": BooleanAnswer(0.96),
        "action": ChoiceAnswer("x2", {"x1": 0.1, "x2": 0.85, "none": 0.05}),
        "words": ChoiceAnswer("w1_4", {"w1_4": 0.99, "none": 0.01}),
    }
    reading = decide.read(answers, options, words)
    assert reading.step is not None
    assert reading.step.id == "x2"
    assert reading.step.affordance is options["x2"]
    assert reading.step.probability == pytest.approx(0.85)
    assert reading.step.margin == pytest.approx(0.75)
    assert reading.runner_up == "x1"
    assert reading.words == "hold the line"
    assert reading.words_p == pytest.approx(0.99)
    assert reading.addressed == pytest.approx(0.96)


def test_read_treats_an_id_that_was_never_offered_as_unanswered():
    reading = decide.read({"action": ChoiceAnswer("zz", {"zz": 1.0})}, {"x1": _aff("x1")}, {})
    assert reading.step is None


def test_read_treats_none_as_no_step():
    answer = ChoiceAnswer("none", {"none": 0.9, "x1": 0.1})
    reading = decide.read({"action": answer}, {"x1": _aff("x1")}, {})
    assert reading.step is None
    assert reading.runner_up == "x1"


def test_read_of_a_failed_request_is_empty_and_unaddressed():
    reading = decide.read({}, {"x1": _aff("x1")}, decide.spans("pause"))
    assert reading.step is None
    assert reading.words == ""
    assert reading.addressed == 0.0


def test_read_ignores_a_words_answer_that_names_no_span():
    answers = {"words": ChoiceAnswer("w9_9", {"w9_9": 1.0})}
    reading = decide.read(answers, {}, decide.spans("pause"))
    assert reading.words == ""


# --------------------------------------------------------------------------- the questions


def test_questions_are_addressed_action_and_words_plus_the_extras():
    from hyprsay.jev.types import Boolean, Choice

    seam = Boolean("does 'and' separate two commands?", true="yes", false="no")
    built = decide.questions(
        {"x1": _aff("x1")}, decide.spans("pause it"), extra={"separates_0": seam}
    )
    assert set(built) == {"addressed", "action", "words", "separates_0"}
    assert isinstance(built["addressed"], Boolean)
    assert isinstance(built["action"], Choice)
    assert isinstance(built["words"], Choice)


def test_the_action_choice_offers_every_option_and_none():
    built = decide.questions({"x1": _aff("x1"), "w2": _aff("w2")}, {})
    assert set(built["action"].options) == {"x1", "w2", "none"}
    # the option carries what `Affordance.as_option` says and never a handle
    assert built["action"].options["x1"] == _aff("x1").as_option()


def test_the_words_choice_offers_every_span_and_none():
    words = decide.spans("play hold the line")
    built = decide.questions({"x1": _aff("x1")}, words)
    assert set(built["words"].options) == {*words, "none"}
    assert built["words"].options["w1_4"] == "hold the line"


def test_no_words_means_no_words_question():
    built = decide.questions({"x1": _aff("x1")}, {})
    assert "words" not in built


def test_a_harvest_sized_request_fits_the_token_cap():
    """30 options at the measured label length (affordances.py: 20.3 characters on
    average) and a seven word utterance. Longer lists are shrunk by the caller."""
    from hyprsay.config import Config
    from hyprsay.nlu import requests

    options = {f"x{i}": _aff(f"x{i}", f"go to the kitty window {i}") for i in range(30)}
    words = decide.spans("search spotify for hold the line please")
    state = requests.utterance_state("search spotify for hold the line please")
    assert requests.estimate(state, decide.questions(options, words)) <= Config().jev.token_cap
