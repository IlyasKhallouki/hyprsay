"""The settle rule, on its own: when a half-spoken clause may run and when it may not.

Everything here is about a transcript that is still growing. The engine's side of it is in
test_daemon.py under "while the key is held"; this file is the rule itself, with no engine,
no audio and no model, because the rule is the part that must be right whatever those do.
"""

import pytest

from hyprsay import incremental
from hyprsay.config import Config
from hyprsay.model import Action, Decision, Intent, Recognizer, Verdict


def act(said, *, tier=0, intent=Intent.FOCUS_WINDOW, verdict=Verdict.ACT):
    return Decision(verdict, Action(intent), tier=tier, heard=said)


def chain(*decisions):
    return list(decisions)


# --------------------------------------------------------------------------- the rule


def test_a_clause_with_another_clause_behind_it_is_ready_on_its_first_decode():
    settler = incremental.Settler()
    look = settler.look(
        "open spotify and turn it", chain(act("open spotify"), act("turn it", tier=0))
    )
    assert [r.index for r in look.ready] == [0]
    assert look.ready[0].why == "a further clause has begun"


def test_the_last_clause_waits_for_two_decodes_that_say_the_same_thing():
    settler = incremental.Settler()
    first = settler.look("open spotify", chain(act("open spotify")))
    assert first.ready == ()
    second = settler.look("open spotify", chain(act("open spotify")))
    assert [r.index for r in second.ready] == [0]
    assert second.ready[0].why.startswith("unchanged across 2 decodes")


def test_a_decoder_that_stopped_moving_while_the_speaker_did_not_settles_nothing():
    # measured: a streaming recognizer sat at "Open Chrome" for four decodes while the
    # speaker was still saying "on workspace three". Identical decodes prove only that the
    # DECODER has not moved, so the room has to have gone quiet as well
    settler = incremental.Settler()
    for _ in range(4):
        look = settler.look("open chrome", chain(act("open chrome")), quiet=False)
        assert look.ready == ()
    assert settler.look("open chrome", chain(act("open chrome")), quiet=True).ready != ()


def test_a_further_clause_is_evidence_enough_without_any_silence_at_all():
    # the speaker moving on is what makes the clause behind them final, and they are by
    # definition still talking when they do it
    settler = incremental.Settler()
    look = settler.look(
        "open spotify and turn it", chain(act("open spotify"), act("turn it")), quiet=False
    )
    assert [r.index for r in look.ready] == [0]


def test_a_prefix_whose_words_grow_starts_its_evidence_again_from_nothing():
    # "open chrome" is a perfectly good command and "open chrome on workspace three" is a
    # different one. The run of identical decodes belongs to the words, not to the clause
    settler = incremental.Settler()
    settler.look("open chrome", chain(act("open chrome")))
    grown = settler.look("open chrome on workspace 3", chain(act("open chrome on workspace 3")))
    assert grown.ready == ()
    again = settler.look("open chrome on workspace 3", chain(act("open chrome on workspace 3")))
    assert [r.index for r in again.ready] == [0]


def test_the_number_of_settling_decodes_is_a_knob():
    settler = incremental.Settler(stable_decodes=4)
    for _ in range(3):
        assert settler.look("volume up", chain(act("volume up"))).ready == ()
    assert settler.look("volume up", chain(act("volume up"))).ready != ()


def test_a_tier_two_clause_is_never_ready_however_settled_it_looks():
    settler = incremental.Settler()
    closing = chain(act("close this", tier=2, intent=Intent.CLOSE_WINDOW), act("and open kitty"))
    for _ in range(5):
        assert settler.look("close this and open kitty", closing).ready == ()


def test_a_recipe_is_never_ready_early_however_low_its_tier():
    # "find lofi" is a perfectly good prefix of "find lofi hip hop radio", and a recipe has
    # usually pressed enter on it by the time the rest of the sentence arrives
    settler = incremental.Settler()
    recipe = Decision(
        Verdict.ACT,
        Action(Intent.RUN_RECIPE, text="lofi"),
        tier=1,
        heard="find lofi",
    )
    look = settler.look("find lofi and scroll down", chain(recipe, act("scroll down")))
    assert look.ready == ()


def test_a_clause_carrying_the_speakers_own_words_is_never_ready_early():
    settler = incremental.Settler()
    typed = Decision(
        Verdict.ACT, Action(Intent.FOCUS_WINDOW, text="dear sir"), tier=0, heard="type dear sir"
    )
    assert settler.look("type dear sir and stop", chain(typed, act("stop"))).ready == ()


def test_a_dictation_is_never_ready_early_even_at_tier_zero():
    settler = incremental.Settler()
    dictated = Decision(
        Verdict.ACT, Action(Intent.FOCUS_WINDOW), tier=0, heard="hello", dictation=True
    )
    assert settler.look("hello and stop", chain(dictated, act("stop"))).ready == ()


def test_a_tier_three_clause_is_never_ready_either():
    settler = incremental.Settler()
    locking = chain(act("lock the screen", tier=3, intent=Intent.LOCK_SCREEN), act("goodbye"))
    assert settler.look("lock the screen and goodbye", locking).ready == ()


def test_a_question_in_front_of_an_action_stops_the_whole_offer():
    # clause 1 is an order that assumes clause 0 happened. Offering it while clause 0 is
    # still a question would aim it at whatever the desktop happened to be showing
    settler = incremental.Settler()
    asked = Decision(Verdict.HINTS, Action(Intent.FOCUS_WINDOW), tier=0, heard="focus the browser")
    look = settler.look("focus the browser and turn it up", chain(asked, act("turn it up")))
    assert look.ready == ()


def test_a_refusal_in_front_of_an_action_stops_the_whole_offer():
    settler = incremental.Settler()
    refused = Decision(Verdict.REFUSE, None, tier=0, heard="open nothing")
    look = settler.look("open nothing and turn it up", chain(refused, act("turn it up")))
    assert look.ready == ()


def test_a_countdown_is_not_an_early_verdict_even_at_tier_zero():
    settler = incremental.Settler()
    slow = Decision(Verdict.COUNTDOWN, Action(Intent.CLOSE_WINDOW), tier=0, heard="close it")
    assert settler.look("close it and stop", chain(slow, act("stop"))).ready == ()


def test_a_swap_may_act_early_because_a_number_can_still_retarget_it():
    settler = incremental.Settler()
    swap = Decision(Verdict.ACT_SWAP, Action(Intent.FOCUS_WINDOW), tier=0, heard="focus firefox")
    look = settler.look("focus firefox and turn it up", chain(swap, act("turn it up")))
    assert [r.index for r in look.ready] == [0]


# --------------------------------------------------------------------------- consumption


def test_a_clause_that_has_been_carried_out_is_never_offered_again():
    settler = incremental.Settler()
    first = settler.look("open spotify and turn it", chain(act("open spotify"), act("turn it")))
    settler.take(first.ready[0])
    later = settler.look(
        "open spotify and turn it down",
        chain(act("open spotify"), act("turn it down"), act("and lower it")),
    )
    assert [r.index for r in later.ready] == [1]
    assert later.consumed_index == 1


def test_the_consumed_span_is_the_characters_of_the_clauses_already_run():
    settler = incremental.Settler()
    look = settler.look("open spotify and turn it", chain(act("open spotify"), act("turn it")))
    settler.take(look.ready[0])
    after = settler.look(
        "open spotify and turn it down", chain(act("open spotify"), act("turn it down"))
    )
    assert after.consumed_chars == len("open spotify")
    assert after.spans[0] == (0, 12) and after.exact


def test_nothing_is_consumed_before_anything_has_been_carried_out():
    settler = incremental.Settler()
    look = settler.look("open spotify", chain(act("open spotify")))
    assert look.consumed_index == 0 and look.consumed_chars == 0


def test_a_hold_that_is_reset_forgets_what_the_hold_before_it_spent():
    settler = incremental.Settler()
    look = settler.look("open spotify and turn it", chain(act("open spotify"), act("turn it")))
    settler.take(look.ready[0])
    settler.reset()
    assert settler.consumed == 0
    fresh = settler.look("open spotify and turn it", chain(act("open spotify"), act("turn it")))
    assert fresh.consumed_index == 0 and [r.index for r in fresh.ready] == [0]


def test_the_final_decode_settles_nothing_and_only_says_where_the_spent_part_ends():
    settler = incremental.Settler()
    look = settler.look("open spotify and turn it", chain(act("open spotify"), act("turn it")))
    settler.take(look.ready[0])
    final = settler.look(
        "open spotify and turn it down",
        chain(act("open spotify"), act("turn it down")),
        growing=False,
    )
    assert final.ready == ()
    assert final.consumed_index == 1


def test_a_clause_that_reads_differently_at_the_end_is_reported_and_stays_spent():
    # the partials and the final decode may come from two models that heard the same
    # second of audio differently. Running the command twice is the worse outcome
    settler = incremental.Settler()
    look = settler.look("open spotifi and turn it", chain(act("open spotifi"), act("turn it")))
    settler.take(look.ready[0])
    final = settler.look(
        "open spotify and turn it down",
        chain(act("open spotify"), act("turn it down")),
        growing=False,
    )
    assert final.drifted == ("open spotify",)
    assert final.consumed_index == 1


def test_a_final_decode_that_found_fewer_clauses_still_spends_what_was_run():
    settler = incremental.Settler()
    look = settler.look("open bits and bytes", chain(act("open bits"), act("bytes")))
    settler.take(look.ready[0])
    final = settler.look("open bits and bytes", chain(act("open bits and bytes")), growing=False)
    assert final.consumed_index == 1  # never more than there are clauses


def test_an_utterance_that_got_shorter_does_not_let_a_clause_inherit_a_stale_run():
    settler = incremental.Settler()
    settler.look("turn it up and stop", chain(act("turn it up"), act("stop")))
    settler.look("turn it up and stop", chain(act("turn it up"), act("stop")))
    shorter = settler.look("turn it up", chain(act("turn it up")))
    assert [r.index for r in shorter.ready] == [0]  # its own run is 3, which is settled
    grown = settler.look("turn it up and stop", chain(act("turn it up"), act("stopping")))
    assert [r.index for r in grown.ready] == [0]  # clause 1 is new, and has run 1
    assert 1 not in [r.index for r in grown.ready]


# --------------------------------------------------------------------------- the spans


def test_each_clause_is_located_verbatim_in_the_transcript():
    placed = incremental.spans(
        "Open spotify and turn it down", chain(act("open spotify"), act("turn it down"))
    )
    assert placed.spans == ((0, 12), (17, 29))
    assert placed.exact


def test_a_clause_the_normalizer_repaired_is_placed_by_counting_words():
    # "work space three" became "workspace 3": there is no verbatim slice to find, so the
    # span is an estimate and says so rather than pretending
    placed = incremental.spans("go to work space three", chain(act("go to workspace 3")))
    assert not placed.exact
    assert placed.spans[0] == (0, 16)  # four words, because the clause has four


def test_an_empty_clause_takes_no_characters_and_does_not_break_the_walk():
    placed = incremental.spans("open spotify", chain(act(""), act("open spotify")))
    assert placed.spans[0][0] == placed.spans[0][1]
    assert placed.spans[1] == (0, 12)


# --------------------------------------------------------------------------- the config


def test_incremental_decoding_is_on_by_default_with_the_owners_chunk_interval():
    cfg = Config()
    assert cfg.stt.incremental is True
    assert 300 <= cfg.stt.chunk_ms <= 500
    assert cfg.stt.settle_decodes >= incremental.STABLE_DECODES


def test_turning_it_off_leaves_the_engine_with_nothing_listening_during_the_hold():
    from dataclasses import replace

    from hyprsay.stt import stream

    cfg = replace(Config(), stt=replace(Config().stt, incremental=False))
    assert stream.make_partials(cfg, _NoRecognizer()) is None


def test_an_absent_streaming_model_falls_back_to_re_decoding_the_buffer():
    from dataclasses import replace

    from hyprsay.stt import stream

    cfg = replace(Config(), stt=replace(Config().stt, stream_model="kroko-en-streaming"))
    partials = stream.make_partials(
        cfg,
        _NoRecognizer(),
    )
    assert isinstance(partials, stream.StreamingPartials | stream.RedecodePartials)


def test_no_streaming_model_asked_for_means_the_fallback_without_a_log_line():
    from dataclasses import replace

    from hyprsay.stt import stream

    cfg = replace(Config(), stt=replace(Config().stt, stream_model=""))
    assert isinstance(stream.make_partials(cfg, _NoRecognizer()), stream.RedecodePartials)


def test_a_cloud_recognizer_is_never_re_decoded_eight_times_during_one_hold():
    # `stt.backend = "cloud"` is someone choosing to send ONE clip per utterance. A
    # re-decode of a growing buffer through it is eight uploads of the same sentence
    from dataclasses import replace

    from hyprsay.stt import stream

    class Gateway(_NoRecognizer):
        name = "gateway:fish-audio/transcribe-1"

    cfg = replace(Config(), stt=replace(Config().stt, backend="cloud"))
    assert stream.make_partials(cfg, Gateway()) is None


def test_a_hybrid_recognizer_still_decodes_during_the_hold_because_it_stays_local():
    from hyprsay.stt import stream

    class Hybrid(_NoRecognizer):
        name = "hybrid:local:parakeet-110m+gateway:fish-audio/transcribe-1"

    assert isinstance(stream.make_partials(Config(), Hybrid()), stream.RedecodePartials)


class _NoRecognizer(Recognizer):
    name = "local:fake"

    async def transcribe(self, pcm, sample_rate):  # pragma: no cover - never called here
        raise AssertionError("nothing should decode in a configuration test")


# --------------------------------------------------------------------------- the sessions


def test_the_online_session_only_ever_feeds_the_encoder_what_is_new():
    """Feeding the whole snapshot again would replay the utterance into an encoder that
    still remembers it, and every word would come back twice."""
    from hyprsay.stt import stream

    fed: list[int] = []

    class FakeStream:
        def accept_waveform(self, rate, samples):
            fed.append(len(samples))

    class FakeRecognizer:
        def create_stream(self):
            return FakeStream()

        def is_ready(self, stream):
            return False

        def get_result(self, stream):
            return f"heard {sum(fed)} samples "

    owner = stream.StreamingPartials.__new__(stream.StreamingPartials)
    owner._recognizer = FakeRecognizer()
    owner.load = lambda: None
    session = stream._OnlineSession(owner)
    assert session._decode(b"\x01\x00" * 100, 16000) == "heard 100 samples"
    assert session._decode(b"\x01\x00" * 250, 16000) == "heard 250 samples"
    assert fed == [100, 150]


def test_the_online_session_answers_without_decoding_when_no_audio_arrived():
    from hyprsay.stt import stream

    class FakeRecognizer:
        def create_stream(self):
            return object()

        def is_ready(self, stream):
            raise AssertionError("nothing new to decode")

        def get_result(self, stream):
            return "so far"

    owner = stream.StreamingPartials.__new__(stream.StreamingPartials)
    owner._recognizer = FakeRecognizer()
    owner.load = lambda: None
    session = stream._OnlineSession(owner)
    assert session._decode(b"", 16000) == "so far"


def test_closing_a_session_drops_the_encoder_state_of_that_hold():
    from hyprsay.stt import stream

    owner = stream.StreamingPartials.__new__(stream.StreamingPartials)
    session = stream._OnlineSession(owner)
    session._stream, session._fed = object(), 999
    session.close()
    assert session._stream is None and session._fed == 0


def test_the_fallback_session_hands_back_what_the_ordinary_recognizer_heard():
    import asyncio

    from hyprsay.model import Transcript
    from hyprsay.stt import stream

    class Local:
        name = "local:fake"
        seen: list[int] = []

        async def transcribe(self, pcm, sample_rate):
            self.seen.append(len(pcm))
            return Transcript("  open spotify  ", backend="local:fake")

    partials = stream.RedecodePartials(Local())
    assert partials.name == "redecode:local:fake"
    session = partials.open()
    assert asyncio.run(session.hear(b"\x01\x00" * 100)) == "open spotify"


def test_a_model_that_is_not_a_streaming_model_is_refused_by_name():
    from hyprsay.stt import models, stream

    with pytest.raises(models.ModelError, match="not a streaming model"):
        stream._build(models.get("parakeet-110m"), {})
