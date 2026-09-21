"""What was heard, what was decided, what was done. One JSON object per line.

This is the file `hyprsay inspect` reads, and the raw material for learning from
corrections. It records utterances, so it is private by construction:

- dictated text is never written. It is replaced by its length and a short hash,
  enough to tell two dictations apart and useless for recovering either.
- window titles are dropped unless HYPRSAY_JOURNAL_TITLES=1.
- the file is created 0600 in a 0700 directory, capped in size, and rotated once.
- HYPRSAY_JOURNAL=off disables it entirely.

Writing must never break a command: every failure here is swallowed.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import time
from enum import Enum
from pathlib import Path
from typing import Any

MAX_BYTES = 5 * 1024 * 1024


def path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(base) / "hyprsay" / "journal.jsonl"


def redact_text(text: str) -> dict[str, Any]:
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]
    return {"redacted": True, "chars": len(text), "sha256_12": digest}


def _omitted(name: str, item: Any, keep_titles: bool) -> bool:
    if name == "title":
        return not keep_titles  # written by the window's owner, and often private
    return name in ("pcm", "raw") and isinstance(item, bytes | bytearray)  # audio


def _plain(value: Any, keep_titles: bool) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        out: dict[str, Any] = {}
        for f in dataclasses.fields(value):
            item = getattr(value, f.name)
            if (
                f.name == "text"
                and isinstance(item, str)
                and type(value).__name__ in ("Action", "Slots")
            ):
                out[f.name] = redact_text(item)  # dictated text
            elif _omitted(f.name, item, keep_titles):
                continue
            else:
                out[f.name] = _plain(item, keep_titles)
        return out
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): _plain(v, keep_titles) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_plain(v, keep_titles) for v in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return repr(value)


def record(kind: str, **fields: Any) -> None:
    if os.environ.get("HYPRSAY_JOURNAL", "").lower() in ("off", "0", "false", "no"):
        return
    try:
        keep_titles = os.environ.get("HYPRSAY_JOURNAL_TITLES") == "1"
        entry = {"ts": round(time.time(), 3), "kind": kind, **_plain(fields, keep_titles)}
        file = path()
        file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if file.exists() and file.stat().st_size > MAX_BYTES:
            file.replace(file.with_suffix(".jsonl.1"))
        fd = os.open(file, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception:  # noqa: BLE001 - the journal must never break a command
        return


def tail(n: int = 1, kind: str | None = None) -> list[dict[str, Any]]:
    """The last n entries, newest last. Missing or unreadable file: an empty list."""
    try:
        lines = path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if kind is None or entry.get("kind") == kind:
            out.append(entry)
            if len(out) >= n:
                break
    return out[::-1]
