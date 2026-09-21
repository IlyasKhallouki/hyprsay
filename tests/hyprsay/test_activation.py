"""Push to talk against fakes. Nothing here talks to a compositor: the global transport
is exercised against a scripted Unix socket in tmp_path that speaks just enough Wayland."""

import asyncio
import socket
import struct
import threading
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from hyprsay import activation
from hyprsay.activation import (
    GlobalShortcut,
    PttEvent,
    PushToTalk,
    ShortcutSignal,
    bind_lines,
    decode_shortcut_event,
    encode_bind,
    encode_register_shortcut,
)
from hyprsay.config import PTT, Config
from hypruse.wire import WireError, encode_msg, parse_events, wl_string

PROTOCOL_XML = Path("/usr/share/hyprland-protocols/protocols/hyprland-global-shortcuts-v1.xml")


def cfg(max_hold_s: float = 15.0, latch: bool = False) -> Config:
    return Config(ptt=PTT(max_hold_s=max_hold_s, latch=latch))


def play(config: Config, script, clock=None) -> list[PttEvent]:
    """Run `script(ptt)` on a loop, then close and return every event it produced."""

    async def scenario() -> list[PttEvent]:
        ptt = PushToTalk(config, **({"clock": clock} if clock else {}))
        result = script(ptt)
        if asyncio.iscoroutine(result):
            await result
        ptt.close()
        return [event async for event in ptt.events()]

    return asyncio.run(scenario())


def shape(events: list[PttEvent]) -> list[tuple[str, str]]:
    return [(e.kind, e.reason) for e in events]


# --------------------------------------------------------------------------- pairing


def test_a_press_and_a_release_make_one_down_and_one_up():
    def script(ptt):
        ptt.feed_custom("hyprsay:down")
        ptt.feed_custom("hyprsay:up")

    assert shape(play(cfg(), script)) == [("down", "key"), ("up", "key")]


def test_a_second_down_while_held_is_ignored():
    def script(ptt):
        ptt.feed_custom("hyprsay:down")
        ptt.feed_custom("hyprsay:down")
        ptt.feed_custom("hyprsay:up")

    assert shape(play(cfg(), script)) == [("down", "key"), ("up", "key")]


def test_an_up_with_no_down_is_ignored():
    def script(ptt):
        ptt.feed_custom("hyprsay:up")
        ptt.feed_custom("hyprsay:down")
        ptt.feed_custom("hyprsay:up")
        ptt.feed_custom("hyprsay:up")

    assert shape(play(cfg(), script)) == [("down", "key"), ("up", "key")]


def test_each_utterance_gets_a_fresh_pair():
    def script(ptt):
        for _ in range(3):
            ptt.press()
            ptt.release()

    assert shape(play(cfg(), script)) == [("down", "key"), ("up", "key")] * 3


def test_events_carry_the_clock_at_the_moment_of_the_cause():
    ticks = iter([10.0, 12.5, 99.0])  # the third reading is close(), which emits nothing here

    def script(ptt):
        ptt.press()
        ptt.release()

    events = play(cfg(), script, clock=lambda: next(ticks))
    assert [e.at for e in events] == [10.0, 12.5]


def test_held_reflects_the_state():
    seen = []

    def script(ptt):
        seen.append(ptt.held)
        ptt.press()
        seen.append(ptt.held)
        ptt.release()
        seen.append(ptt.held)

    play(cfg(), script)
    assert seen == [False, True, False]


# --------------------------------------------------------------------------- custom data


@pytest.mark.parametrize(
    "junk",
    [
        "",
        "down",
        "hyprsay:",
        "hyprsay:DOWN",
        "hyprsay:down ",
        " hyprsay:down",
        "hyprsay:down\n",
        "hyprsay:downhyprsay:up",
        "voice:start",
        "custom>>hyprsay:down",
        "hyprsay:toggle,hyprsay:down",
    ],
)
def test_custom_data_that_is_not_exactly_ours_is_ignored(junk):
    assert play(cfg(), lambda ptt: ptt.feed_custom(junk)) == []


def test_the_toggle_event_starts_and_then_stops_even_without_latch_mode():
    def script(ptt):
        ptt.feed_custom("hyprsay:toggle")
        ptt.feed_custom("hyprsay:toggle")

    assert shape(play(cfg(), script)) == [("down", "toggle"), ("up", "toggle")]


# --------------------------------------------------------------------------- latch mode


def test_in_latch_mode_each_press_toggles_and_releases_mean_nothing():
    def script(ptt):
        ptt.feed_custom("hyprsay:down")  # tap one: start
        ptt.feed_custom("hyprsay:up")
        ptt.feed_custom("hyprsay:down")  # tap two: stop
        ptt.feed_custom("hyprsay:up")

    assert shape(play(cfg(latch=True), script)) == [("down", "toggle"), ("up", "toggle")]


def test_latch_mode_is_still_bounded_by_the_maximum_hold():
    async def script(ptt):
        ptt.press()
        ptt.release()
        await asyncio.sleep(0.12)

    events = play(cfg(max_hold_s=0.03, latch=True), script)
    assert shape(events) == [("down", "toggle"), ("up", "max_hold")]


# --------------------------------------------------------------------------- max hold


def test_a_hold_past_the_maximum_ends_with_a_synthetic_up():
    async def script(ptt):
        ptt.press()
        await asyncio.sleep(0.12)

    assert shape(play(cfg(max_hold_s=0.03), script)) == [("down", "key"), ("up", "max_hold")]


def test_the_real_release_after_a_max_hold_is_ignored_and_the_next_press_is_fresh():
    async def script(ptt):
        ptt.press()
        await asyncio.sleep(0.12)
        ptt.release()
        ptt.press()
        ptt.release()

    assert shape(play(cfg(max_hold_s=0.03), script)) == [
        ("down", "key"),
        ("up", "max_hold"),
        ("down", "key"),
        ("up", "key"),
    ]


def test_a_release_in_time_disarms_the_maximum_hold():
    async def script(ptt):
        ptt.press()
        ptt.release()
        await asyncio.sleep(0.12)

    assert shape(play(cfg(max_hold_s=0.03), script)) == [("down", "key"), ("up", "key")]


def test_the_maximum_hold_is_measured_from_the_latest_down_not_the_first():
    async def script(ptt):
        ptt.press()
        await asyncio.sleep(0.06)
        ptt.release()
        ptt.press()
        await asyncio.sleep(0.06)  # 0.12 since the first down, 0.06 since this one
        ptt.release()

    assert shape(play(cfg(max_hold_s=0.1), script)) == [("down", "key"), ("up", "key")] * 2


@pytest.mark.parametrize("bad", [0.0, -1.0])
def test_a_maximum_hold_that_could_never_expire_is_refused(bad):
    with pytest.raises(ValueError, match="max_hold_s"):
        PushToTalk(cfg(max_hold_s=bad))


# --------------------------------------------------------------------------- force_up


def test_force_up_ends_a_hold_with_the_given_reason():
    def script(ptt):
        ptt.press()
        ptt.force_up("locked")
        ptt.release()  # never arrives while locked; harmless if it does

    assert shape(play(cfg(), script)) == [("down", "key"), ("up", "locked")]


def test_force_up_when_idle_emits_nothing():
    assert play(cfg(), lambda ptt: ptt.force_up("locked")) == []


def test_force_up_works_from_another_thread():
    async def scenario():
        ptt = PushToTalk(cfg())
        stream = ptt.events()
        ptt.press()
        down = await asyncio.wait_for(anext(stream), 2.0)
        thread = threading.Thread(target=ptt.force_up, args=("locked",))
        thread.start()
        up = await asyncio.wait_for(anext(stream), 2.0)
        thread.join()
        return shape([down, up])

    assert asyncio.run(scenario()) == [("down", "key"), ("up", "locked")]


def test_force_up_before_any_loop_exists_is_a_no_op():
    PushToTalk(cfg()).force_up("locked")


def test_feeding_a_press_with_no_loop_is_an_error_not_a_silent_drop():
    with pytest.raises(RuntimeError, match="event loop"):
        PushToTalk(cfg()).feed_custom("hyprsay:down")


def test_close_completes_an_open_pair_before_ending_the_stream():
    assert shape(play(cfg(), lambda ptt: ptt.press())) == [("down", "key"), ("up", "closed")]


# --------------------------------------------------------------------------- bind lines


def test_hyprlang_bind_lines_use_the_event_dispatcher_on_press_and_release():
    assert bind_lines("hyprlang") == [
        "bind = SUPER, V, event, hyprsay:down",
        "bindr = SUPER, V, event, hyprsay:up",
    ]


def test_bind_lines_carry_the_chosen_key():
    assert bind_lines("hyprlang", "SUPER SHIFT, space") == [
        "bind = SUPER SHIFT, space, event, hyprsay:down",
        "bindr = SUPER SHIFT, space, event, hyprsay:up",
    ]


def test_the_lines_feed_back_into_the_state_machine_they_are_written_for():
    payloads = [line.rsplit(", ", 1)[1] for line in bind_lines("hyprlang")]

    def script(ptt):
        for data in payloads:
            ptt.feed_custom(data)

    assert shape(play(cfg(), script)) == [("down", "key"), ("up", "key")]


def test_lua_bind_lines_are_marked_unverified_and_bind_press_and_release():
    lines = bind_lines("lua", "SUPER SHIFT, V")
    assert lines[0].startswith("-- UNVERIFIED")
    assert lines[1:] == [
        'hl.bind("SUPER + SHIFT + V", hl.dsp.event("hyprsay:down"))',
        'hl.bind("SUPER + SHIFT + V", hl.dsp.event("hyprsay:up"), { release = true })',
    ]


def test_a_key_with_no_modifier_survives_the_lua_translation():
    assert 'hl.bind("F9", ' in bind_lines("lua", ", F9")[1]


def test_the_global_transport_needs_one_bind_naming_app_and_shortcut():
    assert bind_lines("hyprlang", transport="global") == ["bind = SUPER, V, global, hyprsay:ptt"]
    assert bind_lines("lua", transport="global")[1] == (
        'hl.bind("SUPER + V", hl.dsp.global("hyprsay:ptt"))'
    )


def test_unknown_providers_and_transports_are_errors():
    with pytest.raises(ValueError, match="provider"):
        bind_lines("toml")
    with pytest.raises(ValueError, match="transport"):
        bind_lines("hyprlang", transport="dbus")


def test_no_line_contains_an_em_dash():
    every = bind_lines("hyprlang") + bind_lines("lua") + bind_lines("lua", transport="global")
    assert not any(chr(0x2014) in line for line in every)


# --------------------------------------------------------------------------- wire encoding


def read_string(body: bytes, offset: int) -> tuple[str, int]:
    (length,) = struct.unpack_from("<I", body, offset)
    text = body[offset + 4 : offset + 4 + length - 1].decode()
    return text, offset + 4 + length + (4 - length % 4) % 4


def test_register_shortcut_is_a_new_id_followed_by_four_strings():
    message = encode_register_shortcut(4, 5)
    events, rest = parse_events(message)
    assert rest == b""
    [(obj, opcode, body)] = events
    assert (obj, opcode) == (4, activation.MGR_REGISTER_SHORTCUT)
    assert struct.unpack_from("<I", body, 0) == (5,)
    offset, strings = 4, []
    for _ in range(4):
        text, offset = read_string(body, offset)
        strings.append(text)
    assert strings == ["ptt", "hyprsay", "Push to talk", ""]
    assert offset == len(body)
    assert len(message) % 4 == 0


def test_bind_carries_the_interface_name_and_version_before_the_new_id():
    [(obj, opcode, body)] = parse_events(encode_bind(2, 42, activation.MANAGER_INTERFACE, 1, 4))[0]
    assert (obj, opcode) == (2, 0)
    assert struct.unpack_from("<I", body, 0) == (42,)
    interface, offset = read_string(body, 4)
    assert interface == "hyprland_global_shortcuts_manager_v1"
    assert struct.unpack_from("<II", body, offset) == (1, 4)


def test_pressed_and_released_decode_with_a_64_bit_seconds_timestamp():
    body = struct.pack("<III", 1, 5, 500_000_000)
    assert decode_shortcut_event(0, body) == ShortcutSignal(True, (1 << 32) + 5.5)
    assert decode_shortcut_event(1, body) == ShortcutSignal(False, (1 << 32) + 5.5)


def test_an_unknown_opcode_or_a_short_body_decodes_to_nothing():
    assert decode_shortcut_event(2, struct.pack("<III", 0, 1, 0)) is None
    assert decode_shortcut_event(0, b"\x00" * 8) is None


@pytest.mark.skipif(not PROTOCOL_XML.exists(), reason="hyprland-protocols is not installed")
def test_the_opcodes_match_the_installed_protocol_xml():
    interfaces = {i.get("name"): i for i in ET.parse(PROTOCOL_XML).getroot().iter("interface")}
    manager = interfaces[activation.MANAGER_INTERFACE]
    shortcut = interfaces["hyprland_global_shortcut_v1"]

    requests = [r.get("name") for r in manager.findall("request")]
    assert requests.index("register_shortcut") == activation.MGR_REGISTER_SHORTCUT
    assert requests.index("destroy") == activation.MGR_DESTROY
    assert int(manager.get("version")) == activation.MANAGER_VERSION

    register = manager.findall("request")[activation.MGR_REGISTER_SHORTCUT]
    assert [(a.get("name"), a.get("type")) for a in register.findall("arg")] == [
        ("shortcut", "new_id"),
        ("id", "string"),
        ("app_id", "string"),
        ("description", "string"),
        ("trigger_description", "string"),
    ]

    events = shortcut.findall("event")
    assert [e.get("name") for e in events].index("pressed") == activation.SHORTCUT_EV_PRESSED
    assert [e.get("name") for e in events].index("released") == activation.SHORTCUT_EV_RELEASED
    for event in events:
        assert [(a.get("name"), a.get("type")) for a in event.findall("arg")] == [
            ("tv_sec_hi", "uint"),
            ("tv_sec_lo", "uint"),
            ("tv_nsec", "uint"),
        ]
    assert [r.get("name") for r in shortcut.findall("request")].index("destroy") == (
        activation.SHORTCUT_DESTROY
    )


# --------------------------------------------------------------------------- global transport


class FakeCompositor:
    """A one-client Wayland server: registry, sync, bind and register_shortcut only."""

    MANAGER_NAME = 42

    def __init__(self, path: Path, *, advertise: bool = True, taken: bool = False) -> None:
        self.path = path
        self.advertise, self.taken = advertise, taken
        self.bound: tuple[int, str, int] | None = None
        self.registered: list[str] | None = None
        self.destroyed: list[str] = []
        self.manager_id = self.shortcut_id = self.registry_id = 0
        self.gone = threading.Event()
        self._conn: socket.socket | None = None
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(path))
        self._server.listen(1)
        self._server.settimeout(5.0)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            self._conn, _ = self._server.accept()
        except OSError:
            return
        buf = b""
        try:
            while chunk := self._conn.recv(65536):
                buf += chunk
                requests, buf = parse_events(buf)
                for obj, opcode, body in requests:
                    self._handle(obj, opcode, body)
        except OSError:
            pass
        self.gone.set()

    def _handle(self, obj: int, opcode: int, body: bytes) -> None:
        conn = self._conn
        assert conn is not None
        if obj == 1 and opcode == 1:  # wl_display.get_registry
            (self.registry_id,) = struct.unpack("<I", body)
            advertised = [(7, "wl_compositor", 6)]
            if self.advertise:
                advertised.append((self.MANAGER_NAME, activation.MANAGER_INTERFACE, 1))
            for name, interface, version in advertised:
                head, tail = struct.pack("<I", name), struct.pack("<I", version)
                conn.sendall(encode_msg(self.registry_id, 0, head + wl_string(interface) + tail))
        elif obj == 1 and opcode == 0:  # wl_display.sync
            (callback,) = struct.unpack("<I", body)
            conn.sendall(encode_msg(callback, 0, struct.pack("<I", 0)))
        elif obj == self.registry_id and opcode == 0:  # wl_registry.bind
            (name,) = struct.unpack_from("<I", body, 0)
            interface, offset = read_string(body, 4)
            version, self.manager_id = struct.unpack_from("<II", body, offset)
            self.bound = (name, interface, version)
        elif obj == self.manager_id and opcode == 0:  # register_shortcut
            (self.shortcut_id,) = struct.unpack_from("<I", body, 0)
            offset, self.registered = 4, []
            for _ in range(4):
                text, offset = read_string(body, offset)
                self.registered.append(text)
            if self.taken:
                error = struct.pack("<II", self.manager_id, 0) + wl_string("already taken")
                conn.sendall(encode_msg(1, 0, error))
        elif obj == self.manager_id and opcode == 1:
            self.destroyed.append("manager")
        elif obj == self.shortcut_id and opcode == 0:
            self.destroyed.append("shortcut")

    def signal(self, opcode: int, *, split: bool = False) -> None:
        assert self._conn is not None
        message = encode_msg(self.shortcut_id, opcode, struct.pack("<III", 0, 100, 0))
        if split:
            self._conn.sendall(message[:10])
            self._conn.sendall(message[10:])
        else:
            self._conn.sendall(message)

    def drop(self) -> None:
        assert self._conn is not None
        self._conn.shutdown(socket.SHUT_RDWR)

    def close(self) -> None:
        self._server.close()
        if self._conn is not None:
            self._conn.close()
        self._thread.join(timeout=2.0)


@pytest.fixture
def compositor(tmp_path):
    made: list[FakeCompositor] = []

    def make(**kwargs) -> FakeCompositor:
        made.append(FakeCompositor(tmp_path / f"wayland-{len(made)}", **kwargs))
        return made[-1]

    yield make
    for fake in made:
        fake.close()


async def following(stream, count: int) -> list[tuple[str, str]]:
    return shape([await asyncio.wait_for(anext(stream), 2.0) for _ in range(count)])


def test_the_shortcut_is_registered_as_hyprsay_ptt_on_the_advertised_manager(compositor):
    fake = compositor()

    async def scenario():
        transport = GlobalShortcut(PushToTalk(cfg()), display=str(fake.path))
        transport.start()
        transport.stop()

    asyncio.run(scenario())
    assert fake.bound == (42, "hyprland_global_shortcuts_manager_v1", 1)
    assert fake.registered == ["ptt", "hyprsay", "Push to talk", ""]


def test_pressed_and_released_on_the_wire_become_down_and_up(compositor):
    fake = compositor()

    async def scenario():
        ptt = PushToTalk(cfg())
        transport = GlobalShortcut(ptt, display=str(fake.path))
        transport.start()
        stream = ptt.events()
        fake.signal(activation.SHORTCUT_EV_PRESSED)
        fake.signal(activation.SHORTCUT_EV_RELEASED, split=True)
        try:
            return await following(stream, 2)
        finally:
            transport.stop()

    assert asyncio.run(scenario()) == [("down", "key"), ("up", "key")]


def test_stop_destroys_the_shortcut_and_the_manager_and_ends_the_thread(compositor):
    fake = compositor()

    async def scenario():
        transport = GlobalShortcut(PushToTalk(cfg()), display=str(fake.path))
        transport.start()
        thread = transport._thread
        transport.stop()
        return thread

    thread = asyncio.run(scenario())
    assert fake.gone.wait(2.0)
    assert fake.destroyed == ["shortcut", "manager"]
    assert not thread.is_alive()


def test_a_lost_compositor_ends_the_hold_and_tells_the_daemon(compositor):
    fake = compositor()

    async def scenario():
        lost: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        ptt = PushToTalk(cfg())
        transport = GlobalShortcut(ptt, display=str(fake.path), on_lost=lost.set_result)
        transport.start()
        stream = ptt.events()
        fake.signal(activation.SHORTCUT_EV_PRESSED)
        events = await following(stream, 1)
        fake.drop()
        events += await following(stream, 1)
        return events, await asyncio.wait_for(lost, 2.0)

    events, reason = asyncio.run(scenario())
    assert events == [("down", "key"), ("up", "transport_lost")]
    assert "closed the connection" in reason


def test_a_shortcut_that_is_already_taken_fails_start_with_the_compositors_words(compositor):
    fake = compositor(taken=True)

    async def scenario():
        transport = GlobalShortcut(PushToTalk(cfg()), display=str(fake.path))
        with pytest.raises(WireError, match="already taken"):
            transport.start()
        return transport

    transport = asyncio.run(scenario())
    assert transport._thread is None and transport._sock is None


def test_a_compositor_without_the_protocol_fails_start_by_naming_it(compositor):
    fake = compositor(advertise=False)

    async def scenario():
        with pytest.raises(WireError, match="hyprland_global_shortcuts_manager_v1"):
            GlobalShortcut(PushToTalk(cfg()), display=str(fake.path)).start()

    asyncio.run(scenario())


def test_a_missing_display_socket_is_a_wire_error(tmp_path):
    async def scenario():
        with pytest.raises(WireError, match="cannot connect"):
            GlobalShortcut(PushToTalk(cfg()), display=str(tmp_path / "absent")).start()

    asyncio.run(scenario())
