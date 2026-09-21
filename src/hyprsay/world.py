"""The desktop as hyprsay sees it: Hyprland's two sockets, and the state built from them.

Why talk to `.socket.sock` directly instead of running `hyprctl`: measured on this
machine, one query over the socket takes about 0.1 ms and the whole eight-part snapshot
about 0.3 ms, while spawning `hyprctl` costs 6 to 22 ms per call with a fat tail. The
state has to be ready before the speaker finishes the sentence, so the spawn is the one
cost in this layer worth removing. `use_socket_transport()` gives the inherited hypruse
code the same saving without touching it.

Facts about the request socket that shape `HyprSocket` (Hyprland 0.56.2 source, and
checked against the live socket with read-only requests):

- One request per connection. The compositor accepts, then polls the connection ON ITS
  MAIN THREAD for up to 5 s, so a client that connects and stalls freezes the desktop.
  The request is therefore built first, then: connect, sendall, shutdown(SHUT_WR), read
  to EOF, close. No connection is ever held idle.
- The server reads 1023 byte chunks and stops at the first short one. The half-close is
  what ends a request whose length is an exact multiple of 1023.
- The wire form is `flags/command`, and `hyprctl` sends the slash even with no flags.
  `[[BATCH]]a;b` carries no flag prefix of its own; each part has its own.
- Batch replies are joined with three newlines, but several replies carry newlines of
  their own at either end (`j/locked` is `\\n{...}\\n`), so the run between two replies is
  3 to 5 newlines long. Splitting on the literal separator would see a phantom empty
  reply in a run of 6. No reply holds more than 2 newlines in a row inside it (`j/layers`
  has the 2), so the split is on any run of 3 or more.
- Error replies are plain text with nothing to mark them: `unknown request`,
  `no such option`. `hyprctl` exits 0 on those and non-zero only when it cannot connect.

The event socket sends `EVENT>>DATA` lines, never reads, and drops a client that falls 64
events behind. Window addresses arrive there WITHOUT the `0x` that every request uses;
`addr()` puts it back. There is no lock event on it, so `DesktopState.locked` is only as
fresh as the last snapshot: the lock latch has to poll (`read_locked`) or use the Wayland
notifier, and must not rely on the world model alone.

Everything here fails closed. A snapshot that cannot be read in full is `DesktopState()`,
whose default is locked, and the world model falls back to it while the event socket is
down, because a state that has stopped following the desktop is not a state to act on.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import socket
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from hyprsay.model import DesktopState, Layer, Monitor, Window, Workspace
from hypruse import hyprctl

log = logging.getLogger(__name__)

REQUEST_SOCKET = ".socket.sock"
EVENT_SOCKET = ".socket2.sock"


class HyprSocketError(RuntimeError):
    """The request socket is unreachable, timed out, or answered something unusable."""


def socket_path(name: str) -> Path:
    """Where this session's Hyprland keeps the socket called `name`."""
    signature = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if not signature:
        raise HyprSocketError("HYPRLAND_INSTANCE_SIGNATURE is not set, is Hyprland running?")
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime) / "hypr" / signature / name


def addr(field: str) -> str:
    """A window address in the `0x...` form that requests and dispatch selectors use.

    Event data carries addresses bare (`activewindowv2>>55a448972080`). An empty field
    stays empty: that is how the compositor says "no window".
    """
    field = field.strip()
    if not field or field.startswith("0x"):
        return field
    return "0x" + field


# --------------------------------------------------------------------------- requests

_REPLY_GAP = re.compile(r"\n{3,}")


class HyprSocket:
    """One request per connection to `.socket.sock`. Stateless, so safe from any thread."""

    def __init__(self, path: str | Path | None = None, *, timeout: float = 2.0) -> None:
        # resolved per request when not given, so a daemon outlives a compositor restart
        self._path = Path(path) if path is not None else None
        self._timeout = timeout

    @property
    def path(self) -> Path:
        return self._path if self._path is not None else socket_path(REQUEST_SOCKET)

    def request(self, command: str, *, flags: str = "") -> str:
        """Send one command, return the raw reply."""
        return self._exchange(f"{flags}/{command}")

    def query(self, command: str) -> Any:
        """A JSON request (`clients`, `monitors`, ...), decoded."""
        reply = self.request(command, flags="j")
        try:
            return json.loads(reply)
        except json.JSONDecodeError as exc:
            raise HyprSocketError(f"j/{command} did not answer JSON: {reply[:80]!r}") from exc

    def batch(self, commands: list[str], *, flags: str = "j") -> list[str]:
        """Run the commands back to back in one main-loop callback, one reply each."""
        if not commands:
            return []
        for command in commands:
            # the compositor splits a batch on every ';' outside [...], with no escape
            if ";" in command:
                raise HyprSocketError(f"a ';' cannot travel inside a batch: {command[:80]!r}")
        reply = self._exchange("[[BATCH]]" + ";".join(f"{flags}/{c}" for c in commands))
        replies = _REPLY_GAP.split(reply.strip("\n"))
        if len(replies) != len(commands):
            raise HyprSocketError(f"batch of {len(commands)} answered {len(replies)} replies")
        return replies

    def _exchange(self, wire: str) -> str:
        # one deadline for the whole exchange: a per-call timeout alone would let a
        # reply that trickles in keep the caller here for ever
        deadline = time.monotonic() + self._timeout
        payload = wire.encode()
        try:
            path = str(self.path)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(self._timeout)
                conn.connect(path)
                conn.sendall(payload)
                conn.shutdown(socket.SHUT_WR)
                chunks: list[bytes] = []
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("reply not finished in time")
                    conn.settimeout(remaining)
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
        except OSError as exc:  # TimeoutError and ConnectionRefusedError are OSErrors
            raise HyprSocketError(f"Hyprland request {wire[:60]!r} failed: {exc}") from exc
        return b"".join(chunks).decode(errors="replace")


# --------------------------------------------------------------------------- snapshot

# `locked` goes first so the lock is read before anything that could be acted on
_SNAPSHOT = (
    "locked",
    "status",
    "clients",
    "workspaces",
    "monitors",
    "activewindow",
    "activeworkspace",
    "layers",
)


def snapshot(sock: HyprSocket) -> DesktopState:
    """The whole desktop from ONE batch. Never raises: what cannot be read is locked."""
    try:
        return _state_from(sock.batch(list(_SNAPSHOT)))
    except Exception as exc:
        log.warning("desktop snapshot failed, treating the session as locked: %s", exc)
        return DesktopState()


def read_locked(sock: HyprSocket) -> bool:
    """The session lock alone, about 0.1 ms. Any doubt is True."""
    try:
        return _locked(sock.request("locked", flags="j"))
    except Exception:
        return True


def _locked(reply: str) -> bool:
    # measured: `j/locked` answers {"locked": false}. Only a real boolean counts, so a
    # compositor too old to know the request ("unknown request") reads as locked.
    value = json.loads(reply)["locked"]
    if not isinstance(value, bool):
        raise ValueError(f"locked is not a boolean: {value!r}")
    return value


def _state_from(replies: list[str]) -> DesktopState:
    locked, status, *rest = replies
    clients, workspaces, monitors, active, workspace, layers = (json.loads(r) for r in rest)
    return DesktopState(
        windows=tuple(_window(c) for c in clients if c.get("mapped", True)),
        workspaces=tuple(sorted((_workspace(w) for w in workspaces), key=lambda w: w.id)),
        monitors=tuple(_monitor(m) for m in monitors),
        layers=tuple(_layers(layers)),
        # `j/activewindow` answers {} when nothing is focused
        active_address=addr(active.get("address", "")),
        active_workspace_id=int(workspace["id"]),
        locked=_locked(locked),
        # measured: {"configProvider": "hyprlang", "backend": "drm"}. hypruse's reading
        # is kept: anything unreadable is the legacy manager, the only other dialect.
        provider=hyprctl.parse_provider(status),
        stamp=time.monotonic(),
    )


def _pair(value: Any) -> tuple[int, int]:
    x, y = value
    return int(x), int(y)


def _window(c: dict[str, Any]) -> Window:
    where = c.get("workspace") or {}
    return Window(
        address=addr(c["address"]),
        cls=c.get("class", ""),
        initial_class=c.get("initialClass", ""),
        title=c.get("title", ""),
        workspace_id=int(where.get("id", 0)),
        workspace_name=str(where.get("name", "")),
        monitor=int(c.get("monitor", 0)),
        floating=bool(c.get("floating", False)),
        # an int since Hyprland 0.42 (0 none, 1 maximized, 2 fullscreen), a bool before
        fullscreen=bool(c.get("fullscreen", False)),
        pinned=bool(c.get("pinned", False)),
        hidden=bool(c.get("hidden", False)),
        pid=int(c.get("pid", 0)),
        focus_rank=int(c.get("focusHistoryID", 99)),
        at=_pair(c.get("at", (0, 0))),
        size=_pair(c.get("size", (0, 0))),
    )


def _workspace(w: dict[str, Any]) -> Workspace:
    return Workspace(
        id=int(w["id"]),
        name=str(w.get("name", "")),
        monitor=str(w.get("monitor", "")),
        windows=int(w.get("windows", 0)),
    )


def _monitor(m: dict[str, Any]) -> Monitor:
    # the reply gives width and height in physical mode pixels. They are stored LOGICAL
    # (divided by scale, swapped when rotated), the one space Window.at and size live in.
    x, y, width, height = hyprctl.logical_rect(m)
    return Monitor(
        id=int(m["id"]),
        name=str(m["name"]),
        x=x,
        y=y,
        width=width,
        height=height,
        scale=float(m.get("scale", 1.0)),
        focused=bool(m.get("focused", False)),
        active_workspace_id=int((m.get("activeWorkspace") or {}).get("id", 0)),
    )


def _layers(raw: dict[str, Any]) -> list[Layer]:
    """Flatten monitor -> level -> surfaces. Listed means tracked, not necessarily shown."""
    return [
        Layer(namespace=str(surface.get("namespace", "")), monitor=monitor, level=int(level))
        for monitor, entry in raw.items()
        for level, surfaces in (entry.get("levels") or {}).items()
        for surface in surfaces
    ]


# --------------------------------------------------------------------------- hypruse shim

_original_run: Callable[..., str] | None = None


def wire_for(args: tuple[str, ...]) -> str:
    """The bytes `hyprctl <args>` would put on the socket.

    hyprctl joins its arguments with single spaces, which is why hypruse can pass a whole
    Lua expression as one argument: over the socket the two spellings are the same bytes.
    """
    rest = list(args)
    flags = ""
    while rest and rest[0] == "-j":
        flags = "j"
        rest.pop(0)
    if rest and rest[0] == "--batch":
        spec = " ".join(rest[1:])
        if flags:
            spec = ";".join(f"{flags}/{part.strip()}" for part in spec.split(";"))
        return "[[BATCH]]" + spec
    if not rest or rest[0].startswith("-"):
        raise hyprctl.HyprctlError(f"hyprctl {' '.join(args)}: no socket form for this call")
    return f"{flags}/{' '.join(rest)}"


def use_socket_transport(sock: HyprSocket | None = None) -> None:
    """Make every hypruse query and dispatch use the socket instead of spawning hyprctl.

    `hyprctl._run` is the one seam all of hypruse goes through. The replacement keeps its
    contract: the stripped reply on success, and hypruse's own HyprctlError when the
    compositor cannot be reached, which is the only case where the binary exits non-zero.
    That error is what hypruse's guards catch to fail closed, so the type matters.
    """
    global _original_run
    transport = sock if sock is not None else HyprSocket()

    def _run(*args: str) -> str:
        wire = wire_for(args)
        try:
            return transport._exchange(wire).strip()
        except HyprSocketError as exc:
            raise hyprctl.HyprctlError(f"hyprctl {' '.join(args)}: {exc}") from exc

    if _original_run is None:
        _original_run = hyprctl._run
    hyprctl._run = _run


def restore_transport() -> None:
    """Put hypruse back on the hyprctl binary."""
    global _original_run
    if _original_run is not None:
        hyprctl._run = _original_run
        _original_run = None


# --------------------------------------------------------------------------- events

# Pseudo events from the reader itself. A name off the wire is whatever came before the
# first ">>", so it can never contain one and these cannot be forged by the compositor.
CONNECTED = ">>connected"
DISCONNECTED = ">>disconnected"


class EventReader:
    """`async for event, data in EventReader()`: socket2 lines, for ever.

    Yields `(CONNECTED, "")` after every connect and `(DISCONNECTED, "")` after every
    loss, so a consumer knows when it has missed events and must look again. Reconnects
    with a doubling backoff that only resets once a line has actually arrived, so a
    socket that accepts and hangs up is not hammered. `data` is left unsplit: titles
    contain commas, and only the consumer knows how many fields its event has.
    """

    def __init__(
        self, path: str | Path | None = None, *, backoff: tuple[float, float] = (0.05, 2.0)
    ) -> None:
        self._path = Path(path) if path is not None else None
        self._backoff = backoff

    def __aiter__(self) -> AsyncIterator[tuple[str, str]]:
        return self._events()

    async def _events(self) -> AsyncIterator[tuple[str, str]]:
        shortest, longest = self._backoff
        delay = shortest
        while True:
            try:
                path = self._path if self._path is not None else socket_path(EVENT_SOCKET)
                reader, writer = await asyncio.open_unix_connection(str(path))
            except (OSError, HyprSocketError):
                await asyncio.sleep(delay)
                delay = min(delay * 2, longest)
                continue
            try:
                yield CONNECTED, ""
                # ValueError: a line longer than the stream limit, which the compositor
                # (1024 byte cap on DATA) never sends; treat it as a broken connection
                with contextlib.suppress(OSError, ValueError):
                    # a last line without its newline was cut off by the disconnect
                    while (line := await reader.readline()).endswith(b"\n"):
                        event, sep, data = line.decode(errors="replace").partition(">>")
                        if sep:
                            delay = shortest
                            yield event, data.removesuffix("\n")
            finally:
                writer.close()
            yield DISCONNECTED, ""
            await asyncio.sleep(delay)
            delay = min(delay * 2, longest)


# --------------------------------------------------------------------------- world model

# events that change nothing a DesktopState holds. Every other event, including ones
# this version has never heard of, triggers a fresh snapshot.
_NO_STATE_CHANGE = frozenset(
    {"custom", "bell", "activelayout", "submap", "screencast", "screencastv2"}
)

StateCallback = Callable[[DesktopState], None]
CustomCallback = Callable[[str], None]


class WorldModel:
    """The latest DesktopState, kept current from the event socket.

    Events are not applied one by one. Any event that could change the state schedules one
    fresh snapshot about 30 ms later and the events in between ride along, so a burst (a
    workspace switch is five events) costs one read of under a millisecond. The timer
    is not restarted by later events: a terminal whose title spins several times a second
    would otherwise postpone the snapshot for ever.

    Callbacks are plain functions called on the event loop; keep them short. One that
    raises is logged and does not stop the others.
    """

    def __init__(
        self,
        sock: HyprSocket | None = None,
        reader: EventReader | None = None,
        *,
        debounce: float = 0.03,
    ) -> None:
        self._sock = sock if sock is not None else HyprSocket()
        self._reader = reader if reader is not None else EventReader()
        self._debounce = debounce
        self._state = DesktopState()
        self._pending: asyncio.TimerHandle | None = None
        self._state_callbacks: list[StateCallback] = []
        self._custom_callbacks: list[CustomCallback] = []

    @property
    def state(self) -> DesktopState:
        return self._state

    def subscribe(self, callback: StateCallback) -> Callable[[], None]:
        """Call `callback(state)` whenever the desktop changes. Returns the unsubscribe."""
        self._state_callbacks.append(callback)
        return lambda: self._state_callbacks.remove(callback)

    def on_custom(self, callback: CustomCallback) -> Callable[[], None]:
        """Call `callback(data)` for every `custom>>data` event, which is how the
        push-to-talk binds reach the daemon. Returns the unsubscribe."""
        self._custom_callbacks.append(callback)
        return lambda: self._custom_callbacks.remove(callback)

    def refresh(self) -> DesktopState:
        """Snapshot now. Blocking socket IO of under a millisecond, fine to call inline."""
        self._cancel_pending()
        self._replace(snapshot(self._sock))
        return self._state

    async def run(self) -> None:
        """Follow the event socket until cancelled."""
        async with contextlib.aclosing(aiter(self._reader)) as events:
            try:
                async for event, data in events:
                    self._handle(event, data)
            finally:
                self._cancel_pending()

    def _handle(self, event: str, data: str) -> None:
        if event == "custom":
            # take a waiting snapshot first, so the push-to-talk handler pins the window
            # that is focused at key down and not the one from 30 ms earlier
            if self._pending is not None:
                self.refresh()
            self._call(self._custom_callbacks, data)
        elif event == DISCONNECTED:
            self._cancel_pending()
            self._replace(DesktopState())
        elif event in (CONNECTED, "configreloaded"):
            # configreloaded can change the config provider, which a snapshot re-reads
            self.refresh()
        elif event not in _NO_STATE_CHANGE and self._pending is None:
            loop = asyncio.get_running_loop()
            self._pending = loop.call_later(self._debounce, self.refresh)

    def _cancel_pending(self) -> None:
        if self._pending is not None:
            self._pending.cancel()
            self._pending = None

    def _replace(self, new: DesktopState) -> None:
        # the stamp always moves on; subscribers only hear about a change of content
        changed = replace(new, stamp=self._state.stamp) != self._state
        self._state = new
        if changed:
            self._call(self._state_callbacks, new)

    @staticmethod
    def _call(callbacks: list[Callable[[Any], None]], value: Any) -> None:
        for callback in list(callbacks):
            try:
                callback(value)
            except Exception:
                # never the value: custom data and window titles stay out of the log
                log.exception("a world model subscriber raised")
