"""The fast a11y transport against a fake D-Bus: a real AF_UNIX socket under tmp_path.

No test here touches the accessibility bus. The fake speaks the same handshake a
dbus-daemon does (the leading NUL, SASL EXTERNAL, BEGIN) and answers from a table, so
the transport swap is exercised through the inherited `hypruse.a11y` code that will use
it, including a whole `find_elements` walk.

Two tests build bytes by hand rather than through the module, one in each direction,
because a marshaller and a parser that are wrong in the same way still round trip.
"""

import contextlib
import os
import socket
import struct
import threading
from pathlib import Path

import pytest

from hyprsay import a11ybus
from hypruse import a11y

SERVICE = ":1.9"
ROOT = "/org/a11y/atspi/accessible/root"
ACCESSIBLE = "org.a11y.atspi.Accessible"
COMPONENT = "org.a11y.atspi.Component"
TEXT = "org.a11y.atspi.Text"

# SHOWING, VISIBLE and SENSITIVE, which is what `_clickable_now` wants to see
SHOWN = (1 << 25) | (1 << 30) | (1 << 24)

TREE = {
    ROOT: {
        "Name": "fake app",
        "role": 75,
        "role_name": "application",
        "children": [],
        "extents": [0, 0, 400, 300],
        "states": [0, 0],
    },
    "/1": {
        "Name": "Send",
        "role": 43,  # push button, an actionable role
        "role_name": "push button",
        "children": [],
        "extents": [10, 20, 60, 24],
        "states": [SHOWN, 0],
    },
}
TREE[ROOT]["children"] = ["/1"]


# --------------------------------------------------------------------------- the fake


def tree_handlers(tree):
    """The AT-SPI calls `a11y.py` makes, answered from a table of accessibles."""

    def get_children(message):
        return "a(so)", [[[SERVICE, child] for child in tree[message.path]["children"]]]

    def get_role(message):
        return "u", [tree[message.path]["role"]]

    def get_role_name(message):
        return "s", [tree[message.path]["role_name"]]

    def get_state(message):
        return "au", [tree[message.path]["states"]]

    def get_interfaces(_message):
        return "as", [[ACCESSIBLE, COMPONENT]]

    def get_extents(message):
        return "(iiii)", [tree[message.path]["extents"]]

    def get_text(message):
        start, end = message.body
        return "s", ["fake text"[start:end]]

    def get_property(message):
        _interface, name = message.body
        value = tree[message.path][name]
        return "v", [a11ybus.Variant("i" if isinstance(value, int) else "s", value)]

    def get_bus_address(_message):
        return "s", ["unix:path=/run/user/1000/at-spi/bus_9"]

    return {
        (ACCESSIBLE, "GetChildren"): get_children,
        (ACCESSIBLE, "GetRole"): get_role,
        (ACCESSIBLE, "GetRoleName"): get_role_name,
        (ACCESSIBLE, "GetState"): get_state,
        (ACCESSIBLE, "GetInterfaces"): get_interfaces,
        (COMPONENT, "GetExtents"): get_extents,
        (TEXT, "GetText"): get_text,
        (a11ybus.PROPERTIES, "Get"): get_property,
        ("org.a11y.Bus", "GetAddress"): get_bus_address,
    }


class FakeBus:
    """A D-Bus on a real AF_UNIX socket: the SASL handshake, Hello, then canned replies.

    One client at a time, which is all a persistent connection ever is. `drop` hangs up
    the way a bus restart does, and the accept loop is waiting again straight after.
    """

    def __init__(self, path: Path, handlers=None) -> None:
        self.path = path
        self.address = f"unix:path={path}"
        self.handlers = dict(handlers or {})
        self.calls: list[tuple[str, str, list]] = []
        self.auth_lines: list[str] = []
        self.connections = 0
        self.signal_before_reply: bytes = b""
        self._serial = 0
        self._live: socket.socket | None = None
        self._stop = threading.Event()
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(path))
        self._server.listen(8)
        self._server.settimeout(0.02)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def drop(self) -> None:
        live = self._live
        if live is not None:
            with contextlib.suppress(OSError):
                live.shutdown(socket.SHUT_RDWR)

    def close(self) -> None:
        self._stop.set()
        self.drop()
        self._thread.join(2)
        self._server.close()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except OSError:
                continue
            self.connections += 1
            self._live = conn
            with conn, contextlib.suppress(OSError, ValueError):
                conn.settimeout(2)
                self._session(conn)
            self._live = None

    def _session(self, conn: socket.socket) -> None:
        buffer = bytearray()

        def fill() -> None:
            chunk = conn.recv(65536)
            if not chunk:
                raise OSError("the client went away")
            buffer.extend(chunk)

        def line() -> str:
            while b"\r\n" not in buffer:
                fill()
            head, _, rest = bytes(buffer).partition(b"\r\n")
            buffer[:] = rest
            return head.lstrip(b"\0").decode()

        self.auth_lines.append(line())
        conn.sendall(b"OK 0123456789abcdef0123456789abcdef\r\n")
        self.auth_lines.append(line())
        while not self._stop.is_set():
            while (total := a11ybus.message_length(buffer)) is None:
                fill()
            raw = bytes(buffer[:total])
            del buffer[:total]
            conn.sendall(self.answer(a11ybus.parse_message(raw)))

    def answer(self, message: a11ybus.Message) -> bytes:
        self.calls.append((message.interface, message.member, list(message.body)))
        if message.member == "Hello":
            return self._reply(message, "s", [":1.99"])
        handler = self.handlers.get((message.interface, message.member))
        if handler is None:
            return self._error(message, f'Method "{message.member}" does not exist')
        try:
            signature, args = handler(message)
        except KeyError:
            return self._error(message, f"No such object {message.path}")
        prefix, self.signal_before_reply = self.signal_before_reply, b""
        return prefix + self._reply(message, signature, args)

    def _next(self) -> int:
        self._serial += 1
        return self._serial

    def _reply(self, message, signature, args) -> bytes:
        return a11ybus.marshal_message(
            a11ybus.METHOD_RETURN,
            self._next(),
            reply_serial=message.serial,
            destination=":1.99",
            signature=signature,
            args=args,
        )

    def _error(self, message, text: str) -> bytes:
        return a11ybus.marshal_message(
            a11ybus.ERROR,
            self._next(),
            error_name="org.freedesktop.DBus.Error.UnknownMethod",
            reply_serial=message.serial,
            destination=":1.99",
            signature="s",
            args=[text],
        )


@pytest.fixture
def bus(tmp_path):
    fake = FakeBus(tmp_path / "b.sock", tree_handlers(TREE))
    yield fake
    fake.close()


@pytest.fixture(autouse=True)
def _uninstall():
    """No test may leave hypruse pointed at a socket that is about to be deleted."""
    yield
    a11ybus.restore_transport()


# --------------------------------------------------------------------------- the wire


def test_a_method_call_is_marshalled_with_the_alignment_the_spec_requires():
    # every offset below was counted by hand from the D-Bus specification, so a
    # marshaller and a parser that agree with each other still have to agree with it
    raw = a11ybus.marshal_method_call(
        1, "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "Hello"
    )
    assert raw[:12] == b"l\x01\x00\x01" + struct.pack("<II", 0, 1)
    assert struct.unpack_from("<I", raw, 12)[0] == 109  # the a(yv) field array
    assert raw[16] == a11ybus.FIELD_PATH
    assert raw[17:20] == b"\x01o\x00"  # the variant's signature, one byte of length
    assert struct.unpack_from("<I", raw, 20)[0] == len("/org/freedesktop/DBus")
    assert raw[24:45] == b"/org/freedesktop/DBus"
    assert raw[45] == 0  # strings are NUL terminated on top of their length
    assert raw[48] == a11ybus.FIELD_INTERFACE  # each field starts on an 8 boundary
    assert raw[80] == a11ybus.FIELD_MEMBER
    assert raw[96] == a11ybus.FIELD_DESTINATION
    assert len(raw) == 128  # 125 bytes of header, padded to 8, and no body


def test_a_reply_built_by_hand_parses_back_into_its_out_arguments():
    body = (
        struct.pack("<I", 45)  # a(so): 45 bytes of elements
        + b"\x00" * 4  # the (so) struct aligns to 8
        + struct.pack("<I", 4)
        + b":1.2\x00"
        + b"\x00" * 3  # the object path that follows aligns to 4
        + struct.pack("<I", 28)
        + b"/org/a11y/atspi/accessible/1\x00"
    )
    fields = (
        bytes([a11ybus.FIELD_REPLY_SERIAL])
        + b"\x01u\x00"
        + struct.pack("<I", 7)
        + bytes([a11ybus.FIELD_SIGNATURE])  # lands on 8 with no padding
        + b"\x01g\x00"
        + bytes([5])
        + b"a(so)\x00"
    )
    raw = bytearray(b"l\x02\x00\x01" + struct.pack("<II", len(body), 99))
    raw += struct.pack("<I", len(fields)) + fields
    raw += b"\x00" * (-len(raw) % 8)
    raw += body

    assert a11ybus.message_length(raw) == len(raw)
    message = a11ybus.parse_message(raw)
    assert message.type == a11ybus.METHOD_RETURN
    assert (message.serial, message.reply_serial) == (99, 7)
    assert message.signature == "a(so)"
    assert message.body == [[[":1.2", "/org/a11y/atspi/accessible/1"]]]


def test_a_message_is_not_complete_until_its_last_byte_has_arrived():
    raw = a11ybus.marshal_method_call(1, "x", "/", "i", "M", "s", ["hello"])
    assert a11ybus.message_length(raw[:4]) is None
    assert a11ybus.message_length(raw[:-1]) is None
    assert a11ybus.message_length(raw + b"leftover") == len(raw)


def test_every_type_the_accessibility_calls_use_survives_a_round_trip():
    scalars = ["text", "/org/x", 4294967295, True, 1.5, -3, 200]
    first = a11ybus.parse_message(
        a11ybus.marshal_method_call(1, "x", "/", "i", "M", "soubdiy", scalars)
    )
    assert first.body == scalars

    containers = [
        ["a", "bb"],
        [1, 2, 3],
        [[":1", "/p"]],
        [10, 20, 60, 24],
        a11ybus.Variant("s", "inner"),
    ]
    second = a11ybus.parse_message(
        a11ybus.marshal_method_call(2, "x", "/", "i", "M", "asaua(so)(iiii)v", containers)
    )
    assert second.body == containers
    assert second.body[-1].signature == "s"


def test_a_signature_is_cut_into_complete_types():
    assert a11ybus.split_signature("") == []
    assert a11ybus.split_signature("ii") == ["i", "i"]
    assert a11ybus.split_signature("a(so)s") == ["a(so)", "s"]
    assert a11ybus.split_signature("aa{sv}u") == ["aa{sv}", "u"]
    with pytest.raises(a11ybus.TransportError):
        a11ybus.split_signature("(so")


def test_a_bus_address_names_a_path_or_an_abstract_socket():
    assert a11ybus.socket_path("unix:path=/run/user/1000/bus") == ("/run/user/1000/bus", False)
    assert a11ybus.socket_path("unix:abstract=/tmp/dbus-Ab,guid=ff") == ("/tmp/dbus-Ab", True)
    assert a11ybus.socket_path("unix:path=/a%20b") == ("/a b", False)
    assert a11ybus.socket_path("tcp:host=x;unix:path=/s") == ("/s", False)
    with pytest.raises(a11ybus.TransportError):
        a11ybus.socket_path("tcp:host=localhost,port=1")


# --------------------------------------------------------------------------- the client


def test_the_client_authenticates_with_sasl_external_and_says_hello_first(bus):
    conn = a11ybus.Connection(bus.address, timeout=2.0)
    try:
        assert conn.call(SERVICE, ROOT, ACCESSIBLE, "GetRoleName") == ("s", ["application"])
        assert conn.unique_name == ":1.99"
    finally:
        conn.close()
    assert bus.auth_lines == [f"AUTH EXTERNAL {str(os.getuid()).encode().hex()}", "BEGIN"]
    assert [member for _, member, _ in bus.calls] == ["Hello", "GetRoleName"]


def test_a_signal_on_the_way_is_not_mistaken_for_the_reply(bus):
    bus.signal_before_reply = a11ybus.marshal_message(
        a11ybus.SIGNAL,
        99,
        path="/org/freedesktop/DBus",
        interface="org.freedesktop.DBus",
        member="NameAcquired",
        signature="s",
        args=[":1.99"],
    )
    conn = a11ybus.Connection(bus.address, timeout=2.0)
    try:
        assert conn.call(SERVICE, ROOT, ACCESSIBLE, "GetRole") == ("u", [75])
    finally:
        conn.close()


def test_a_bus_that_is_not_there_is_a_transport_error_not_a_hang(tmp_path):
    conn = a11ybus.Connection(f"unix:path={tmp_path / 'absent.sock'}", timeout=0.5)
    with pytest.raises(a11ybus.TransportError):
        conn.call(SERVICE, ROOT, ACCESSIBLE, "GetRole")


# --------------------------------------------------------------------------- the swap


def test_a_call_comes_back_in_the_shape_busctl_json_short_prints(bus):
    a11ybus.use_fast_transport(timeout=2.0)
    assert a11y._busctl(bus.address, "call", SERVICE, ROOT, ACCESSIBLE, "GetChildren") == {
        "type": "a(so)",
        "data": [[[SERVICE, "/1"]]],
    }


def test_a_property_is_read_through_the_properties_interface_and_unwrapped(bus):
    a11ybus.use_fast_transport(timeout=2.0)
    assert a11y._busctl(bus.address, "get-property", SERVICE, ROOT, ACCESSIBLE, "Name") == {
        "type": "s",
        "data": "fake app",
    }
    interface, member, body = bus.calls[-1]
    assert (interface, member, body) == (a11ybus.PROPERTIES, "Get", [ACCESSIBLE, "Name"])


def test_an_argument_is_typed_by_the_signature_busctl_was_given(bus):
    a11ybus.use_fast_transport(timeout=2.0)
    client = a11y.Bus(bus.address)
    assert client.call(SERVICE, "/1", COMPONENT, "GetExtents", "u", "1") == [[10, 20, 60, 24]]
    assert bus.calls[-1][2] == [1]  # the number 1, not the text "1"
    assert client.call(SERVICE, "/1", TEXT, "GetText", "ii", "0", "4") == ["fake"]
    assert bus.calls[-1][2] == [0, 4]


def test_an_error_reply_becomes_the_a11y_error_the_inherited_guards_catch(bus, monkeypatch):
    monkeypatch.setattr(a11y, "_busctl", _never_called)
    a11ybus.use_fast_transport(timeout=2.0)
    with pytest.raises(a11y.A11yError, match="UnknownMethod"):
        a11y.Bus(bus.address).call(SERVICE, ROOT, "org.a11y.atspi.Value", "GetCurrentValue")
    # an error is an answer: no retry, no second connection, and no busctl
    assert bus.connections == 1


def test_a_whole_tree_walk_goes_over_one_connection(bus, monkeypatch):
    monkeypatch.setattr(a11y, "_busctl", _never_called)
    a11ybus.use_fast_transport(timeout=2.0)
    found, truncated = a11y.find_elements(a11y.Bus(bus.address), SERVICE, ROOT)
    assert [(e["role"], e["name"], e["extent"], e["clickable"]) for e in found] == [
        ("push button", "Send", (10, 20, 60, 24), True)
    ]
    assert truncated is False
    assert bus.connections == 1
    assert len(bus.calls) > 5


def test_an_empty_address_means_the_session_bus_where_the_a11y_address_lives(bus, monkeypatch):
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", bus.address)
    monkeypatch.setattr(a11y, "_busctl", _never_called)
    a11ybus.use_fast_transport(timeout=2.0)
    assert a11y.bus_address() == "unix:path=/run/user/1000/at-spi/bus_9"
    assert a11ybus.connection_for("").address == bus.address


def test_a_dropped_connection_is_reopened_without_the_caller_noticing(bus, monkeypatch):
    monkeypatch.setattr(a11y, "_busctl", _never_called)
    a11ybus.use_fast_transport(timeout=2.0)
    client = a11y.Bus(bus.address)
    assert client.call(SERVICE, ROOT, ACCESSIBLE, "GetRoleName") == ["application"]
    bus.drop()
    assert client.call(SERVICE, ROOT, ACCESSIBLE, "GetRoleName") == ["application"]
    assert bus.connections == 2


def test_two_threads_on_one_connection_each_get_their_own_answer(bus, monkeypatch):
    # controls.py walks the tree on a worker while the daemon keeps serving, so a
    # crossed reply would put one window's button inside another window's reading
    monkeypatch.setattr(a11y, "_busctl", _never_called)
    a11ybus.use_fast_transport(timeout=2.0)
    client = a11y.Bus(bus.address)
    wrong: list[tuple[str, list]] = []

    def hammer(path: str, expected: str) -> None:
        for _ in range(20):
            answer = client.call(SERVICE, path, ACCESSIBLE, "GetRoleName")
            if answer != [expected]:
                wrong.append((path, answer))

    threads = [
        threading.Thread(target=hammer, args=(ROOT, "application")),
        threading.Thread(target=hammer, args=("/1", "push button")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert wrong == []
    assert bus.connections == 1


def test_a_bus_that_cannot_be_reached_falls_back_to_the_original_busctl(tmp_path, monkeypatch):
    fallbacks = []

    def original(address, verb, *args):
        fallbacks.append((address, verb, args))
        return {"type": "s", "data": ["from busctl"]}

    monkeypatch.setattr(a11y, "_busctl", original)
    a11ybus.use_fast_transport(timeout=0.5)
    missing = a11y.Bus(f"unix:path={tmp_path / 'absent.sock'}")
    assert missing.call(SERVICE, ROOT, ACCESSIBLE, "GetRole") == ["from busctl"]
    assert [verb for _, verb, _ in fallbacks] == ["call"]


def test_a_verb_with_no_wire_form_falls_back_to_the_original_busctl(bus, monkeypatch):
    fallbacks = []

    def original(address, verb, *args):
        fallbacks.append(verb)
        return {"type": "s", "data": ["from busctl"]}

    monkeypatch.setattr(a11y, "_busctl", original)
    a11ybus.use_fast_transport(timeout=2.0)
    assert a11y._busctl(bus.address, "introspect", SERVICE, ROOT)["data"] == ["from busctl"]
    assert fallbacks == ["introspect"]


def test_restore_puts_the_original_busctl_back_even_after_two_installs(bus):
    original = a11y._busctl
    a11ybus.use_fast_transport(timeout=2.0)
    a11ybus.use_fast_transport(timeout=2.0)
    assert a11y._busctl is not original
    a11y.Bus(bus.address).call(SERVICE, ROOT, ACCESSIBLE, "GetRole")
    live = a11ybus.connection_for(bus.address)
    assert live.connected
    a11ybus.restore_transport()
    assert a11y._busctl is original
    assert not live.connected  # and the socket was let go, not left open for ever
    a11ybus.restore_transport()  # a second restore is harmless
    assert a11y._busctl is original


def _never_called(*args):
    raise AssertionError(f"the fast transport should not have fallen back to busctl: {args}")
