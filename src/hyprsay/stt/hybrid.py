"""Local first, the cloud only as a rescue.

The two backends fail in opposite ways. Local answers in 30 to 93 ms but dropped to 6 or
7 of 8 on degraded audio; the gateway held 8 of 8 but costs 452 ms at best and sends the
clip off the machine. So `transcribe` is always local, and the cloud is asked only when
the daemon reports that the local transcript led to no confident decision. Most commands
never leave the machine, and the ones that would have failed get a second hearing.

The recognizer cannot know whether a transcript was good enough; only the pipeline after
it can. That is why `rescue` is a separate call the daemon makes, not a fallback hidden
inside `transcribe`.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import httpx

from ..config import Config
from ..model import Recognizer, Transcript
from .gateway import GatewayRecognizer
from .local import LocalRecognizer
from .models import SttError


class HybridRecognizer:
    def __init__(self, local: Recognizer, cloud: Recognizer) -> None:
        self.local = local
        self.cloud = cloud
        self.name = f"hybrid:{local.name}+{cloud.name}"

    async def transcribe(self, pcm: bytes, sample_rate: int) -> Transcript:
        return await self.local.transcribe(pcm, sample_rate)

    async def rescue(self, pcm: bytes, sample_rate: int) -> Transcript:
        """The same buffered clip, heard by the cloud model. Raises SttError on failure."""
        transcript = await self.cloud.transcribe(pcm, sample_rate)
        return replace(transcript, rescued=True)

    async def warm(self) -> None:
        """On key down: load the local model and open the gateway connection together."""
        warmers = [_method(recognizer, "warm") for recognizer in (self.local, self.cloud)]
        await asyncio.gather(*(warm() for warm in warmers if warm))

    async def aclose(self) -> None:
        for recognizer in (self.local, self.cloud):
            if close := _method(recognizer, "aclose"):
                await close()


def _method(recognizer: Recognizer, name: str):
    method = getattr(recognizer, name, None)
    return method if callable(method) else None


def make_recognizer(
    cfg: Config, key: str | None, *, transport: httpx.AsyncBaseTransport | None = None
) -> Recognizer:
    """The recognizer `cfg.stt.backend` asks for.

    "local" never needs a key. "cloud" cannot work without one and says so. "hybrid"
    without a key degrades to local alone, so a machine with no gateway account still
    works; the daemon can tell by the absence of `rescue`.
    """
    backend = cfg.stt.backend
    if backend == "local":
        return LocalRecognizer(cfg.stt.local_model)
    if backend == "cloud":
        if not key:
            raise SttError('stt.backend = "cloud" needs a gateway key')
        return _cloud(cfg, key, transport)
    if backend == "hybrid":
        local = LocalRecognizer(cfg.stt.local_model)
        return HybridRecognizer(local, _cloud(cfg, key, transport)) if key else local
    raise SttError(f"unknown stt.backend {backend!r}")


def _cloud(cfg: Config, key: str, transport: httpx.AsyncBaseTransport | None) -> GatewayRecognizer:
    # jev.zero_data_retention is deliberately not forwarded. Measured 2026-09-21: with it
    # set, the gateway answers fish-audio/transcribe-1 with HTTP 400 "No ZDR providers",
    # so the default cloud model would fail on every call. Audio retention needs its own
    # [stt] setting and a model that has a ZDR provider; until then it is off.
    return GatewayRecognizer(cfg.stt.cloud_model, key, transport=transport)
