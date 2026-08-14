"""Minimal Wayland client speaking zwlr_virtual_pointer_unstable_v1.

hypruse injects pointer input by being a regular Wayland client of the
user's compositor, the same mechanism wlrctl uses, rather than a uinput
daemon (ydotool) or the RemoteDesktop portal (which
xdg-desktop-portal-hyprland does not implement). Nothing here needs root,
/dev/uinput access, or a background service.

Wire format, little-endian:
    message: u32 object-id, u32 (size << 16 | opcode), args
    string:  u32 length (incl. NUL), bytes, NUL, pad to 4
    fixed:   signed 24.8 fixed point

Opcodes match the protocol XML shipped in Hyprland's own tree
(protocols/wlr-virtual-pointer-unstable-v1.xml).
"""

from __future__ import annotations

import os
import socket
import tempfile
import struct
import time

DISPLAY_ID = 1
# wl_display requests
REQ_SYNC, REQ_GET_REGISTRY = 0, 1
# wl_display events
EV_ERROR, EV_DELETE_ID = 0, 1
# zwlr_virtual_pointer_manager_v1 requests
MGR_CREATE_POINTER, MGR_DESTROY = 0, 1
# zwlr_virtual_pointer_v1 requests
PTR_MOTION, PTR_MOTION_ABSOLUTE, PTR_BUTTON, PTR_AXIS, PTR_FRAME = 0, 1, 2, 3, 4
PTR_AXIS_SOURCE, PTR_AXIS_STOP, PTR_AXIS_DISCRETE, PTR_DESTROY = 5, 6, 7, 8

MANAGER_INTERFACE = "zwlr_virtual_pointer_manager_v1"
SEAT_INTERFACE    = "wl_seat"
SEAT_EV_NAME      = 1

# Multi-seat: when HYPRUSE_SEAT names a seat, the virtual pointer is created ON
# that seat instead of the compositor default. Requires a compositor that
# advertises more than one wl_seat; on stock Hyprland this finds nothing and we
# fall back to the default seat, so the variable is safe to leave set.
SEAT_ENV          = "HYPRUSE_SEAT"

BUTTONS = {
    "left": 0x110,
    "right": 0x111,
    "middle": 0x112,
    "back": 0x113,
    "forward": 0x114,
}
PRESSED, RELEASED = 1, 0
AXIS_VERTICAL, AXIS_HORIZONTAL = 0, 1
AXIS_SOURCE_WHEEL = 0
SCROLL_UNITS_PER_NOTCH = 15.0  # touchpad-coordinate length of one wheel notch


class WireError(RuntimeError):
    """Protocol error or unusable compositor connection."""


# --- pure wire helpers -----------------------------------------------------


def wl_string(s: str) -> bytes:
    raw = s.encode() + b"\x00"
    pad = (4 - len(raw) % 4) % 4
    return struct.pack("<I", len(raw)) + raw + b"\x00" * pad


def to_fixed(value: float) -> int:
    """Wayland signed 24.8 fixed point."""
    return int(round(value * 256))


def encode_msg(obj: int, opcode: int, body: bytes = b"") -> bytes:
    size = 8 + len(body)
    return struct.pack("<II", obj, (size << 16) | opcode) + body


def parse_events(buf: bytes) -> tuple[list[tuple[int, int, bytes]], bytes]:
    """Split a byte stream into complete (object, opcode, body) events + remainder."""
    events = []
    while len(buf) >= 8:
        obj, sizeop = struct.unpack_from("<II", buf, 0)
        size, opcode = sizeop >> 16, sizeop & 0xFFFF
        if size < 8 or len(buf) < size:
            break
        events.append((obj, opcode, buf[8:size]))
        buf = buf[size:]
    return events, buf


def parse_global(body: bytes) -> tuple[int, str, int]:
    """wl_registry.global(name: uint, interface: string, version: uint)."""
    name = struct.unpack_from("<I", body, 0)[0]
    slen = struct.unpack_from("<I", body, 4)[0]
    interface = body[8 : 8 + slen - 1].decode()
    pad = (4 - slen % 4) % 4
    version = struct.unpack_from("<I", body, 8 + slen + pad)[0]
    return name, interface, version


def parse_error(body: bytes) -> tuple[int, int, str]:
    """wl_display.error(object_id: object, code: uint, message: string)."""
    obj, code = struct.unpack_from("<II", body, 0)
    slen = struct.unpack_from("<I", body, 8)[0]
    message = body[12 : 12 + slen - 1].decode(errors="replace")
    return obj, code, message


def _now_ms() -> int:
    return int(time.monotonic() * 1000) & 0xFFFFFFFF


def scroll_messages(
    dy: float = 0.0, dx: float = 0.0, discrete_ok: bool = True
) -> list[tuple[int, bytes]]:
    """The (opcode, body) sequence for one wheel scroll. Whole-notch deltas
    go out as axis_discrete (carrying both the continuous value and the
    notch count) so applications that step per wheel click see real
    notches; fractional deltas keep the plain continuous axis event.
    axis_discrete exists only since protocol v2, so callers bound at v1
    pass discrete_ok=False to stay on the continuous path."""
    t = _now_ms()
    msgs = [(PTR_AXIS_SOURCE, struct.pack("<I", AXIS_SOURCE_WHEEL))]
    for axis, v in ((AXIS_VERTICAL, dy), (AXIS_HORIZONTAL, dx)):
        if not v:
            continue
        value = to_fixed(v * SCROLL_UNITS_PER_NOTCH)
        notches = int(round(v))
        if discrete_ok and notches and abs(v - notches) < 1e-6:
            msgs.append((PTR_AXIS_DISCRETE, struct.pack("<IIii", t, axis, value, notches)))
        else:
            msgs.append((PTR_AXIS, struct.pack("<IIi", t, axis, value)))
    msgs.append((PTR_FRAME, b""))
    return msgs


# --- live connection ---------------------------------------------------------


class VirtualPointer:
    """One compositor connection owning one virtual pointer device.

    Cheap to create (~1 ms); the server keeps one alive and recreates it if
    the connection drops. Use as a context manager to guarantee the device
    is destroyed, a leaked virtual pointer lingers in `hyprctl devices`.
    """

    def __init__(self, display: str | None = None):
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        display = display or os.environ.get("WAYLAND_DISPLAY", "wayland-0")
        if not runtime and not display.startswith("/"):
            raise WireError("XDG_RUNTIME_DIR not set, not inside a Wayland session?")
        path = display if display.startswith("/") else os.path.join(runtime, display)
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(3.0)
        try:
            self._sock.connect(path)
        except OSError as exc:
            raise WireError(f"cannot connect to Wayland display at {path}: {exc}") from exc
        self._buf = b""
        self._next_id = 2
        self._registry = self._new_id()
        self._send(DISPLAY_ID, REQ_GET_REGISTRY, struct.pack("<I", self._registry))
        globals_seen = self._roundtrip(collect_globals=True)
        match = [(n, v) for n, i, v in globals_seen if i == MANAGER_INTERFACE]
        if not match:
            raise WireError(f"compositor does not advertise {MANAGER_INTERFACE}")
        name, version = match[0]
        # The pointer inherits the manager's negotiated version; axis_discrete
        # only exists since v2, so remember what we actually bound.
        self._version = min(version, 2)
        self._manager = self._new_id()
        self._send(
            self._registry,
            0,  # wl_registry.bind
            struct.pack("<I", name)
            + wl_string(MANAGER_INTERFACE)
            + struct.pack("<II", self._version, self._manager),
        )
        self._globals_cache = globals_seen
        self._seat = self._resolve_seat(globals_seen)

        self._pointer = self._new_id()
        self._send(self._manager, MGR_CREATE_POINTER, struct.pack("<II", self._seat, self._pointer))
        self._roundtrip()

    def _resolve_seat(self, globals_seen: list[tuple[int, str, int]]) -> int:
        """Object id of the requested wl_seat, or 0 for the compositor default.

        zwlr_virtual_pointer_manager_v1.create_virtual_pointer takes the seat the
        device belongs to. hypruse has always passed 0 (null) because there was
        only ever one seat. Naming one routes this pointer to it, which is how an
        agent gets its own cursor instead of sharing the human's.
        """
        wanted = os.environ.get(SEAT_ENV, "").strip()
        if not wanted:
            return 0

        bound: dict[int, str] = {}
        for name, iface, version in globals_seen:
            if iface != SEAT_INTERFACE:
                continue
            oid = self._new_id()
            self._send(
                self._registry,
                0,
                struct.pack("<I", name) + wl_string(SEAT_INTERFACE) + struct.pack("<II", min(version, 9), oid),
            )
            bound[oid] = ""

        if not bound:
            return 0

        # names arrive as wl_seat.name events during the next roundtrip
        pending = dict(bound)
        self._seat_names = pending
        self._roundtrip()

        for oid, seat_name in pending.items():
            if seat_name == wanted:
                return oid

        # Fall back rather than fail. The variable is easy to leave set in a shell
        # profile and then use on a compositor with only one seat, where hard
        # failure makes every single call error out for no good reason. Seat 0 is
        # the compositor default, which is what hypruse always used.
        import warnings

        warnings.warn(
            f"{SEAT_ENV}={wanted!r} but this compositor advertises no such seat "
            f"(saw: {sorted(n for n in pending.values() if n)}); "
            f"using the default seat",
            RuntimeWarning,
            stacklevel=2,
        )
        return 0

    # -- plumbing --

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id - 1

    def _send(self, obj: int, opcode: int, body: bytes = b"") -> None:
        try:
            self._sock.sendall(encode_msg(obj, opcode, body))
        except OSError as exc:
            raise WireError(f"compositor connection lost: {exc}") from exc

    def _roundtrip(self, collect_globals: bool = False) -> list[tuple[int, str, int]]:
        """Sync barrier: returns once the compositor has processed everything sent."""
        done_cb = self._new_id()
        self._send(DISPLAY_ID, REQ_SYNC, struct.pack("<I", done_cb))
        globals_seen: list[tuple[int, str, int]] = []
        while True:
            try:
                chunk = self._sock.recv(65536)
            except TimeoutError as exc:
                raise WireError("compositor did not answer sync") from exc
            if not chunk:
                raise WireError("compositor closed the connection")
            self._buf += chunk
            events, self._buf = parse_events(self._buf)
            for obj, opcode, body in events:
                if obj == DISPLAY_ID and opcode == EV_ERROR:
                    err_obj, code, msg = parse_error(body)
                    raise WireError(f"wl_display.error object={err_obj} code={code}: {msg}")
                if obj == self._registry and opcode == 0 and collect_globals:
                    globals_seen.append(parse_global(body))
                names = getattr(self, "_seat_names", None)
                if names is not None and obj in names and opcode == SEAT_EV_NAME:
                    slen = struct.unpack_from("<I", body, 0)[0]
                    names[obj] = body[4 : 4 + slen - 1].decode(errors="replace")
                if obj == done_cb and opcode == 0:  # wl_callback.done
                    return globals_seen

    # -- pointer actions --

    def button(self, name: str, state: int) -> None:
        code = BUTTONS[name]
        self._send(self._pointer, PTR_BUTTON, struct.pack("<III", _now_ms(), code, state))
        self._send(self._pointer, PTR_FRAME)
        self._roundtrip()

    def click(self, name: str = "left", *, double: bool = False) -> None:
        for i in range(2 if double else 1):
            if i:
                time.sleep(0.06)
            self.button(name, PRESSED)
            time.sleep(0.02)
            self.button(name, RELEASED)

    def scroll(self, dy: float = 0.0, dx: float = 0.0) -> None:
        """Scroll by wheel notches; positive dy scrolls content down."""
        for opcode, body in scroll_messages(dy, dx, discrete_ok=self._version >= 2):
            self._send(self._pointer, opcode, body)
        self._roundtrip()

    def close(self) -> None:
        try:
            self._send(self._pointer, PTR_DESTROY)
            self._send(self._manager, MGR_DESTROY)
            self._roundtrip()
        except WireError:
            pass
        finally:
            self._sock.close()

    def __enter__(self) -> VirtualPointer:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

# --- virtual keyboard -------------------------------------------------------
# wtype cannot target a seat, so multi-seat text needs our own keyboard. The
# trick wtype uses, and we reuse: instead of mapping characters onto the user's
# layout, generate a keymap where consecutive keycodes ARE the characters we want
# to type. Layout-independent and unicode-correct by construction.

VK_INTERFACE = "zwp_virtual_keyboard_manager_v1"
VK_CREATE = 0
VK_KEYMAP, VK_KEY, VK_MODIFIERS = 0, 1, 2
XKB_KEYMAP_FORMAT_V1 = 1
KEYCODE_BASE = 8  # evdev offset: xkb keycode = evdev + 8


def _keymap_for(chars: list[str]) -> bytes:
    """An XKB keymap whose Nth key produces the Nth character."""
    keycodes, symbols = [], []
    for i, ch in enumerate(chars):
        kc = KEYCODE_BASE + 1 + i
        keycodes.append(f"    <K{i}> = {kc};")
        symbols.append(f"    key <K{i}> {{ [ U{ord(ch):04X} ] }};")
    return (
        "xkb_keymap {\n"
        "  xkb_keycodes {\n"
        f"    minimum = {KEYCODE_BASE};\n"
        f"    maximum = {KEYCODE_BASE + len(chars) + 1};\n"
        + "\n".join(keycodes)
        + "\n  };\n"
        '  xkb_types { include "complete" };\n'
        '  xkb_compat { include "complete" };\n'
        '  xkb_symbols "(unnamed)" {\n'
        + "\n".join(symbols)
        + "\n  };\n"
        "};\n"
    ).encode()


class VirtualKeyboard(VirtualPointer):
    """A virtual keyboard on a chosen seat. Reuses VirtualPointer's connection.

    Subclassing keeps the wire plumbing in one place; the inherited pointer is
    created and simply unused.
    """

    def type_text(self, text: str) -> None:
        if not text:
            return

        chars = list(dict.fromkeys(text))  # unique, order preserved
        keymap = _keymap_for(chars)
        index = {ch: i for i, ch in enumerate(chars)}

        vk = [(n, v) for n, i, v in self._globals_cache if i == VK_INTERFACE]
        if not vk:
            raise WireError(f"compositor does not advertise {VK_INTERFACE}")
        name, version = vk[0]
        mgr = self._new_id()
        self._send(
            self._registry,
            0,
            struct.pack("<I", name) + wl_string(VK_INTERFACE) + struct.pack("<II", min(version, 1), mgr),
        )
        kb = self._new_id()
        self._send(mgr, VK_CREATE, struct.pack("<II", self._seat, kb))
        self._roundtrip()

        with tempfile.TemporaryFile() as tf:
            tf.write(keymap)
            tf.flush()
            tf.seek(0)
            msg = encode_msg(kb, VK_KEYMAP, struct.pack("<II", XKB_KEYMAP_FORMAT_V1, len(keymap)))
            self._sock.sendmsg(
                [msg], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack("i", tf.fileno()))]
            )
            self._roundtrip()

            for ch in text:
                code = index[ch] + 1  # evdev code; compositor adds the +8 offset
                for state in (PRESSED, RELEASED):
                    self._send(kb, VK_KEY, struct.pack("<III", _now_ms(), code, state))
                self._roundtrip()
