"""The world against a fake Hyprland: real AF_UNIX sockets under tmp_path, no compositor.

The request fake reads every request to EOF before answering, like nothing else would
tell us the client half-closes; a client that forgot would hang there and fail by
timeout. Canned replies keep the newline padding the live 0.56.2 socket was seen to send.
"""

import asyncio
import contextlib
import json
import socket
import threading
import time
from pathlib import Path

import pytest

from hyprsay import world
from hyprsay.model import DesktopState
from hyprsay.world import (
    CONNECTED,
    DISCONNECTED,
    EventReader,
    HyprSocket,
    HyprSocketError,
    WorldModel,
    addr,
    read_locked,
    restore_transport,
    snapshot,
    use_socket_transport,
    wire_for,
)
from hypruse import hyprctl

# --------------------------------------------------------------------------- the fake


class FakeRequests:
    """`.socket.sock`: one request per connection, recorded, answered from a table."""

    def __init__(self, path: Path, replies: dict[str, str] | None = None) -> None:
        self.path = path
        self.replies = dict(replies or {})
        self.received: list[str] = []
        self._stop = threading.Event()
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(path))
        self._server.listen(8)
        self._server.settimeout(0.02)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except OSError:
                continue
            with conn, contextlib.suppress(OSError):
                conn.settimeout(2)
                data = b""
                while chunk := conn.recv(65536):
                    data += chunk
                request = data.decode()
                self.received.append(request)
                conn.sendall(self.answer(request).encode())

    def answer(self, request: str) -> str:
        if request.startswith("[[BATCH]]"):
            parts = [p.strip() for p in request.removeprefix("[[BATCH]]").split(";")]
            return "\n\n\n".join(self.replies.get(p, "unknown request") for p in parts)
        return self.replies.get(request, "unknown request")

    def close(self) -> None:
        self._stop.set()
        self._thread.join(2)
        self._server.close()


class FakeEvents:
    """`.socket2.sock`: writes lines to whoever is connected, and can hang up on them."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.accepted = 0
        self._writers: list[asyncio.StreamWriter] = []
        self._server: asyncio.Server | None = None

    async def start(self) -> "FakeEvents":
        self._server = await asyncio.start_unix_server(self._on_client, path=str(self.path))
        return self

    async def _on_client(self, _reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._writers.append(writer)
        self.accepted += 1

    async def emit(self, text: str) -> None:
        await until(lambda: self._writers)
        for writer in self._writers:
            writer.write(text.encode())
            await writer.drain()

    async def drop(self) -> None:
        for writer in self._writers:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
        self._writers.clear()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
        # a client the loop accepted a moment ago may not have reached _on_client yet
        await asyncio.sleep(0.01)
        await self.drop()


async def until(condition, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "the condition never became true"
        await asyncio.sleep(0.005)


def run(scenario) -> None:
    asyncio.run(asyncio.wait_for(scenario, 10))


def pretty(value) -> str:
    return json.dumps(value, indent=4)


def client(address: str, cls: str, **more) -> dict:
    base = {
        "address": address,
        "mapped": True,
        "hidden": False,
        "at": [7, 45],
        "size": [949, 1028],
        "workspace": {"id": 2, "name": "2"},
        "floating": False,
        "monitor": 0,
        "class": cls,
        "title": f"{cls} window",
        "initialClass": cls,
        "pid": 4242,
        "pinned": False,
        "fullscreen": 0,
        "focusHistoryID": 1,
    }
    return base | more


FOCUSED = "0x55a4aaaa0001"
OTHER = "0x55a4aaaa0002"

LAYERS = {
    "eDP-1": {
        "levels": {
            "0": [{"address": "0x1", "namespace": "wallpaper", "pid": 1}],
            "1": [],
            "2": [{"address": "0x2", "namespace": "waybar", "pid": 2}],
            "3": [],
        }
    }
}


def desktop(**overrides: str) -> dict[str, str]:
    """A whole canned desktop, keyed by wire command. Override a part as `clients="..."`."""
    replies = {
        # these two arrive padded with newlines on the live socket; the rest do not
        "locked": '\n{\n    "locked": false\n}\n',
        "status": '\n{\n    "configProvider": "hyprlang",\n    "backend": "drm"\n}\n',
        "clients": pretty(
            [
                client(OTHER, "kitty"),
                client(FOCUSED, "firefox", focusHistoryID=0, fullscreen=2, floating=True),
                client("0x55a4aaaa0003", "ghost", mapped=False),
            ]
        ),
        "workspaces": pretty(
            [
                {"id": 2, "name": "2", "monitor": "eDP-1", "windows": 2},
                {"id": -98, "name": "special:notes", "monitor": "eDP-1", "windows": 0},
            ]
        ),
        "monitors": pretty(
            [
                {
                    "id": 0,
                    "name": "eDP-1",
                    "width": 3840,
                    "height": 2160,
                    "x": 0,
                    "y": 0,
                    "scale": 2.0,
                    "transform": 0,
                    "focused": True,
                    "activeWorkspace": {"id": 2, "name": "2"},
                }
            ]
        ),
        "activewindow": pretty(client(FOCUSED, "firefox")),
        "activeworkspace": pretty({"id": 2, "name": "2"}),
        "layers": pretty(LAYERS),
    }
    replies.update(overrides)
    return {f"j/{command}": reply for command, reply in replies.items()}


SNAPSHOT_WIRE = (
    "[[BATCH]]j/locked;j/status;j/clients;j/workspaces;j/monitors;"
    "j/activewindow;j/activeworkspace;j/layers"
)


@pytest.fixture
def requests(tmp_path):
    fake = FakeRequests(tmp_path / "s.sock", desktop())
    yield fake
    fake.close()


@pytest.fixture
def sock(requests):
    return HyprSocket(requests.path, timeout=1.0)


# --------------------------------------------------------------------------- HyprSocket


def test_a_request_is_sent_with_the_slash_hyprctl_sends_and_then_half_closed(requests, sock):
    requests.replies["/locked"] = "false"
    assert sock.request("locked") == "false"
    # the fake only answers after EOF, so getting here proves the half-close
    assert requests.received == ["/locked"]


def test_query_sends_the_j_flag_and_decodes_the_reply(requests, sock):
    assert sock.query("activeworkspace") == {"id": 2, "name": "2"}
    assert requests.received == ["j/activeworkspace"]


def test_query_raises_when_the_compositor_answers_plain_text(sock):
    with pytest.raises(HyprSocketError, match="did not answer JSON"):
        sock.query("nonsense")


def test_a_batch_is_one_request_and_comes_back_as_one_reply_per_command(requests, sock):
    replies = sock.batch(["locked", "status", "nonsense"])
    assert requests.received == ["[[BATCH]]j/locked;j/status;j/nonsense"]
    assert json.loads(replies[0]) == {"locked": False}
    assert json.loads(replies[1])["configProvider"] == "hyprlang"
    assert replies[2] == "unknown request"


def test_a_batch_uses_the_flags_it_is_given(requests, sock):
    requests.replies |= {"/locked": "false", "/submap": "default\n"}
    assert sock.batch(["locked", "submap"], flags="") == ["false", "default"]


def test_reply_padding_that_adds_up_to_six_newlines_is_still_one_gap(requests, sock):
    requests.replies |= {"j/a": "one\n\n", "j/b": "\ntwo"}
    assert sock.batch(["a", "b"]) == ["one", "two"]


def test_a_batch_that_answers_the_wrong_number_of_replies_raises(requests, sock):
    requests.replies |= {"j/a": "one", "j/b": "", "j/c": "three"}
    with pytest.raises(HyprSocketError, match="answered 2 replies"):
        sock.batch(["a", "b", "c"])


def test_a_semicolon_is_refused_before_anything_is_sent(requests, sock):
    with pytest.raises(HyprSocketError, match="';'"):
        sock.batch(["dispatch exec true; dispatch exit"], flags="")
    assert requests.received == []


def test_an_empty_batch_sends_nothing(requests, sock):
    assert sock.batch([]) == []
    assert requests.received == []


def test_a_compositor_that_never_answers_is_a_timeout_not_a_hang(tmp_path):
    silent = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    silent.bind(str(tmp_path / "s.sock"))
    silent.listen(1)  # connects land in the backlog and nobody ever accepts them
    started = time.monotonic()
    with pytest.raises(HyprSocketError):
        HyprSocket(tmp_path / "s.sock", timeout=0.1).request("locked")
    assert time.monotonic() - started < 1.0
    silent.close()


def test_a_missing_socket_and_a_dead_one_both_raise(tmp_path):
    with pytest.raises(HyprSocketError):
        HyprSocket(tmp_path / "absent.sock").request("locked")
    dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dead.bind(str(tmp_path / "dead.sock"))
    dead.close()  # the file stays, nobody listens: connection refused
    with pytest.raises(HyprSocketError):
        HyprSocket(tmp_path / "dead.sock").request("locked")


def test_the_default_path_comes_from_the_session_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "sig")
    assert HyprSocket().path == tmp_path / "hypr" / "sig" / ".socket.sock"


def test_without_an_instance_signature_a_request_raises(monkeypatch):
    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
    with pytest.raises(HyprSocketError, match="HYPRLAND_INSTANCE_SIGNATURE"):
        HyprSocket().request("locked")


# --------------------------------------------------------------------------- snapshot


def test_snapshot_reads_the_whole_desktop_in_one_batch_with_the_lock_first(requests, sock):
    state = snapshot(sock)
    assert requests.received == [SNAPSHOT_WIRE]
    assert state.locked is False
    assert state.provider == "hyprlang"
    assert state.active_address == FOCUSED
    assert state.active_workspace_id == 2
    assert state.stamp > 0


def test_snapshot_windows_carry_what_the_resolver_needs_and_skip_unmapped_ones(sock):
    state = snapshot(sock)
    assert [w.cls for w in state.windows] == ["kitty", "firefox"]
    focused = state.focused
    assert focused is not None
    assert (focused.cls, focused.initial_class, focused.title) == (
        "firefox",
        "firefox",
        "firefox window",
    )
    assert (focused.workspace_id, focused.workspace_name, focused.monitor) == (2, "2", 0)
    assert (focused.floating, focused.fullscreen, focused.pinned, focused.hidden) == (
        True,
        True,
        False,
        False,
    )
    assert (focused.focus_rank, focused.pid) == (0, 4242)
    assert (focused.at, focused.size) == ((7, 45), (949, 1028))


def test_snapshot_addresses_are_always_in_0x_form(requests, sock):
    bare = "55a4aaaa0009"
    requests.replies |= desktop(
        clients=pretty([client(bare, "kitty")]), activewindow=pretty(client(bare, "kitty"))
    )
    state = snapshot(sock)
    assert state.windows[0].address == "0x" + bare
    assert state.active_address == "0x" + bare
    assert state.focused is state.windows[0]


def test_snapshot_with_nothing_focused_has_an_empty_active_address(requests, sock):
    requests.replies |= desktop(activewindow="{}")
    state = snapshot(sock)
    assert state.locked is False
    assert state.active_address == ""
    assert state.focused is None


def test_snapshot_workspaces_are_sorted_and_know_when_they_are_special(sock):
    workspaces = snapshot(sock).workspaces
    assert [w.id for w in workspaces] == [-98, 2]
    assert [w.special for w in workspaces] == [True, False]
    assert workspaces[1].windows == 2


def test_snapshot_monitors_are_in_the_logical_space_windows_live_in(sock):
    (monitor,) = snapshot(sock).monitors
    assert (monitor.width, monitor.height, monitor.scale) == (1920, 1080, 2.0)
    assert (monitor.name, monitor.focused, monitor.active_workspace_id) == ("eDP-1", True, 2)


def test_snapshot_layers_are_flattened_with_their_level(sock):
    layers = snapshot(sock).layers
    assert [(layer.namespace, layer.monitor, layer.level) for layer in layers] == [
        ("wallpaper", "eDP-1", 0),
        ("waybar", "eDP-1", 2),
    ]


def test_a_locked_session_is_reported_locked_with_its_real_windows(requests, sock):
    requests.replies |= desktop(locked='\n{\n    "locked": true\n}\n')
    state = snapshot(sock)
    assert state.locked is True
    assert len(state.windows) == 2


def test_the_lua_provider_is_read_from_status(requests, sock):
    requests.replies |= desktop(status='\n{\n    "configProvider": "lua"\n}\n')
    assert snapshot(sock).provider == "lua"


def test_a_compositor_without_the_status_request_is_the_legacy_provider(requests, sock):
    requests.replies |= desktop(status="unknown request")
    state = snapshot(sock)
    assert (state.provider, state.locked) == ("hyprlang", False)


@pytest.mark.parametrize(
    "broken",
    [
        {"clients": "unknown request"},
        {"locked": "unknown request"},
        {"locked": '{"locked": "false"}'},
        {"locked": '{"unlocked": true}'},
        {"activeworkspace": "{}"},
        {"activewindow": "[]"},
        {"clients": pretty([{"class": "kitty"}])},
        {"clients": pretty([client("0x1", "kitty", at=[1])])},
        {"layers": "[1, 2]"},
        {"monitors": ""},
    ],
)
def test_any_malformed_reply_fails_closed_to_the_locked_default(requests, sock, broken):
    requests.replies |= desktop(**broken)
    assert snapshot(sock) == DesktopState()
    assert snapshot(sock).locked is True


def test_a_refused_connection_fails_closed_without_raising(tmp_path):
    state = snapshot(HyprSocket(tmp_path / "absent.sock"))
    assert state == DesktopState()
    assert state.locked is True


def test_read_locked_is_the_answer_or_true_on_any_doubt(requests, sock, tmp_path):
    assert read_locked(sock) is False
    requests.replies["j/locked"] = '{"locked": true}'
    assert read_locked(sock) is True
    requests.replies["j/locked"] = "unknown request"
    assert read_locked(sock) is True
    assert read_locked(HyprSocket(tmp_path / "absent.sock")) is True


def test_addr_adds_the_prefix_only_when_it_is_missing():
    assert addr("55a448972080") == "0x55a448972080"
    assert addr("0x55a448972080") == "0x55a448972080"
    assert addr("") == ""


# --------------------------------------------------------------------------- hypruse shim


@pytest.fixture
def shim(requests, sock):
    use_socket_transport(sock)
    yield requests
    restore_transport()


@pytest.mark.parametrize(
    ("argv", "wire"),
    [
        (("-j", "clients"), "j/clients"),
        (("--batch", "j/monitors ; j/clients"), "[[BATCH]]j/monitors ; j/clients"),
        (("-j", "--batch", "monitors ; clients"), "[[BATCH]]j/monitors;j/clients"),
        (("dispatch", "focuswindow", "address:0x1"), "/dispatch focuswindow address:0x1"),
        (("notify", "-1", "4000", "0", "hello there"), "/notify -1 4000 0 hello there"),
        (("eval", "hl.notify('hi')"), "/eval hl.notify('hi')"),
    ],
)
def test_argv_becomes_the_bytes_hyprctl_would_have_sent(argv, wire):
    assert wire_for(argv) == wire


def test_an_option_the_shim_does_not_know_is_refused_in_hypruse_terms():
    with pytest.raises(hyprctl.HyprctlError):
        wire_for(("--instance", "1", "clients"))


def test_hypruse_query_travels_over_the_socket(shim):
    assert hyprctl.query("activeworkspace") == {"id": 2, "name": "2"}
    assert shim.received == ["j/activeworkspace"]


def test_hypruse_batch_query_travels_as_one_batch(shim):
    monitors, workspace = hyprctl.batch_query(["monitors", "activeworkspace"])
    assert shim.received == ["[[BATCH]]j/monitors ; j/activeworkspace"]
    assert monitors[0]["name"] == "eDP-1"
    assert workspace["id"] == 2


def test_hypruse_dispatch_travels_over_the_socket_and_ok_is_success(shim):
    shim.replies["/dispatch focuswindow address:0x1"] = "ok"
    hyprctl.dispatch("focuswindow", "address:0x1")
    assert shim.received == ["/dispatch focuswindow address:0x1"]


def test_a_lua_dispatch_is_one_expression_on_the_wire(shim, monkeypatch):
    monkeypatch.setattr(hyprctl, "_provider", hyprctl.LUA)
    expression = '/dispatch hl.dsp.focus({ window = "address:0x1" })'
    shim.replies[expression] = "ok"
    hyprctl.dispatch("focuswindow", "address:0x1")
    assert shim.received == [expression]


def test_a_dispatch_the_compositor_rejects_raises_hypruses_own_error(shim, monkeypatch):
    # the re-probe after a failure must find the same provider, or dispatch retries
    monkeypatch.setattr(hyprctl, "forget_provider", lambda: None)
    shim.replies["/dispatch focuswindow address:0x1"] = "No such window found"
    with pytest.raises(hyprctl.HyprctlError, match="No such window found"):
        hyprctl.dispatch("focuswindow", "address:0x1")


def test_an_unreachable_compositor_raises_hypruses_own_error_so_guards_fail_closed(tmp_path):
    use_socket_transport(HyprSocket(tmp_path / "absent.sock"))
    try:
        with pytest.raises(hyprctl.HyprctlError):
            hyprctl.query("clients")
        with pytest.raises(hyprctl.HyprctlError):
            hyprctl.batch_query(["clients"])
    finally:
        restore_transport()


def test_restore_puts_the_original_run_back_even_after_two_installs(sock):
    original = hyprctl._run
    try:
        use_socket_transport(sock)
        use_socket_transport(sock)
        assert hyprctl._run is not original
    finally:
        restore_transport()
    assert hyprctl._run is original
    restore_transport()  # a second restore is harmless
    assert hyprctl._run is original


# --------------------------------------------------------------------------- EventReader


async def take(events, count: int) -> list[tuple[str, str]]:
    return [await asyncio.wait_for(anext(events), 2) for _ in range(count)]


def test_the_reader_splits_a_line_at_the_first_separator_and_keeps_the_data_whole(tmp_path):
    async def scenario():
        fake = await FakeEvents(tmp_path / "e.sock").start()
        async with contextlib.aclosing(aiter(EventReader(fake.path))) as events:
            assert await take(events, 1) == [(CONNECTED, "")]
            await fake.emit("openwindow>>55a4aaaa0001,2,kitty,a >> b, c\nnoise\nconfigreloaded>>\n")
            assert await take(events, 2) == [
                ("openwindow", "55a4aaaa0001,2,kitty,a >> b, c"),
                ("configreloaded", ""),
            ]
        await fake.stop()

    run(scenario())


def test_the_reader_reconnects_after_the_socket_drops(tmp_path):
    async def scenario():
        fake = await FakeEvents(tmp_path / "e.sock").start()
        reader = EventReader(fake.path, backoff=(0.01, 0.05))
        async with contextlib.aclosing(aiter(reader)) as events:
            assert await take(events, 1) == [(CONNECTED, "")]
            await fake.emit("workspace>>2\n")
            assert await take(events, 1) == [("workspace", "2")]
            await fake.drop()
            assert await take(events, 2) == [(DISCONNECTED, ""), (CONNECTED, "")]
            await fake.emit("workspace>>3\n")
            assert await take(events, 1) == [("workspace", "3")]
            assert fake.accepted == 2
        await fake.stop()

    run(scenario())


def test_the_reader_keeps_trying_until_the_socket_exists(tmp_path):
    async def scenario():
        reader = EventReader(tmp_path / "e.sock", backoff=(0.01, 0.02))
        async with contextlib.aclosing(aiter(reader)) as events:
            first = asyncio.ensure_future(anext(events))
            await asyncio.sleep(0.05)
            assert not first.done()
            fake = await FakeEvents(tmp_path / "e.sock").start()
            assert await asyncio.wait_for(first, 2) == (CONNECTED, "")
        await fake.stop()

    run(scenario())


def test_a_line_cut_off_by_the_disconnect_is_not_delivered(tmp_path):
    async def scenario():
        fake = await FakeEvents(tmp_path / "e.sock").start()
        reader = EventReader(fake.path, backoff=(0.01, 0.05))
        async with contextlib.aclosing(aiter(reader)) as events:
            await take(events, 1)
            await fake.emit("workspace>>2\nclosewindow>>55a4")
            await fake.drop()
            assert await take(events, 2) == [("workspace", "2"), (DISCONNECTED, "")]
        await fake.stop()

    run(scenario())


# --------------------------------------------------------------------------- WorldModel


@contextlib.asynccontextmanager
async def following(requests: FakeRequests, tmp_path: Path, debounce: float = 0.02):
    """A WorldModel running against both fakes, connected and past its first snapshot."""
    fake = await FakeEvents(tmp_path / "e.sock").start()
    model = WorldModel(
        HyprSocket(requests.path, timeout=1.0),
        EventReader(fake.path, backoff=(0.01, 0.05)),
        debounce=debounce,
    )
    task = asyncio.create_task(model.run())
    try:
        await until(lambda: requests.received)
        yield model, fake
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await fake.stop()


def test_the_world_starts_locked_and_snapshots_as_soon_as_it_connects(requests, tmp_path):
    async def scenario():
        assert WorldModel(HyprSocket(requests.path)).state == DesktopState()
        async with following(requests, tmp_path) as (model, _):
            await until(lambda: not model.state.locked)
            assert model.state.active_address == FOCUSED
            assert requests.received == [SNAPSHOT_WIRE]

    run(scenario())


def test_a_burst_of_events_costs_one_snapshot(requests, tmp_path):
    async def scenario():
        async with following(requests, tmp_path) as (model, fake):
            requests.replies |= desktop(activewindow=pretty(client(OTHER, "kitty")))
            await fake.emit(
                "workspace>>2\nworkspacev2>>2,2\nactivewindow>>kitty,kitty window\n"
                f"activewindowv2>>{OTHER[2:]}\nfocusedmon>>eDP-1,2\n"
            )
            await until(lambda: model.state.active_address == OTHER)
            await asyncio.sleep(0.1)
            assert len(requests.received) == 2

    run(scenario())


def test_events_that_change_no_state_do_not_snapshot(requests, tmp_path):
    async def scenario():
        async with following(requests, tmp_path) as (_, fake):
            await fake.emit("activelayout>>kbd,English (US)\nsubmap>>resize\nbell>>\n")
            await asyncio.sleep(0.1)
            assert len(requests.received) == 1

    run(scenario())


def test_custom_events_reach_on_custom_subscribers_with_their_data(requests, tmp_path):
    async def scenario():
        async with following(requests, tmp_path) as (model, fake):
            heard: list[str] = []
            model.on_custom(heard.append)
            await fake.emit("custom>>hyprsay:ptt:down\ncustom>>hyprsay:ptt:up\n")
            await until(lambda: len(heard) == 2)
            assert heard == ["hyprsay:ptt:down", "hyprsay:ptt:up"]
            assert len(requests.received) == 1

    run(scenario())


def test_a_custom_event_sees_the_focus_change_that_arrived_just_before_it(requests, tmp_path):
    async def scenario():
        # a debounce this long never fires in the test: only the flush can deliver it
        async with following(requests, tmp_path, debounce=30.0) as (model, fake):
            focused_at_key_down: list[str] = []
            model.on_custom(lambda _: focused_at_key_down.append(model.state.active_address))
            requests.replies |= desktop(activewindow=pretty(client(OTHER, "kitty")))
            await fake.emit(f"activewindowv2>>{OTHER[2:]}\ncustom>>hyprsay:ptt:down\n")
            await until(lambda: focused_at_key_down)
            assert focused_at_key_down == [OTHER]

    run(scenario())


def test_configreloaded_snapshots_at_once_and_picks_up_a_new_provider(requests, tmp_path):
    async def scenario():
        async with following(requests, tmp_path, debounce=30.0) as (model, fake):
            requests.replies |= desktop(status='{"configProvider": "lua"}')
            await fake.emit("configreloaded>>\n")
            await until(lambda: model.state.provider == "lua")

    run(scenario())


def test_losing_the_event_socket_locks_the_world_until_it_is_back(requests, tmp_path):
    async def scenario():
        async with following(requests, tmp_path) as (model, fake):
            await until(lambda: not model.state.locked)
            states: list[DesktopState] = []
            model.subscribe(states.append)
            await fake.drop()
            await until(lambda: len(states) == 2 and fake.accepted == 2)
            assert states[0] == DesktopState()
            assert states[1].locked is False
            assert requests.received == [SNAPSHOT_WIRE] * 2

    run(scenario())


def test_subscribers_hear_a_change_of_content_not_every_snapshot(requests, tmp_path):
    async def scenario():
        async with following(requests, tmp_path) as (model, fake):
            await until(lambda: not model.state.locked)
            states: list[DesktopState] = []
            unsubscribe = model.subscribe(states.append)
            before = model.state.stamp

            await fake.emit("windowtitle>>55a4aaaa0001\n")
            await until(lambda: len(requests.received) == 2)
            await until(lambda: model.state.stamp > before)
            assert states == []

            requests.replies |= desktop(clients=pretty([client(FOCUSED, "firefox")]))
            await fake.emit("closewindow>>55a4aaaa0002\n")
            await until(lambda: states)
            assert [w.cls for w in states[0].windows] == ["firefox"]

            unsubscribe()
            requests.replies |= desktop(clients="[]")
            await fake.emit("closewindow>>55a4aaaa0001\n")
            await until(lambda: model.state.windows == ())
            assert len(states) == 1

    run(scenario())


def test_a_subscriber_that_raises_does_not_silence_the_others(requests, tmp_path):
    async def scenario():
        async with following(requests, tmp_path) as (model, fake):
            heard: list[str] = []

            def broken(_data: str) -> None:
                raise RuntimeError("subscriber bug")

            model.on_custom(broken)
            model.on_custom(heard.append)
            await fake.emit("custom>>one\ncustom>>two\n")
            await until(lambda: heard == ["one", "two"])

    run(scenario())


def test_refresh_snapshots_inline_without_the_event_socket(requests):
    model = WorldModel(HyprSocket(requests.path), EventReader("/nonexistent"))
    assert model.refresh().active_address == FOCUSED
    assert model.state.locked is False


def test_the_module_never_holds_a_request_connection_open(requests, sock):
    snapshot(sock)
    sock.query("clients")
    # every request the fake saw ended in EOF from the client side, one per connection
    assert len(requests.received) == 2
    assert world.HyprSocket(requests.path).path == requests.path
