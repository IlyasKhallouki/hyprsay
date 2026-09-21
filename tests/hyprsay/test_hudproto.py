"""The HUD protocol, engine side. Real AF_UNIX sockets under tmp_path, no HUD process,
no compositor. The clock is a fake wherever time matters."""

import ast
import asyncio
import contextlib
import json
import os
import socket
import stat
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from hyprsay import hudproto
from hyprsay.config import HUD
from hyprsay.hudproto import (
    HELP_EXAMPLES,
    HIDDEN,
    PROTO,
    STATES,
    HudServer,
    RateLimit,
    badges_for,
    compose,
    from_decision,
    level_update,
    next_state,
    socket_path,
    state_message,
)
from hyprsay.model import (
    Action,
    App,
    Candidate,
    Decision,
    DesktopState,
    Intent,
    Monitor,
    Verdict,
    Window,
)

HUD_SOURCE = Path(__file__).resolve().parents[2] / "hud" / "hyprsay_hud.py"


def window(address: str, **kw) -> Window:
    base = dict(
        address=address,
        cls="firefox",
        initial_class="firefox",
        title="untrusted",
        workspace_id=1,
        workspace_name="1",
        monitor=0,
        at=(7, 7),
        size=(950, 1066),
    )
    return Window(**{**base, **kw})


LEFT = window("0xa", at=(7, 7), size=(950, 1066))
RIGHT = window("0xb", cls="kitty", at=(963, 7), size=(950, 1066))
ELSEWHERE = window("0xc", workspace_id=2, workspace_name="2", at=(7, 7), size=(1906, 1066))
DESKTOP = DesktopState(
    windows=(LEFT, RIGHT, ELSEWHERE),
    monitors=(Monitor(0, "eDP-1", width=1920, height=1080, focused=True, active_workspace_id=1),),
    active_address="0xa",
    active_workspace_id=1,
    locked=False,
)
FOCUS = Action(Intent.FOCUS_WINDOW, window=LEFT)
CANDIDATES = (
    Candidate("Firefox", window=LEFT),
    Candidate("kitty", window=RIGHT),
    Candidate("Firefox (workspace 2)", window=ELSEWHERE),
)


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


# --------------------------------------------------------------------------- messages


def test_a_state_message_always_carries_every_field():
    assert state_message("thinking") == {
        "t": "state",
        "state": "thinking",
        "text": "",
        "chip": "",
        "level": 0.0,
        "badges": [],
        "countdown_ms": 0,
        "suggestions": [],
        "ttl_ms": 0,
    }


def test_an_unknown_state_is_an_error_for_the_builder():
    with pytest.raises(ValueError):
        state_message("listening")


def test_text_is_one_bounded_line_without_control_or_bidi_characters():
    hostile = 'evil\n{"t":"state"}\u202e' + "x" * 500
    text = state_message("heard", text=hostile)["text"]
    assert "\n" not in text and "\u202e" not in text
    assert len(text) <= hudproto.TEXT_CHARS


def test_the_level_is_clamped_and_never_nan():
    assert state_message("hearing", level=7)["level"] == 1.0
    assert state_message("hearing", level=-1)["level"] == 0.0
    assert state_message("hearing", level=float("nan"))["level"] == 0.0
    assert level_update(0.123456) == {"t": "state", "state": "hearing", "level": 0.123}


def test_the_socket_lives_in_the_hyprsay_runtime_directory():
    assert socket_path({"XDG_RUNTIME_DIR": "/run/user/1000"}) == Path(
        "/run/user/1000/hyprsay/hud.sock"
    )


# --------------------------------------------------------------------------- decisions


def test_act_shows_what_was_heard_and_the_action_chip_then_hides_by_itself():
    msg = from_decision(Decision(Verdict.ACT, action=FOCUS, heard="focus firefox"), DESKTOP)
    assert (msg["state"], msg["text"], msg["chip"]) == (
        "heard",
        "focus firefox",
        "focus window firefox",
    )
    assert msg["ttl_ms"] > 0 and msg["badges"] == []


def test_act_swap_shows_badges_for_the_swap_timeout():
    decision = Decision(Verdict.ACT_SWAP, action=FOCUS, candidates=CANDIDATES, heard="firefox")
    msg = from_decision(decision, DESKTOP, hud=HUD(swap_timeout_s=2.0))
    assert msg["state"] == "swap" and msg["ttl_ms"] == 2000
    assert [b["n"] for b in msg["badges"]] == [1, 2, 3]
    assert msg["chip"] == "focus window firefox"


def test_hints_ask_for_a_number_for_the_hint_timeout():
    msg = from_decision(Decision(Verdict.HINTS, candidates=CANDIDATES, heard="firefox"), DESKTOP)
    assert msg["state"] == "hints" and msg["ttl_ms"] == 4000
    assert msg["chip"] == "say a number" and len(msg["badges"]) == 3


def test_countdown_carries_the_configured_duration_and_no_ttl():
    decision = Decision(Verdict.COUNTDOWN, action=Action(Intent.CLOSE_WINDOW, window=LEFT), tier=2)
    msg = from_decision(decision, DESKTOP, countdown_s=1.2)
    assert (msg["state"], msg["countdown_ms"], msg["ttl_ms"]) == ("countdown", 1200, 0)


def test_confirm_key_is_a_countdown_without_a_timer():
    decision = Decision(Verdict.CONFIRM_KEY, action=Action(Intent.LOCK_SCREEN), tier=3)
    msg = from_decision(decision, DESKTOP)
    assert (msg["state"], msg["countdown_ms"]) == ("countdown", 0)
    assert msg["text"] == hudproto.CONFIRM_TEXT and msg["chip"] == "lock screen"


def test_refuse_says_why():
    msg = from_decision(Decision(Verdict.REFUSE, reason="the session is locked"), DESKTOP)
    assert (msg["state"], msg["text"]) == ("refused", "the session is locked")
    assert msg["ttl_ms"] > 0


def test_suggest_is_never_a_dead_end():
    decision = Decision(
        Verdict.SUGGEST,
        reason="say which workspace",
        suggestions=("workspace 3", "move this to workspace 2"),
        heard="move it",
    )
    msg = from_decision(decision, DESKTOP)
    assert msg["state"] == "suggest"
    assert (msg["text"], msg["chip"]) == ("move it", "say which workspace")
    assert msg["suggestions"] == ["workspace 3", "move this to workspace 2"]


def test_nothing_hides_the_hud():
    assert from_decision(Decision(Verdict.NOTHING), DESKTOP) == HIDDEN


def test_help_falls_back_to_built_in_examples_when_the_decision_has_none():
    msg = from_decision(Decision(Verdict.ACT, action=Action(Intent.HELP), heard="help"), DESKTOP)
    assert msg["state"] == "help" and msg["suggestions"] == list(HELP_EXAMPLES)
    own = Decision(Verdict.ACT, action=Action(Intent.HELP), suggestions=("undo",))
    assert from_decision(own, DESKTOP)["suggestions"] == ["undo"]


def test_every_verdict_builds_a_message_the_state_machine_accepts_after_thinking():
    for verdict in Verdict:
        msg = from_decision(Decision(verdict, action=FOCUS, candidates=CANDIDATES), DESKTOP)
        assert msg["state"] in STATES
        assert next_state("thinking", msg) == msg["state"]
        json.dumps(msg, allow_nan=False)


def test_the_server_builds_messages_with_its_own_timeouts():
    server = HudServer(Path("/nonexistent/hud.sock"), hud=HUD(hint_timeout_s=9.0), countdown_s=3.0)
    assert server.from_decision(Decision(Verdict.HINTS), DESKTOP)["ttl_ms"] == 9000
    counting = Decision(Verdict.COUNTDOWN, action=FOCUS)
    assert server.from_decision(counting, DESKTOP)["countdown_ms"] == 3000


# --------------------------------------------------------------------------- badge geometry


def test_a_badge_is_the_window_rectangle_in_global_logical_pixels():
    first, second, _ = badges_for(CANDIDATES, DESKTOP)
    assert first == {"n": 1, "label": "Firefox", "x": 7, "y": 7, "w": 950, "h": 1066}
    assert (second["x"], second["y"], second["w"], second["h"]) == (963, 7, 950, 1066)


def test_geometry_comes_from_the_live_snapshot_not_the_stale_candidate():
    moved = replace(LEFT, at=(1927, 7), size=(1200, 700))
    badge = badges_for(CANDIDATES[:1], replace(DESKTOP, windows=(moved, RIGHT)))[0]
    assert (badge["x"], badge["y"], badge["w"], badge["h"]) == (1927, 7, 1200, 700)


def test_a_window_on_another_workspace_keeps_its_number_but_has_no_rectangle():
    third = badges_for(CANDIDATES, DESKTOP)[2]
    assert third["n"] == 3 and (third["w"], third["h"]) == (0, 0)


def test_a_closed_window_an_app_and_a_hidden_group_member_have_no_rectangle():
    gone = Candidate("gone", window=window("0xdead"))
    app = Candidate("Files", app=App(id="nautilus", name="Files"))
    grouped = replace(RIGHT, hidden=True)
    state = replace(DESKTOP, windows=(LEFT, grouped))
    badges = badges_for((gone, app, Candidate("kitty", window=grouped)), state)
    assert [b["n"] for b in badges] == [1, 2, 3]
    assert all(b["w"] == 0 for b in badges)


def test_only_the_fullscreen_window_of_a_workspace_is_on_screen():
    full = replace(RIGHT, fullscreen=True, at=(0, 0), size=(1920, 1080))
    badges = badges_for(CANDIDATES[:2], replace(DESKTOP, windows=(LEFT, full)))
    assert badges[0]["w"] == 0 and badges[1]["w"] == 1920


def test_a_pinned_window_is_on_screen_on_every_workspace():
    pinned = replace(ELSEWHERE, pinned=True, floating=True)
    badge = badges_for((Candidate("pip", window=pinned),), replace(DESKTOP, windows=(pinned,)))[0]
    assert badge["w"] == 1906


def test_a_second_monitor_keeps_its_global_offset():
    far = window("0xd", workspace_id=5, monitor=1, at=(1927, 7), size=(2546, 1426))
    state = replace(
        DESKTOP,
        windows=(LEFT, far),
        monitors=(*DESKTOP.monitors, Monitor(1, "DP-1", x=1920, active_workspace_id=5)),
    )
    badge = badges_for((Candidate("far", window=far),), state)[0]
    assert (badge["x"], badge["y"]) == (1927, 7)


def test_without_a_snapshot_the_candidate_window_is_used_as_it_is():
    assert badges_for(CANDIDATES)[2]["w"] == 1906


def test_there_are_never_more_badges_than_single_spoken_digits():
    many = tuple(Candidate(f"w{i}", window=LEFT) for i in range(14))
    assert [b["n"] for b in badges_for(many, DESKTOP)] == list(range(1, 10))


def test_a_hostile_title_in_a_label_cannot_break_the_line_protocol():
    badge = badges_for((Candidate('x\n{"t":"state","state":"hidden"}', window=LEFT),), DESKTOP)[0]
    line = json.dumps(state_message("hints", badges=[badge]))
    assert "\n" not in line and "\n" not in badge["label"]


# --------------------------------------------------------------------------- next_state


def test_an_utterance_walks_hearing_thinking_still_thinking_result():
    state = "hidden"
    for step in ("hearing", "hearing", "thinking", "still_thinking", "hints", "heard", "hidden"):
        state = next_state(state, state_message(step))
        assert state == step


def test_hidden_hearing_and_refused_are_reachable_from_every_state():
    for current in STATES:
        for target in ("hidden", "hearing", "refused"):
            assert next_state(current, state_message(target)) == target


def test_the_grammar_may_answer_straight_from_hearing():
    assert next_state("hearing", state_message("heard")) == "heard"


def test_a_countdown_ends_in_the_action_or_in_swap_badges():
    assert next_state("countdown", state_message("heard")) == "heard"
    assert next_state("countdown", state_message("swap")) == "swap"


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("hidden", "thinking"),
        ("hidden", "heard"),
        ("hidden", "countdown"),
        ("still_thinking", "thinking"),
        ("heard", "countdown"),
        ("suggest", "heard"),
        ("refused", "swap"),
        ("hints", "countdown"),
    ],
)
def test_an_illegal_transition_leaves_the_state_alone(current, target):
    assert next_state(current, state_message(target)) == current


def test_malformed_messages_leave_the_state_alone():
    for bad in ({}, {"t": "level"}, {"t": "state"}, {"t": "state", "state": 7}, [], None, "x"):
        assert next_state("hearing", bad) == "hearing"
    assert next_state("hearing", {"t": "state", "state": "dancing"}) == "hearing"


def test_an_update_keeps_the_fields_it_does_not_mention():
    shown = state_message("hearing", text="focus fire", level=0.2)
    after = compose(shown, level_update(0.6))
    assert (after["text"], after["level"]) == ("focus fire", 0.6)


def test_a_new_state_starts_from_defaults():
    shown = state_message("heard", text="focus firefox", chip="focus window firefox", ttl_ms=1500)
    after = compose(shown, {"t": "state", "state": "hearing", "level": 0.1})
    assert (after["text"], after["chip"], after["ttl_ms"]) == ("", "", 0)


def test_badges_stay_up_through_the_utterance_that_answers_them():
    badges = badges_for(CANDIDATES, DESKTOP)
    shown = state_message("hints", badges=badges, ttl_ms=4000)
    hearing = compose(shown, level_update(0.0))
    thinking = compose(hearing, {"t": "state", "state": "thinking"})
    assert hearing["badges"] == badges and thinking["badges"] == badges
    assert hearing["ttl_ms"] == 0
    assert compose(thinking, state_message("heard", text="two"))["badges"] == []


def test_compose_refuses_what_next_state_refuses():
    assert compose(HIDDEN, state_message("heard")) is None
    assert compose(HIDDEN, {"t": "nonsense"}) is None


# --------------------------------------------------------------------------- rate limiting


def test_level_updates_are_limited_to_thirty_a_second():
    clock = Clock()
    limit = RateLimit(30, clock)
    allowed = 0
    for _ in range(1000):  # one second of a caller that sends a thousand
        allowed += limit.allow()
        clock.now += 0.001
    assert 28 <= allowed <= 30


def test_a_caller_already_at_thirty_hertz_loses_nothing():
    clock = Clock()
    limit = RateLimit(30, clock)
    for _ in range(30):
        assert limit.allow()
        clock.now += 0.034


# --------------------------------------------------------------------------- the server


def serve(tmp_path, **kw) -> HudServer:
    return HudServer(tmp_path / "rt" / "hud.sock", **kw)


async def connect(server: HudServer, hello: object = None):
    reader, writer = await asyncio.open_unix_connection(str(server.path))
    hello = {"t": "hello", "proto": PROTO} if hello is None else hello
    raw = hello if isinstance(hello, bytes) else json.dumps(hello).encode() + b"\n"
    writer.write(raw)
    await writer.drain()
    return reader, writer


async def until(predicate, seconds: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "condition never became true"
        await asyncio.sleep(0.005)


async def line(reader) -> dict:
    return json.loads(await asyncio.wait_for(reader.readline(), 2.0))


def test_the_directory_is_0700_and_the_socket_0600(tmp_path):
    async def scenario():
        server = serve(tmp_path)
        assert await server.start()
        directory, sock = server.path.parent.stat(), server.path.stat()
        await server.close()
        return stat.S_IMODE(directory.st_mode), stat.S_IMODE(sock.st_mode), sock.st_mode

    directory_mode, socket_mode, raw = asyncio.run(scenario())
    assert (directory_mode, socket_mode) == (0o700, 0o600)
    assert stat.S_ISSOCK(raw)
    assert not (tmp_path / "rt" / "hud.sock").exists(), "close() removes the socket"


def test_a_loose_directory_is_tightened(tmp_path):
    (tmp_path / "rt").mkdir(mode=0o755)

    async def scenario():
        server = serve(tmp_path)
        await server.start()
        mode = stat.S_IMODE(server.path.parent.stat().st_mode)
        await server.close()
        return mode

    assert asyncio.run(scenario()) == 0o700


def test_send_never_raises_with_no_client_and_even_before_start(tmp_path):
    async def scenario():
        server = serve(tmp_path)
        await server.send(state_message("hearing"))  # not started at all
        await server.start()
        await server.send(state_message("thinking"))
        await server.send({"t": "state", "state": "no-such-state"})
        await server.send({"garbage": object()})
        await server.send(None)
        assert server.send_nowait(state_message("heard")) is False
        state = server.state
        await server.close()
        await server.send(state_message("hidden"))  # and after close
        return state

    assert asyncio.run(scenario()) == "heard"


def test_a_correct_hello_is_accepted_and_messages_arrive_as_lines(tmp_path):
    async def scenario():
        server = serve(tmp_path)
        await server.start()
        reader, writer = await connect(server)
        await until(lambda: server.connected)
        await server.send(state_message("hearing", text="focus"))
        await server.send({"t": "state", "state": "thinking"})
        got = [await line(reader), await line(reader)]
        writer.close()
        await until(lambda: not server.connected)
        await server.close()
        return got

    first, second = asyncio.run(scenario())
    assert (first["state"], first["text"]) == ("hearing", "focus")
    assert second == state_message("thinking")


@pytest.mark.parametrize(
    "hello",
    [
        {"t": "hello", "proto": PROTO + 1},
        {"t": "hello"},
        {"t": "hello", "proto": True},
        {"t": "state", "state": "hidden"},
        b"not json at all\n",
        b"[1, 2]\n",
        b"x" * 9000 + b"\n",
    ],
)
def test_a_wrong_hello_is_refused_and_remembered(tmp_path, hello):
    async def scenario():
        server = serve(tmp_path)
        await server.start()
        reader, _writer = await connect(server, hello)
        answer = await line(reader)
        assert await asyncio.wait_for(reader.read(), 2.0) == b"", "the server hangs up"
        connected, refusal = server.connected, server.last_refusal
        await server.close()
        return answer, connected, refusal

    answer, connected, refusal = asyncio.run(scenario())
    assert answer["t"] == "refused" and answer["reason"] == refusal
    assert not connected


def test_a_protocol_mismatch_names_both_versions(tmp_path):
    async def scenario():
        server = serve(tmp_path)
        await server.start()
        reader, _ = await connect(server, {"t": "hello", "proto": 7})
        answer = await line(reader)
        await server.close()
        return answer["reason"]

    reason = asyncio.run(scenario())
    assert "engine speaks 1" in reason and "HUD speaks 7" in reason


def test_a_silent_client_is_refused_after_the_hello_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(hudproto, "HELLO_TIMEOUT_S", 0.05)

    async def scenario():
        server = serve(tmp_path)
        await server.start()
        reader, _writer = await asyncio.open_unix_connection(str(server.path))
        answer = await line(reader)
        await server.close()
        return answer

    assert asyncio.run(scenario())["t"] == "refused"


def test_a_peer_with_another_uid_is_refused_before_its_hello_is_read(tmp_path):
    async def scenario():
        server = serve(tmp_path, expected_uid=os.getuid() + 1)
        await server.start()
        reader, _writer = await connect(server)
        answer = await line(reader)
        await server.send(state_message("hearing"))
        try:
            rest = await asyncio.wait_for(reader.read(), 2.0)
        except ConnectionResetError:
            # our hello was never read, so the kernel reports the hang-up as a reset
            rest = b""
        connected = server.connected
        await server.close()
        return answer, rest, connected

    answer, rest, connected = asyncio.run(scenario())
    assert answer["t"] == "refused" and "uid" in answer["reason"]
    assert rest == b"" and not connected


def test_the_peer_uid_is_read_from_the_socket_itself():
    ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        assert hudproto._peer_uid(ours) == os.getuid()
        assert hudproto._peer_uid(object()) is None, "unknown peers fail closed"
    finally:
        ours.close()
        theirs.close()


def test_exactly_one_hud_is_served(tmp_path):
    async def scenario():
        server = serve(tmp_path)
        await server.start()
        first_reader, first_writer = await connect(server)
        await until(lambda: server.connected)
        second_reader, _ = await connect(server)
        refused = await line(second_reader)
        await server.send(state_message("hearing"))
        still_served = await line(first_reader)
        # once the first HUD is gone, a new one is welcome
        first_writer.close()
        await until(lambda: not server.connected)
        third_reader, _ = await connect(server)
        await until(lambda: server.connected)
        replay = await line(third_reader)
        await server.close()
        return refused, still_served, replay

    refused, still_served, replay = asyncio.run(scenario())
    assert refused == {"t": "refused", "reason": "a HUD is already connected"}
    assert still_served["state"] == "hearing"
    assert replay["state"] == "hearing", "a restarted HUD is told what is on screen"


def test_send_survives_a_hud_that_died_without_saying_goodbye(tmp_path):
    async def scenario():
        server = serve(tmp_path)
        await server.start()
        _reader, writer = await connect(server)
        await until(lambda: server.connected)
        writer.transport.abort()
        for _ in range(50):
            await server.send(level_update(0.5))
            await server.send(state_message("hearing", text="x" * 100))
            await asyncio.sleep(0)
        await until(lambda: not server.connected)
        await server.send(state_message("thinking"))
        await server.close()

    asyncio.run(scenario())


def test_a_hud_that_stopped_reading_is_disconnected_not_waited_for(tmp_path, monkeypatch):
    monkeypatch.setattr(hudproto, "BACKLOG_LIMIT", 2048)

    async def scenario():
        server = serve(tmp_path)
        await server.start()
        _reader, _writer = await connect(server)  # never reads
        await until(lambda: server.connected)
        big = state_message("help", suggestions=["a phrase that takes some room"] * 8)
        loop = asyncio.get_running_loop()
        started = loop.time()
        for _ in range(20000):
            if not server.send_nowait(big):
                break
        elapsed = loop.time() - started
        connected = server.connected
        await server.close()
        return connected, elapsed

    connected, elapsed = asyncio.run(scenario())
    assert not connected and elapsed < 2.0


def test_the_server_rate_limits_the_meter_but_never_a_state_change(tmp_path):
    clock = Clock()

    async def scenario():
        server = serve(tmp_path, clock=clock)
        await server.start()
        reader, _ = await connect(server)
        await until(lambda: server.connected)
        await server.send(level_update(0.0))  # hidden -> hearing: a state change
        for i in range(300):  # 0.3 s of a meter running at 1 kHz
            await server.send(level_update(i / 300))
            clock.now += 0.001
        await server.send({"t": "state", "state": "thinking"})
        lines = []
        while not lines or lines[-1]["state"] != "thinking":
            lines.append(await line(reader))
        await server.close()
        return lines

    lines = asyncio.run(scenario())
    assert set(lines[0]) == set(HIDDEN), "the first hearing message is complete"
    meter = lines[1:-1]
    assert 8 <= len(meter) <= 10
    assert all(set(m) == {"t", "state", "level"} for m in meter), "meter lines stay small"


def test_an_illegal_transition_is_dropped_not_sent(tmp_path):
    async def scenario():
        server = serve(tmp_path)
        await server.start()
        reader, _ = await connect(server)
        await until(lambda: server.connected)
        assert server.send_nowait(state_message("heard")) is False  # hidden -> heard
        assert server.send_nowait(state_message("hearing")) is True
        got = await line(reader)
        await server.close()
        return got

    assert asyncio.run(scenario())["state"] == "hearing"


def test_a_state_with_a_ttl_counts_as_hidden_once_it_ran_out(tmp_path):
    clock = Clock()

    async def scenario():
        server = serve(tmp_path, clock=clock)
        await server.start()
        await server.send(state_message("hearing"))
        await server.send(state_message("heard", ttl_ms=1500))
        before = server.state
        clock.now += 1.6
        after = server.state
        # and a HUD that connects now is not shown the stale result
        reader, _ = await connect(server)
        await until(lambda: server.connected)
        await server.send(state_message("hearing"))
        first = await line(reader)
        await server.close()
        return before, after, first["state"]

    assert asyncio.run(scenario()) == ("heard", "hidden", "hearing")


def test_a_stale_socket_file_is_replaced_but_a_live_engine_is_left_alone(tmp_path):
    async def scenario():
        path = tmp_path / "rt" / "hud.sock"
        path.parent.mkdir(mode=0o700)
        dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        dead.bind(str(path))
        dead.close()  # the file stays, nobody listens
        first = HudServer(path)
        started = await first.start()
        second = HudServer(path)
        stolen = await second.start()
        await second.send(state_message("hearing"))
        await second.close()
        still_there = path.exists()
        await first.close()
        return started, stolen, still_there

    assert asyncio.run(scenario()) == (True, False, True)


def test_a_regular_file_in_the_way_is_not_deleted(tmp_path):
    path = tmp_path / "rt" / "hud.sock"
    path.parent.mkdir()
    path.write_text("precious")

    async def scenario():
        server = HudServer(path)
        return await server.start()

    assert asyncio.run(scenario()) is False
    assert path.read_text() == "precious"


# --------------------------------------------------------------------------- the other half


def hud_constants() -> dict:
    """Top level literals of the HUD script. It cannot be imported here (it needs the
    system PyGObject), and it may import nothing of ours, so its copies are compared."""
    found = {}
    for node in ast.parse(HUD_SOURCE.read_text()).body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            try:
                found[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                continue
    return found


def hud_functions(*names: str) -> list:
    """Pure functions lifted out of the HUD script by name, without running the rest."""
    tree = ast.parse(HUD_SOURCE.read_text())
    picked = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    namespace = {"contextlib": contextlib, "json": json}
    exec(compile(ast.Module(body=picked, type_ignores=[]), str(HUD_SOURCE), "exec"), namespace)
    return [namespace[name] for name in names]


def test_the_hud_reads_a_border_gradient_the_way_hyprland_prints_it():
    (parse_gradient,) = hud_functions("parse_gradient")
    stops, angle = parse_gradient("ffa4a4a4 ff4e4e4e 45deg")
    assert angle == 45 and len(stops) == 2
    assert stops[0] == pytest.approx((0.643, 0.643, 0.643, 1.0), abs=0.001)
    # Hyprland does not zero pad: alpha 0x0a comes out as seven digits
    (faint,), _ = parse_gradient("a33ccff 0deg")
    assert faint == pytest.approx((0x33 / 255, 0xCC / 255, 1.0, 0x0A / 255))
    assert parse_gradient("garbage") == ([], 0)


def test_the_hud_splits_a_batch_reply_into_its_json_documents():
    (json_stream,) = hud_functions("json_stream")
    reply = (
        '{"option": "general:border_size", "int": 2, "set": true }\n\n\n'
        '{"option": "general:gaps_out", "custom": "5 5 5 5", "set": true }\n\n\n\n'
        '{\n    "configProvider": "hyprlang",\n    "backend": "drm"\n}\n'
    )
    assert [sorted(doc)[0] for doc in json_stream(reply)] == ["int", "custom", "backend"]
    assert json_stream("unknown request") == []


def test_the_hud_script_copies_the_protocol_constants_exactly():
    theirs = hud_constants()
    assert theirs["PROTO"] == PROTO
    assert tuple(theirs["STATES"]) == STATES
    assert theirs["RUNTIME_SUBDIR"] == hudproto.RUNTIME_SUBDIR
    assert theirs["SOCKET_NAME"] == hudproto.SOCKET_NAME


def test_the_hud_script_imports_nothing_of_ours_and_nothing_beyond_gi():
    # pycairo is what PyGObject itself depends on, and cairo.Region is the input region
    allowed_roots = {"gi", "cairo"}
    for node in ast.walk(ast.parse(HUD_SOURCE.read_text())):
        names = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        for name in names:
            root = name.split(".")[0]
            assert root in sys.stdlib_module_names or root in allowed_roots, name
