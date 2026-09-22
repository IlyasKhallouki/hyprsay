"""The Phase 0 measurement harness, against synthetic frames and a fake recognizer.

No test here opens a microphone, reaches the network or loads a speech model. The frame
data is written by hand so the arithmetic that turns a hold into a lag can be checked
against a number a reader can count, and the one test that uses a real `Recorder` drives
it through a fake PortAudio stream, the way tests/hyprsay/test_audio.py does.
"""

import asyncio
import json
import sys
from pathlib import Path

import numpy as np
import pytest

from hyprsay import audio
from hyprsay.audio import FRAME, RATE, Recorder
from hyprsay.config import STT, Config
from hyprsay.model import Action, App, Decision, Intent, Transcript, Verdict, Window

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "evals"))

import cases  # noqa: E402
import measure  # noqa: E402

# 20 ms a frame, so 50 frames is a second and every count below is readable as time
VOICED, QUIET = True, False


def frames(pattern: str) -> tuple[bool, ...]:
    """ "..###..." as frames: # is voiced, . is not. One character is 20 ms."""
    return tuple(c == "#" for c in pattern)


# --------------------------------------------------------------------- the lag arithmetic


def test_the_lag_is_the_silence_after_the_last_word_plus_the_capture_flush():
    # 5 voiced frames then 10 quiet ones: 300 ms of audio, the last word ends at 100 ms
    hold = measure.measure_hold(1, "open firefox", frames("#####.........."), 300.0, 340.0)
    assert hold.trailing_ms == 200.0
    assert hold.flush_ms == 40.0
    assert hold.lag_ms == 240.0


def test_a_hold_released_the_instant_the_word_ended_has_no_lag():
    hold = measure.measure_hold(1, "mute", frames("..#####"), 140.0, 140.0)
    assert hold.trailing_ms == 0.0
    assert hold.lag_ms == 0.0


def test_a_hold_with_nothing_in_it_has_no_lag_to_report():
    hold = measure.measure_hold(1, "mute", frames("........"), 160.0, 200.0)
    assert hold.lag_ms is None
    assert hold.voiced_frames == 0


def test_the_lag_never_goes_negative_when_the_buffer_outran_the_clock():
    # more audio than wall time is impossible and still must not print a negative lag
    hold = measure.measure_hold(1, "mute", frames("#####"), 100.0, 60.0)
    assert hold.lag_ms == 0.0


def test_the_device_starting_up_is_not_the_speaker_still_holding_the_key():
    """The one measurement Phase 0 exists to produce carried a PortAudio stream start-up
    inside it, because the hold clock began when `start()` returned rather than when the
    first block arrived, and with pre-roll off that happens once per hold. An inflated
    lag argues FOR building speculation, so the error pointed the expensive way."""
    raw = measure.measure_hold(1, "open firefox", frames("#####.........."), 300.0, 400.0)
    fixed = measure.measure_hold(
        1, "open firefox", frames("#####.........."), 300.0, 400.0, startup_ms=60.0
    )
    assert raw.lag_ms == 300.0
    assert fixed.lag_ms == 240.0
    assert fixed.flush_ms == 40.0  # the genuine undelivered tail, and nothing else


def test_both_the_corrected_and_the_uncorrected_number_are_kept_on_every_hold():
    """The correction has to be auditable from the file, not believed from the source."""
    hold = measure.measure_hold(
        1, "open firefox", frames("#####.........."), 300.0, 400.0, startup_ms=60.0
    )
    assert (hold.lag_ms, hold.startup_ms, hold.lag_raw_ms) == (240.0, 60.0, 300.0)


def test_a_hold_nothing_was_removed_from_reads_exactly_as_it_always_did():
    """Every offline caller, and every recomputation from a file written before this,
    passes no start-up at all and must get the number it got then."""
    hold = measure.measure_hold(1, "open firefox", frames("#####.........."), 300.0, 340.0)
    assert hold.startup_ms == 0.0
    assert hold.lag_raw_ms == hold.lag_ms == 240.0


def test_a_start_up_longer_than_the_hold_still_cannot_produce_a_negative_lag():
    hold = measure.measure_hold(1, "mute", frames("#####"), 100.0, 120.0, startup_ms=500.0)
    assert hold.lag_ms == 0.0


def test_the_summary_says_how_much_was_removed_and_what_it_would_have_read():
    said = [
        measure.measure_hold(i, "p", frames("#####....."), 200.0, 260.0, startup_ms=40.0)
        for i in range(1, 4)
    ]
    summary = measure.summarize_holds(said)
    assert summary["median_ms"] == 120.0
    assert summary["median_startup_ms"] == 40.0
    assert summary["median_raw_ms"] == 160.0


def test_the_start_up_is_measured_from_the_first_block_minus_the_frame_it_covers():
    """The first block carries the first 20 ms of capture and arrives at the end of it,
    so capture began one frame before PortAudio handed anything over."""
    assert measure.startup_deficit(10.0, 10.1) == pytest.approx(100.0 - measure.FRAME_MS)
    # a hold where nothing was ever delivered is corrected by nothing at all
    assert measure.startup_deficit(10.0, None) == 0.0
    # and a clock that disagrees with itself may not lengthen the lag
    assert measure.startup_deficit(10.0, 9.9) == 0.0


class Deaf:
    """A recorder whose device delivers `after` polls, or never when that is None."""

    def __init__(self, after=None):
        self.after = after
        self.polls = 0

    def seconds(self):
        self.polls += 1
        return 0.02 if self.after is not None and self.polls > self.after else 0.0


def test_waiting_for_the_first_block_answers_the_moment_there_is_audio():
    """The clock is read once for the deadline, once per poll that found nothing, and
    once more when the audio is there. That last reading is the answer."""
    recorder = Deaf(after=2)
    ticks = iter([0.0, 0.001, 0.002, 0.003])
    assert measure.first_block(recorder, now=lambda: next(ticks)) == 0.003
    assert recorder.polls == 3


def test_a_device_that_never_delivers_times_out_instead_of_hanging_the_harness():
    """The correction is worth having and it is not worth a wedged prompt: a hold with
    nothing in it has no lag to report anyway."""
    late = iter([0.0, measure.FIRST_BLOCK_TIMEOUT_S + 1])
    assert measure.first_block(Deaf(), now=lambda: next(late)) is None


def test_the_last_voiced_frame_is_found_through_the_silence_inside_a_sentence():
    assert measure.last_voiced_frame(frames("###.....###...")) == 10


def test_a_hold_that_is_pure_silence_has_no_last_voiced_frame():
    assert measure.last_voiced_frame(frames("......")) is None


# ------------------------------------------------------------------- the key-click discount


def test_a_key_click_on_the_final_frames_is_not_the_last_word():
    # speech, 200 ms of real silence, then two frames of Enter. Without the discount the
    # lag reads 0 ms and argues Phase 3 away on an artifact.
    voiced = frames("#####..........##")
    assert measure.last_voiced_frame(voiced, click_frames=0) == 16
    assert measure.last_voiced_frame(voiced) == 4


def test_a_short_last_word_close_behind_the_speech_is_not_discounted_as_a_click():
    # 40 ms of silence, not 100: this is the end of the utterance, not a keystroke
    voiced = frames("#####..##")
    assert measure.last_voiced_frame(voiced) == 8


def test_a_click_with_no_speech_before_it_is_still_all_there_is():
    assert measure.last_voiced_frame(frames("........##")) == 9


def test_the_strict_lag_is_kept_beside_the_discounted_one():
    hold = measure.measure_hold(1, "mute", frames("#####..........##"), 340.0, 340.0)
    assert hold.clicked is True
    assert hold.strict_ms == 0.0
    assert hold.trailing_ms == 240.0


def test_a_hold_with_no_click_is_not_marked_as_having_one():
    assert measure.measure_hold(1, "mute", frames("#####....."), 200.0, 200.0).clicked is False


# ----------------------------------------------------------------- the summary and verdict


def holds(*lags: float) -> list[measure.Hold]:
    return [
        measure.measure_hold(
            i, "p", frames("#" * 5 + "." * int(lag / 20)), 100.0 + lag, 100.0 + lag
        )
        for i, lag in enumerate(lags, 1)
    ]


def test_the_summary_reports_the_median_the_tails_and_how_many_holds_counted():
    summary = measure.summarize_holds(holds(100, 200, 300, 400, 500))
    assert summary["n"] == 5
    assert summary["median_ms"] == 300.0
    assert summary["p10_ms"] == 100.0
    assert summary["p90_ms"] == 500.0


def test_holds_with_no_speech_are_counted_apart_and_not_averaged_in():
    silent = measure.measure_hold(9, "p", frames("......"), 120.0, 120.0)
    summary = measure.summarize_holds([*holds(200, 200, 200), silent])
    assert summary["n"] == 3
    assert summary["silent"] == 1
    assert summary["median_ms"] == 200.0


def test_a_summary_of_nothing_says_nothing_rather_than_dividing_by_zero():
    assert measure.summarize_holds([])["n"] == 0
    assert measure.lag_verdict(measure.summarize_holds([]))[0] == "no-data"


@pytest.mark.parametrize(
    ("q", "expected"), [(0.0, 10.0), (0.1, 10.0), (0.5, 30.0), (0.9, 50.0), (1.0, 50.0)]
)
def test_percentiles_are_nearest_rank_the_way_the_eval_harness_does_them(q, expected):
    assert measure.percentile([30.0, 10.0, 50.0, 20.0, 40.0], q) == expected


def test_a_percentile_of_nothing_is_an_error_rather_than_a_number():
    with pytest.raises(ValueError, match="no values"):
        measure.percentile([], 0.5)


def test_a_median_above_three_hundred_milliseconds_says_build_phase_three_in_full():
    key, sentence = measure.lag_verdict(measure.summarize_holds(holds(*[400] * 10)))
    assert key == "full"
    assert "in full" in sentence


def test_a_median_under_a_hundred_and_fifty_milliseconds_says_cut_to_decode_only():
    key, sentence = measure.lag_verdict(measure.summarize_holds(holds(*[100] * 10)))
    assert key == "decode-only"
    assert "decode-only" in sentence


def test_a_median_between_the_two_thresholds_refuses_to_decide():
    key, sentence = measure.lag_verdict(measure.summarize_holds(holds(*[220] * 10)))
    assert key == "inconclusive"
    assert "S1" in sentence


def test_a_thin_sample_says_so_in_the_verdict_it_still_gives():
    _, thin = measure.lag_verdict(measure.summarize_holds(holds(400, 400, 400)))
    _, thick = measure.lag_verdict(measure.summarize_holds(holds(*[400] * 10)))
    assert "Only 3 holds" in thin
    assert "Only" not in thick


# --------------------------------------------------------------------- prefix correctness


def test_punctuation_and_case_are_not_a_lexical_change():
    assert measure.words("Switch to Workspace 3.") == ("switch", "to", "workspace", "3")
    assert measure.is_prefix("Switch to workspace", "switch to workspace three")


def test_an_apostrophe_is_a_split_and_not_a_different_word():
    assert measure.words("verrouille l'ecran") == ("verrouille", "l", "ecran")


def test_an_english_only_model_dropping_the_accents_is_not_a_different_word():
    # Parakeet 110M can never emit the circumflex, so scoring it on one would answer a
    # question about orthography with a number that claims to be about drift
    assert measure.words("ferme la fenêtre") == ("ferme", "la", "fenetre")
    assert measure.is_prefix("ferme la fenetre", "ferme la fenêtre maintenant")


def test_the_wrong_content_word_is_not_a_prefix_however_close_it_sounds():
    # STRATEGY 8 R2, measured: Parakeet decoded "wish to say" where truth was "wish to see"
    assert not measure.is_prefix("well i don't wish to say", "well i don't wish to see the world")


def test_a_decode_longer_than_the_truth_is_not_a_prefix_of_it():
    assert not measure.is_prefix("open firefox now", "open firefox")


def test_an_empty_decode_is_a_prefix_of_everything_which_is_why_it_is_counted_apart():
    assert measure.is_prefix("", "open firefox")
    assert measure.is_prefix("  ...  ", "open firefox")


# ------------------------------------------------------------------------- the ladder rungs


def test_the_ladder_climbs_in_steps_and_always_ends_on_the_whole_clip():
    assert measure.rungs(1.5, start=0.6, step=0.2) == (0.6, 0.8, 1.0, 1.2, 1.4, 1.5)


def test_a_clip_shorter_than_the_first_rung_is_its_own_only_rung():
    assert measure.rungs(0.4, start=0.6, step=0.2) == (0.4,)


def test_a_clip_that_lands_exactly_on_a_rung_does_not_get_that_rung_twice():
    assert measure.rungs(1.0, start=0.6, step=0.2) == (0.6, 0.8, 1.0)


def test_a_prefix_is_cut_on_a_sample_boundary():
    pcm = b"\x01\x02" * RATE  # one second
    assert len(measure.prefix_pcm(pcm, 0.5)) == RATE  # 8000 samples, 16000 bytes
    assert len(measure.prefix_pcm(pcm, 0.0)) == 0
    assert measure.prefix_pcm(pcm, 9.0) == pcm


# ------------------------------------------------------------------------- the decision key


def window(address: str, cls: str = "firefox") -> Window:
    return Window(address, cls, cls, "whatever the owner wrote", 1, "1", 0)


def test_two_decisions_that_do_the_same_thing_share_a_key():
    one = Decision(Verdict.ACT, Action(Intent.FOCUS_WINDOW, window=window("0x1")), heard="a")
    two = Decision(Verdict.ACT, Action(Intent.FOCUS_WINDOW, window=window("0x1")), heard="b")
    assert measure.decision_key(one) == measure.decision_key(two)


def test_the_words_that_were_heard_are_not_part_of_the_key():
    # S2 compares rungs on the act, never the transcript: a reworded prefix that means
    # the same thing must not cancel speculated work
    said = Decision(Verdict.ACT, Action(Intent.VOLUME, verb="up"), heard="louder")
    other = Decision(Verdict.ACT, Action(Intent.VOLUME, verb="up"), heard="volume up")
    assert measure.decision_key(said) == measure.decision_key(other)


def test_a_different_target_is_a_different_decision():
    one = Decision(Verdict.ACT, Action(Intent.FOCUS_WINDOW, window=window("0x1")))
    two = Decision(Verdict.ACT, Action(Intent.FOCUS_WINDOW, window=window("0x2")))
    assert measure.decision_key(one) != measure.decision_key(two)


def test_a_different_slot_is_a_different_decision():
    one = Decision(Verdict.ACT, Action(Intent.SWITCH_WORKSPACE, workspace="2"))
    two = Decision(Verdict.ACT, Action(Intent.SWITCH_WORKSPACE, workspace="3"))
    assert measure.decision_key(one) != measure.decision_key(two)


def test_an_app_and_a_window_of_the_same_name_are_different_decisions():
    app = App("firefox", "Firefox", wm_classes=("firefox",), trusted=True)
    launch = Decision(Verdict.ACT, Action(Intent.LAUNCH_APP, app=app))
    focus = Decision(Verdict.ACT, Action(Intent.FOCUS_WINDOW, window=window("0x1")))
    assert measure.decision_key(launch) != measure.decision_key(focus)


def test_a_decision_with_no_action_keys_on_its_verdict():
    nothing = measure.decision_key(Decision(Verdict.NOTHING))
    assert nothing[0] == "nothing"
    assert measure.decision_key(Decision(Verdict.HINTS)) != nothing


def test_the_same_act_at_a_different_tier_is_a_different_decision():
    one = Decision(Verdict.ACT, Action(Intent.CLOSE_WINDOW, window=window("0x1")), tier=1)
    two = Decision(Verdict.COUNTDOWN, Action(Intent.CLOSE_WINDOW, window=window("0x1")), tier=2)
    assert measure.decision_key(one) != measure.decision_key(two)


# -------------------------------------------------------------------- the ladder, end to end


class FakeRecognizer:
    """Decodes by clock length, from a table of {seconds: text}. Never loads a model."""

    name = "fake:table"

    def __init__(self, table: dict[float, str]) -> None:
        self.table = table
        self.asked: list[float] = []

    async def transcribe(self, pcm: bytes, sample_rate: int) -> Transcript:
        seconds = len(pcm) / 2 / sample_rate
        self.asked.append(round(seconds, 3))
        text = ""
        for at, said in sorted(self.table.items()):
            if seconds + 1e-6 >= at:
                text = said
        return Transcript(text=text, backend=self.name)


def decider(table: dict[str, Decision]):
    async def decide(transcript: Transcript) -> Decision:
        return table.get(transcript.text, Decision(Verdict.NOTHING))

    return decide


def silence(seconds: float) -> bytes:
    return b"\x00\x00" * int(seconds * RATE)


def test_every_rung_is_decoded_and_scored_against_the_whole_clip():
    focus = Decision(Verdict.ACT, Action(Intent.FOCUS_WINDOW, window=window("0x1")))
    recognizer = FakeRecognizer({0.0: "", 0.6: "focus", 1.0: "focus firefox"})
    rows = asyncio.run(
        measure.run_ladder(
            silence(1.4),
            "focus firefox",
            recognizer=recognizer,
            decide=decider({"focus firefox": focus}),
            start=0.6,
            step=0.2,
        )
    )
    assert recognizer.asked == [0.6, 0.8, 1.0, 1.2, 1.4]
    assert [r["text"] for r in rows] == ["focus", "focus", "focus firefox", "focus firefox",
                                         "focus firefox"]  # fmt: skip
    assert [r["matches_final"] for r in rows] == [False, False, True, True, True]
    assert all(r["prefix_ok"] for r in rows)


def test_a_rung_whose_decode_drifts_off_the_truth_is_not_a_prefix():
    recognizer = FakeRecognizer({0.0: "wish to say", 1.0: "wish to see"})
    rows = asyncio.run(
        measure.run_ladder(
            silence(1.2), "wish to see", recognizer=recognizer, decide=decider({}), start=0.6
        )
    )
    assert [r["prefix_ok"] for r in rows] == [False, False, True, True]


# --------------------------------------------------------------------- the ladder summaries


def rung(seconds: float, key: str, *, acts: bool = True, empty: bool = False,
         prefix_ok: bool = True, exact: bool = True) -> dict:  # fmt: skip
    return {
        "seconds": seconds,
        "text": "" if empty else key,
        "decode_ms": 1.0,
        "empty": empty,
        "prefix_ok": prefix_ok,
        "exact": exact,
        "verdict": "act" if acts else "nothing",
        "acts": acts,
        "key": [key],
    }


def scored(rows: list[dict]) -> list[dict]:
    final = rows[-1]["key"]
    for row in rows:
        row["matches_final"] = row["key"] == final
    return rows


def test_an_empty_rung_is_not_counted_as_a_correct_prefix():
    rows = scored([rung(0.6, "", empty=True), rung(0.8, "a"), rung(1.0, "a"), rung(1.2, "a")])
    summary = measure.summarize_ladder(rows)
    assert summary["rungs"] == 4
    assert summary["non_empty"] == 2
    assert summary["prefix_ok"] == 2


def test_the_whole_clip_rung_is_left_out_of_every_rate_because_it_is_the_answer():
    # counting the rung that defines the answer would pad each rate by one
    rows = scored([rung(0.6, "a"), rung(0.8, "b"), rung(1.0, "b")])
    summary = measure.summarize_ladder(rows)
    assert summary["rungs"] == 3
    assert summary["early_rungs"] == 2
    assert summary["early_match"] == 1
    assert summary["non_empty"] == 2


def test_the_whole_clip_transcribing_word_for_word_is_reported_on_its_own():
    right = scored([rung(0.6, "a"), rung(0.8, "a"), rung(1.0, "a", exact=True)])
    wrong = scored([rung(0.6, "a"), rung(0.8, "a"), rung(1.0, "a", exact=False)])
    assert measure.summarize_ladder(right)["final_correct"] is True
    assert measure.summarize_ladder(wrong)["final_correct"] is False


def test_the_settle_point_is_the_first_rung_nothing_later_disagrees_with():
    rows = scored([rung(0.6, "a"), rung(0.8, "b"), rung(1.0, "c"), rung(1.2, "c")])
    assert measure.summarize_ladder(rows)["settles_at"] == 0.833


def test_a_ladder_that_never_settles_before_the_end_says_so():
    rows = scored([rung(0.6, "a"), rung(0.8, "b"), rung(1.0, "c")])
    assert measure.summarize_ladder(rows)["settles_at"] == 1.0


def test_two_agreeing_rungs_only_count_when_they_would_have_acted():
    quiet = scored([rung(0.6, "x", acts=False), rung(0.8, "x", acts=False), rung(1.0, "x"),
                    rung(1.2, "x")])  # fmt: skip
    assert measure.summarize_ladder(quiet)["two_rung_fires"] == 0
    loud = scored([rung(0.6, "x"), rung(0.8, "x"), rung(1.0, "x"), rung(1.2, "x")])
    assert measure.summarize_ladder(loud)["two_rung_fires"] == 2


def test_two_agreeing_rungs_that_the_whole_clip_contradicts_count_as_wrong():
    rows = scored([rung(0.6, "say"), rung(0.8, "say"), rung(1.0, "say"), rung(1.2, "see")])
    summary = measure.summarize_ladder(rows)
    assert summary["two_rung_fires"] == 2
    assert summary["two_rung_correct"] == 0


def test_the_whole_clip_rung_is_never_half_of_an_agreeing_pair():
    # by the last rung there is nothing left to speculate about, so a pair ending there
    # would flatter the rule with a decision that was never a guess
    rows = scored([rung(0.6, "a"), rung(0.8, "b"), rung(1.0, "b")])
    assert measure.summarize_ladder(rows)["two_rung_fires"] == 0


def test_a_ladder_of_one_rung_has_nothing_to_compare():
    summary = measure.summarize_ladder(scored([rung(0.9, "a")]))
    assert summary["two_rung_fires"] == 0
    assert summary["early_rungs"] == 0
    assert summary["non_empty"] == 0


def test_a_ladder_of_no_rungs_is_reported_rather_than_crashing():
    assert measure.summarize_ladder([])["rungs"] == 0


def clip(lang: str, rows: list[dict]) -> dict:
    return {"lang": lang, "summary": measure.summarize_ladder(scored(rows))}


def test_the_rollup_splits_by_language():
    english = clip("en", [rung(0.6, "a"), rung(0.8, "a"), rung(1.0, "a"), rung(1.2, "a")])
    french = clip("fr", [rung(0.6, "b"), rung(0.8, "b"), rung(1.0, "b"), rung(1.2, "c")])
    totals = measure.aggregate_ladder([english, french])
    assert totals["overall"]["clips"] == 2
    assert totals["overall"]["early_rungs"] == 6
    assert totals["by_language"]["en"]["two_rung_correct"] == 2
    assert totals["by_language"]["fr"]["two_rung_correct"] == 0
    assert totals["overall"]["two_rung_precision"] == 0.5


def test_agreement_below_ninety_percent_forbids_acting_early():
    overall = {"two_rung_precision": 0.83, "two_rung_fires": 60}
    key, sentence = measure.ladder_verdict(overall)
    assert key == "pre-warm-only"
    assert "Never act early" in sentence


def test_agreement_at_ninety_percent_allows_acting_early():
    key, sentence = measure.ladder_verdict({"two_rung_precision": 0.9, "two_rung_fires": 60})
    assert key == "act-early-allowed"
    assert "tier 0 only" in sentence


def test_a_handful_of_firings_is_reported_as_a_reading_and_not_a_rate():
    _, sentence = measure.ladder_verdict({"two_rung_precision": 1.0, "two_rung_fires": 4})
    assert "Only 4 firings" in sentence


def test_no_agreeing_pair_at_all_leaves_speculation_pre_warming_only():
    key, sentence = measure.ladder_verdict({"two_rung_precision": None, "two_rung_fires": 0})
    assert key == "no-data"
    assert "pre-warm" in sentence


# -------------------------------------------------------------------------------- prompts


def test_half_the_default_prompts_are_french():
    plan = measure.default_prompts(40)
    assert len(plan) == 40
    assert sum(1 for lang, _ in plan if lang == "fr") == 20


def test_any_prefix_of_the_plan_is_still_about_half_french():
    plan = measure.default_prompts(40)
    for n in (4, 10, 26):
        assert sum(1 for lang, _ in plan[:n] if lang == "fr") == n // 2


def test_a_plan_longer_than_the_prompt_lists_wraps_instead_of_running_out():
    assert len(measure.default_prompts(300)) == 300


def test_the_english_prompts_come_from_the_eval_cases():
    known = {utterance for utterance, *_ in cases.CANONICAL + cases.PARAPHRASE}
    english = measure.english_prompts()
    assert set(english) <= known
    assert "open firefox" in english
    assert len(set(english)) == len(english)


def test_the_french_prompts_are_written_the_way_they_are_read_aloud():
    # the owner reads these off the screen, so they keep their accents even though the
    # prefix comparison folds them away
    accented = [p for p in measure.FRENCH_PROMPTS if any(c in p for c in "éèàêç")]
    assert len(accented) >= 10


def test_a_prompt_file_may_tag_its_own_languages(tmp_path):
    path = tmp_path / "prompts.txt"
    path.write_text("# mine\nfr: ouvre firefox\nen: open firefox\nplain line\n\n", encoding="utf-8")
    assert measure.read_prompt_file(path) == (
        ("fr", "ouvre firefox"),
        ("en", "open firefox"),
        ("own", "plain line"),
    )


# ------------------------------------------------------------------------ clips and manifest


def test_a_manifest_round_trips_and_resolves_its_clip_paths(tmp_path):
    manifest = tmp_path / "corpus" / "manifest.json"
    measure.write_manifest(manifest, [{"path": "001-en.wav", "prompt": "open firefox",
                                       "seconds": 0.9, "lang": "en"}])  # fmt: skip
    rows = measure.load_manifest(manifest)
    assert rows[0]["path"] == str(manifest.parent / "001-en.wav")
    assert rows[0]["prompt"] == "open firefox"
    assert rows[0]["seconds"] == 0.9


def test_a_recorded_clip_reads_back_as_sixteen_kilohertz_mono(tmp_path):
    pcm = (np.arange(RATE, dtype=np.int16) % 1000).tobytes()
    measure.write_wav(tmp_path / "clip.wav", pcm)
    assert measure.read_wav(tmp_path / "clip.wav") == pcm


def test_a_stereo_clip_is_folded_to_mono(tmp_path):
    import wave

    with wave.open(str(tmp_path / "two.wav"), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(np.array([100, 300, 200, 400], dtype="<i2").tobytes())
    assert np.frombuffer(measure.read_wav(tmp_path / "two.wav"), dtype="<i2").tolist() == [200, 300]


def test_a_clip_that_is_not_sixteen_bit_is_refused_by_name(tmp_path):
    import wave

    with wave.open(str(tmp_path / "eight.wav"), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(1)
        handle.setframerate(RATE)
        handle.writeframes(b"\x01\x02")
    with pytest.raises(ValueError, match="8 bit"):
        measure.read_wav(tmp_path / "eight.wav")


def test_results_are_written_as_utf_eight_json_a_later_run_can_reread(tmp_path):
    out = measure.write_json(tmp_path / "deep" / "out.json", {"prompt": "ferme la fenêtre"})
    assert "fenêtre" in out.read_text(encoding="utf-8")


# --------------------------------------------------------------- against the real Recorder


class FakeStream:
    """PortAudio's shape, none of PortAudio. One 20 ms block per callback."""

    def __init__(self, **kwargs):
        self.callback = kwargs["callback"]
        self.active = False

    def start(self):
        self.active = True

    def stop(self):
        self.active = False

    def close(self):
        self.active = False

    def feed(self, samples: np.ndarray) -> None:
        for at in range(0, len(samples), FRAME):
            block = samples[at : at + FRAME]
            self.callback(block.astype("<i2").tobytes(), len(block), None, None)


def tone(seconds: float) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    return (np.sin(2 * np.pi * 220 * t) * 0.3 * 32767).astype(np.int16)


def quiet(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * RATE), dtype=np.int16)


def recording() -> tuple[Recorder, FakeStream]:
    streams: list[FakeStream] = []

    def factory(**kwargs):
        streams.append(FakeStream(**kwargs))
        return streams[-1]

    recorder = Recorder(Config(), stream_factory=factory)
    recorder.start()
    return recorder, streams[0]


def test_the_harness_reads_the_recorders_own_gate_and_never_a_second_one():
    recorder, stream = recording()
    stream.feed(tone(0.4))
    stream.feed(quiet(0.3))
    flags = measure.voiced_flags(recorder)
    assert len(flags) == 35  # 0.7 s of 20 ms frames
    assert flags.count(VOICED) > 10
    assert flags[-10:] == (QUIET,) * 10
    # the harness and the recorder agree about where the speech stopped
    assert recorder.trailing_silence(200.0) is True
    assert measure.last_voiced_frame(flags) == 19


def test_a_real_hold_measures_the_silence_the_recorder_itself_heard():
    recorder, stream = recording()
    stream.feed(tone(0.4))
    stream.feed(quiet(0.3))
    hold = measure.measure_hold(
        1, "open firefox", measure.voiced_flags(recorder), recorder.seconds() * 1000, 740.0
    )
    assert hold.trailing_ms == pytest.approx(300.0, abs=20.0)
    assert hold.flush_ms == pytest.approx(40.0, abs=1.0)
    assert hold.lag_ms == pytest.approx(340.0, abs=20.0)


def test_the_flags_are_empty_rather_than_an_error_when_there_is_no_gate_to_read():
    assert measure.voiced_flags(object()) == ()


def test_the_measuring_recorder_never_ducks_the_volume_or_keeps_the_preroll(monkeypatch):
    seen: dict[str, Config] = {}

    def spy(cfg):
        seen["cfg"] = cfg
        return cfg

    monkeypatch.setattr(audio, "Recorder", spy)
    measure.open_recorder(Config(stt=STT(preroll=True, duck_volume=True)))
    assert seen["cfg"].stt.preroll is False
    assert seen["cfg"].stt.duck_volume is False


# ----------------------------------------------------------------------------- the command


def test_the_modes_are_release_corpus_and_ladder():
    parser = measure.build_parser()
    assert parser.parse_args(["release", "--n", "3"]).n == 3
    assert parser.parse_args(["corpus", "--count", "8"]).count == 8
    assert parser.parse_args(["ladder", "--wav", "a.wav"]).step == 0.2


def test_a_mode_nobody_wrote_is_refused():
    with pytest.raises(SystemExit):
        measure.build_parser().parse_args(["guess"])


def test_a_ladder_with_no_clip_and_no_manifest_says_so_before_loading_a_model(capsys):
    args = measure.build_parser().parse_args(["ladder"])
    assert measure.mode_ladder(Config(), args) == 2
    assert "--manifest" in capsys.readouterr().err


def test_a_ladder_over_a_clip_with_no_transcript_is_refused(capsys):
    args = measure.build_parser().parse_args(["ladder", "--wav", "clip.wav"])
    assert measure.mode_ladder(Config(), args) == 2
    assert "true transcript" in capsys.readouterr().err


def walked_away(prompt: str = "") -> str:
    """stdin at end of file: what Ctrl-D, and a run with no terminal, look like."""
    raise EOFError


def test_a_model_nobody_downloaded_is_a_plain_message_and_not_a_traceback(tmp_path, capsys):
    args = measure.build_parser().parse_args(
        ["ladder", "--wav", str(tmp_path / "c.wav"), "--truth", "open firefox", "--model", "nope"]
    )
    assert measure.mode_ladder(Config(), args) == 1
    assert "unknown model" in capsys.readouterr().err


def test_asking_turns_the_owner_leaving_into_a_stop_and_not_a_traceback(monkeypatch):
    monkeypatch.setattr("builtins.input", walked_away)
    with pytest.raises(measure.Stopped):
        measure.ask("anything ")


def test_typing_q_stops_the_release_run_before_any_device_is_opened(tmp_path, monkeypatch):
    opened: list[int] = []
    monkeypatch.setattr(Recorder, "start", lambda self: opened.append(1))
    monkeypatch.setattr("builtins.input", lambda prompt="": "q")
    args = measure.build_parser().parse_args(
        ["release", "--n", "5", "--out", str(tmp_path / "release.json")]
    )
    assert measure.mode_release(Config(), args) == 0
    assert opened == []
    saved = json.loads((tmp_path / "release.json").read_text(encoding="utf-8"))
    assert saved["summary"]["n"] == 0
    assert saved["verdict"] == "no-data"


def test_a_corpus_run_that_is_abandoned_still_leaves_a_readable_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(Recorder, "start", lambda self: pytest.fail("opened the microphone"))
    monkeypatch.setattr("builtins.input", walked_away)
    manifest = tmp_path / "corpus" / "manifest.json"
    args = measure.build_parser().parse_args(["corpus", "--count", "4", "--out", str(manifest)])
    assert measure.mode_corpus(Config(), args) == 0
    assert measure.load_manifest(manifest) == []
