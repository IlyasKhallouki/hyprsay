"""Action journal, dry run, and the substrate `hypruse replay` runs on.

The trust layers in `trust.py` decide what an agent may do in the moment
and then forget it happened. This module is the memory: an append-only
NDJSON record of every tool call, and a mode that runs every validation
and every guard while acting on nothing.

  HYPRUSE_JOURNAL       record every tool call: `1` for the default path
                        ($XDG_STATE_HOME/hypruse/journal.ndjson), or a
                        path of your own. Off by default
  HYPRUSE_JOURNAL_TEXT  record typed and copied text verbatim instead of
                        a length plus digest. Off by default, because
                        keystrokes are passwords; needed only to REPLAY
                        typing
  HYPRUSE_DRYRUN        validate-only: every argument check and every
                        trust guard runs, then the call reports what it
                        WOULD have done and no input is delivered

One JSON object per line, so `tail -f`, `grep`, and `jq` all work on a
live file:

    {"v": 1, "seq": 7, "ts": "2026-08-02T09:12:13.456Z", "kind": "act",
     "tool": "pointer", "args": {"action": "click", "x": 800, "y": 60},
     "outcome": "ok", "ms": 37, "result": "click ok; cursor now at ..."}

`kind` splits the two things a reader wants separately: "act" is input
delivered to the desktop (what replay re-issues), "observe" is the agent
looking (what a privacy audit cares about: when the screen was captured,
what was read). Observation results are never recorded, only that they
happened and what was asked for, so the journal never becomes a second
copy of everything the agent saw.

`outcome` is "ok", "refused" (a trust layer said no, and `error` carries
which one), or "error". Refusals are the point: they are the record of
the guards doing their job, and the only place that history exists.

Two ordering notes for anything parsing this file. Lines are in
COMPLETION order while `seq` is issue order, so a `sequence` lands after
the steps it ran; sort by `seq` to get causal order. And steps run inside
a `sequence` carry `parent`, the sequence's own `seq`, which is how
replay avoids running both the sequence and its steps.

The journal is a recorder, not a guard: if the file cannot be written the
action still happens, and hypruse warns once on stderr. Failing an
action because an audit trail is unavailable would trade a working
desktop for a log line.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import inspect
import json
import os
import sys
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RECORD_VERSION = 1

_OFF = ("", "0", "false", "no", "off")


class DryRunError(RuntimeError):
    """A real effect was reached while HYPRUSE_DRYRUN is set.

    Never expected: every acting tool returns its "would have" plan
    before touching the input path. Raised by the barriers in `input`,
    `hyprctl.dispatch`, and `clipboard.write` so that a path nobody
    thought of fails loudly with nothing delivered, instead of quietly
    acting during a run the caller was told was a simulation.
    """


def _flag(name: str, default: str = "") -> bool:
    return os.environ.get(name, default).lower() not in _OFF


def dry_run() -> bool:
    return _flag("HYPRUSE_DRYRUN")


def refuse_if_dry(what: str) -> None:
    """Barrier at the effect boundary. See DryRunError."""
    if dry_run():
        raise DryRunError(
            f"dry-run barrier: {what} reached the real input path while "
            "HYPRUSE_DRYRUN is set. Nothing was delivered; this is a hypruse "
            "bug, please report the tool call that produced it."
        )


def text_recorded() -> bool:
    return _flag("HYPRUSE_JOURNAL_TEXT")


def default_path() -> Path:
    """$XDG_STATE_HOME/hypruse/journal.ndjson. State, not runtime: an audit
    trail that vanishes when the tmpfs does is not an audit trail."""
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "hypruse" / "journal.ndjson"


def path() -> Path | None:
    """Where the journal is written, or None when recording is off."""
    raw = os.environ.get("HYPRUSE_JOURNAL", "").strip()
    if raw.lower() in _OFF:
        return None
    if raw.lower() in ("1", "true", "yes", "on"):
        return default_path()
    return Path(raw).expanduser()


def enabled() -> bool:
    return path() is not None


# XDG_STATE_HOME is real disk, so an unattended agent must not fill it.
# One rotation generation: the current file plus `.1`, capped at this
# size each. A busy day is a few hundred KB, so the default holds months.
def _max_bytes() -> int:
    with contextlib.suppress(ValueError):
        return max(int(os.environ.get("HYPRUSE_JOURNAL_MAX_BYTES", "8388608")), 4096)
    return 8388608


_write_lock = threading.Lock()
_seq_lock = threading.Lock()
_seq = 0
_broken = False  # warn once, then stay quiet: stderr is not a log sink
_local = threading.local()


def _next_seq() -> int:
    global _seq
    with _seq_lock:
        _seq += 1
        return _seq


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _rotate(target: Path, limit: int) -> None:
    with contextlib.suppress(OSError):
        if target.stat().st_size >= limit:
            target.replace(target.with_suffix(target.suffix + ".1"))


def _emit(entry: dict[str, Any]) -> None:
    """Append one line. Best-effort by contract: see the module docstring."""
    global _broken
    target = path()
    if target is None:
        return
    line = json.dumps(entry, default=str) + "\n"
    try:
        with _write_lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            _rotate(target, _max_bytes())
            with open(target, "a", encoding="utf-8") as fh:
                fh.write(line)
    except OSError as exc:
        if not _broken:
            _broken = True
            print(f"hypruse: cannot write the journal at {target} ({exc})", file=sys.stderr)


# --- redaction --------------------------------------------------------------


def _digest(text: str) -> dict[str, Any]:
    """What a keystroke looks like in the record when text is not kept: a
    length and a digest, enough to prove two entries typed the same thing
    without the record itself becoming the password leak."""
    return {
        "redacted": True,
        "chars": len(text),
        "sha256": hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:12],
    }


def redact(args: dict[str, Any]) -> dict[str, Any]:
    """Replace typed and copied text with a digest, including inside a
    `sequence`'s steps, unless HYPRUSE_JOURNAL_TEXT is set."""
    if text_recorded():
        return args
    out: dict[str, Any] = {}
    for key, value in args.items():
        if key == "text" and isinstance(value, str):
            out[key] = _digest(value)
        elif key == "steps" and isinstance(value, list):
            out[key] = [redact(s) if isinstance(s, dict) else s for s in value]
        else:
            out[key] = value
    return out


def redacted_text(value: Any) -> bool:
    """Whether a recorded `text` argument is a digest rather than the text
    itself. Replay needs this: a digest cannot be typed."""
    return isinstance(value, dict) and value.get("redacted") is True


# --- recording --------------------------------------------------------------


_RESULT_MAX = 240


def _summarize(result: Any) -> str:
    """The head line of a tool result. Acting tools return a short string,
    or that string plus a `then=` observation; only the string is the
    record of what was done, and the observation can be a whole snapshot."""
    if isinstance(result, str):
        text = result
    elif isinstance(result, list) and result:
        text = getattr(result[0], "text", "") or f"<{type(result[0]).__name__}>"
    else:
        text = ""
    text = " ".join(text.split())
    return text[:_RESULT_MAX] + "..." if len(text) > _RESULT_MAX else text


def _bound_args(sig: inspect.Signature, args: tuple, kwargs: dict) -> dict[str, Any]:
    """The arguments as CALLED, without defaults filled in, so the record
    shows what the agent asked for and replay re-issues exactly that."""
    try:
        bound = sig.bind(*args, **kwargs)
    except TypeError:
        return {"<unbindable>": [list(args), kwargs]}
    return dict(bound.arguments)


def record(
    tool: str,
    kind: str,
    args: dict[str, Any],
    outcome: str,
    ms: float,
    result: Any = None,
    error: str = "",
    seq: int | None = None,
    parent: int | None = None,
) -> None:
    """Write one tool call to the journal. No-op when recording is off."""
    if not enabled():
        return
    entry: dict[str, Any] = {
        "v": RECORD_VERSION,
        "seq": _next_seq() if seq is None else seq,
        "ts": _now(),
        "kind": kind,
        "tool": tool,
        "args": redact(args),
        "outcome": outcome,
        "ms": round(ms, 1),
    }
    if parent is not None:
        entry["parent"] = parent
    if dry_run():
        entry["dry"] = True
    if error:
        entry["error"] = error[:_RESULT_MAX]
    elif kind == "act":
        # observation payloads are never recorded: the record of a
        # screenshot is that it happened, not what your screen showed
        entry["result"] = _summarize(result)
    _emit(entry)


def journaled(
    kind: str | Callable[[dict[str, Any]], str],
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Wrap a tool function so every call lands in the journal, outcome
    included. Applied at the definition site rather than at MCP
    registration, so the steps `sequence` dispatches internally are
    recorded too (they are the entries replay re-issues).

    `kind` is "act" or "observe", or a function of the call's arguments
    for the one tool that is both (`clipboard` reads and writes).

    FastMCP builds a tool's schema from the wrapped function's signature,
    which `functools.wraps` keeps reachable through `__wrapped__`.
    """
    resolve = kind if callable(kind) else (lambda _args: kind)

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        sig = inspect.signature(fn)
        name = fn.__name__

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not enabled():
                return fn(*args, **kwargs)
            seq = _next_seq()
            parent = getattr(_local, "parent", None)
            bound = _bound_args(sig, args, kwargs)
            call_kind = resolve(bound)
            started = time.monotonic()
            token = None
            if name == "sequence":
                token = _push_parent(seq)
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                outcome = "refused" if _is_refusal(exc) else "error"
                record(
                    name, call_kind, bound, outcome, (time.monotonic() - started) * 1000,
                    error=f"{type(exc).__name__}: {exc}", seq=seq, parent=parent,
                )
                raise
            finally:
                if token is not None:
                    _pop_parent(token)
            record(
                name, call_kind, bound, "ok", (time.monotonic() - started) * 1000,
                result=result, seq=seq, parent=parent,
            )
            return result

        return wrapper

    return decorate


def _is_refusal(exc: BaseException) -> bool:
    """A trust layer's no, told apart from a crash without importing
    `trust` (which imports `hyprctl`, which imports this module)."""
    return type(exc).__name__ == "TrustError"


def _push_parent(seq: int) -> Any:
    prev = getattr(_local, "parent", None)
    _local.parent = seq
    return (prev,)


def _pop_parent(token: Any) -> None:
    _local.parent = token[0]


# --- session boundaries -----------------------------------------------------


def _mode() -> dict[str, Any]:
    """The trust configuration in force, recorded once per session: a
    refusal three hours in only means something next to the flags that
    were set when it happened."""
    return {
        "readonly": _flag("HYPRUSE_READONLY"),
        "confine": os.environ.get("HYPRUSE_CONFINE", "") or None,
        "auth_guard": os.environ.get("HYPRUSE_AUTH_GUARD", "1"),
        "strict": _flag("HYPRUSE_STRICT"),
        "mark": _flag("HYPRUSE_MARK"),
        "clipboard": _flag("HYPRUSE_CLIPBOARD"),
        "dry_run": dry_run(),
        "text_recorded": text_recorded(),
    }


def start(version: str) -> None:
    """Open a session in the journal. Safe to call when recording is off."""
    if not enabled():
        return
    _emit(
        {
            "v": RECORD_VERSION,
            "seq": _next_seq(),
            "ts": _now(),
            "kind": "session",
            "event": "start",
            "pid": os.getpid(),
            "version": version,
            "mode": _mode(),
        }
    )


def stop() -> None:
    """Close the session. Registered with safety.on_shutdown, so it runs on
    the kill switch's SIGTERM path too."""
    if not enabled():
        return
    _emit(
        {
            "v": RECORD_VERSION,
            "seq": _next_seq(),
            "ts": _now(),
            "kind": "session",
            "event": "stop",
            "pid": os.getpid(),
        }
    )


# --- reading ----------------------------------------------------------------


def read(source: Path) -> Iterator[dict[str, Any]]:
    """Parse a journal file, skipping lines that are not JSON objects: a
    process killed mid-write can leave a partial last line, and one torn
    line must not make the rest of the history unreadable."""
    with open(source, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                yield entry


# Tools this version knows how to re-issue. An act entry naming anything
# else (a journal written by a NEWER hypruse) is deliberately not filtered
# out by `replayable`: replay refuses the whole run instead, because
# silently dropping an action and then reporting success would be the
# worst of the three outcomes.
REPLAYABLE = ("pointer", "keyboard", "click_ui", "hypr", "launch", "use_bind", "clipboard")

# Recorded, but never re-issued: `sequence`'s steps are recorded
# individually (each with `parent` set), so replaying the wrapper as well
# would run everything twice.
_NOT_REPLAYED = ("sequence",)


def replayable(entry: dict[str, Any]) -> bool:
    """Whether an entry is an action replay should re-issue: it delivered
    input (or would have, under a dry run), it succeeded, and it is not
    the `sequence` wrapper around steps that were recorded separately."""
    return (
        entry.get("kind") == "act"
        and entry.get("outcome") == "ok"
        and entry.get("tool") not in _NOT_REPLAYED
    )
