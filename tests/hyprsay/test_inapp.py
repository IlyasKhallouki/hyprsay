"""Gestures: what reaches the inherited hypruse calls, at what tier, and where it is
refused. Nothing here touches a compositor: both delivery functions are replaced."""

from dataclasses import replace

import pytest

from hyprsay import inapp
from hyprsay.config import Config, Safety
from hyprsay.inapp import CHORD, SCROLL, Gesture, GestureError
from hyprsay.model import DesktopState, Direction, Layer, Window
from hypruse import input as hinput
from hypruse import server

CFG = Config()


def win(cls="google-chrome", address="0xc1", at=(0, 0), size=(1000, 800)) -> Window:
    return Window(
        address=address,
        cls=cls,
        initial_class=cls,
        title="",
        workspace_id=1,
        workspace_name="1",
        monitor=0,
        at=at,
        size=size,
    )


CHROME = win()
DESKTOP = DesktopState(windows=(CHROME,), active_address=CHROME.address, locked=False)


class Seat:
    """What reached the inherited functions. Nothing is delivered anywhere."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.reply = ""

    @property
    def last(self) -> tuple:
        assert self.calls, "nothing was delivered"
        return self.calls[-1]


@pytest.fixture
def seat(monkeypatch) -> Seat:
    s = Seat()

    def keyboard(action, text="", keys="", window="", then="none", allow_auth=False):
        s.calls.append(("keyboard", action, keys, window, allow_auth))
        return s.reply or f"pressed {keys}"

    def pointer(
        action,
        x=None,
        y=None,
        button="left",
        to_x=None,
        to_y=None,
        scroll_dy=0,
        scroll_dx=0,
        double=False,
        then="none",
        allow_auth=False,
    ):
        s.calls.append(("pointer", action, x, y, scroll_dy, scroll_dx))
        return s.reply or "scroll ok"

    monkeypatch.setattr(server, "keyboard", keyboard)
    monkeypatch.setattr(server, "pointer", pointer)
    return s


# ---------------------------------------------------------------------------- scrolling


def test_a_scroll_down_sends_wheel_notches_into_the_middle_of_the_window(seat):
    inapp.perform(inapp.scroll(Direction.DOWN), CHROME)
    assert seat.last == ("pointer", "scroll", 500.0, 400.0, 4, 0)


def test_scrolling_up_is_the_same_notches_the_other_way(seat):
    inapp.perform(inapp.scroll(Direction.UP), CHROME)
    assert seat.last == ("pointer", "scroll", 500.0, 400.0, -4, 0)


def test_scrolling_sideways_uses_the_horizontal_axis(seat):
    inapp.perform(inapp.scroll(Direction.RIGHT), CHROME)
    assert seat.last[4:] == (0, 4)
    inapp.perform(inapp.scroll(Direction.LEFT), CHROME)
    assert seat.last[4:] == (0, -4)


def test_a_size_word_changes_how_many_notches_go_out():
    assert inapp.scroll(Direction.DOWN, 0).dy == 1
    assert inapp.scroll(Direction.DOWN, 4).dy == 16
    # out of range is clamped, never an index error on a number the grammar mis-said
    assert inapp.scroll(Direction.DOWN, 99).dy == 16


def test_the_wheel_is_aimed_at_the_window_wherever_it_sits(seat):
    elsewhere = win(address="0xd2", at=(1920, 100), size=(800, 600))
    inapp.perform(inapp.scroll(Direction.DOWN, window=elsewhere.address), elsewhere)
    assert seat.last[2:4] == (2320.0, 400.0)


def test_scrolling_a_window_with_no_size_is_refused_rather_than_aimed_at_its_corner(seat):
    nowhere = replace(CHROME, size=(0, 0))
    with pytest.raises(GestureError):
        inapp.perform(inapp.scroll(Direction.DOWN), nowhere)
    assert not seat.calls


def test_a_scroll_is_tier_0_in_every_direction():
    assert all(inapp.scroll(d).tier == 0 for d in Direction)


def test_a_scroll_with_no_direction_is_refused():
    with pytest.raises(GestureError):
        inapp.scroll(None)  # type: ignore[arg-type]


# ------------------------------------------------------------------------------- paging


def test_paging_sends_the_key_the_application_handles_not_the_wheel(seat):
    inapp.perform(inapp.page(Direction.DOWN, CHROME.address), CHROME)
    assert seat.last == ("keyboard", "key", "Page_Down", "0xc1", False)
    inapp.perform(inapp.page(Direction.UP, CHROME.address), CHROME)
    assert seat.last[2] == "Page_Up"


def test_top_and_bottom_are_home_and_end(seat):
    assert inapp.top().keys == "Home"
    assert inapp.bottom().keys == "End"
    inapp.perform(inapp.top(CHROME.address), CHROME)
    assert seat.last[2] == "Home"


def test_paging_sideways_is_refused_because_there_is_no_such_key():
    for direction in (Direction.LEFT, Direction.RIGHT):
        with pytest.raises(GestureError):
            inapp.page(direction)


def test_paging_and_the_ends_of_a_page_are_tier_0():
    assert inapp.page(Direction.DOWN).tier == 0
    assert inapp.top().tier == 0
    assert inapp.bottom().tier == 0


# ------------------------------------------------------------------------------- chords


@pytest.mark.parametrize("keys", ["Page_Down", "Page_Up", "Home", "End"])
def test_scrolling_and_paging_chords_are_tier_0(keys):
    assert inapp.chord_tier(keys) == 0


@pytest.mark.parametrize(
    "keys", ["ctrl+l", "ctrl+f", "ctrl+t", "ctrl+r", "alt+Left", "ctrl+Tab", "ctrl+shift+t"]
)
def test_navigation_chords_are_tier_1(keys):
    assert inapp.chord_tier(keys) == 1


@pytest.mark.parametrize("keys", ["ctrl+w", "ctrl+q", "alt+F4", "Return", "Delete", "ctrl+s"])
def test_anything_that_can_close_submit_or_destroy_is_tier_2(keys):
    assert inapp.chord_tier(keys) == 2


def test_the_safe_set_is_the_tiers_that_need_no_countdown():
    assert {k for k, v in inapp.CHORD_TIERS.items() if v <= 1} == inapp.SAFE_CHORDS
    assert "ctrl+l" in inapp.SAFE_CHORDS
    # the closing and the submitting keys, named one by one so a later edit has to argue
    for dangerous in ("ctrl+w", "ctrl+q", "alt+F4", "Return", "Delete", "BackSpace", "ctrl+s"):
        assert dangerous not in inapp.SAFE_CHORDS


def test_every_chord_in_the_table_is_one_the_inherited_parser_accepts():
    # parse_combo is pure, so this costs nothing and catches a keysym invented by hand:
    # wtype used to surface an unknown one only AFTER it had focused the target window
    for name in inapp.CHORD_TIERS:
        mods, key = hinput.parse_combo(name)
        assert key, f"{name} resolves to no key"


def test_an_unknown_chord_is_rejected_rather_than_passed_through(seat):
    with pytest.raises(GestureError):
        inapp.press("ctrl+alt+shift+z")
    with pytest.raises(GestureError):
        inapp.press("")
    assert not seat.calls


def test_an_unknown_chord_tiers_as_session_level_so_nothing_treats_it_as_cheap():
    assert inapp.chord_tier("ctrl+alt+Delete") == 3
    assert inapp.chord_tier("rm -rf") == 3


def test_a_modifier_may_be_spelled_in_any_case_but_a_one_character_key_may_not():
    assert inapp.press("CTRL+l").keys == "ctrl+l"
    assert inapp.press("Alt+f4").keys == "alt+F4"
    # ctrl+L is shift+ctrl+l in XKB, which is a different keystroke, so it is not accepted
    with pytest.raises(GestureError):
        inapp.press("ctrl+L")


def test_a_chord_goes_out_through_the_inherited_keyboard_call(seat):
    inapp.perform(inapp.press("ctrl+l", CHROME.address), CHROME)
    assert seat.last == ("keyboard", "key", "ctrl+l", "0xc1", False)


# ------------------------------------------------------------------------------ pinning


def test_a_gesture_carries_the_window_it_was_authorized_against():
    assert inapp.press("ctrl+l", "0xc1").window == "0xc1"
    assert inapp.scroll(Direction.DOWN, window="0xc1").window == "0xc1"
    assert inapp.page(Direction.DOWN, "0xc1").window == "0xc1"


def test_a_gesture_authorized_against_another_window_is_refused_not_redirected(seat):
    moved = win(address="0xbeef")
    with pytest.raises(GestureError):
        inapp.perform(inapp.press("ctrl+l", CHROME.address), moved)
    with pytest.raises(GestureError):
        inapp.perform(inapp.scroll(Direction.DOWN, window=CHROME.address), moved)
    assert not seat.calls


def test_the_pinned_address_is_what_the_inherited_call_is_told_to_focus(seat):
    inapp.perform(inapp.press("ctrl+f", CHROME.address), CHROME)
    assert seat.last[3] == CHROME.address


def test_a_gesture_of_an_unknown_kind_performs_nothing(seat):
    with pytest.raises(GestureError):
        inapp.perform(Gesture("click", window=CHROME.address), CHROME)
    assert not seat.calls


def test_a_scroll_of_no_notches_is_not_delivered(seat):
    with pytest.raises(GestureError):
        inapp.perform(Gesture(SCROLL), CHROME)
    assert not seat.calls


# ----------------------------------------------------------------------------- refusals

TERMINAL = win("kitty", address="0xk1")


def test_an_ordinary_known_app_takes_gestures():
    assert inapp.refusal(CHROME, "web browser", DESKTOP, CFG) == ""


@pytest.mark.parametrize("cls", ["kitty", "Alacritty", "com.mitchellh.ghostty"])
def test_a_gesture_into_a_terminal_is_refused_like_typing_into_one(cls):
    assert "terminal" in inapp.refusal(win(cls), "web browser", DESKTOP, CFG)


def test_a_gesture_is_refused_while_a_launcher_holds_the_keyboard():
    state = replace(DESKTOP, layers=(Layer("waybar", "eDP-1", 2), Layer("rofi", "eDP-1", 3)))
    assert "rofi" in inapp.refusal(CHROME, "web browser", state, CFG)


def test_a_configured_locker_namespace_blocks_a_gesture_too():
    cfg = replace(CFG, safety=Safety(locker_namespaces=("quickshell-lock",)))
    state = replace(DESKTOP, layers=(Layer("quickshell-lock", "eDP-1", 3),))
    assert inapp.refusal(CHROME, "web browser", state, cfg)


@pytest.mark.parametrize("cls", ["hyprpolkitagent", "pinentry-qt", "gcr-prompter"])
def test_a_gesture_into_an_authentication_dialog_is_refused(cls):
    assert "authentication" in inapp.refusal(win(cls), "utility", DESKTOP, CFG)


def test_a_gesture_with_no_window_is_refused():
    assert inapp.refusal(None, "web browser", DESKTOP, CFG)


def test_the_terminal_is_refused_again_at_the_moment_of_delivery(seat):
    # the resolver refused it already; this is the line that holds if it did not
    with pytest.raises(GestureError):
        inapp.perform(inapp.press("ctrl+l", TERMINAL.address), TERMINAL)
    with pytest.raises(GestureError):
        inapp.perform(inapp.scroll(Direction.DOWN, window=TERMINAL.address), TERMINAL)
    assert not seat.calls


# ------------------------------------------------------------------------------ the rest


def test_a_dry_run_is_passed_back_word_for_word(seat):
    seat.reply = "DRY RUN, nothing was delivered: would press ctrl+l into 0xc1"
    assert inapp.perform(inapp.press("ctrl+l"), CHROME) == seat.reply
    seat.reply = "DRY RUN, nothing was delivered: would scroll dy=4 dx=0 at (500, 400)"
    assert inapp.perform(inapp.scroll(Direction.DOWN), CHROME) == seat.reply


def test_what_happened_is_one_sentence_naming_the_window(seat):
    assert inapp.perform(inapp.press("ctrl+l"), CHROME) == "Pressed ctrl+l in google-chrome."
    assert inapp.perform(inapp.scroll(Direction.DOWN), CHROME) == "Scrolled google-chrome."


def test_a_layer_over_the_window_is_reported_rather_than_called_a_scroll(seat):
    seat.reply = (
        "scroll ok; cursor now at (500, 400); NOTE: the overlay layer 'rofi' covers this "
        "point, so the input went to it, not to any window beneath"
    )
    said = inapp.perform(inapp.scroll(Direction.DOWN), CHROME)
    assert said == "A layer was over google-chrome, so the scroll went to that."
    assert "rofi" not in said  # the namespace is a string another program chose


def test_a_scroll_is_undone_by_the_same_notches_the_other_way():
    down = inapp.scroll(Direction.DOWN, window="0xc1")
    back = inapp.inverse(down)
    assert back == replace(down, dy=-down.dy)
    assert back.window == "0xc1"


def test_a_page_is_undone_by_the_other_page_key():
    assert inapp.inverse(inapp.page(Direction.DOWN)).keys == "Page_Up"
    assert inapp.inverse(inapp.press("alt+Left")).keys == "alt+Right"


def test_no_undo_is_offered_where_the_undo_would_be_worse_than_the_act():
    # undoing "new tab" means closing one, which is tier 2 and not this module's to offer
    assert inapp.inverse(inapp.press("ctrl+t")) is None
    assert inapp.inverse(inapp.top()) is None


def test_a_gesture_describes_itself_without_quoting_the_window():
    assert inapp.scroll(Direction.DOWN).describe() == "scroll down"
    assert inapp.scroll(Direction.LEFT).describe() == "scroll left"
    assert inapp.press("ctrl+l").describe() == "press ctrl+l"
    assert Gesture(CHORD, keys="Page_Down").describe() == "press Page_Down"
