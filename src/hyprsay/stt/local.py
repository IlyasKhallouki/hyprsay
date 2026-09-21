"""Speech to text on this machine with sherpa-onnx. No audio leaves it.

Measured on the idle development machine (i5-8350U, no GPU), eight command clips of
about one second, one thread: Moonshine Tiny 30 ms p50, Parakeet TDT 110M 93 ms. That is
15 to 100 times faster than any gateway model. The price is accuracy on bad audio: on a
simulated poor laptop microphone local fell to 6 or 7 of 8 where the cloud held 8 of 8,
mostly with slips the normalizer repairs ("workspace too", "work space three").

Streaming recognition was measured and is not viable on this CPU, so the clip is
buffered and decoded once. Loading the model takes about 2 s and happens once, off the
event loop. One thread on purpose: a command clip is short, and more threads only
compete with the compositor for the same four cores.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from ..model import Transcript
from . import models
from .models import ModelError, SttError


def pcm_to_float32(pcm: bytes) -> np.ndarray:
    """Signed 16 bit little endian PCM to the -1..1 float32 that sherpa-onnx expects."""
    usable = len(pcm) - len(pcm) % 2
    return np.frombuffer(pcm[:usable], dtype="<i2").astype(np.float32) / 32768.0


class LocalRecognizer:
    def __init__(self, name: str = "parakeet-110m", *, root: Path | None = None) -> None:
        self.spec = models.get(name)
        self.name = f"local:{name}"
        self._root = root
        self._recognizer: Any = None
        self._load_lock = threading.Lock()
        # decodes are serialized: with one compute thread, two at once only slow each other
        self._decode_lock = threading.Lock()

    def __repr__(self) -> str:
        return f"LocalRecognizer({self.spec.name!r}, loaded={self._recognizer is not None})"

    @property
    def loaded(self) -> bool:
        return self._recognizer is not None

    def load(self) -> None:
        """Blocking, about 2 s, idempotent. Never downloads: a missing model is an error."""
        with self._load_lock:
            if self._recognizer is None:
                models.ensure(self.spec.name, root=self._root, download=False)
                self._recognizer = _build(self.spec, models.paths(self.spec.name, self._root))

    async def warm(self) -> None:
        await asyncio.to_thread(self.load)

    async def transcribe(self, pcm: bytes, sample_rate: int) -> Transcript:
        if len(pcm) < 2:
            return Transcript(text="", backend=self.name)
        started = time.perf_counter()
        text = await asyncio.to_thread(self._decode, pcm, sample_rate)
        ms = (time.perf_counter() - started) * 1000
        return Transcript(text=text, backend=self.name, ms=ms)

    def _decode(self, pcm: bytes, sample_rate: int) -> str:
        self.load()
        samples = pcm_to_float32(pcm)
        with self._decode_lock:
            stream = self._recognizer.create_stream()
            stream.accept_waveform(sample_rate, samples)
            self._recognizer.decode_stream(stream)
            return stream.result.text.strip()


def _build(spec: models.ModelSpec, files: dict[str, str]) -> Any:
    try:
        import sherpa_onnx
    except ImportError as exc:
        raise SttError("sherpa-onnx is not installed; local speech to text needs it") from exc
    try:
        if spec.kind == "moonshine_v2":
            return sherpa_onnx.OfflineRecognizer.from_moonshine_v2(**files, num_threads=1)
        if spec.kind == "nemo_transducer":
            return sherpa_onnx.OfflineRecognizer.from_transducer(
                **files, num_threads=1, model_type="nemo_transducer"
            )
    except (RuntimeError, ValueError) as exc:
        raise ModelError(f"could not load model {spec.name!r}: {exc}") from exc
    raise ModelError(f"model {spec.name!r} has unknown kind {spec.kind!r}")
