"""Hearing the sentence while it is still being said.

Two ways to get a transcript out of a buffer that is still growing, measured on this
machine (i5-8350U, no GPU, one thread, load average 3.7 to 5.2, a 7.4 s clip of real
speech, sherpa-onnx 1.12):

- **Re-decode the whole buffer** with the offline Parakeet recognizer the engine already
  uses. Cost per decode, p50 of 7 runs: 60 ms at 0.5 s of audio, 89 ms at 1 s, 159 ms at
  2 s, 271 ms at 3 s, 335 ms at 4 s, 360 ms at 5 s, 623 ms at 7 s. That is linear in the
  LENGTH OF THE BUFFER, not in the new audio, so a 400 ms chunk interval stops keeping up
  at about four seconds of speech and past that one core decodes continuously while the
  compositor wants it. Four seconds is not a long sentence; "open chrome, go to youtube
  and look up ltt" is longer than that.

- **A genuine online recognizer**: `sherpa_onnx.OnlineRecognizer` over a streaming
  zipformer, whose encoder is causal and keeps its state between chunks. Same clip, same
  machine, 400 ms chunks: p50 1.2 ms per chunk and 90 ms at the worst chunk, because the
  model only runs its encoder when it has a whole 640 ms window and does nothing the rest
  of the time. Flat in the length of the buffer. Nineteen chunks of a 7.4 s utterance cost
  399 ms of CPU in total, against 623 ms for ONE re-decode of the same buffer at its end.

The cheap one is not the default, and the reason is the second measurement, taken by
driving four synthesized commands through both paths against the live desktop:

    the online recognizer emits its words about 1.0 to 1.2 s behind the speaker.

At t = 2.0 s of "open spotify and turn it down" it still read "Open Spotif"; at t = 2.4 s
of "open chrome on workspace three" it still read "Open Chrome", unchanged across four
decodes, and the settle rule focused Chrome instead of launching it onto workspace 3.
Cheap partials that describe a sentence the speaker finished a second ago are worse than
no partials, because the settle rule cannot tell a lagging decoder from a speaker who has
stopped. The re-decode has no lag at all: its partials say what has been said, in the
words the final decode will use, so the spans are exact and there is no drift. On the same
four clips it acted on "open spotify" 1.3 s before the key came up and refused to act
early on "open chrome" at all, which is the whole point.

So `stt.stream_model` is "" by default and the re-decode is what runs. The online path is
there for the utterances the re-decode cannot keep up with, past about four seconds, and
it is a real trade rather than an upgrade.

**The final decode is never one of these.** On key up the engine decodes the buffer once
with the recognizer it always used, so the decision that runs the rest of the utterance
has exactly the accuracy it had before this file existed. The partials only ever bring an
action FORWARD, and `incremental.py` decides which ones are allowed to move.

The price of that split is that the words a clause was acted on under may not be the words
the final decode produces, because two models heard the same audio. `incremental.Settler`
counts clauses rather than trusting text for exactly this reason, and reports the
disagreement as drift.

One session per hold. The online stream carries the encoder state of one utterance and
mixing two of them into one stream would let the tail of a command the speaker already
released steer the one they are saying now.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any, Protocol

from ..config import Config
from ..model import Recognizer
from . import models
from .local import pcm_to_float32
from .models import ModelError, SttError

log = logging.getLogger("hyprsay")

RATE = 16000


class Session(Protocol):
    """One push-to-talk hold's worth of incremental decoding."""

    async def hear(self, pcm: bytes, sample_rate: int = RATE) -> str: ...

    def close(self) -> None: ...


class Partials(Protocol):
    """What the engine needs to decode a hold as it happens."""

    name: str

    async def warm(self) -> None: ...

    def open(self) -> Session: ...


# --------------------------------------------------------------------- the online model


class StreamingPartials:
    """A real sherpa-onnx online recognizer: the encoder state survives between chunks."""

    def __init__(self, name: str = "kroko-en-streaming", *, root: Path | None = None) -> None:
        self.spec = models.get(name)
        self.name = f"stream:{name}"
        self._root = root
        self._recognizer: Any = None
        self._load_lock = threading.Lock()

    def __repr__(self) -> str:
        return f"StreamingPartials({self.spec.name!r}, loaded={self._recognizer is not None})"

    @property
    def loaded(self) -> bool:
        return self._recognizer is not None

    def load(self) -> None:
        """Blocking, 5.7 s measured on this machine, idempotent. Never downloads.

        Five and a half seconds is why this is warmed at startup beside the offline model
        and not on the first key press: a lazy load here would swallow the first thing
        anyone said, which is the bug `daemon._warm_speech` was written for.
        """
        with self._load_lock:
            if self._recognizer is None:
                models.ensure(self.spec.name, root=self._root, download=False)
                self._recognizer = _build(self.spec, models.paths(self.spec.name, self._root))

    async def warm(self) -> None:
        await asyncio.to_thread(self.load)

    def open(self) -> Session:
        return _OnlineSession(self)


class _OnlineSession:
    def __init__(self, owner: StreamingPartials) -> None:
        self._owner = owner
        self._stream: Any = None
        self._fed = 0  # bytes of the hold already handed to the encoder

    async def hear(self, pcm: bytes, sample_rate: int = RATE) -> str:
        return await asyncio.to_thread(self._decode, pcm, sample_rate)

    def _decode(self, pcm: bytes, sample_rate: int) -> str:
        self._owner.load()
        recognizer = self._owner._recognizer
        if self._stream is None:
            self._stream = recognizer.create_stream()
        # only what has arrived since the last call: feeding the whole snapshot again
        # would replay the utterance into an encoder that still remembers it, and the
        # transcript would come back with every word twice
        fresh, self._fed = pcm[self._fed :], len(pcm)
        if not fresh:
            return recognizer.get_result(self._stream).strip()
        self._stream.accept_waveform(sample_rate, pcm_to_float32(fresh))
        while recognizer.is_ready(self._stream):
            recognizer.decode_stream(self._stream)
        return recognizer.get_result(self._stream).strip()

    def close(self) -> None:
        # dropped rather than reset: the encoder state belongs to one utterance, and a
        # stream kept across holds would let a released command steer the next one
        self._stream = None
        self._fed = 0


def _build(spec: models.ModelSpec, files: dict[str, str]) -> Any:
    try:
        import sherpa_onnx
    except ImportError as exc:
        raise SttError("sherpa-onnx is not installed; incremental speech needs it") from exc
    if spec.kind != "streaming_transducer":
        raise ModelError(f"model {spec.name!r} is not a streaming model ({spec.kind!r})")
    try:
        return sherpa_onnx.OnlineRecognizer.from_transducer(**files, num_threads=1, provider="cpu")
    except (RuntimeError, ValueError) as exc:
        raise ModelError(f"could not load streaming model {spec.name!r}: {exc}") from exc


# ------------------------------------------------------------------------ the fallback


class RedecodePartials:
    """The offline recognizer, run again over the whole buffer every chunk.

    Needs no second model and no second download, and gives partials in exactly the words
    the final decode will use, which is the one thing the online path cannot promise. It
    pays for that linearly: see the table in the module docstring. Long utterances, which
    are the ones worth acting on early, are where it stops keeping up.
    """

    def __init__(self, recognizer: Recognizer) -> None:
        self.recognizer = recognizer
        self.name = f"redecode:{recognizer.name}"

    def __repr__(self) -> str:
        return f"RedecodePartials({self.recognizer.name!r})"

    async def warm(self) -> None:
        warm = getattr(self.recognizer, "warm", None)
        if warm is not None:
            await warm()

    def open(self) -> Session:
        return _RedecodeSession(self.recognizer)


class _RedecodeSession:
    def __init__(self, recognizer: Recognizer) -> None:
        self._recognizer = recognizer

    async def hear(self, pcm: bytes, sample_rate: int = RATE) -> str:
        transcript = await self._recognizer.transcribe(pcm, sample_rate)
        return transcript.text.strip()

    def close(self) -> None:
        return None


# --------------------------------------------------------------------------- the choice


# the recognizer backends whose `transcribe` stays on this machine. "hybrid" qualifies:
# its transcribe is local only and the cloud is a separate `rescue` call the daemon makes
# once, on the whole clip (stt/hybrid.py).
_LOCAL_BACKENDS = ("local:", "hybrid:")


def make_partials(cfg: Config, recognizer: Recognizer) -> Partials | None:
    """What decodes this hold as it happens, or None when nothing should.

    None means the engine behaves exactly as it did before this feature: buffer, release,
    decode once. That is what `stt.incremental = false` asks for, and it is what a machine
    with no local recognizer gets, because the only thing left to re-decode with would be
    the gateway.

    That last one is a refusal and not a degradation. `stt.backend = "cloud"` is someone
    choosing to send one clip per utterance; re-decoding a growing buffer through it would
    quietly turn that into eight uploads of the same sentence, each one longer than the
    last, at 452 ms and a charge apiece. Incremental decoding is a local feature.
    """
    if not cfg.stt.incremental:
        return None
    wanted = cfg.stt.stream_model
    if wanted and models.installed(wanted):
        return StreamingPartials(wanted)
    if wanted:
        # not an error: the streaming model is an extra download under a licence the other
        # two do not carry, so an absent one degrades instead of refusing to start
        log.info("streaming model %r is not installed; partials re-decode the buffer", wanted)
    if not recognizer.name.startswith(_LOCAL_BACKENDS):
        log.info(
            "stt.backend %r sends audio off the machine; nothing decodes during the hold",
            recognizer.name,
        )
        return None
    return RedecodePartials(recognizer)
