"""HYPRUSE_DRYRUN: run every check, deliver nothing.

Two properties matter and are tested separately. A dry run must produce
the SAME refusals a real run would, or it is worth nothing as a
rehearsal. And it must reach no effect at all, which is checked twice:
once by spying on the effect functions from the tool boundary, and once
by calling those functions directly to prove the barrier inside them
fires for any path nobody thought of.
"""

import pytest

from hypruse import clipboard as clip
from hypruse import hyprctl, journal, trust
from hypruse import input as hinput
from hypruse import server as srv

CLIENT = {
    "address": "0xabc", "class": "kitty", "title": "shell", "pid": 7,
    "at": [0, 0], "size": [800, 600], "mapped": True,
    "workspace": {"id": 1, "name": "1"},
}
MONITORS = [{"name": "DP-1", "x": 0, "y": 0, "width": 1920, "height": 1080,
             "scale": 1.0, "activeWorkspace": {"id": 1}}]


@pytest.fixture
def dry(monkeypatch):
    """Dry run, with every effect replaced by a recorder. Anything that
    lands in `effects` is an action a dry run delivered."""
    monkeypatch.setenv("HYPRUSE_DRYRUN", "1")
    effects: list[tuple] = []
    monkeypatch.setattr(srv.safety, "touch", lambda *a: None)

    def query(cmd):
        return {
            "clients": [CLIENT],
            "activewindow": CLIENT,
            "monitors": MONITORS,
            "layers": {},
            "cursorpos": {"x": 10, "y": 10},
        }.get(cmd, {})

    monkeypatch.setattr(hyprctl, "query", query)
    monkeypatch.setattr(hyprctl, "batch_query", lambda cmds: [MONITORS, [CLIENT]])
    monkeypatch.setattr(hyprctl, "snapshot", lambda: {"windows": [CLIENT]})
    for name in ("dispatch", "keyword"):
        monkeypatch.setattr(hyprctl, name, lambda *a, _n=name: effects.append((_n, *a)))
    for name in ("move", "click", "drag", "scroll", "type_text", "key_combo"):
        monkeypatch.setattr(hinput, name, lambda *a, _n=name, **k: effects.append((_n, a, k)))
    monkeypatch.setattr(clip, "write", lambda t: effects.append(("clipboard", t)))
    return effects


def plan(result) -> str:
    return result if isinstance(result, str) else result[0].text


# --- every acting tool reports a plan and delivers nothing -------------------


def test_pointer_move(dry):
    assert plan(srv.pointer("move", x=800, y=60)) == (
        "DRY RUN, nothing was delivered: would move the cursor to (800, 60)"
    )
    assert dry == []


def test_pointer_click(dry):
    assert "would click left at (800, 60)" in plan(srv.pointer("click", x=800, y=60))
    assert dry == []


def test_pointer_click_without_coordinates_does_not_invent_a_target(dry):
    # the click would land wherever the cursor is when it runs, which is
    # not the same thing as where it is now
    assert "at the cursor" in plan(srv.pointer("click"))
    assert dry == []


def test_pointer_drag(dry):
    out = plan(srv.pointer("drag", x=1, y=2, to_x=3, to_y=4))
    assert "would drag left from (1, 2) to (3, 4)" in out
    assert dry == []


def test_pointer_scroll(dry):
    assert "would scroll dy=3" in plan(srv.pointer("scroll", scroll_dy=3, x=5, y=6))
    assert dry == []


def test_keyboard_type(dry):
    assert plan(srv.keyboard("type", text="hello")) == (
        "DRY RUN, nothing was delivered: would type 5 characters"
    )
    assert dry == []


def test_keyboard_key(dry):
    assert "would press ctrl+t" in plan(srv.keyboard("key", keys="ctrl+t"))
    assert dry == []


def test_keyboard_does_not_focus_the_target_window(dry):
    # focusing is itself a visible change to the human's seat, so a dry
    # run stops short of it, not just short of the keystrokes
    out = plan(srv.keyboard("type", text="hi", window="0xabc"))
    assert "would type 2 characters into 0xabc" in out
    assert dry == []


def test_click_ui(dry, monkeypatch):
    monkeypatch.setattr(
        srv, "_ui_read",
        lambda window="", name="", actionable=True: [
            {"role": "push button", "name": "OK", "x": 40, "y": 50, "clickable": True}
        ],
    )
    out = plan(srv.click_ui(name="OK"))
    assert "would click push button 'OK' at (40, 50) in kitty" in out
    assert dry == []


def test_hypr_close_window(dry):
    assert "would ask 0xabc to close" in plan(srv.hypr("close_window", target="0xabc"))
    assert dry == []


def test_hypr_workspace(dry):
    assert "would switch to workspace 3" in plan(srv.hypr("workspace", workspace="3"))
    assert dry == []


def test_hypr_move_window(dry):
    out = plan(srv.hypr("move_window", target="0xabc", workspace="4"))
    assert "would move 0xabc to workspace 4" in out
    assert dry == []


def test_hypr_fullscreen_names_the_active_window(dry):
    assert "would toggle fullscreen on the active window" in plan(srv.hypr("fullscreen"))
    assert dry == []


def test_launch(dry):
    out = srv.launch("kitty", workspace="2")
    assert "would run [workspace 2 silent] kitty" in out
    assert dry == []


def test_use_bind(dry, monkeypatch):
    monkeypatch.setattr(
        hyprctl, "find_bind", lambda combo: {"combo": "SUPER+F", "action": "exec", "arg": "wofi"}
    )
    assert "would run SUPER+F: exec wofi" in plan(srv.use_bind("SUPER+F"))
    assert dry == []


def test_clipboard_write(dry):
    assert srv.clipboard("write", text="hello") == (
        "DRY RUN, nothing was delivered: would copy 5 characters to the clipboard"
    )
    assert dry == []


def test_sequence_says_so_in_its_summary(dry):
    steps = [{"op": "pointer", "action": "click", "x": 1, "y": 2},
             {"op": "keyboard", "action": "type", "text": "hi"}]
    out = plan(srv.sequence(steps, stop_on_change=False, then="none"))
    # the per-step lines say it, but the summary line is what gets skimmed
    assert out.startswith("sequence (DRY RUN, nothing was delivered): all 2/2 steps ran")
    assert out.count("DRY RUN") == 3
    assert dry == []


# --- the guards are the point: a rehearsal that skips them proves nothing ----


def test_confinement_still_refuses(dry, monkeypatch):
    monkeypatch.setenv("HYPRUSE_CONFINE", "class:firefox")
    with pytest.raises(trust.TrustError, match="confinement scope"):
        srv.pointer("click", x=10, y=10)


def test_the_auth_guard_still_refuses(dry, monkeypatch):
    monkeypatch.setattr(
        hyprctl, "batch_query",
        lambda cmds: [MONITORS, [{**CLIENT, "class": "hyprpolkitagent"}]],
    )
    with pytest.raises(trust.TrustError, match="authentication dialog"):
        srv.pointer("click", x=10, y=10)


def test_a_locked_session_still_refuses(dry, monkeypatch):
    monkeypatch.setattr(trust, "session_locked", lambda: "hyprlock")
    with pytest.raises(trust.TrustError, match="session is locked"):
        srv.keyboard("type", text="hi", window="0xabc")


def test_the_seat_guard_still_refuses(dry, monkeypatch):
    monkeypatch.setenv("HYPRUSE_STRICT", "1")
    monkeypatch.setattr(trust, "_seat", {"cursor": (0, 0), "active": "0xold"})
    with pytest.raises(trust.TrustError, match="seat moved"):
        srv.pointer("click", x=10, y=10)


def test_argument_errors_are_identical_to_a_real_run(dry):
    with pytest.raises(ValueError, match="move needs x and y"):
        srv.pointer("move")
    with pytest.raises(ValueError, match="type needs text"):
        srv.keyboard("type")
    with pytest.raises(ValueError, match="workspace action needs"):
        srv.hypr("workspace")
    with pytest.raises(ValueError, match="move_window needs"):
        srv.hypr("move_window", target="0xabc")
    with pytest.raises(ValueError, match="not a window address"):
        srv.hypr("focus_window", target="firefox")
    with pytest.raises(ValueError, match="unknown action"):
        srv.hypr("teleport")


# --- observation is not action ----------------------------------------------


def test_reads_still_work(dry, monkeypatch):
    assert srv.desktop() == {"windows": [CLIENT]}
    monkeypatch.setattr(srv, "_ui_read", lambda *a, **k: [{"role": "button"}])
    assert srv.ui() == [{"role": "button"}]
    assert dry == []


def test_a_clipboard_read_still_reads(dry, monkeypatch):
    monkeypatch.setattr(clip, "read", lambda: "on the clipboard")
    assert srv.clipboard("read") == "on the clipboard"


# --- the barrier: for any path the tools above missed -----------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda: hinput.move(1, 2),
        lambda: hinput.click(1, 2),
        lambda: hinput.drag(1, 2, 3, 4),
        lambda: hinput.scroll(dy=1),
        lambda: hinput.type_text("x"),
        lambda: hinput.key_combo("ctrl+t"),
        lambda: hyprctl.dispatch("killactive"),
        lambda: clip.write("x"),
    ],
)
def test_the_effect_boundary_refuses_under_dry_run(monkeypatch, call):
    monkeypatch.setenv("HYPRUSE_DRYRUN", "1")
    # If the barrier ever regresses, this test must FAIL, not dispatch
    # killactive at whatever the developer had focused. Every backend
    # below the barrier is replaced with something that says so.
    def escaped(*_a, **_k):
        raise AssertionError("the dry-run barrier let a real effect through")

    monkeypatch.setattr(hyprctl, "_run", escaped)
    monkeypatch.setattr(hinput, "_wtype", escaped)
    monkeypatch.setattr(hinput, "_with_pointer", escaped)
    monkeypatch.setattr(clip, "_tool", escaped)
    with pytest.raises(journal.DryRunError, match="dry-run barrier"):
        call()


def test_reads_are_never_barriered(monkeypatch):
    monkeypatch.setenv("HYPRUSE_DRYRUN", "1")
    monkeypatch.setattr(hyprctl, "_run", lambda *a: '{"x": 1, "y": 2}')
    assert hyprctl.cursor_pos() == (1, 2)


def test_the_barrier_is_off_when_dry_run_is(monkeypatch):
    monkeypatch.delenv("HYPRUSE_DRYRUN", raising=False)
    journal.refuse_if_dry("anything")  # no raise


# --- round-6 review: a rehearsal must reject what the real run rejects -------


def test_an_unknown_button_is_rejected(dry):
    # the check lives in hinput.click, which a dry run never reaches, so
    # the tool has to make it before deciding whether to act
    with pytest.raises(hinput.InputError, match="unknown button"):
        srv.pointer("click", x=1, y=1, button="wheel")


def test_a_half_given_point_is_rejected(dry):
    with pytest.raises(hinput.InputError, match="both x and y"):
        srv.pointer("click", x=1)


def test_a_scroll_of_nothing_is_rejected(dry):
    with pytest.raises(hinput.InputError, match="non-zero"):
        srv.pointer("scroll", x=1, y=1)


def test_an_unknown_modifier_is_rejected(dry):
    with pytest.raises(hinput.InputError, match="unknown modifier"):
        srv.keyboard("key", keys="ctlr+t")


def test_a_bad_combo_does_not_focus_the_window_first(dry, monkeypatch):
    # a refused call must not have moved the human's focus on its way out
    monkeypatch.delenv("HYPRUSE_DRYRUN", raising=False)
    with pytest.raises(hinput.InputError):
        srv.keyboard("key", keys="ctlr+t", window="0xabc")
    assert dry == []


def test_a_malformed_target_is_rejected_for_every_action(dry):
    for action in ("fullscreen", "toggle_floating"):
        with pytest.raises(ValueError, match="not a window address"):
            srv.hypr(action, target="firefox")


def test_the_same_rejections_happen_for_real(dry, monkeypatch):
    monkeypatch.delenv("HYPRUSE_DRYRUN", raising=False)
    with pytest.raises(hinput.InputError, match="unknown button"):
        srv.pointer("click", x=1, y=1, button="wheel")
    with pytest.raises(hinput.InputError, match="non-zero"):
        srv.pointer("scroll", x=1, y=1)
    assert dry == []
