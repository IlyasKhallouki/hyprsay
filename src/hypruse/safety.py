"""Activity beacon and kill-switch support.

The seat, cursor, keyboard focus, is shared between the agent and the
human at the desk. hypruse therefore keeps a runtime beacon at
$XDG_RUNTIME_DIR/hypruse/state.json:

    {"pid": 1234, "started": 1789...,
     "last_action": "pointer:click", "last_ts": 1789...}

written at startup, updated on every acting tool call, removed on clean
shutdown. Anything can watch it, the shipped Waybar module (waybar/)
shows a red indicator while an agent has hands on the desktop, and its
click action (or a Hyprland keybind) runs `pkill -f hypruse`: the
process dies mid-action at worst, never holding a button down, because
button press/release pairs are sent inside one tool call and the SIGTERM
path runs registered cleanups (on_shutdown) that release anything a
long-running drag still holds.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import os
import re
import signal
import threading
import time
from collections.abc import Callable
from pathlib import Path

_state_path: Path | None = None
_started: float = 0.0
_cleanups: list[Callable[[], None]] = []
_write_lock = threading.Lock()  # concurrent tool calls share one tmp file

# last_action is interpolated from tool arguments BEFORE they are
# validated, and beacon consumers (the Waybar module) re-embed it in
# output they build themselves: whitelist it down to a plain token so a
# crafted argument can never forge or break that output.
_ACTION_JUNK = re.compile(r"[^A-Za-z0-9:_.-]+")


def state_path() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
    d = Path(base) / "hypruse"
    d.mkdir(parents=True, exist_ok=True)
    return d / "state.json"


def _write(payload: dict) -> None:
    if _state_path is None:
        return
    with _write_lock:
        tmp = _state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(_state_path)


_armed = False


def arm() -> None:
    """Install the shutdown path (atexit + SIGTERM) without raising the
    beacon. A CLI verb running beside a live server must leave the server's
    beacon alone, but `pkill -f hypruse` matches the verb too, and a verb
    killed mid-drag must still release the button it holds: the cleanups
    are what make the kill switch safe, the beacon is only what makes the
    agent visible."""
    global _armed
    if _armed:
        return
    _armed = True
    atexit.register(shutdown)
    # SIGTERM (what the kill switch's `pkill` sends) has no default cleanup,
    # so remove the beacon then let the default disposition terminate us.
    # We deliberately do NOT touch SIGINT: the MCP/anyio runtime handles
    # Ctrl+C gracefully, and overriding it with sys.exit() deadlocks the
    # interpreter against the stdin-reader thread at shutdown ("could not
    # acquire lock for stdin"). A lingering beacon after SIGTERM is harmless
    #, the Waybar module liveness-checks the pid.
    with contextlib.suppress(ValueError):  # signal() only works on the main thread
        signal.signal(signal.SIGTERM, _on_sigterm)


def init() -> None:
    """Start the beacon; safe to call once at server startup."""
    global _state_path, _started
    _state_path = state_path()
    _started = time.time()
    _write({"pid": os.getpid(), "started": _started, "last_action": "", "last_ts": 0})
    arm()


def touch(action: str) -> None:
    """Record an acting tool call (no-op if init() was never called)."""
    if _state_path is None:
        return
    _write(
        {
            "pid": os.getpid(),
            "started": _started,
            "last_action": _ACTION_JUNK.sub("", action)[:64],
            "last_ts": time.time(),
        }
    )


def on_shutdown(fn: Callable[[], None]) -> None:
    """Register a best-effort cleanup to run before the process dies,
    whether by the SIGTERM kill switch or a normal shutdown; e.g. releasing
    a pointer button an in-flight drag is holding."""
    _cleanups.append(fn)


def shutdown() -> None:
    global _state_path
    while _cleanups:
        with contextlib.suppress(Exception):
            _cleanups.pop()()
    if _state_path is not None:
        # only our own beacon: a server started during a long verb (a launch
        # waiting on its window) has overwritten the file with its pid, and
        # removing that would leave the live server invisible and un-stoppable
        with contextlib.suppress(OSError, ValueError, TypeError, KeyError):
            if int(json.loads(_state_path.read_text())["pid"]) == os.getpid():
                _state_path.unlink(missing_ok=True)
        _state_path = None


def _on_sigterm(signum: int, _frame: object) -> None:
    shutdown()
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)
