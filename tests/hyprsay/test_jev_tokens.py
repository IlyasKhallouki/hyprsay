"""The token estimator, held to the requests it was fitted on."""

import pytest

from hyprsay.jev import tokens

# (state chars, question chars, questions, input tokens the API reported),
# compact JSON, from docs/research/live/dayone.py on 2026-09-21
MEASURED = [
    (377, 1043, 5, 725),
    (4042, 1043, 5, 2246),
    (12999, 1043, 5, 6037),
    (25902, 1043, 5, 11542),
    (570, 104, 1, 517),
    (565, 516, 5, 605),
    (564, 1556, 15, 837),
    (567, 4181, 40, 1438),
]


def _estimate(state_chars: int, question_chars: int, n_questions: int) -> float:
    return (
        tokens.FIXED_OVERHEAD
        + tokens.TOKENS_PER_STATE_CHAR * state_chars
        + tokens.TOKENS_PER_QUESTION_CHAR * question_chars
        + tokens.TOKENS_PER_QUESTION * n_questions
    )


@pytest.mark.parametrize(("state_chars", "question_chars", "n_questions", "reported"), MEASURED)
def test_the_fit_stays_within_four_percent_of_every_measured_request(
    state_chars, question_chars, n_questions, reported
):
    assert _estimate(state_chars, question_chars, n_questions) == pytest.approx(reported, rel=0.04)


def test_the_fit_never_runs_far_under_which_is_the_unsafe_direction():
    # a low estimate lets an oversized request through the cap
    worst = min(_estimate(s, q, n) / reported for s, q, n, reported in MEASURED)
    assert worst > 0.95


def test_many_questions_are_not_mistaken_for_a_large_request():
    """Forty questions reported 1438 tokens. An estimator without the per-question
    term said 1915 and the default cap refused it, which would have made the
    cheapest thing Jev offers, more questions, unusable."""
    forty = _estimate(567, 4181, 40)
    assert forty < tokens.DEFAULT_CAP
    assert forty == pytest.approx(1438, rel=0.04)


def test_estimate_measures_the_compact_wire_form():
    state = {"heard": "close this"}
    questions = {"q": {"type": "noul", "instructions": "Is it a command?"}}
    chars_state = len('{"heard":"close this"}')
    chars_questions = len('{"q":{"type":"noul","instructions":"Is it a command?"}}')
    assert tokens.estimate(state, questions) == round(_estimate(chars_state, chars_questions, 1))


def test_drift_flags_an_estimator_that_runs_low():
    drift = tokens.Drift()
    for _ in range(5):
        drift.record(estimated=1000, reported=1300)
    assert drift.ratio == pytest.approx(1.3)
    assert drift.unreliable
    assert drift.worst_under == pytest.approx(0.3)


def test_drift_stays_quiet_when_the_fit_holds_and_ignores_missing_usage():
    drift = tokens.Drift()
    drift.record(estimated=1000, reported=None)
    for _ in range(10):
        drift.record(estimated=1000, reported=1020)
    assert drift.samples == 10
    assert not drift.unreliable
