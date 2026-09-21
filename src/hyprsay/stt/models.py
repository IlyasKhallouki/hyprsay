"""The local model registry: what each model is called, where it lives, where it comes from.

Models live under `~/.cache/hyprsay/models/<directory>`, one directory per model, laid
out exactly as the sherpa-onnx release tarball unpacks. Both URLs below answered a HEAD
request with 200 on 2026-09-21 (108 MB for Parakeet, 30 MB for Moonshine).

Integrity is trust on first use. The sherpa-onnx project publishes no checksums, so the
sha256 of the tarball and of every model file is recorded in a manifest beside the model
the first time it is seen, and checked on every later `ensure`. That catches a truncated
download, a bad disk and a half-finished extraction. It does not stop someone who can
already write to the cache directory, because they can rewrite the manifest too.

`SttError` is defined here because this is the one module every backend imports.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

MODELS_DIR = Path("~/.cache/hyprsay/models")
RELEASES = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models"

# progress(bytes_done, bytes_total); total is 0 when the server does not say
Progress = Callable[[int, int], None]


class SttError(Exception):
    """Base class for speech to text failures. Messages never contain the gateway key."""


class ModelError(SttError):
    """A local model is unknown, missing, or does not match its recorded checksum."""


@dataclass(frozen=True)
class ModelSpec:
    name: str
    directory: str
    # which sherpa-onnx constructor loads it: "nemo_transducer" | "moonshine_v2"
    kind: str
    # role -> file name inside the directory; the roles are the constructor's keywords
    files: dict[str, str]
    license: str

    @property
    def url(self) -> str:
        return f"{RELEASES}/{self.directory}.tar.bz2"


REGISTRY: dict[str, ModelSpec] = {
    spec.name: spec
    for spec in (
        ModelSpec(
            name="parakeet-110m",
            directory="sherpa-onnx-nemo-parakeet_tdt_transducer_110m-en-36000-int8",
            kind="nemo_transducer",
            files={
                "tokens": "tokens.txt",
                "encoder": "encoder.int8.onnx",
                "decoder": "decoder.int8.onnx",
                "joiner": "joiner.int8.onnx",
            },
            license="CC-BY-4.0",
        ),
        ModelSpec(
            name="moonshine-tiny",
            directory="sherpa-onnx-moonshine-tiny-en-quantized-2026-02-27",
            kind="moonshine_v2",
            files={
                "tokens": "tokens.txt",
                "encoder": "encoder_model.ort",
                "decoder": "decoder_model_merged.ort",
            },
            license="MIT",
        ),
    )
}


def get(name: str) -> ModelSpec:
    try:
        return REGISTRY[name]
    except KeyError:
        raise ModelError(f"unknown model {name!r}, expected one of {sorted(REGISTRY)}") from None


def model_dir(name: str, root: Path | None = None) -> Path:
    return (root or MODELS_DIR).expanduser() / get(name).directory


def paths(name: str, root: Path | None = None) -> dict[str, str]:
    """Role to absolute file path, ready to pass to the sherpa-onnx constructor."""
    base = model_dir(name, root)
    return {role: str(base / file) for role, file in get(name).files.items()}


def installed(name: str, root: Path | None = None) -> bool:
    return all(Path(p).is_file() for p in paths(name, root).values())


def ensure(
    name: str,
    progress: Progress | None = None,
    *,
    root: Path | None = None,
    download: bool = True,
    transport: httpx.BaseTransport | None = None,
) -> Path:
    """Return the model directory, downloading it first when it is absent.

    Blocking: a first download is 30 to 108 MB, and a checksum pass over Parakeet reads
    131 MB. Call it from a thread or from a setup command, never from the event loop.
    With `download=False` a missing model is an error instead of a surprise transfer.
    """
    spec = get(name)
    base = model_dir(name, root)
    if installed(name, root):
        _verify_or_record(spec, base)
        return base
    if not download:
        raise ModelError(f"model {name!r} is not installed in {base}; fetch it first")
    _download(spec, base, progress, transport)
    return base


# --------------------------------------------------------------------------- checksums


def _manifest_path(base: Path) -> Path:
    # beside the directory, not inside it, so the directory stays what the tarball held
    return base.with_name(base.name + ".sha256.json")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _file_hashes(spec: ModelSpec, base: Path) -> dict[str, str]:
    return {file: _sha256(base / file) for file in sorted(spec.files.values())}


def _write_manifest(base: Path, files: dict[str, str], archive: str | None) -> None:
    body = {"archive": archive, "files": files}
    _manifest_path(base).write_text(json.dumps(body, indent=1) + "\n")


def _verify_or_record(spec: ModelSpec, base: Path) -> None:
    manifest = _manifest_path(base)
    actual = _file_hashes(spec, base)
    if not manifest.exists():
        # a model that was put here by hand: this is its first sighting
        _write_manifest(base, actual, archive=None)
        return
    try:
        recorded = json.loads(manifest.read_text())["files"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ModelError(f"{manifest} is unreadable; delete it and {base} to refetch") from exc
    changed = sorted(f for f, digest in actual.items() if recorded.get(f) != digest)
    if changed:
        raise ModelError(
            f"model {spec.name!r} does not match its recorded sha256 ({', '.join(changed)}); "
            f"delete {base} and {manifest} to refetch"
        )


# --------------------------------------------------------------------------- download


def _download(
    spec: ModelSpec, base: Path, progress: Progress | None, transport: httpx.BaseTransport | None
) -> None:
    base.parent.mkdir(parents=True, exist_ok=True)
    # staged inside the models directory so the final rename never crosses a filesystem
    with tempfile.TemporaryDirectory(dir=base.parent, prefix=".fetch-") as staging:
        archive = Path(staging) / "model.tar.bz2"
        digest = _fetch(spec.url, archive, progress, transport)
        unpacked = Path(staging) / "unpacked"
        _extract(archive, unpacked)
        source = unpacked / spec.directory
        missing = sorted(f for f in spec.files.values() if not (source / f).is_file())
        if missing:
            raise ModelError(f"{spec.url} does not contain {', '.join(missing)}")
        if base.exists():
            shutil.rmtree(base)  # a partial directory from an interrupted run
        source.rename(base)
    _write_manifest(base, _file_hashes(spec, base), archive=digest)


def _fetch(
    url: str, dest: Path, progress: Progress | None, transport: httpx.BaseTransport | None
) -> str:
    digest = hashlib.sha256()
    timeout = httpx.Timeout(30.0, connect=10.0)
    try:
        with (
            httpx.Client(follow_redirects=True, timeout=timeout, transport=transport) as http,
            http.stream("GET", url) as response,
        ):
            if response.status_code != 200:
                raise ModelError(f"{url} answered HTTP {response.status_code}")
            total = int(response.headers.get("content-length") or 0)
            done = 0
            with dest.open("wb") as out:
                for chunk in response.iter_bytes(1 << 16):
                    out.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
    except httpx.HTTPError as exc:
        raise ModelError(f"download of {url} failed: {type(exc).__name__}") from exc
    return digest.hexdigest()


def _extract(archive: Path, dest: Path) -> None:
    dest.mkdir()
    try:
        with tarfile.open(archive, "r:bz2") as tar:
            # the "data" filter refuses absolute paths, "..", links out of the tree and devices
            tar.extractall(dest, filter="data")
    except (tarfile.TarError, OSError) as exc:
        raise ModelError(f"could not unpack {archive.name}: {exc}") from exc
