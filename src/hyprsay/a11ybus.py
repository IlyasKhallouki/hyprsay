"""AT-SPI over one persistent D-Bus connection, installed under the inherited a11y code.

WHY. `hypruse.a11y` reaches the accessibility bus by spawning `busctl` once per D-Bus
call, and a tree walk is three of those per node visited and six per control kept. A
window the size of the KDE calculator, 48 controls, took 16.2 s that way. A spoken
command has to be decided in well under a second, so click-by-name cannot ship on that
transport. This module keeps ONE authenticated connection to the bus and speaks the wire
protocol on it, and `use_fast_transport()` gives the inherited hypruse code that saving
without touching it, exactly as `world.py` did for `hyprctl`.

MEASURED, on this machine, against the live bus:

- ONE D-BUS CALL. Through busctl, a median of 9.8 ms (min 6.5) over 60 spawns; a run
  earlier the same day, under heavier load, had a median of 22.7 ms and a max of 49.9.
  Over the connection here, a median of 0.37 ms (min 0.25) over 500 calls. About 26x.
- ONE WHOLE TREE. `hypruse.a11y.find_elements` over waybar's subtree, 91 nodes, 3
  controls: busctl a median of 4.8 s over 7 walks (2.9 to 6.2), this transport 0.47 s
  (0.29 to 0.72). About 10x. Both transports were run alternately in one interpreter, so
  machine load lands on both, and both walks returned the same controls.
- ONE `hypruse.server.ui()`. Against a window that publishes no tree, which is the case
  the walk answers fastest and the one a speaker hits most often by accident: 0.807 s
  through busctl, 0.108 s here, and the same sentence out of both.
- WHY 10x AND NOT 26x. What is left is the application's own main loop. A call the bus
  daemon answers itself costs 0.37 ms, while the same call answered by waybar costs 1 to
  9 ms, and no transport can remove that. Pipelining several calls per node is the next
  lever, and it would need a different shape than one blocking call at a time.

The 48-control window above was not re-measured, because KCalc was not running and this
work was not allowed to launch it. On the tree ratio, its 16.2 s lands near 1.6 s.

WHY THE STDLIB AND NOT jeepney. jeepney is pure python and would carry the marshalling,
but it would add a dependency and a second connection model (its blocking client opens
its own connection and has its own auth) for the handful of AT-SPI methods and the one
property getter that `a11y.py` actually calls. What is needed is under 200 lines of
struct packing, and keeping it here means the reconnect and the fall back to busctl live
next to the swap that needs them. The cost of that choice is that only the subset below
is implemented: anything else falls back to the subprocess rather than failing.

THE SUBSET. Little-endian METHOD_CALL out, METHOD_RETURN and ERROR in (both endiannesses
read, since a message carries its own and the daemon relays it unconverted). SASL
EXTERNAL on connect, then `Hello`. Signals and stale replies are skipped by serial, which
is also why a call that overruns its deadline may keep the connection: its late reply is
discarded by the next read rather than mistaken for the next answer.

FAILURE. An ERROR reply is a real answer (`UnknownMethod` is how `a11y.py` discovers that
a widget has no Text interface), so it arrives as the `A11yError` the callers already
catch. A connection that drops is reopened once, silently. Anything else at all, an
address this code cannot parse, a verb with no wire form, a bus that will not authenticate,
falls back to the original busctl implementation, so a broken bus degrades rather than
breaks.

WHERE TO INSTALL IT. Next to `use_socket_transport()` at daemon startup. Nothing here
runs until something calls `use_fast_transport()`.
"""

from __future__ import annotations

import contextlib
import logging
import os
import socket
import struct
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any, NamedTuple
from urllib.parse import unquote

from hypruse import a11y

log = logging.getLogger(__name__)

LITTLE_ENDIAN = ord("l")
PROTOCOL_VERSION = 1

METHOD_CALL, METHOD_RETURN, ERROR, SIGNAL = 1, 2, 3, 4

# header field codes, D-Bus specification 0.42. UNIX_FDS (9) is never sent or read.
FIELD_PATH, FIELD_INTERFACE, FIELD_MEMBER, FIELD_ERROR_NAME = 1, 2, 3, 4
FIELD_REPLY_SERIAL, FIELD_DESTINATION, FIELD_SENDER, FIELD_SIGNATURE = 5, 6, 7, 8

DBUS_SERVICE = "org.freedesktop.DBus"
DBUS_PATH = "/org/freedesktop/DBus"
PROPERTIES = "org.freedesktop.DBus.Properties"

_ALIGNMENT = {
    "y": 1,
    "b": 4,
    "n": 2,
    "q": 2,
    "i": 4,
    "u": 4,
    "x": 8,
    "t": 8,
    "d": 8,
    "h": 4,
    "s": 4,
    "o": 4,
    "g": 1,
    "a": 4,
    "v": 1,
    "(": 8,
    "{": 8,
}
# the fixed-width types and their struct codes; the endianness prefix is added per message
_FIXED = {"y": "B", "n": "h", "q": "H", "i": "i", "u": "I", "x": "q", "t": "Q", "d": "d"}
_STRINGS = ("s", "o")


class DBusError(RuntimeError):
    """The peer answered an ERROR reply. That is an answer, not a transport failure."""

    def __init__(self, name: str, message: str = "") -> None:
        super().__init__(f"{name}: {message}" if message else name)
        self.name = name
        self.message = message


class TransportError(RuntimeError):
    """The connection could not be made or kept, or the call has no wire form here."""


class ReplyTimeout(RuntimeError):
    """No reply inside the deadline, which is what busctl's own timeout means."""


class Variant(NamedTuple):
    """A variant kept whole: a property read needs the signature inside it, not only
    the value, to report the `type` that busctl prints."""

    signature: str
    value: Any


class Message(NamedTuple):
    """One parsed D-Bus message. Header fields that were absent read as empty."""

    type: int
    serial: int
    reply_serial: int
    signature: str
    body: list[Any]
    error_name: str
    member: str
    interface: str
    path: str


# --------------------------------------------------------------------------- signatures


def split_signature(signature: str) -> list[str]:
    """A signature cut into its complete single types: `"ii"` is two, `"a(so)"` is one."""
    types: list[str] = []
    index = 0
    while index < len(signature):
        end = _type_end(signature, index)
        types.append(signature[index:end])
        index = end
    return types


def _type_end(signature: str, index: int) -> int:
    code = signature[index] if index < len(signature) else ""
    if code == "a":
        return _type_end(signature, index + 1)
    if code in "({":
        closer = ")" if code == "(" else "}"
        depth = 0
        for position in range(index, len(signature)):
            if signature[position] == code:
                depth += 1
            elif signature[position] == closer:
                depth -= 1
                if depth == 0:
                    return position + 1
        raise TransportError(f"unterminated container in the signature {signature!r}")
    if code in _ALIGNMENT and code not in "a({":
        return index + 1
    raise TransportError(f"unsupported D-Bus type {code!r} in {signature!r}")


def _alignment(signature: str) -> int:
    try:
        return _ALIGNMENT[signature[0]]
    except (KeyError, IndexError) as exc:
        raise TransportError(f"no alignment for the D-Bus type {signature!r}") from exc


# --------------------------------------------------------------------------- marshalling


def _pad(buffer: bytearray, boundary: int) -> None:
    buffer.extend(b"\0" * (-len(buffer) % boundary))


def _write(buffer: bytearray, signature: str, value: Any) -> None:
    """Append one value of type `signature`, little-endian, padded to its alignment.

    Every offset is counted from the start of the buffer, which is why a body may be
    built on its own: the header before it is always padded to 8 first.
    """
    code = signature[0]
    if code in _FIXED:
        _pad(buffer, _ALIGNMENT[code])
        buffer.extend(struct.pack("<" + _FIXED[code], value))
    elif code == "b":
        _pad(buffer, 4)
        buffer.extend(struct.pack("<I", 1 if value else 0))
    elif code == "h":
        _pad(buffer, 4)
        buffer.extend(struct.pack("<I", int(value)))
    elif code in _STRINGS:
        raw = str(value).encode()
        _pad(buffer, 4)
        buffer.extend(struct.pack("<I", len(raw)) + raw + b"\0")
    elif code == "g":
        raw = str(value).encode()
        buffer.append(len(raw))
        buffer.extend(raw + b"\0")
    elif code == "v":
        inner_signature, inner = value
        _write(buffer, "g", inner_signature)
        _write(buffer, inner_signature, inner)
    elif code == "a":
        _write_array(buffer, signature[1:], value)
    elif code == "(":
        _pad(buffer, 8)
        members = split_signature(signature[1:-1])
        for member_signature, item in zip(members, value, strict=True):
            _write(buffer, member_signature, item)
    else:
        raise TransportError(f"cannot marshal the D-Bus type {signature!r}")


def _write_array(buffer: bytearray, element: str, value: Any) -> None:
    _pad(buffer, 4)
    length_at = len(buffer)
    buffer.extend(b"\0\0\0\0")
    # the length counts from AFTER the padding that the first element needs, so the
    # padding is laid down before the start is taken
    _pad(buffer, _alignment(element))
    start = len(buffer)
    if element.startswith("{"):
        key_signature, value_signature = split_signature(element[1:-1])
        items = value.items() if isinstance(value, dict) else value
        for key, item in items:
            _pad(buffer, 8)
            _write(buffer, key_signature, key)
            _write(buffer, value_signature, item)
    else:
        for item in value:
            _write(buffer, element, item)
    struct.pack_into("<I", buffer, length_at, len(buffer) - start)


def marshal_message(
    message_type: int,
    serial: int,
    *,
    path: str = "",
    interface: str = "",
    member: str = "",
    error_name: str = "",
    destination: str = "",
    reply_serial: int = 0,
    signature: str = "",
    args: Sequence[Any] = (),
) -> bytes:
    """One whole message: fixed header, the `a(yv)` field array, 8-byte pad, body."""
    body = bytearray()
    types = split_signature(signature)
    for member_signature, value in zip(types, args, strict=True):
        _write(body, member_signature, value)
    fields: list[tuple[int, Variant]] = []
    for code, kind, value in (
        (FIELD_PATH, "o", path),
        (FIELD_INTERFACE, "s", interface),
        (FIELD_MEMBER, "s", member),
        (FIELD_ERROR_NAME, "s", error_name),
        (FIELD_REPLY_SERIAL, "u", reply_serial),
        (FIELD_DESTINATION, "s", destination),
        (FIELD_SIGNATURE, "g", signature),
    ):
        if value:  # serial 0 is not a valid serial, so an absent reply_serial is 0
            fields.append((code, Variant(kind, value)))
    raw = bytearray(
        struct.pack("<BBBBII", LITTLE_ENDIAN, message_type, 0, PROTOCOL_VERSION, len(body), serial)
    )
    _write(raw, "a(yv)", fields)
    _pad(raw, 8)
    return bytes(raw + body)


def marshal_method_call(
    serial: int,
    destination: str,
    path: str,
    interface: str,
    member: str,
    signature: str = "",
    args: Sequence[Any] = (),
) -> bytes:
    return marshal_message(
        METHOD_CALL,
        serial,
        path=path,
        interface=interface,
        member=member,
        destination=destination,
        signature=signature,
        args=args,
    )


# --------------------------------------------------------------------------- parsing


class _Reader:
    """A cursor over one message. `position` is the offset alignment is counted from."""

    def __init__(self, data: bytes, position: int = 0, little: bool = True) -> None:
        self.data = data
        self.position = position
        self.endian = "<" if little else ">"

    def align(self, boundary: int) -> None:
        self.position += -self.position % boundary

    def take(self, count: int) -> bytes:
        end = self.position + count
        if end > len(self.data):
            raise TransportError("the message ended in the middle of a value")
        chunk = self.data[self.position : end]
        self.position = end
        return chunk

    def read(self, signature: str) -> Any:
        code = signature[0]
        if code in _FIXED:
            self.align(_ALIGNMENT[code])
            packing = self.endian + _FIXED[code]
            return struct.unpack(packing, self.take(struct.calcsize(packing)))[0]
        if code == "b":
            self.align(4)
            return bool(struct.unpack(self.endian + "I", self.take(4))[0])
        if code == "h":
            self.align(4)
            return struct.unpack(self.endian + "I", self.take(4))[0]
        if code in _STRINGS:
            self.align(4)
            length = struct.unpack(self.endian + "I", self.take(4))[0]
            text = self.take(length).decode()
            self.take(1)
            return text
        if code == "g":
            length = self.take(1)[0]
            text = self.take(length).decode()
            self.take(1)
            return text
        if code == "v":
            inner = self.read("g")
            return Variant(inner, self.read(inner) if inner else None)
        if code == "a":
            return self._read_array(signature[1:])
        if code == "(":
            self.align(8)
            return [self.read(member) for member in split_signature(signature[1:-1])]
        raise TransportError(f"cannot parse the D-Bus type {signature!r}")

    def _read_array(self, element: str) -> Any:
        self.align(4)
        length = struct.unpack(self.endian + "I", self.take(4))[0]
        self.align(_alignment(element))
        end = self.position + length
        if end > len(self.data):
            raise TransportError("an array ran past the end of the message")
        if element.startswith("{"):
            key_signature, value_signature = split_signature(element[1:-1])
            entries = {}
            while self.position < end:
                self.align(8)
                entries[self.read(key_signature)] = self.read(value_signature)
            return entries
        items = []
        while self.position < end:
            items.append(self.read(element))
        return items


def message_length(raw: bytes | bytearray) -> int | None:
    """The whole message's length, or None while too few bytes have arrived to say."""
    if len(raw) < 16:
        return None
    endian = "<" if raw[0] == LITTLE_ENDIAN else ">"
    body_length, _serial, fields_length = struct.unpack_from(endian + "III", raw, 4)
    header_end = 16 + fields_length
    header_end += -header_end % 8
    total = header_end + body_length
    return total if len(raw) >= total else None


def parse_message(raw: bytes | bytearray) -> Message:
    """One complete message off the wire. The body is only read when a signature says so."""
    raw = bytes(raw)
    if len(raw) < 16:
        raise TransportError("a message shorter than its own fixed header")
    little = raw[0] == LITTLE_ENDIAN
    endian = "<" if little else ">"
    message_type = raw[1]
    body_length, serial = struct.unpack_from(endian + "II", raw, 4)
    reader = _Reader(raw, 12, little)
    fields = reader.read("a(yv)")
    reader.align(8)
    header = {int(code): variant.value for code, variant in fields}
    signature = str(header.get(FIELD_SIGNATURE, ""))
    body: list[Any] = []
    if signature:
        body_reader = _Reader(raw[reader.position : reader.position + body_length], 0, little)
        body = [body_reader.read(kind) for kind in split_signature(signature)]
    return Message(
        type=message_type,
        serial=serial,
        reply_serial=int(header.get(FIELD_REPLY_SERIAL, 0)),
        signature=signature,
        body=body,
        error_name=str(header.get(FIELD_ERROR_NAME, "")),
        member=str(header.get(FIELD_MEMBER, "")),
        interface=str(header.get(FIELD_INTERFACE, "")),
        path=str(header.get(FIELD_PATH, "")),
    )


# --------------------------------------------------------------------------- the connection


def socket_path(address: str) -> tuple[str, bool]:
    """`(path, abstract)` for the first unix socket in a D-Bus address.

    An address is several alternatives joined by `;`, each `transport:key=value,...`
    with the values percent-escaped, and carries a `guid=` that is not ours to check.
    """
    for entry in address.split(";"):
        transport, _, rest = entry.partition(":")
        if transport != "unix":
            continue
        params = dict(part.split("=", 1) for part in rest.split(",") if "=" in part)
        for key, abstract in (("path", False), ("abstract", True)):
            if params.get(key):
                return unquote(params[key]), abstract
    raise TransportError(f"no unix socket in the bus address {address!r}")


class Connection:
    """One authenticated D-Bus connection, held open and reused.

    Safe to share between threads: a call holds the lock for the whole exchange, so two
    walks on two worker threads take turns rather than interleaving bytes.
    """

    def __init__(self, address: str, *, timeout: float = 10.0) -> None:
        self.address = address
        self.unique_name = ""
        self._timeout = timeout
        self._socket: socket.socket | None = None
        self._buffer = bytearray()
        self._serial = 0
        self._pid = os.getpid()
        self._lock = threading.RLock()

    @property
    def connected(self) -> bool:
        return self._socket is not None

    def call(
        self,
        destination: str,
        path: str,
        interface: str,
        member: str,
        signature: str = "",
        args: Sequence[Any] = (),
    ) -> tuple[str, list[Any]]:
        """`(out signature, out arguments)`, opening or reopening the connection as needed."""
        with self._lock:
            try:
                return self._exchange(destination, path, interface, member, signature, args)
            except TransportError as exc:
                log.debug("a11y bus connection lost, reopening: %s", exc)
                self.close()
            return self._exchange(destination, path, interface, member, signature, args)

    def close(self) -> None:
        with self._lock:
            if self._socket is not None:
                with contextlib.suppress(OSError):
                    self._socket.close()
            self._socket = None
            self._buffer.clear()
            self.unique_name = ""

    # ------------------------------------------------------------------ the exchange

    def _exchange(
        self,
        destination: str,
        path: str,
        interface: str,
        member: str,
        signature: str,
        args: Sequence[Any],
    ) -> tuple[str, list[Any]]:
        if self._pid != os.getpid():
            # a fork left two processes holding one socket, which would interleave bytes
            self._socket, self._pid, self._serial = None, os.getpid(), 0
            self._buffer.clear()
        if self._socket is None:
            self._open()
        return self._request(destination, path, interface, member, signature, args)

    def _open(self) -> None:
        path, abstract = socket_path(self.address)
        try:
            conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            conn.settimeout(self._timeout)
            conn.connect("\0" + path if abstract else path)
        except OSError as exc:
            raise TransportError(f"cannot reach the bus at {self.address}: {exc}") from exc
        self._socket = conn
        self._buffer.clear()
        self._serial = 0
        self._authenticate()
        _, body = self._request(DBUS_SERVICE, DBUS_PATH, DBUS_SERVICE, "Hello")
        self.unique_name = str(body[0]) if body else ""

    def _authenticate(self) -> None:
        # SASL EXTERNAL over a unix socket: the leading NUL, then the uid in hex. The
        # kernel has already told the bus who we are, so there is nothing else to prove.
        credential = str(os.getuid()).encode().hex().encode()
        self._send(b"\0AUTH EXTERNAL " + credential + b"\r\n")
        try:
            reply = self._read_line()
        except ReplyTimeout as exc:
            # a bus that will not finish the handshake is a broken bus, not a slow app:
            # this must reach the caller as a transport failure so busctl is tried
            raise TransportError("the bus did not finish the SASL handshake") from exc
        if not reply.startswith("OK"):
            raise TransportError(f"the bus refused SASL EXTERNAL: {reply!r}")
        self._send(b"BEGIN\r\n")

    def _request(
        self,
        destination: str,
        path: str,
        interface: str,
        member: str,
        signature: str = "",
        args: Sequence[Any] = (),
    ) -> tuple[str, list[Any]]:
        self._serial += 1
        serial = self._serial
        try:
            payload = marshal_method_call(
                serial, destination, path, interface, member, signature, args
            )
        except (struct.error, TypeError, ValueError) as exc:
            raise TransportError(f"cannot put {interface}.{member} on the wire: {exc}") from exc
        self._send(payload)
        deadline = time.monotonic() + self._timeout
        while True:
            message = self._read_message(deadline)
            # signals the bus sends us (NameAcquired), and the late reply to a call an
            # earlier deadline gave up on: both are told apart by the serial alone
            if message.reply_serial != serial:
                continue
            if message.type == ERROR:
                text = str(message.body[0]) if message.body else ""
                raise DBusError(message.error_name, text)
            if message.type != METHOD_RETURN:
                raise TransportError(f"the bus answered message type {message.type}")
            return message.signature, message.body

    # ------------------------------------------------------------------ the socket

    def _send(self, payload: bytes) -> None:
        conn = self._socket
        if conn is None:
            raise TransportError("the bus connection is closed")
        try:
            conn.settimeout(self._timeout)
            conn.sendall(payload)
        except OSError as exc:
            raise TransportError(f"the bus connection failed: {exc}") from exc

    def _fill(self, deadline: float) -> None:
        conn = self._socket
        if conn is None:
            raise TransportError("the bus connection is closed")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ReplyTimeout("the bus did not answer in time")
        conn.settimeout(remaining)
        try:
            chunk = conn.recv(65536)
        except TimeoutError as exc:  # a TimeoutError is an OSError, so it is caught first
            raise ReplyTimeout("the bus did not answer in time") from exc
        except OSError as exc:
            raise TransportError(f"the bus connection failed: {exc}") from exc
        if not chunk:
            raise TransportError("the bus closed the connection")
        self._buffer.extend(chunk)

    def _read_line(self) -> str:
        deadline = time.monotonic() + self._timeout
        while b"\r\n" not in self._buffer:
            self._fill(deadline)
        line, _, rest = bytes(self._buffer).partition(b"\r\n")
        self._buffer[:] = rest
        return line.decode(errors="replace")

    def _read_message(self, deadline: float) -> Message:
        while (total := message_length(self._buffer)) is None:
            self._fill(deadline)
        raw = bytes(self._buffer[:total])
        del self._buffer[:total]
        try:
            return parse_message(raw)
        except (struct.error, IndexError, ValueError, UnicodeDecodeError) as exc:
            raise TransportError(f"unreadable reply from the bus: {exc}") from exc


# --------------------------------------------------------------------------- hypruse shim

_connections: dict[str, Connection] = {}
_connections_lock = threading.Lock()
_original: Callable[..., Any] | None = None
_timeout = 10.0


def session_address() -> str:
    """Where the session bus is. `a11y.bus_address()` asks org.a11y.Bus there."""
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    if not address:
        raise TransportError("DBUS_SESSION_BUS_ADDRESS is not set")
    return address


def connection_for(address: str) -> Connection:
    """The one live connection to `address`, opened on first use. `""` is the session bus,
    the same meaning `hypruse.a11y._busctl` gives an empty address."""
    key = address or session_address()
    with _connections_lock:
        conn = _connections.get(key)
        if conn is None:
            conn = _connections[key] = Connection(key, timeout=_timeout)
        return conn


def close_all() -> None:
    """Drop every connection. The next call opens a new one."""
    with _connections_lock:
        live = list(_connections.values())
        _connections.clear()
    for conn in live:
        conn.close()


def _cli_value(signature: str, text: str) -> Any:
    """One busctl command-line argument, as the type its signature says it is."""
    if signature in "ynqiuxt":
        return int(text, 0)
    if signature == "d":
        return float(text)
    if signature == "b":
        return text.lower() in ("1", "true", "yes", "on")
    if signature in "sog":
        return text
    raise TransportError(f"a busctl argument of type {signature!r} has no fast path")


def _cli_arguments(signature: str, texts: Sequence[str]) -> list[Any]:
    types = split_signature(signature)
    if len(types) != len(texts):
        raise TransportError(f"{signature!r} wants {len(types)} arguments, got {len(texts)}")
    return [_cli_value(kind, text) for kind, text in zip(types, texts, strict=True)]


def _plain(value: Any) -> Any:
    """What busctl --json=short would print: a variant shows as the value inside it."""
    if isinstance(value, Variant):
        return _plain(value.value)
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    return value


def busctl_shaped(address: str, verb: str, *args: str) -> dict[str, Any]:
    """One call over the persistent connection, in the shape `busctl --json=short` prints.

    `call` answers `{"type": <out signature>, "data": [out args]}` and `get-property` the
    bare value under the same two keys, which is busctl's own asymmetry: it unwraps the
    variant that `org.freedesktop.DBus.Properties.Get` returns and prints what was inside.
    `hypruse.a11y.Bus.call` and `.prop` read exactly those two shapes.
    """
    conn = connection_for(address)
    if verb == "call":
        destination, path, interface, member, *rest = args
        signature = rest[0] if rest else ""
        values = _cli_arguments(signature, rest[1:])
        out_signature, body = conn.call(destination, path, interface, member, signature, values)
        return {"type": out_signature, "data": [_plain(item) for item in body]}
    if verb == "get-property":
        destination, path, interface, name = args
        _, body = conn.call(destination, path, PROPERTIES, "Get", "ss", (interface, name))
        variant = body[0]
        if not isinstance(variant, Variant):
            raise TransportError(f"{interface}.{name} did not come back as a variant")
        return {"type": variant.signature, "data": _plain(variant.value)}
    raise TransportError(f"busctl {verb} has no fast path")


def _fast_busctl(address: str, verb: str, *args: str) -> Any:
    """What `hypruse.a11y._busctl` becomes: same signature, same return shape, same error."""
    try:
        return busctl_shaped(address, verb, *args)
    except DBusError as exc:
        # an answer, not a failure: a11y.py finds out that a widget has no Text
        # interface by calling it and catching this, so it may not turn into a retry
        raise a11y.A11yError(f"busctl {verb} failed: {exc}") from exc
    except ReplyTimeout as exc:
        raise a11y.A11yError(f"busctl {verb} timed out (unresponsive app?)") from exc
    except Exception as exc:
        if _original is None:
            raise
        log.debug("the fast a11y transport fell back to busctl: %s", exc)
        return _original(address, verb, *args)


def use_fast_transport(*, timeout: float = 10.0) -> None:
    """Put every hypruse accessibility read on a persistent connection to the a11y bus.

    `a11y._busctl` is the one seam all of `a11y.py` goes through, and `server.ui`,
    `click_ui` and `marks` reach the bus only through it, so nothing above needs to know.
    The replacement keeps its contract: the busctl JSON shape on success and hypruse's
    own A11yError on failure, which is what the guards in `a11y.py` catch to report a
    missing control rather than crash.
    """
    global _original, _timeout
    _timeout = timeout
    if _original is None:
        _original = a11y._busctl
    a11y._busctl = _fast_busctl


def restore_transport() -> None:
    """Put hypruse back on the busctl binary and drop every connection."""
    global _original
    if _original is not None:
        a11y._busctl = _original
        _original = None
    close_all()
