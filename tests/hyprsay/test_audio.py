"""The recorder against a fake stream. No test here opens a microphone or runs wpctl."""

import sys
import types

import numpy as np
import pytest

from hyprsay import audio
from hyprsay.audio import FRAME, RATE, AudioError, Recorder
from hyprsay.config import PTT, STT, Config


class FakeStream:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.callback = kwargs["callback"]
        self.active = False
        self.closed = False

    def start(self):
        self.active = True

    def stop(self):
        self.active = False

    def close(self):
        self.closed = True

    def feed(self, samples: np.ndarray) -> None:
        """Deliver int16 samples the way PortAudio does: one 20 ms block per callback."""
        for at in range(0, len(samples), FRAME):
            block = samples[at : at + FRAME]
            self.callback(block.astype("<i2").tobytes(), len(block), None, None)


class Factory:
    def __init__(self):
        self.streams: list[FakeStream] = []

    def __call__(self, **kwargs):
        self.streams.append(FakeStream(**kwargs))
        return self.streams[-1]

    @property
    def stream(self) -> FakeStream:
        return self.streams[-1]


def tone(seconds: float, amplitude: float = 0.3) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    return (np.sin(2 * np.pi * 220 * t) * amplitude * 32767).astype(np.int16)


def hiss(seconds: float, amplitude: int = 30, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.integers(-amplitude, amplitude, int(seconds * RATE)).astype(np.int16)


def recorder(**overrides) -> tuple[Recorder, Factory]:
    factory = Factory()
    cfg = Config(ptt=overrides.pop("ptt", PTT()), stt=overrides.pop("stt", STT()))
    return Recorder(cfg, stream_factory=factory, **overrides), factory


@pytest.fixture(autouse=True)
def no_wpctl(monkeypatch):
    def refuse(*args):
        raise AssertionError(f"wpctl {args} was called by a test that did not expect it")

    monkeypatch.setattr(audio, "_wpctl", refuse)


# --------------------------------------------------------------------------- the stream


def test_start_opens_a_16_khz_mono_int16_stream_with_20_ms_blocks():
    rec, factory = recorder()
    rec.start()
    kwargs = factory.stream.kwargs
    assert (kwargs["samplerate"], kwargs["channels"], kwargs["dtype"]) == (16000, 1, "int16")
    assert kwargs["blocksize"] == 320
    assert kwargs["device"] is None
    assert factory.stream.active


@pytest.mark.parametrize(("setting", "device"), [("", None), ("3", 3), ("USB Mic", "USB Mic")])
def test_the_configured_device_is_an_index_a_name_or_the_default(setting, device):
    rec, factory = recorder(stt=STT(device=setting))
    rec.start()
    assert factory.stream.kwargs["device"] == device


def test_stop_returns_exactly_what_was_fed_and_releases_the_device():
    rec, factory = recorder()
    rec.start()
    samples = tone(0.5)
    factory.stream.feed(samples)
    assert rec.seconds() == pytest.approx(0.5)
    assert rec.stop() == samples.tobytes()
    assert factory.stream.closed
    assert not rec.recording


def test_stop_without_a_start_returns_nothing():
    rec, _ = recorder()
    assert rec.stop() == b""


def test_a_second_hold_does_not_contain_the_first():
    rec, factory = recorder()
    rec.start()
    factory.stream.feed(tone(0.2))
    rec.stop()
    rec.start()
    second = hiss(0.1)
    factory.stream.feed(second)
    assert rec.stop() == second.tobytes()
    assert len(factory.streams) == 2


def test_snapshot_reads_the_clip_so_far_without_stopping():
    rec, factory = recorder()
    rec.start()
    factory.stream.feed(tone(0.1))
    assert rec.snapshot() == tone(0.1).tobytes()
    assert rec.recording


# --------------------------------------------------------------------------- pre-roll


def test_preroll_keeps_the_device_open_and_prepends_the_last_400_ms():
    rec, factory = recorder(stt=STT(preroll=True))
    rec.open()
    before = np.concatenate([hiss(0.3, seed=2), tone(0.4, 0.1)])
    factory.stream.feed(before)
    rec.start()
    spoken = tone(0.2)
    factory.stream.feed(spoken)
    pcm = rec.stop()
    assert pcm == before[-int(0.4 * RATE) :].tobytes() + spoken.tobytes()
    assert not factory.stream.closed
    assert len(factory.streams) == 1


def test_a_quick_second_hold_is_not_handed_preroll_from_before_the_first():
    rec, factory = recorder(stt=STT(preroll=True))
    rec.open()
    factory.stream.feed(tone(0.4, 0.1))
    rec.start()
    rec.stop()
    between = hiss(0.1)
    factory.stream.feed(between)
    rec.start()
    assert rec.stop() == between.tobytes()


def test_preroll_reopens_a_stream_that_died():
    rec, factory = recorder(stt=STT(preroll=True))
    rec.open()
    factory.stream.active = False
    rec.start()
    assert len(factory.streams) == 2
    assert factory.streams[0].closed


# --------------------------------------------------------------------------- level


def test_level_is_zero_before_recording_and_for_digital_silence():
    rec, factory = recorder()
    assert rec.level() == 0.0
    rec.start()
    factory.stream.feed(np.zeros(RATE // 10, dtype=np.int16))
    assert rec.level() == 0.0


def test_level_rises_with_loudness_and_stays_within_zero_and_one():
    levels = []
    for amplitude in (0.001, 0.03, 0.3, 1.0):
        rec, factory = recorder()
        rec.start()
        factory.stream.feed(tone(0.2, amplitude))
        levels.append(rec.level())
    assert levels == sorted(levels)
    assert all(0.0 <= level <= 1.0 for level in levels)
    assert levels[0] < 0.1
    assert 0.4 < levels[1] < 0.6  # about -33 dBFS RMS: ordinary speech sits mid meter
    assert levels[-1] > 0.9


def test_level_follows_the_recent_audio_not_the_whole_clip():
    rec, factory = recorder()
    rec.start()
    factory.stream.feed(tone(0.5))
    loud = rec.level()
    factory.stream.feed(hiss(0.2))
    assert rec.level() < loud / 2


# --------------------------------------------------------------------------- max hold


def test_buffering_stops_at_max_hold_and_the_overrun_callback_fires_once():
    fired = []
    rec, factory = recorder(ptt=PTT(max_hold_s=0.1), on_overrun=lambda: fired.append(True))
    rec.start()
    samples = tone(0.3)
    factory.stream.feed(samples)
    assert fired == [True]
    assert rec.seconds() == pytest.approx(0.1)
    assert rec.stop() == samples[: int(0.1 * RATE)].tobytes()


def test_the_overrun_is_per_hold():
    fired = []
    rec, factory = recorder(ptt=PTT(max_hold_s=0.1), on_overrun=lambda: fired.append(True))
    for _ in range(2):
        rec.start()
        factory.stream.feed(tone(0.2))
        rec.stop()
    assert fired == [True, True]


# --------------------------------------------------------------------------- abort


def test_abort_overwrites_the_buffer_and_releases_the_device():
    rec, factory = recorder()
    rec.start()
    factory.stream.feed(tone(0.5))
    held = rec._buf
    assert any(held)
    rec.abort()
    assert not any(held)
    assert rec._buf is held  # the same memory was overwritten, not swapped for a clean one
    assert rec.seconds() == 0.0
    assert rec.level() == 0.0
    assert rec.stop() == b""
    assert factory.stream.closed


def test_abort_releases_the_microphone_even_in_preroll_mode_and_wipes_the_preroll():
    rec, factory = recorder(stt=STT(preroll=True))
    rec.open()
    factory.stream.feed(tone(0.4))
    rec.abort()
    assert factory.stream.closed
    assert not any(rec._ring)
    rec.start()
    assert rec.stop() == b""


def test_stop_also_overwrites_the_recorders_own_copy():
    rec, factory = recorder()
    rec.start()
    factory.stream.feed(tone(0.2))
    rec.stop()
    assert not any(rec._buf)


# --------------------------------------------------------------------------- energy gate


def test_trailing_silence_is_seen_after_speech_then_silence():
    rec, factory = recorder()
    rec.start()
    factory.stream.feed(hiss(0.2))
    factory.stream.feed(tone(0.6))
    assert not rec.trailing_silence(150)
    factory.stream.feed(hiss(0.1, seed=3))
    assert not rec.trailing_silence(150)  # only 100 ms so far
    factory.stream.feed(hiss(0.06, seed=4))
    assert rec.trailing_silence(150)


def test_silence_before_any_speech_is_not_trailing_silence():
    rec, factory = recorder()
    rec.start()
    factory.stream.feed(hiss(1.0))
    assert not rec.trailing_silence(150)


def test_a_key_click_is_not_speech():
    rec, factory = recorder()
    rec.start()
    factory.stream.feed(np.concatenate([tone(0.04, 0.5), hiss(0.5)]))
    assert not rec.trailing_silence(150)


def test_speech_that_starts_on_the_first_frame_is_still_heard():
    rec, factory = recorder()
    rec.start()
    factory.stream.feed(np.concatenate([tone(0.5), hiss(0.2)]))
    assert rec.trailing_silence(150)


def test_the_gate_adapts_to_a_noisy_room():
    rec, factory = recorder()
    rec.start()
    # a fan at about -37 dBFS, well above the absolute gate: still not speech
    factory.stream.feed(hiss(1.0, amplitude=800))
    assert not rec.trailing_silence(150)
    factory.stream.feed(tone(0.5))
    factory.stream.feed(hiss(0.2, amplitude=800, seed=5))
    assert rec.trailing_silence(150)


def test_voiced_after_tells_whether_speech_followed_a_snapshot():
    rec, factory = recorder()
    rec.start()
    factory.stream.feed(np.concatenate([tone(0.4), hiss(0.2)]))
    mark = rec.seconds()
    factory.stream.feed(hiss(0.2, seed=6))
    assert not rec.voiced_after(mark)
    factory.stream.feed(tone(0.2))
    assert rec.voiced_after(mark)


def test_blocks_of_an_odd_size_are_framed_correctly():
    rec, factory = recorder()
    rec.start()
    samples = np.concatenate([tone(0.5), hiss(0.2)])
    for at in range(0, len(samples), 500):
        block = samples[at : at + 500]
        factory.stream.callback(block.tobytes(), len(block), None, None)
    assert rec.trailing_silence(150)
    assert rec.stop() == samples.tobytes()


# --------------------------------------------------------------------------- errors


def test_a_portaudio_error_becomes_a_plain_audio_error(monkeypatch):
    fake = types.ModuleType("sounddevice")

    class PortAudioError(Exception):
        pass

    fake.PortAudioError = PortAudioError
    monkeypatch.setitem(sys.modules, "sounddevice", fake)

    def broken(**kwargs):
        raise PortAudioError("Error opening RawInputStream: Device unavailable", -9985)

    rec = Recorder(Config(), stream_factory=broken)
    with pytest.raises(AudioError, match="cannot open the microphone: Error opening") as caught:
        rec.start()
    assert "-9985" not in str(caught.value)
    assert not rec.recording


def test_an_unknown_device_name_becomes_an_audio_error_that_names_it():
    def broken(**kwargs):
        raise ValueError("No input device matching 'Nope'")

    rec = Recorder(Config(stt=STT(device="Nope")), stream_factory=broken)
    with pytest.raises(AudioError, match="'Nope'"):
        rec.start()


def test_a_stream_that_fails_to_start_is_closed():
    factory = Factory()

    def failing(**kwargs):
        stream = factory(**kwargs)
        stream.start = lambda: (_ for _ in ()).throw(OSError("device busy"))
        return stream

    rec = Recorder(Config(), stream_factory=failing)
    with pytest.raises(AudioError, match="device busy"):
        rec.start()
    assert factory.stream.closed


def test_an_unrelated_bug_in_the_factory_is_not_disguised_as_an_audio_error():
    def buggy(**kwargs):
        raise KeyError("oops")

    with pytest.raises(KeyError):
        Recorder(Config(), stream_factory=buggy).start()


# --------------------------------------------------------------------------- ducking


def wpctl_recording(monkeypatch, volume_line: str) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    def fake(*args):
        calls.append(args)
        return volume_line if args[0] == "get-volume" else ""

    monkeypatch.setattr(audio, "_wpctl", fake)
    return calls


def test_ducking_lowers_the_sink_while_held_and_restores_it(monkeypatch):
    calls = wpctl_recording(monkeypatch, "Volume: 0.50\n")
    rec, _ = recorder(stt=STT(duck_volume=True))
    rec.start()
    assert calls[-1] == ("set-volume", "@DEFAULT_AUDIO_SINK@", "0.15")
    rec.stop()
    assert calls[-1] == ("set-volume", "@DEFAULT_AUDIO_SINK@", "0.50")
    assert len(calls) == 3


def test_abort_restores_the_volume_too(monkeypatch):
    calls = wpctl_recording(monkeypatch, "Volume: 0.80\n")
    rec, _ = recorder(stt=STT(duck_volume=True))
    rec.start()
    rec.abort()
    assert calls[-1] == ("set-volume", "@DEFAULT_AUDIO_SINK@", "0.80")


def test_a_muted_sink_is_left_alone(monkeypatch):
    calls = wpctl_recording(monkeypatch, "Volume: 0.50 [MUTED]\n")
    rec, _ = recorder(stt=STT(duck_volume=True))
    rec.start()
    rec.stop()
    assert calls == [("get-volume", "@DEFAULT_AUDIO_SINK@")]


def test_a_missing_wpctl_does_not_break_recording(monkeypatch):
    monkeypatch.setattr(audio, "_wpctl", lambda *args: None)
    rec, factory = recorder(stt=STT(duck_volume=True))
    rec.start()
    factory.stream.feed(tone(0.1))
    assert len(rec.stop()) == int(0.1 * RATE) * 2
