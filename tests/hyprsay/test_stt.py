"""Speech to text against fakes. No test here touches the network or, unless marked e2e, a model."""

import asyncio
import base64
import hashlib
import io
import json
import tarfile
import threading
import wave

import httpx
import numpy as np
import pytest

from hyprsay import config as config_module
from hyprsay.config import STT, Config
from hyprsay.model import Transcript
from hyprsay.stt import (
    GatewayRecognizer,
    HybridRecognizer,
    LocalRecognizer,
    ModelError,
    SttAuthError,
    SttError,
    SttTimeout,
    SttUnavailable,
    make_recognizer,
    models,
)
from hyprsay.stt import gateway as gateway_module
from hyprsay.stt import local as local_module

KEY = "test-key-not-real"
PCM = (np.sin(np.arange(8000) / 9.0) * 12000).astype("<i2").tobytes()


def ok(text: str = " focus firefox ") -> httpx.Response:
    return httpx.Response(200, json={"text": text, "durationInSeconds": 0.5})


def gateway(handler, **kwargs) -> GatewayRecognizer:
    kwargs.setdefault("timeout", 1.0)
    return GatewayRecognizer(
        kwargs.pop("model_id", "fish-audio/transcribe-1"),
        KEY,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def transcribe(recognizer, pcm: bytes = PCM) -> Transcript:
    async def run():
        try:
            return await recognizer.transcribe(pcm, 16000)
        finally:
            await recognizer.aclose()

    return asyncio.run(run())


class Sequence:
    """A transport handler that answers from a script and remembers what it was sent."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


# --------------------------------------------------------------------------- gateway wire


def test_the_request_goes_to_the_transcription_route_with_the_exact_headers():
    script = Sequence(ok())
    transcribe(gateway(script))
    request = script.requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://ai-gateway.vercel.sh/v4/ai/transcription-model"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert request.headers["content-type"] == "application/json"
    assert request.headers["ai-gateway-protocol-version"] == "0.0.1"
    assert request.headers["ai-transcription-model-specification-version"] == "4"
    assert request.headers["ai-model-id"] == "fish-audio/transcribe-1"


def test_the_body_is_a_base64_wav_that_decodes_to_the_input_pcm():
    script = Sequence(ok())
    transcribe(gateway(script))
    body = json.loads(script.requests[0].content)
    assert set(body) == {"audio", "mediaType"}
    assert body["mediaType"] == "audio/wav"
    with wave.open(io.BytesIO(base64.b64decode(body["audio"]))) as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
        assert wav.readframes(wav.getnframes()) == PCM


def test_the_transcript_is_stripped_and_names_its_backend():
    result = transcribe(gateway(Sequence(ok())))
    assert result.text == "focus firefox"
    assert result.backend == "gateway:fish-audio/transcribe-1"
    assert result.ms > 0
    assert result.rescued is False


def test_zero_data_retention_is_a_provider_option_only_when_asked_for():
    script = Sequence(ok())
    transcribe(gateway(script, zero_data_retention=True))
    body = json.loads(script.requests[0].content)
    assert body["providerOptions"] == {"gateway": {"zeroDataRetention": True}}


def test_an_empty_clip_is_not_sent():
    script = Sequence()
    assert transcribe(gateway(script), b"").text == ""
    assert script.requests == []


def test_short_model_names_resolve_to_gateway_ids():
    for short, full in [
        ("whisper-1", "openai/whisper-1"),
        ("Gemini-3.5-Transcribe", "google/gemini-3.5-transcribe"),
        ("fish-audio/transcribe-1", "fish-audio/transcribe-1"),
        ("some-vendor/new-model", "some-vendor/new-model"),
    ]:
        assert gateway_module.resolve_model(short) == full
    script = Sequence(ok())
    transcribe(gateway(script, model_id="whisper"))
    assert script.requests[0].headers["ai-model-id"] == "openai/whisper-1"


def test_the_default_timeout_is_four_seconds_and_longer_only_for_a_model_measured_slow():
    assert gateway_module.timeout_for("fish-audio/transcribe-1") == 4.0
    assert gateway_module.timeout_for("openai/whisper-1") == 4.0
    assert gateway_module.timeout_for("unknown/model") == 4.0
    assert gateway_module.timeout_for("gemini") == pytest.approx(7.95)
    assert GatewayRecognizer("fish", KEY).timeout == 4.0


# --------------------------------------------------------------------------- gateway failures


def test_a_503_is_retried_once_and_the_second_answer_wins():
    script = Sequence(httpx.Response(503, text="busy"), ok())
    assert transcribe(gateway(script)).text == "focus firefox"
    assert len(script.requests) == 2


def test_a_dropped_connection_is_retried_once():
    script = Sequence(httpx.ConnectError("reset"), ok())
    assert transcribe(gateway(script)).text == "focus firefox"
    assert len(script.requests) == 2


def test_two_failures_in_a_row_give_up():
    script = Sequence(httpx.Response(503), httpx.Response(502), ok())
    with pytest.raises(SttUnavailable, match="HTTP 502"):
        transcribe(gateway(script))
    assert len(script.requests) == 2


def test_a_400_is_not_retried():
    script = Sequence(httpx.Response(400, json={"error": "unsupported media type"}), ok())
    with pytest.raises(SttError, match="HTTP 400.*unsupported media type") as caught:
        transcribe(gateway(script))
    assert not isinstance(caught.value, SttUnavailable)
    assert len(script.requests) == 1


def test_a_rejected_key_is_an_auth_error_and_is_not_retried():
    script = Sequence(httpx.Response(401, text="bad key"), ok())
    with pytest.raises(SttAuthError, match="HTTP 401"):
        transcribe(gateway(script))
    assert len(script.requests) == 1


def test_the_hard_timeout_covers_a_gateway_that_never_answers():
    async def stall(request):
        await asyncio.sleep(30)
        return ok()

    with pytest.raises(SttTimeout, match="no transcript within 0.1 s"):
        transcribe(gateway(stall, timeout=0.1))


def test_the_hard_timeout_covers_the_retry_as_well():
    calls = []

    async def slow_failure(request):
        calls.append(request)
        await asyncio.sleep(0.08)
        return httpx.Response(503)

    with pytest.raises(SttTimeout):
        transcribe(gateway(slow_failure, timeout=0.12))
    assert len(calls) == 2


def test_a_200_without_text_is_an_error_not_an_empty_transcript():
    for response in (httpx.Response(200, json={"segments": []}), httpx.Response(200, text="<p>")):
        with pytest.raises(SttError, match="without a transcript"):
            transcribe(gateway(Sequence(response)))


def test_the_key_never_appears_in_an_error_or_a_repr():
    echo = httpx.Response(400, text=f"invalid token Bearer {KEY} for this route")
    failures = [
        Sequence(echo),
        Sequence(httpx.Response(500, text=KEY), httpx.Response(500, text=KEY)),
        Sequence(httpx.Response(403, text=KEY)),
        Sequence(httpx.ConnectError(f"failed with {KEY}"), httpx.ConnectError(KEY)),
    ]
    for script in failures:
        with pytest.raises(SttError) as caught:
            transcribe(gateway(script))
        assert KEY not in str(caught.value)
        assert KEY not in repr(caught.value)
    assert KEY not in repr(GatewayRecognizer("fish", KEY))


def test_an_empty_key_is_refused_before_any_request():
    with pytest.raises(SttAuthError):
        GatewayRecognizer("fish", "")


# --------------------------------------------------------------------------- hybrid


class FakeRecognizer:
    def __init__(self, name: str, text: str):
        self.name = name
        self.text = text
        self.calls = 0
        self.warmed = False
        self.closed = False

    async def transcribe(self, pcm: bytes, sample_rate: int) -> Transcript:
        self.calls += 1
        return Transcript(text=self.text, backend=self.name, ms=1.0)

    async def warm(self) -> None:
        self.warmed = True

    async def aclose(self) -> None:
        self.closed = True


def test_hybrid_transcribe_is_local_only():
    local, cloud = FakeRecognizer("local:x", "focus fire fucks"), FakeRecognizer("gateway:y", "")
    result = asyncio.run(HybridRecognizer(local, cloud).transcribe(PCM, 16000))
    assert (result.text, result.backend, result.rescued) == ("focus fire fucks", "local:x", False)
    assert (local.calls, cloud.calls) == (1, 0)


def test_hybrid_rescue_asks_the_cloud_and_marks_the_transcript():
    local, cloud = FakeRecognizer("local:x", ""), FakeRecognizer("gateway:y", "focus firefox")
    result = asyncio.run(HybridRecognizer(local, cloud).rescue(PCM, 16000))
    assert (result.text, result.backend, result.rescued) == ("focus firefox", "gateway:y", True)
    assert (local.calls, cloud.calls) == (0, 1)


def test_hybrid_warms_and_closes_both_sides():
    local, cloud = FakeRecognizer("local:x", ""), FakeRecognizer("gateway:y", "")
    hybrid = HybridRecognizer(local, cloud)

    async def run():
        await hybrid.warm()
        await hybrid.aclose()

    asyncio.run(run())
    assert local.warmed and cloud.warmed and cloud.closed
    assert hybrid.name == "hybrid:local:x+gateway:y"


def test_a_failed_rescue_raises_so_the_daemon_can_fall_back_to_the_local_transcript():
    cloud = gateway(Sequence(httpx.Response(503), httpx.Response(503)))
    hybrid = HybridRecognizer(FakeRecognizer("local:x", "clothes"), cloud)

    async def run():
        try:
            await hybrid.rescue(PCM, 16000)
        finally:
            await hybrid.aclose()

    with pytest.raises(SttUnavailable):
        asyncio.run(run())


def close(recognizer) -> None:
    if hasattr(recognizer, "aclose"):
        asyncio.run(recognizer.aclose())


def test_make_recognizer_local_needs_no_key_and_loads_nothing_yet():
    cfg = Config(stt=STT(backend="local", local_model="moonshine-tiny"))
    recognizer = make_recognizer(cfg, None)
    assert isinstance(recognizer, LocalRecognizer)
    assert recognizer.name == "local:moonshine-tiny"
    assert not recognizer.loaded


def test_make_recognizer_cloud_is_the_configured_gateway_model():
    cfg = Config(stt=STT(backend="cloud", cloud_model="openai/whisper-1"))
    recognizer = make_recognizer(cfg, KEY)
    assert isinstance(recognizer, GatewayRecognizer)
    assert recognizer.name == "gateway:openai/whisper-1"
    close(recognizer)


def test_make_recognizer_cloud_without_a_key_says_so():
    with pytest.raises(SttError, match="needs a gateway key"):
        make_recognizer(Config(stt=STT(backend="cloud")), None)


def test_make_recognizer_hybrid_is_local_with_a_cloud_rescue():
    recognizer = make_recognizer(Config(), KEY)
    assert isinstance(recognizer, HybridRecognizer)
    assert recognizer.local.name == "local:parakeet-110m"
    assert recognizer.cloud.name == "gateway:fish-audio/transcribe-1"
    close(recognizer)


def test_make_recognizer_hybrid_without_a_key_degrades_to_local():
    recognizer = make_recognizer(Config(), None)
    assert isinstance(recognizer, LocalRecognizer)
    assert not hasattr(recognizer, "rescue")


def test_make_recognizer_passes_the_transport_through():
    script = Sequence(ok("close this"))
    cfg = Config(stt=STT(backend="cloud"))
    recognizer = make_recognizer(cfg, KEY, transport=httpx.MockTransport(script))
    assert transcribe(recognizer).text == "close this"


def test_make_recognizer_does_not_ask_for_zero_data_retention_which_fish_rejects():
    # live, 2026-09-21: providerOptions.gateway.zeroDataRetention on fish is an HTTP 400
    assert Config().jev.zero_data_retention is True
    for backend in ("cloud", "hybrid"):
        script = Sequence(ok())
        cfg = Config(stt=STT(backend=backend))
        recognizer = make_recognizer(cfg, KEY, transport=httpx.MockTransport(script))
        cloud = recognizer.cloud if backend == "hybrid" else recognizer
        transcribe(cloud)
        assert "providerOptions" not in json.loads(script.requests[0].content)


# --------------------------------------------------------------------------- local


class FakeSherpaStream:
    def __init__(self):
        self.result = type("Result", (), {"text": ""})()

    def accept_waveform(self, sample_rate, samples):
        self.sample_rate, self.samples = sample_rate, samples


class FakeSherpa:
    def __init__(self):
        self.streams: list[FakeSherpaStream] = []
        self.threads: list[str] = []

    def create_stream(self):
        self.streams.append(FakeSherpaStream())
        return self.streams[-1]

    def decode_stream(self, stream):
        self.threads.append(threading.current_thread().name)
        stream.result.text = " switch to workspace three "


@pytest.fixture
def fake_sherpa(monkeypatch):
    built: list[FakeSherpa] = []

    def build(spec, files):
        built.append(FakeSherpa())
        return built[-1]

    monkeypatch.setattr(local_module, "_build", build)
    monkeypatch.setattr(local_module.models, "ensure", lambda name, **kwargs: None)
    return built


def test_pcm_becomes_float32_between_minus_one_and_one():
    pcm = np.array([0, 16384, -32768, 32767], dtype="<i2").tobytes()
    samples = local_module.pcm_to_float32(pcm + b"\x01")  # a torn last sample is dropped
    assert samples.dtype == np.float32
    assert samples.tolist() == pytest.approx([0.0, 0.5, -1.0, 32767 / 32768])


def test_local_transcribe_decodes_off_the_event_loop_thread(fake_sherpa):
    recognizer = LocalRecognizer("parakeet-110m")
    result = asyncio.run(recognizer.transcribe(PCM, 16000))
    assert result.text == "switch to workspace three"
    assert result.backend == "local:parakeet-110m"
    stream = fake_sherpa[0].streams[0]
    assert stream.sample_rate == 16000
    assert stream.samples.dtype == np.float32
    assert len(stream.samples) == len(PCM) // 2
    assert fake_sherpa[0].threads[0] != threading.main_thread().name


def test_the_local_model_is_loaded_once(fake_sherpa):
    recognizer = LocalRecognizer("parakeet-110m")

    async def run():
        await recognizer.warm()
        await asyncio.gather(*(recognizer.transcribe(PCM, 16000) for _ in range(3)))

    asyncio.run(run())
    assert len(fake_sherpa) == 1
    assert len(fake_sherpa[0].streams) == 3


def test_an_empty_clip_is_not_decoded(fake_sherpa):
    assert asyncio.run(LocalRecognizer("parakeet-110m").transcribe(b"", 16000)).text == ""
    assert fake_sherpa == []


def test_an_unknown_local_model_is_refused_by_name():
    with pytest.raises(ModelError, match="unknown model 'whisper-large'"):
        LocalRecognizer("whisper-large")


def test_a_missing_local_model_is_an_error_not_a_download(tmp_path):
    recognizer = LocalRecognizer("moonshine-tiny", root=tmp_path)
    with pytest.raises(ModelError, match="not installed"):
        asyncio.run(recognizer.transcribe(PCM, 16000))


@pytest.mark.e2e
def test_the_real_parakeet_model_hears_nothing_in_silence():
    if not models.installed("parakeet-110m"):
        pytest.skip("parakeet-110m is not in ~/.cache/hyprsay/models")
    recognizer = LocalRecognizer("parakeet-110m")
    result = asyncio.run(recognizer.transcribe(bytes(16000 * 2), 16000))
    assert result.backend == "local:parakeet-110m"
    assert len(result.text) < 8  # silence may hallucinate a filler, never a command
    assert result.ms > 0


# --------------------------------------------------------------------------- model registry


def test_the_registry_offers_exactly_the_models_the_config_accepts():
    assert set(models.REGISTRY) == config_module._CHOICES[("stt", "local_model")]


def test_download_urls_are_the_sherpa_onnx_release_tarballs():
    base = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
    assert models.get("parakeet-110m").url == (
        base + "sherpa-onnx-nemo-parakeet_tdt_transducer_110m-en-36000-int8.tar.bz2"
    )
    assert models.get("moonshine-tiny").url == (
        base + "sherpa-onnx-moonshine-tiny-en-quantized-2026-02-27.tar.bz2"
    )


def test_paths_carry_the_keywords_each_sherpa_constructor_takes(tmp_path):
    assert set(models.paths("parakeet-110m", tmp_path)) == {
        "tokens",
        "encoder",
        "decoder",
        "joiner",
    }
    moonshine = models.paths("moonshine-tiny", tmp_path)
    assert set(moonshine) == {"tokens", "encoder", "decoder"}
    assert moonshine["encoder"] == str(
        tmp_path / "sherpa-onnx-moonshine-tiny-en-quantized-2026-02-27" / "encoder_model.ort"
    )


def tarball(members: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:bz2") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


def moonshine_tarball(**extra: bytes) -> bytes:
    spec = models.get("moonshine-tiny")
    members = {f"{spec.directory}/{file}": file.encode() * 50 for file in spec.files.values()}
    return tarball(members | extra)


def serving(payload: bytes, status: int = 200) -> tuple[httpx.MockTransport, list[str]]:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(status, content=payload)

    return httpx.MockTransport(handler), seen


def offline() -> httpx.MockTransport:
    def handler(request):
        raise AssertionError("a download was attempted")

    return httpx.MockTransport(handler)


def test_ensure_downloads_extracts_and_records_the_checksums(tmp_path):
    payload = moonshine_tarball()
    transport, seen = serving(payload)
    progress: list[tuple[int, int]] = []
    base = models.ensure(
        "moonshine-tiny",
        lambda done, total: progress.append((done, total)),
        root=tmp_path,
        transport=transport,
    )
    assert seen == [models.get("moonshine-tiny").url]
    assert base == tmp_path / models.get("moonshine-tiny").directory
    assert models.installed("moonshine-tiny", tmp_path)
    assert progress[-1] == (len(payload), len(payload))
    manifest = json.loads((tmp_path / (base.name + ".sha256.json")).read_text())
    assert manifest["archive"] == hashlib.sha256(payload).hexdigest()
    assert manifest["files"]["tokens.txt"] == hashlib.sha256(b"tokens.txt" * 50).hexdigest()
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".fetch-")] == []


def test_a_second_ensure_verifies_and_does_not_download(tmp_path):
    models.ensure("moonshine-tiny", root=tmp_path, transport=serving(moonshine_tarball())[0])
    assert models.ensure("moonshine-tiny", root=tmp_path, transport=offline()).is_dir()


def test_a_model_file_that_changed_after_it_was_recorded_is_refused(tmp_path):
    base = models.ensure("moonshine-tiny", root=tmp_path, transport=serving(moonshine_tarball())[0])
    (base / "encoder_model.ort").write_bytes(b"tampered")
    with pytest.raises(ModelError, match="does not match its recorded sha256.*encoder_model.ort"):
        models.ensure("moonshine-tiny", root=tmp_path, transport=offline())


def test_a_model_placed_by_hand_is_recorded_on_first_sight(tmp_path):
    spec = models.get("parakeet-110m")
    base = tmp_path / spec.directory
    base.mkdir()
    for file in spec.files.values():
        (base / file).write_bytes(file.encode())
    models.ensure("parakeet-110m", root=tmp_path, transport=offline())
    manifest = json.loads((tmp_path / (spec.directory + ".sha256.json")).read_text())
    assert manifest["archive"] is None
    assert set(manifest["files"]) == set(spec.files.values())


def test_ensure_without_download_refuses_a_missing_model(tmp_path):
    with pytest.raises(ModelError, match="not installed"):
        models.ensure("moonshine-tiny", root=tmp_path, download=False, transport=offline())


def test_a_tarball_without_the_model_files_is_refused_and_nothing_is_installed(tmp_path):
    transport, _ = serving(tarball({"something-else/readme.txt": b"hello"}))
    with pytest.raises(ModelError, match="does not contain"):
        models.ensure("moonshine-tiny", root=tmp_path, transport=transport)
    assert not models.installed("moonshine-tiny", tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_a_tarball_that_escapes_its_directory_is_refused(tmp_path):
    root = tmp_path / "models"
    transport, _ = serving(moonshine_tarball(**{"../../escaped.txt": b"gotcha"}))
    with pytest.raises(ModelError, match="could not unpack"):
        models.ensure("moonshine-tiny", root=root, transport=transport)
    assert not (tmp_path / "escaped.txt").exists()
    assert not models.installed("moonshine-tiny", root)


def test_a_failed_download_is_a_model_error(tmp_path):
    with pytest.raises(ModelError, match="HTTP 404"):
        models.ensure("moonshine-tiny", root=tmp_path, transport=serving(b"", status=404)[0])
    assert not models.installed("moonshine-tiny", tmp_path)
