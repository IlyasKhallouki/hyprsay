"""Push to talk: turning a held key into exactly one "down" and one "up" per utterance.

The key never reaches this process as a key. Hyprland owns the keyboard, so a bind
in the user's config has to tell us, and there are two ways for it to do that:

- "event" (the default, zero dependencies): `bind` and `bindr` run the `event`
  dispatcher, which emits `custom>>DATA` on socket2. The world model already reads
  socket2 and hands DATA to `feed_custom`. The dispatcher name and its single DATA
  argument are from docs/research/hypr-ipc.md (the 0.56.2 dispatcher table). Any
  local client that can reach the request socket can emit the same event, so this
  transport is forgeable by design.
- "global": `hyprland_global_shortcuts_manager_v1` over the Wayland wire. The
  compositor sends pressed and released straight to our connection, so nothing is
  spawned and nothing on socket2 can imitate it. The live 0.56.2 compositor
  advertises the manager at version 1.

Neither transport can be trusted to deliver the release. Hyprland skips every bind
that is not marked locked while the session is locked, so a key released behind the
lock screen produces no "up" at all, and a compositor restart drops it too. The
state machine therefore owns the guarantees, not the transport: a second "down"
while held is ignored, an "up" while idle is ignored, a hold longer than
`cfg.ptt.max_hold_s` is ended with a synthetic "up" (reason "max_hold"), and the
daemon can end one at any moment with `force_up`.

Threading: all state lives on one asyncio loop. Every public method may be called
from any thread (the lock latch fires from whichever thread noticed the lock); calls
made off the loop are forwarded with `call_soon_threadsafe`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import socket
import struct
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Literal

from hypruse.wire import (
    DISPLAY_ID,
    EV_ERROR,
    REQ_GET_REGISTRY,
    REQ_SYNC,
    WireError,
    encode_msg,
    parse_error,
    parse_events,
    parse_global,
    wl_string,
)

from .config import Config

EVENT_DOWN = "hyprsay:down"
EVENT_UP = "hyprsay:up"
EVENT_TOGGLE = "hyprsay:toggle"

APP_ID = "hyprsay"
SHORTCUT_ID = "ptt"


@dataclass(frozen=True)
class PttEvent:
    kind: Literal["down", "up"]
    # "key", "toggle", "max_hold", or whatever the caller of force_up gave
    reason: str
    at: float  # the clock when the cause was seen, not when the event was consumed


class PushToTalk:
    """The state machine. Transports feed it; the daemon iterates `events()`."""

    def __init__(self, cfg: Config, *, clock: Callable[[], float] = time.monotonic) -> None:
        if cfg.ptt.max_hold_s <= 0:
            # a hold that can never expire is the one failure this class exists to prevent
            raise ValueError("ptt.max_hold_s must be greater than zero")
        self._max_hold = cfg.ptt.max_hold_s
        self._latch = cfg.ptt.latch
        self._clock = clock
        self._queue: asyncio.Queue[PttEvent | None] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._held = False
        self._timer: asyncio.TimerHandle | None = None

    @property
    def held(self) -> bool:
        return self._held

    # ------------------------------------------------------------------ inputs

    def feed_custom(self, data: str) -> None:
        """One socket2 `custom>>` payload. Anything but our three exact strings is
        somebody else's event and is ignored without comment."""
        action = {EVENT_DOWN: self._press, EVENT_UP: self._release, EVENT_TOGGLE: self._toggle}
        handler = action.get(data)
        if handler is not None:
            self._on_loop(handler, self._clock())

    def press(self) -> None:
        self._on_loop(self._press, self._clock())

    def release(self) -> None:
        self._on_loop(self._release, self._clock())

    def toggle(self) -> None:
        self._on_loop(self._toggle, self._clock())

    def force_up(self, reason: str) -> None:
        """End the current hold, if there is one. Safe from any thread, and safe
        before the loop exists, when nothing can be held yet."""
        self._on_loop(self._up, reason, self._clock(), required=False)

    def close(self) -> None:
        """End `events()`. A hold in progress is closed first so the pair stays whole."""
        self._on_loop(self._close, self._clock(), required=False)

    # ------------------------------------------------------------------ output

    async def events(self) -> AsyncIterator[PttEvent]:
        self._loop = asyncio.get_running_loop()
        while (event := await self._queue.get()) is not None:
            yield event

    # ------------------------------------------------------------------ on the loop

    def _on_loop(self, fn: Callable[..., None], *args: object, required: bool = True) -> None:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if self._loop is None:
            if running is None:
                if required:
                    raise RuntimeError("PushToTalk was fed before its event loop was running")
                return
            self._loop = running
        if running is self._loop:
            fn(*args)
            return
        # the loop closing under us means the daemon is going away; nothing to deliver to
        with contextlib.suppress(RuntimeError):
            self._loop.call_soon_threadsafe(fn, *args)

    def _press(self, at: float) -> None:
        if self._latch:
            self._toggle(at)
        else:
            self._down("key", at)

    def _release(self, at: float) -> None:
        # in latch mode the key coming back up means nothing; the next press ends it
        if not self._latch:
            self._up("key", at)

    def _toggle(self, at: float) -> None:
        if self._held:
            self._up("toggle", at)
        else:
            self._down("toggle", at)

    def _down(self, reason: str, at: float) -> None:
        if self._held:
            return
        self._held = True
        assert self._loop is not None
        self._timer = self._loop.call_later(self._max_hold, self._expire)
        self._queue.put_nowait(PttEvent("down", reason, at))

    def _up(self, reason: str, at: float) -> None:
        if not self._held:
            return
        self._held = False
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._queue.put_nowait(PttEvent("up", reason, at))

    def _expire(self) -> None:
        self._up("max_hold", self._clock())

    def _close(self, at: float) -> None:
        self._up("closed", at)
        self._queue.put_nowait(None)


# --------------------------------------------------------------------------- config lines


def bind_lines(provider: str, key: str = "SUPER, V", *, transport: str = "event") -> list[str]:
    """The lines the user adds to their Hyprland config, in their config dialect.

    `key` is in hyprlang form, "MODS, KEY". The same lines serve hold and latch mode:
    in latch mode the release event still arrives and is ignored.
    """
    if transport not in {"event", "global"}:
        raise ValueError(f"unknown ptt transport {transport!r}")
    if provider == "lua":
        return _lua_lines(key, transport)
    if provider != "hyprlang":
        raise ValueError(f"unknown config provider {provider!r}")
    if transport == "global":
        # `global` is one of the dispatchers Hyprland fires on release as well as press
        return [f"bind = {key}, global, {APP_ID}:{SHORTCUT_ID}"]
    return [f"bind = {key}, event, {EVENT_DOWN}", f"bindr = {key}, event, {EVENT_UP}"]


def _lua_lines(key: str, transport: str) -> list[str]:
    combo = _lua_combo(key)
    note = (
        "-- UNVERIFIED: written from /usr/share/hypr/stubs/hl.meta.lua "
        "(hl.bind, hl.dsp, BindOptions.release), never run against a Lua config"
    )
    if transport == "global":
        return [note, f'hl.bind("{combo}", hl.dsp.global("{APP_ID}:{SHORTCUT_ID}"))']
    return [
        note,
        f'hl.bind("{combo}", hl.dsp.event("{EVENT_DOWN}"))',
        f'hl.bind("{combo}", hl.dsp.event("{EVENT_UP}"), {{ release = true }})',
    ]


def _lua_combo(key: str) -> str:
    """Turn "SUPER SHIFT, V" into "SUPER + SHIFT + V". Hyprlang lets modifiers be joined
    by almost anything and no modifier name contains an underscore, so the modifier
    half is split freely; the key name is kept verbatim."""
    mods, _, name = key.rpartition(",")
    parts = [m for m in re.split(r"[\s+&|_]+", mods) if m] + [name.strip()]
    return " + ".join(parts)


# --------------------------------------------------------------------------- global shortcuts
#
# Opcodes are positions in /usr/share/hyprland-protocols/protocols/
# hyprland-global-shortcuts-v1.xml; the tests check them against that file when it exists.

MANAGER_INTERFACE = "hyprland_global_shortcuts_manager_v1"
MANAGER_VERSION = 1
MGR_REGISTER_SHORTCUT, MGR_DESTROY = 0, 1
SHORTCUT_DESTROY = 0
SHORTCUT_EV_PRESSED, SHORTCUT_EV_RELEASED = 0, 1
REGISTRY_BIND = 0
REGISTRY_EV_GLOBAL = 0
CALLBACK_EV_DONE = 0

HANDSHAKE_TIMEOUT_S = 3.0


def encode_bind(registry: int, name: int, interface: str, version: int, new_id: int) -> bytes:
    """wl_registry.bind. Its new_id has no interface in the XML, so the wire carries
    the interface name and version in front of the id."""
    body = struct.pack("<I", name) + wl_string(interface) + struct.pack("<II", version, new_id)
    return encode_msg(registry, REGISTRY_BIND, body)


def encode_register_shortcut(
    manager: int,
    new_id: int,
    shortcut_id: str = SHORTCUT_ID,
    app_id: str = APP_ID,
    description: str = "Push to talk",
    trigger_description: str = "",
) -> bytes:
    body = struct.pack("<I", new_id) + b"".join(
        wl_string(s) for s in (shortcut_id, app_id, description, trigger_description)
    )
    return encode_msg(manager, MGR_REGISTER_SHORTCUT, body)


@dataclass(frozen=True)
class ShortcutSignal:
    pressed: bool
    # the compositor's own timestamp, seconds. Kept for the journal; PushToTalk stamps
    # events with our clock so both transports are comparable.
    stamp: float


def decode_shortcut_event(opcode: int, body: bytes) -> ShortcutSignal | None:
    """pressed and released both carry (tv_sec_hi, tv_sec_lo, tv_nsec). None for an
    opcode this protocol version does not define."""
    if opcode not in (SHORTCUT_EV_PRESSED, SHORTCUT_EV_RELEASED) or len(body) < 12:
        return None
    sec_hi, sec_lo, nsec = struct.unpack_from("<III", body, 0)
    return ShortcutSignal(opcode == SHORTCUT_EV_PRESSED, ((sec_hi << 32) | sec_lo) + nsec / 1e9)


class GlobalShortcut:
    """Transport "global": one Wayland connection holding one registered shortcut.

    hypruse.wire supplies the marshalling; its connection class is not reused because
    constructing it creates a virtual pointer device, which a listener has no business
    owning. `start` does the handshake on the calling thread (about a millisecond, and
    an `already_taken` error should fail startup, not a background thread), then a
    daemon thread blocks on the socket and forwards each signal to the loop.
    """

    def __init__(
        self,
        ptt: PushToTalk,
        *,
        display: str | None = None,
        on_lost: Callable[[str], None] | None = None,
    ) -> None:
        self._ptt = ptt
        self._display = display
        self._on_lost = on_lost
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._buf = b""
        self._next_id = 2
        self._manager = 0
        self._shortcut = 0
        self._stopping = False

    def start(self) -> None:
        """Connect and register. Must be called on the running event loop."""
        if self._thread is not None:
            raise RuntimeError("GlobalShortcut already started")
        self._loop = asyncio.get_running_loop()
        self._sock = self._connect()
        try:
            self._register()
        except BaseException:
            self._sock.close()
            self._sock = None
            raise
        self._sock.settimeout(None)
        self._thread = threading.Thread(target=self._pump, name="hyprsay-ptt", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        sock, thread = self._sock, self._thread
        if sock is None:
            return
        self._stopping = True
        with contextlib.suppress(OSError):
            goodbye = encode_msg(self._shortcut, SHORTCUT_DESTROY)
            sock.sendall(goodbye + encode_msg(self._manager, MGR_DESTROY))
        # shutdown, not close: it is what wakes the thread out of a blocking recv
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        sock.close()
        self._sock = self._thread = None
        # with the connection gone the release can no longer arrive
        self._ptt.force_up("transport_stopped")

    # ------------------------------------------------------------------ handshake

    def _connect(self) -> socket.socket:
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        display = self._display or os.environ.get("WAYLAND_DISPLAY", "wayland-0")
        if not runtime and not display.startswith("/"):
            raise WireError("XDG_RUNTIME_DIR not set, not inside a Wayland session?")
        path = display if display.startswith("/") else os.path.join(runtime or "", display)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(HANDSHAKE_TIMEOUT_S)
        try:
            sock.connect(path)
        except OSError as exc:
            sock.close()
            raise WireError(f"cannot connect to Wayland display at {path}: {exc}") from exc
        return sock

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id - 1

    def _send(self, data: bytes) -> None:
        assert self._sock is not None
        try:
            self._sock.sendall(data)
        except OSError as exc:
            raise WireError(f"compositor connection lost: {exc}") from exc

    def _register(self) -> None:
        registry = self._new_id()
        self._send(encode_msg(DISPLAY_ID, REQ_GET_REGISTRY, struct.pack("<I", registry)))
        globals_seen = self._roundtrip(registry)
        match = [(n, v) for n, i, v in globals_seen if i == MANAGER_INTERFACE]
        if not match:
            raise WireError(f"compositor does not advertise {MANAGER_INTERFACE}")
        name, version = match[0]
        self._manager, self._shortcut = self._new_id(), self._new_id()
        self._send(
            encode_bind(
                registry, name, MANAGER_INTERFACE, min(version, MANAGER_VERSION), self._manager
            )
            + encode_register_shortcut(self._manager, self._shortcut)
        )
        # a second barrier so a protocol error (already_taken) surfaces here
        self._roundtrip(registry)

    def _roundtrip(self, registry: int) -> list[tuple[int, str, int]]:
        assert self._sock is not None
        done = self._new_id()
        self._send(encode_msg(DISPLAY_ID, REQ_SYNC, struct.pack("<I", done)))
        globals_seen: list[tuple[int, str, int]] = []
        while True:
            try:
                chunk = self._sock.recv(65536)
            except TimeoutError as exc:
                raise WireError("compositor did not answer sync") from exc
            except OSError as exc:
                raise WireError(f"compositor connection lost: {exc}") from exc
            if not chunk:
                raise WireError("compositor closed the connection")
            self._buf += chunk
            events, self._buf = parse_events(self._buf)
            finished = False
            for obj, opcode, body in events:
                if obj == DISPLAY_ID and opcode == EV_ERROR:
                    raise WireError(_describe_error(body))
                if obj == registry and opcode == REGISTRY_EV_GLOBAL:
                    globals_seen.append(parse_global(body))
                elif obj == done and opcode == CALLBACK_EV_DONE:
                    finished = True
                else:
                    # a key can be pressed between register and the barrier
                    self._dispatch(obj, opcode, body)
            if finished:
                return globals_seen

    # ------------------------------------------------------------------ the thread

    def _pump(self) -> None:
        sock = self._sock
        assert sock is not None
        reason = "compositor closed the connection"
        try:
            while chunk := sock.recv(65536):
                self._buf += chunk
                events, self._buf = parse_events(self._buf)
                for obj, opcode, body in events:
                    if obj == DISPLAY_ID and opcode == EV_ERROR:
                        raise WireError(_describe_error(body))
                    self._dispatch(obj, opcode, body)
        except (OSError, WireError) as exc:
            reason = str(exc)
        if not self._stopping:
            self._lost(reason)

    def _dispatch(self, obj: int, opcode: int, body: bytes) -> None:
        if obj != self._shortcut:
            return
        signal = decode_shortcut_event(opcode, body)
        if signal is not None:
            self._to_loop(self._ptt.press if signal.pressed else self._ptt.release)

    def _lost(self, reason: str) -> None:
        # without a connection the release can never arrive, so end the hold now
        self._to_loop(self._ptt.force_up, "transport_lost")
        if self._on_lost is not None:
            self._to_loop(self._on_lost, reason)

    def _to_loop(self, fn: Callable[..., None], *args: object) -> None:
        assert self._loop is not None
        with contextlib.suppress(RuntimeError):  # loop already closed
            self._loop.call_soon_threadsafe(fn, *args)


def _describe_error(body: bytes) -> str:
    obj, code, message = parse_error(body)
    return f"wl_display.error object={obj} code={code}: {message}"
