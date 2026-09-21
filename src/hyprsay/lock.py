"""The lock latch: one answer to "is this desk locked?", and any doubt is yes.

A microphone that acts on a locked machine lets anyone in the room, or a video that is
playing, say "close window" to it. Hyprland does not help: its IPC dispatch handler has
no lock check at all (only the keybind path has one), and the inherited `hypr`, `launch`
and `use_bind` never look. So the executor asks this latch before every write, and the
daemon asks it at key down, at key up and at countdown expiry (docs/PLAN.md 5.7).

Four sources, because each one alone has a hole:

- `DesktopState.locked`, whatever the world model last saw. It defaults to True, so a
  state nobody could read is a locked state.
- a lock-kind layer surface, for lockers that draw with layer-shell instead of
  ext-session-lock. Quickshell lockers are the live case; the user can add names.
- a fresh `j/locked` query. It is the compositor's own flag, costs about 0.1 ms over the
  socket, and stays true when the locker crashes, which is exactly where the inherited
  /proc scan says "unlocked". A query that fails means locked.
- logind `LockedHint`, read through `loginctl` and cached for one second because that is
  a process spawn. It is the only source allowed to fail quietly: a machine without
  logind is a normal machine, a compositor that will not answer is not.

socket2 carries no lock event on 0.56.2, so nothing here is pushed. The latch notices the
edge when somebody calls `locked()`, which the daemon does on a timer.
"""

from __future__ import annotations

import contextlib
import subprocess
import threading
import time
from collections.abc import Callable
from typing import Any

from hyprsay.config import Config
from hyprsay.model import DesktopState

# "lock" alone would be the broadest net, and it would catch every clock widget
LOCK_NAMESPACES: tuple[str, ...] = (
    "hyprlock",
    "swaylock",
    "gtklock",
    "waylock",
    "lockscreen",
    "lock-screen",
    "lock_screen",
    "sessionlock",
    "session-lock",
    "quickshell:lock",
    "quickshell-lock",
    "qs-lock",
)

LOGIND_TTL_S = 1.0
# the daemon asks from its event loop too, so a hung loginctl must not hold it for long
LOGIND_TIMEOUT_S = 0.5


def _hyprctl_query(command: str) -> Any:
    # imported late: the daemon passes its socket client, and then hypruse is not needed
    from hypruse import hyprctl

    return hyprctl.query(command)


def logind_locked_hint() -> bool | None:
    """LockedHint of the graphical session, or None when it cannot be read.

    "auto" and not "self": a daemon started as a systemd user unit belongs to no
    session, and `loginctl show-session self` fails there (measured on this machine).
    """
    try:
        proc = subprocess.run(
            ["loginctl", "show-session", "auto", "-p", "LockedHint", "--value"],
            capture_output=True,
            text=True,
            timeout=LOGIND_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    answer = proc.stdout.strip().lower()
    if proc.returncode != 0 or answer not in ("yes", "no"):
        return None
    return answer == "yes"


class Latch:
    """Implements `model.LockLatch`. Safe to call from any thread."""

    def __init__(
        self,
        cfg: Config,
        state_provider: Callable[[], DesktopState],
        query: Callable[[str], Any] | None = None,
        *,
        logind: Callable[[], bool | None] | None = logind_locked_hint,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._state_provider = state_provider
        # any `query(command)` that answers decoded JSON: `HyprSocket.query` in the daemon,
        # the inherited `hyprctl.query` (one fork, about 20 ms) when nothing is passed
        self._query = query or _hyprctl_query
        self._logind = logind
        self._clock = clock
        extra = tuple(n.strip().lower() for n in cfg.safety.locker_namespaces if n.strip())
        self._user_names = extra
        self._namespaces = LOCK_NAMESPACES + extra
        self._callbacks: list[Callable[[], None]] = []
        self._guard = threading.Lock()
        # the first locked answer is an edge too: a daemon started on a locked desk
        # must drop its audio like any other
        self._was_locked = False
        self._logind_value: bool | None = None
        self._logind_stamp: float | None = None

    def on_lock(self, callback: Callable[[], None]) -> None:
        """Run `callback` once each time the desk goes from unlocked to locked."""
        self._callbacks.append(callback)

    def locked(self) -> bool:
        now_locked = self._any_source_locked()
        with self._guard:
            edge = now_locked and not self._was_locked
            self._was_locked = now_locked
        if edge:
            for callback in list(self._callbacks):
                # a broken listener must never turn "locked" into an exception
                with contextlib.suppress(Exception):
                    callback()
        return now_locked

    def why(self) -> str:
        """The first source that says locked, for `inspect` and the HUD. Empty when none."""
        return next((name for name, check in self._sources() if _closed(check)), "")

    # ----------------------------------------------------------------- sources

    def _sources(self) -> tuple[tuple[str, Callable[[], bool]], ...]:
        # cheapest first: any() stops at the first source that says locked
        return (
            ("state", self._state_locked),
            ("compositor", self._compositor_locked),
            ("logind", self._logind_locked),
        )

    def _any_source_locked(self) -> bool:
        return any(_closed(check) for _, check in self._sources())

    def _state_locked(self) -> bool:
        state = self._state_provider()
        if state.locked:
            return True
        if any(self._names_a_locker(layer.namespace, self._namespaces) for layer in state.layers):
            return True
        # a Quickshell locker that is an ordinary window shows up by class, and only the
        # user can know its name
        return any(self._names_a_locker(w.cls, self._user_names) for w in state.windows)

    @staticmethod
    def _names_a_locker(text: str, names: tuple[str, ...]) -> bool:
        lowered = text.lower()
        return any(name in lowered for name in names)

    def _compositor_locked(self) -> bool:
        answer = self._query("locked")
        if isinstance(answer, dict):
            answer = answer.get("locked")
        # anything that is not a plain False is doubt
        return answer is not False

    def _logind_locked(self) -> bool:
        if self._logind is None:
            return False
        now = self._clock()
        with self._guard:
            stamp = self._logind_stamp
            fresh = stamp is not None and now - stamp < LOGIND_TTL_S
            if fresh:
                return self._logind_value is True
        try:
            value = self._logind()
        except Exception:
            value = None
        with self._guard:
            self._logind_value, self._logind_stamp = value, now
        return value is True


def _closed(check: Callable[[], bool]) -> bool:
    """Fail closed: a source that raises has answered "locked"."""
    try:
        return bool(check())
    except Exception:
        return True
