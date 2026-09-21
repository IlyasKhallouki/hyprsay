"""Microphone capture for one push-to-talk hold: 16 kHz mono int16, buffered, decoded once.

Streaming recognition measured as not viable on this CPU, so the recorder's whole job is
to hold the clip until key up (docs/PLAN.md 5.2). Three decisions follow from that:

- The buffer is allocated once, at the size of the longest allowed hold. The PortAudio
  callback therefore never allocates, the hard cap is simply "the buffer is full", and
  `abort` can overwrite every byte that ever held speech, which a growing buffer that
  reallocates cannot promise. The session lock calls `abort`.
- Blocks are 20 ms (320 frames), the same as the energy gate's frames, so the gate sees
  each block once. The gate is RMS against an adaptive noise floor, no model: it only
  has to notice "the speaker stopped" early enough to start decoding before key up. When
  it is wrong in either direction the daemon still finalizes on key up, so every failure
  of the gate costs time and never a command.
- By default the device is opened on key down and released on key up, so the microphone
  indicator is lit only while the key is held. `stt.preroll` keeps the stream open and
  prepends the last 400 ms, for machines where opening the device clips the first word.

`start` blocks while PortAudio opens the device, and `on_overrun` fires on PortAudio's
thread; a caller on an event loop wants `asyncio.to_thread` and `call_soon_threadsafe`.
"""

from __future__ import annotations

import contextlib
import math
import re
import subprocess
import sys
import threading
from collections.abc import Callable
from typing import Any

import numpy as np

from .config import Config

RATE = 16000
FRAME = 320  # samples in 20 ms: the PortAudio blocksize and the energy gate's frame
FRAME_BYTES = FRAME * 2
FRAME_MS = 20.0
PREROLL_S = 0.4

# a frame is voiced when its RMS clears both: about -48 dBFS, and 9.5 dB over the floor
ABS_GATE = 0.004
FLOOR_RATIO = 3.0
# the first frame may already be speech; never let it set a floor above about -40 dBFS
INITIAL_FLOOR_MAX = 0.01
# the key press itself clicks for a frame or two; less voiced audio than this is not speech
MIN_VOICED_FRAMES = 5
LEVEL_FRAMES = 5  # the meter averages the last 100 ms
LEVEL_FLOOR_DB = -60.0

SINK = "@DEFAULT_AUDIO_SINK@"
DUCK_FACTOR = 0.3

StreamFactory = Callable[..., Any]


class AudioError(Exception):
    """The microphone could not be opened. The message is plain enough for the HUD."""


def _default_factory(**kwargs: Any) -> Any:
    # imported late: importing sounddevice initializes PortAudio, which probes every
    # audio device on the machine. Tests and `--help` should not pay for or depend on that.
    import sounddevice

    return sounddevice.RawInputStream(**kwargs)


def _is_device_error(exc: Exception) -> bool:
    # sounddevice raises ValueError for an unknown device name, OSError when the
    # PortAudio library is missing, and PortAudioError for everything PortAudio reports
    if isinstance(exc, OSError | ImportError | ValueError):
        return True
    sounddevice = sys.modules.get("sounddevice")
    return sounddevice is not None and isinstance(exc, sounddevice.PortAudioError)


def _device(setting: str) -> int | str | None:
    setting = setting.strip()
    if not setting:
        return None
    return int(setting) if setting.isdigit() else setting


def _wpctl(*args: str) -> str | None:
    """Output of a wpctl call, or None on any failure. Ducking must never break capture."""
    try:
        done = subprocess.run(
            ["wpctl", *args], capture_output=True, text=True, timeout=1.0, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


class _EnergyGate:
    """Per-frame RMS and a voiced flag, against a noise floor that survives between holds."""

    def __init__(self) -> None:
        self.floor: float | None = None
        self.rms: list[float] = []
        self.voiced: list[bool] = []
        self.voiced_count = 0

    def reset(self) -> None:
        # the floor is kept: the room is as noisy on the next hold as on this one
        self.rms = []
        self.voiced = []
        self.voiced_count = 0

    def feed(self, buf: bytearray, used: int) -> None:
        """Frame whatever whole 20 ms frames of `buf[:used]` have not been seen yet."""
        while (len(self.rms) + 1) * FRAME_BYTES <= used:
            frame = np.frombuffer(buf, dtype="<i2", count=FRAME, offset=len(self.rms) * FRAME_BYTES)
            rms = math.sqrt(float(np.mean(np.square(frame, dtype=np.float64)))) / 32768.0
            self._classify(rms)

    def _classify(self, rms: float) -> None:
        if self.floor is None:
            self.floor = min(rms, INITIAL_FLOOR_MAX)
        voiced = rms > max(ABS_GATE, self.floor * FLOOR_RATIO)
        if rms < self.floor:
            # down quickly, but not in one step: a zero-filled first block is not the room
            self.floor += (rms - self.floor) * 0.3
        elif not voiced:
            self.floor += (rms - self.floor) * 0.05
        else:
            # barely: a long sentence must not teach the floor that speech is noise,
            # yet a fan that spins up has to be absorbed eventually
            self.floor += (rms - self.floor) * 0.001
        self.rms.append(rms)
        self.voiced.append(voiced)
        self.voiced_count += voiced


class Recorder:
    def __init__(
        self,
        cfg: Config,
        *,
        stream_factory: StreamFactory | None = None,
        on_overrun: Callable[[], None] | None = None,
    ) -> None:
        self._cfg = cfg
        self._factory = stream_factory or _default_factory
        # fires once per hold, on the audio thread, when max_hold_s of audio is buffered
        self.on_overrun = on_overrun
        self._preroll = cfg.stt.preroll
        ring_bytes = int(PREROLL_S * RATE) * 2 if self._preroll else 0
        self._capacity = int(cfg.ptt.max_hold_s * RATE) * 2 + ring_bytes
        self._buf = bytearray(self._capacity)
        self._used = 0
        self._ring = bytearray(ring_bytes)
        self._ring_written = 0  # total bytes ever written; the write head is this mod size
        self._gate = _EnergyGate()
        self._lock = threading.Lock()
        self._stream: Any = None
        self._recording = False
        self._overrun = False
        self._ducked_from: float | None = None

    # ----------------------------------------------------------------------- lifecycle

    @property
    def recording(self) -> bool:
        return self._recording

    def open(self) -> None:
        """Open the device ahead of need. Only useful with `stt.preroll`."""
        stream = self._stream
        if stream is not None and not getattr(stream, "active", True):
            self._release()  # the source went away (hot-plug); open it again
        if self._stream is None:
            self._open()

    def start(self) -> None:
        if self._recording:
            return
        with self._lock:
            self._wipe()
            self._take_preroll()
            # set before the device opens, so the very first block is kept
            self._recording = True
        try:
            self.open()
        except AudioError:
            self._recording = False
            raise
        self._duck()

    def stop(self) -> bytes:
        """The PCM of this hold. Releases the device unless pre-roll keeps it open."""
        with self._lock:
            self._recording = False
            pcm = bytes(memoryview(self._buf)[: self._used])
            self._wipe()
        if not self._preroll:
            self._release()
        self._unduck()
        return pcm

    def abort(self) -> None:
        """Discard everything, overwrite it, and let go of the microphone.

        Called when the session locks. The device is released even in pre-roll mode: a
        locked screen must not keep a hot microphone. The next `start` reopens it.
        """
        with self._lock:
            self._recording = False
            self._wipe()
            self._ring[:] = bytes(len(self._ring))
            self._ring_written = 0
        self._release()
        self._unduck()

    close = abort

    # ----------------------------------------------------------------------- readings

    def seconds(self) -> float:
        return self._used / 2 / RATE

    def snapshot(self) -> bytes:
        """The PCM so far, without stopping. For the speculative decode (PLAN 5.2)."""
        with self._lock:
            return bytes(memoryview(self._buf)[: self._used])

    def level(self) -> float:
        """0..1 for the HUD meter: RMS of the last 100 ms on a dB scale, -60 dBFS to 0.

        Linear RMS would leave normal speech (about -30 dBFS) at 3 percent of the meter.
        """
        with self._lock:
            recent = self._gate.rms[-LEVEL_FRAMES:]
        if not recent:
            return 0.0
        rms = math.sqrt(sum(r * r for r in recent) / len(recent))
        if rms <= 0.0:
            return 0.0
        return min(1.0, max(0.0, 1.0 - 20.0 * math.log10(rms) / LEVEL_FLOOR_DB))

    def trailing_silence(self, ms: float) -> bool:
        """True once speech has been heard and the last `ms` of audio is below the gate.

        Silence before any speech does not count: there is nothing to finalize yet.
        """
        need = max(1, math.ceil(ms / FRAME_MS))
        with self._lock:
            if self._gate.voiced_count < MIN_VOICED_FRAMES:
                return False
            tail = self._gate.voiced[-need:]
        return len(tail) == need and not any(tail)

    def voiced_after(self, seconds: float) -> bool:
        """Did any voiced frame arrive at or after this offset into the clip?

        The daemon notes `seconds()` when it snapshots; on key up a False here means the
        speculative transcript still covers everything that was said.
        """
        first = max(0, int(seconds * 1000 / FRAME_MS))
        with self._lock:
            return any(self._gate.voiced[first:])

    # ----------------------------------------------------------------------- internals

    def _on_block(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        data = bytes(indata)
        with self._lock:
            if not self._recording:
                self._ring_write(data)
                return
            if self._overrun:
                return
            take = min(len(data), self._capacity - self._used)
            self._buf[self._used : self._used + take] = data[:take]
            self._used += take
            self._gate.feed(self._buf, self._used)
            self._overrun = self._used >= self._capacity
            fire = self._overrun
        if fire and self.on_overrun is not None:
            self.on_overrun()

    def _ring_write(self, data: bytes) -> None:
        size = len(self._ring)
        if not size:
            return
        data = data[-size:]
        head = self._ring_written % size
        first = min(len(data), size - head)
        self._ring[head : head + first] = data[:first]
        self._ring[: len(data) - first] = data[first:]
        self._ring_written += len(data)

    def _take_preroll(self) -> None:
        size = len(self._ring)
        if not size or not self._ring_written:
            return
        head = self._ring_written % size
        wrapped = self._ring_written >= size
        ordered = self._ring[head:] + self._ring[:head] if wrapped else self._ring[:head]
        self._buf[: len(ordered)] = ordered
        self._used = len(ordered)
        self._gate.feed(self._buf, self._used)
        # emptied, so a quick second hold cannot be handed audio from before the first
        self._ring[:] = bytes(size)
        self._ring_written = 0

    def _wipe(self) -> None:
        self._buf[: self._used] = bytes(self._used)
        self._used = 0
        self._overrun = False
        self._gate.reset()

    def _open(self) -> None:
        stream = None
        try:
            stream = self._factory(
                samplerate=RATE,
                channels=1,
                dtype="int16",
                blocksize=FRAME,
                device=_device(self._cfg.stt.device),
                callback=self._on_block,
            )
            stream.start()
        except Exception as exc:
            if stream is not None:
                with contextlib.suppress(Exception):
                    stream.close()
            if not _is_device_error(exc):
                raise
            which = f" {self._cfg.stt.device!r}" if self._cfg.stt.device else ""
            detail = str(exc.args[0]) if exc.args else type(exc).__name__
            raise AudioError(f"cannot open the microphone{which}: {detail}") from exc
        self._stream = stream

    def _release(self) -> None:
        # never while holding the lock: stop() waits for the callback, which wants the lock
        stream, self._stream = self._stream, None
        if stream is None:
            return
        for step in (stream.stop, stream.close):
            # the device may already be gone; there is nothing useful to do about that
            with contextlib.suppress(Exception):
                step()

    def _duck(self) -> None:
        if not self._cfg.stt.duck_volume or self._ducked_from is not None:
            return
        out = _wpctl("get-volume", SINK) or ""
        match = re.match(r"Volume: ([0-9.]+)", out)
        if not match or "MUTED" in out:
            return
        volume = float(match[1])
        if _wpctl("set-volume", SINK, f"{volume * DUCK_FACTOR:.2f}") is not None:
            self._ducked_from = volume

    def _unduck(self) -> None:
        if self._ducked_from is not None:
            _wpctl("set-volume", SINK, f"{self._ducked_from:.2f}")
            self._ducked_from = None
