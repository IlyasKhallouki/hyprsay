"""The single request: spans of what was said, the three questions, and reading them back."""

import pytest

from hyprsay.adapters.base import Affordance, Trust
from hyprsay.jev.types import BooleanAnswer, ChoiceAnswer
from hyprsay.model import Action, Decision, Intent, Parse, Slots, Verdict
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


# ------------------------------------------------------------------ the grammar fast path


def _decided(verdict, intent, **action):
    return Decision(verdict, Action(intent, **action))


@pytest.mark.parametrize(
    "intent",
    [Intent.PICK, Intent.CANCEL, Intent.UNDO, Intent.AGAIN, Intent.HELP, Intent.LOCK_SCREEN,
     Intent.TYPE_TEXT],
)  # fmt: skip
def test_the_grammar_only_intents_are_always_fast(intent):
    assert decide.fast(Parse(intent), Decision(Verdict.SUGGEST))


def test_a_refusal_stands_without_asking():
    parse = Parse(Intent.LAUNCH_APP, Slots(app_ref="gimp"))
    assert decide.fast(parse, Decision(Verdict.REFUSE, reason="session is locked"))


@pytest.mark.parametrize(
    "intent",
    [Intent.SWITCH_WORKSPACE, Intent.FOCUS_DIRECTION, Intent.MOVE_WINDOW, Intent.RESIZE_WINDOW,
     Intent.FULLSCREEN, Intent.TOGGLE_FLOATING, Intent.MOVE_TO_WORKSPACE, Intent.VOLUME,
     Intent.MEDIA, Intent.SCROLL],
)  # fmt: skip
def test_a_complete_command_about_the_desktop_is_fast(intent):
    assert decide.fast(Parse(intent), _decided(Verdict.ACT, intent))


def test_going_to_an_open_window_is_fast_even_when_it_was_said_as_open():
    # "open chrome" with chrome running: ground.reach turned the launch into a focus
    parse = Parse(Intent.LAUNCH_APP, Slots(app_ref="chrome"))
    assert decide.fast(parse, _decided(Verdict.ACT, Intent.FOCUS_WINDOW))
    assert decide.fast(parse, _decided(Verdict.ACT_SWAP, Intent.FOCUS_WINDOW))


def test_starting_an_app_is_not_fast_because_a_tab_or_a_site_may_be_what_was_meant():
    parse = Parse(Intent.LAUNCH_APP, Slots(app_ref="spotify"))
    assert not decide.fast(parse, _decided(Verdict.ACT, Intent.LAUNCH_APP))


def test_closing_a_named_window_is_fast_and_closing_this_is_not():
    named = Parse(Intent.CLOSE_WINDOW, Slots(app_ref="spotify"))
    assert decide.fast(named, _decided(Verdict.COUNTDOWN, Intent.CLOSE_WINDOW))
    pointed = Parse(Intent.CLOSE_WINDOW, Slots(deictic=True))
    assert not decide.fast(pointed, _decided(Verdict.COUNTDOWN, Intent.CLOSE_WINDOW))


def test_a_search_the_grammar_swallowed_whole_is_not_fast():
    # "search for a depth punk on spotify" became a web search with every word as text
    parse = Parse(Intent.RUN_RECIPE, Slots(text="a depth punk on spotify"))
    assert not decide.fast(parse, _decided(Verdict.COUNTDOWN, Intent.RUN_RECIPE))


@pytest.mark.parametrize("verdict", [Verdict.HINTS, Verdict.SUGGEST, Verdict.NOTHING])
def test_a_grammar_that_could_not_settle_it_is_not_fast(verdict):
    assert not decide.fast(
        Parse(Intent.FOCUS_WINDOW, Slots(window_ref="youtube")), Decision(verdict)
    )


def test_a_grammar_refusal_of_something_inside_a_window_is_not_fast():
    # "open github. com" was refused by the address parser, which is a parse failure and
    # not a safety rule, so Jev gets to see the utterance
    parse = Parse(Intent.RUN_RECIPE, Slots(text="github com"))
    refused = Decision(Verdict.REFUSE, reason="that sounded like a phrase rather than an address")
    assert not decide.fast(parse, refused)


# --------------------------------------------------------------------------- choosing


def _recipe(ident: str, name: str) -> Affordance:
    return Affordance(
        id=ident, label=name, kind="recipe", tier=2, handle=f"0x2 {name}", adapter="hyprsay",
        capability="run_recipe", trust=Trust.TRUSTED,
    )  # fmt: skip


def _reading(probabilities: dict[str, float]) -> decide.Reading:
    return decide.Reading(addressed=0.95, probabilities=probabilities)


def test_two_ways_of_going_to_an_address_are_one_vote():
    options = {"r1": _recipe("r1", "open_url"), "r2": _recipe("r2", "go_to_url"),
               "r3": _recipe("r3", "search_web")}  # fmt: skip
    step = decide.choose(_reading({"r1": 0.56, "r2": 0.37, "r3": 0.05, "none": 0.02}), options)
    assert step is not None and step.id == "r1"
    assert step.probability == pytest.approx(0.93)
    assert step.margin == pytest.approx(0.88)


def test_a_search_and_a_find_are_not_the_same_vote():
    options = {"r1": _recipe("r1", "search_web"), "r2": _recipe("r2", "find")}
    step = decide.choose(_reading({"r1": 0.5, "r2": 0.45, "none": 0.05}), options)
    assert step is not None and step.margin == pytest.approx(0.05)


def test_an_option_that_cannot_run_cannot_win():
    options = {"x1": _aff("x1"), "x2": _aff("x2")}
    step = decide.choose(
        _reading({"x1": 0.7, "x2": 0.25, "none": 0.05}), options, viable=lambda a: a.id != "x1"
    )
    assert step is not None and step.id == "x2"
    assert step.probability == pytest.approx(0.25)


def test_none_can_win_the_choice():
    step = decide.choose(_reading({"x1": 0.2, "none": 0.8}), {"x1": _aff("x1")})
    assert step is None


def _capability(ident: str, capability: str) -> Affordance:
    return Affordance(
        id=ident, label=capability, kind="page search", tier=1, handle=ident, adapter="browser",
        capability=capability, trust=Trust.UNTRUSTED,
    )  # fmt: skip


def test_searching_a_site_and_finding_on_it_are_one_vote_that_the_runnable_one_wins():
    # "search for linus tech tips" on YouTube: 0.58 find, 0.31 search, measured live
    options = {"b1": _capability("b1", "browser.search"), "b2": _capability("b2", "browser.find")}
    reading = _reading({"b2": 0.58, "b1": 0.31, "none": 0.11})
    step = decide.choose(reading, options, viable=lambda a: a.capability != "browser.find")
    assert step is not None and step.id == "b1"
    assert step.probability == pytest.approx(0.89)


def test_a_vote_none_of_whose_members_can_run_goes_nowhere():
    options = {"b1": _capability("b1", "browser.search"), "b2": _capability("b2", "browser.find")}
    step = decide.choose(_reading({"b2": 0.58, "b1": 0.31, "none": 0.11}), options, lambda a: False)
    assert step is None
