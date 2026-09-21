"""Speech to text through the Vercel AI Gateway batch route.

Measured from the development machine on eight command clips of about one second,
n=24 per model, p50 from request to transcript: fish-audio/transcribe-1 452 ms,
gpt-4o-mini-transcribe 697, grok-stt 699, gpt-4o-transcribe 715, whisper-1 1164 (worst
6121), gemini-3.5-transcribe 3180. On a simulated bad laptop microphone the cloud models
held 8 of 8 where the local models fell to 6 or 7. So the cloud is the accurate path and
the slow one: fish is the default, and Gemini works but a command waits three seconds.

The wire shape is read from the published `@ai-sdk/gateway` source
(docs/research/gateway-stt.md 1.1): a JSON POST, not multipart, with the model chosen by
header. The audio is a complete WAV file in base64.

Policy mirrors the Jev client: one persistent connection opened ahead of need, a hard
deadline around everything including the retry, one immediate retry on 5xx or a dropped
connection, never on 4xx. The key lives only in the client's headers; every message
built here is scrubbed of it before it is raised.
"""

from __future__ import annotations

import asyncio
import base64
import io
import time
import wave
from typing import Any

import httpx

from ..model import Transcript
from .models import SttError

GATEWAY = "https://ai-gateway.vercel.sh"
PATH = "/v4/ai/transcription-model"
# answers a GET without credentials, so it opens TCP, TLS and HTTP/2 for free
WARM_PATH = "/typesafe/v1/models"
TIMEOUT_S = 4.0

MEASURED_P50_MS: dict[str, int] = {
    "fish-audio/transcribe-1": 452,
    "openai/gpt-4o-mini-transcribe": 697,
    "spacexai/grok-stt": 699,
    "openai/gpt-4o-transcribe": 715,
    "openai/whisper-1": 1164,
    "google/gemini-3.5-transcribe": 3180,
}

# what people type in a config file, mapped to the creator/model id the gateway wants
ALIASES: dict[str, str] = {
    "fish": "fish-audio/transcribe-1",
    "transcribe-1": "fish-audio/transcribe-1",
    "whisper": "openai/whisper-1",
    "whisper-1": "openai/whisper-1",
    "gpt-4o-mini-transcribe": "openai/gpt-4o-mini-transcribe",
    "gpt-4o-transcribe": "openai/gpt-4o-transcribe",
    "grok": "spacexai/grok-stt",
    "grok-stt": "spacexai/grok-stt",
    "gemini": "google/gemini-3.5-transcribe",
    "gemini-3.5-transcribe": "google/gemini-3.5-transcribe",
}


class SttAuthError(SttError):
    """401 or 403. The key is missing, wrong, or rotated."""


class SttUnavailable(SttError):
    """5xx or a transport failure. The one class that earns a retry."""


class SttTimeout(SttError):
    """No transcript inside the hard timeout."""


def resolve_model(model_id: str) -> str:
    return ALIASES.get(model_id.strip().lower(), model_id.strip())


def timeout_for(model_id: str) -> float:
    """4 s, stretched for a model whose measured median would not fit inside it.

    Gemini's median is 3.2 s; under a 4 s cut a large share of its good answers would be
    thrown away, which would make choosing it in the config a trap.
    """
    p50 = MEASURED_P50_MS.get(resolve_model(model_id))
    return max(TIMEOUT_S, 2.5 * p50 / 1000) if p50 else TIMEOUT_S


def wav_bytes(pcm: bytes, sample_rate: int) -> bytes:
    """Wrap signed 16 bit mono PCM in a WAV container."""
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return out.getvalue()


class GatewayRecognizer:
    def __init__(
        self,
        model_id: str,
        key: str,
        *,
        base_url: str = GATEWAY,
        timeout: float | None = None,
        zero_data_retention: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not key:
            raise SttAuthError("empty gateway key")
        self.model_id = resolve_model(model_id)
        self.name = f"gateway:{self.model_id}"
        self.timeout = timeout_for(self.model_id) if timeout is None else timeout
        self.zero_data_retention = zero_data_retention
        self._key = key
        options: dict[str, Any] = {
            "base_url": base_url,
            "headers": {
                "Authorization": f"Bearer {key}",
                "ai-gateway-protocol-version": "0.0.1",
                "ai-transcription-model-specification-version": "4",
                "ai-model-id": self.model_id,
            },
            # the hard timeout is enforced around both attempts; this only stops one
            # dead socket from outliving it
            "timeout": httpx.Timeout(self.timeout, connect=min(self.timeout, 2.0)),
            "transport": transport,
        }
        try:
            self._http = httpx.AsyncClient(http2=True, **options)
            self.http2 = True
        except ImportError:
            # h2 is an optional extra; HTTP/1.1 keepalive still reuses the connection
            self._http = httpx.AsyncClient(**options)
            self.http2 = False

    def __repr__(self) -> str:
        return f"GatewayRecognizer(model_id={self.model_id!r}, timeout={self.timeout})"

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> GatewayRecognizer:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def warm(self) -> bool:
        """Open the connection on key down, so the clip does not pay for the handshake."""
        try:
            await self._http.get(WARM_PATH, timeout=2.0)
        except httpx.HTTPError:
            return False
        return True

    async def transcribe(self, pcm: bytes, sample_rate: int) -> Transcript:
        if len(pcm) < 2:
            return Transcript(text="", backend=self.name)
        body: dict[str, Any] = {
            "audio": base64.b64encode(wav_bytes(pcm, sample_rate)).decode("ascii"),
            "mediaType": "audio/wav",
        }
        if self.zero_data_retention:
            body["providerOptions"] = {"gateway": {"zeroDataRetention": True}}
        started = time.perf_counter()
        try:
            async with asyncio.timeout(self.timeout):
                text = await self._with_retry(body)
        except TimeoutError as exc:
            raise SttTimeout(f"{self.name}: no transcript within {self.timeout:.1f} s") from exc
        ms = (time.perf_counter() - started) * 1000
        return Transcript(text=text, backend=self.name, ms=ms)

    async def _with_retry(self, body: dict[str, Any]) -> str:
        try:
            return await self._once(body)
        except SttUnavailable:
            return await self._once(body)

    async def _once(self, body: dict[str, Any]) -> str:
        try:
            response = await self._http.post(PATH, json=body)
        except httpx.TransportError as exc:
            raise SttUnavailable(f"{self.name}: transport: {type(exc).__name__}") from exc
        status = response.status_code
        if status == 200:
            return self._text(response)
        detail = self._scrub(response.text[:200])
        if status in (401, 403):
            raise SttAuthError(f"{self.name}: the gateway rejected the key (HTTP {status})")
        if status >= 500:
            raise SttUnavailable(f"{self.name}: HTTP {status}: {detail}")
        raise SttError(f"{self.name}: HTTP {status}: {detail}")

    def _text(self, response: httpx.Response) -> str:
        try:
            text = response.json()["text"]
        except (ValueError, KeyError, TypeError) as exc:
            raise SttError(f"{self.name}: a 200 without a transcript") from exc
        if not isinstance(text, str):
            raise SttError(f"{self.name}: a 200 without a transcript")
        return text.strip()

    def _scrub(self, message: str) -> str:
        return message.replace(self._key, "[key]")
