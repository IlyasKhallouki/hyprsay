"""The tier function and the verdict table. Pure functions, no fakes needed."""

from dataclasses import replace

import pytest

from hyprsay.config import Config, Safety
from hyprsay.model import (
    Action,
    DesktopState,
    Intent,
    Layer,
    Monitor,
    Verdict,
    Window,
    Workspace,
)
from hyprsay.nlu import tiers
from hyprsay.nlu.tiers import Evidence

CFG = Config()


def win(cls="firefox", address="0x1", monitor=0, **kw) -> Window:
    return Window(
        address=address,
        cls=cls,
        initial_class=cls,
        title=kw.pop("title", ""),
        workspace_id=kw.pop("workspace_id", 1),
        workspace_name=kw.pop("workspace_name", "1"),
        monitor=monitor,
        **kw,
    )


def two_monitors(window: Window) -> DesktopState:
    return DesktopState(
        windows=(window,),
        workspaces=(
            Workspace(1, "1", monitor="eDP-1"),
            Workspace(2, "2", monitor="eDP-1"),
            Workspace(5, "5", monitor="HDMI-A-1"),
            Workspace(-98, "special:scratch", monitor="eDP-1"),
        ),
        monitors=(Monitor(0, "eDP-1", focused=True), Monitor(1, "HDMI-A-1")),
        active_address=window.address,
        active_workspace_id=1,
        locked=False,
    )


# --------------------------------------------------------------------------- tiers


@pytest.mark.parametrize(
    "intent",
    [Intent.FOCUS_WINDOW, Intent.SWITCH_WORKSPACE, Intent.FOCUS_DIRECTION, Intent.HELP],
)
def test_focus_workspace_switch_direction_and_help_are_tier_0(intent):
    assert tiers.tier(intent, Action(intent), DesktopState(locked=False), CFG) == 0


@pytest.mark.parametrize(
    "intent",
    [
        Intent.LAUNCH_APP,
        Intent.MOVE_WINDOW,
        Intent.RESIZE_WINDOW,
        Intent.TOGGLE_FLOATING,
        Intent.FULLSCREEN,
        Intent.VOLUME,
        Intent.MEDIA,
    ],
)
def test_reversible_operations_are_tier_1(intent):
    assert tiers.tier(intent, Action(intent), DesktopState(locked=False), CFG) == 1


def test_closing_a_window_is_tier_2():
    assert tiers.tier(Intent.CLOSE_WINDOW, Action(Intent.CLOSE_WINDOW), DesktopState(), CFG) == 2


def test_locking_the_screen_is_tier_3():
    assert tiers.tier(Intent.LOCK_SCREEN, Action(Intent.LOCK_SCREEN), DesktopState(), CFG) == 3


def test_moving_to_a_workspace_on_the_same_monitor_is_tier_1():
    window = win()
    action = Action(Intent.MOVE_TO_WORKSPACE, window=window, workspace="2")
    assert tiers.tier(action.intent, action, two_monitors(window), CFG) == 1


def test_the_same_move_aimed_at_another_monitor_is_tier_2():
    window = win()
    action = Action(Intent.MOVE_TO_WORKSPACE, window=window, workspace="5")
    assert tiers.tier(action.intent, action, two_monitors(window), CFG) == 2


def test_the_same_move_aimed_at_a_special_workspace_is_tier_2():
    window = win()
    action = Action(Intent.MOVE_TO_WORKSPACE, window=window, workspace="special:scratch")
    assert tiers.tier(action.intent, action, two_monitors(window), CFG) == 2


def test_next_and_previous_are_resolved_against_the_active_workspace():
    window = win()
    state = two_monitors(window)
    onward = Action(Intent.MOVE_TO_WORKSPACE, window=window, workspace="next")
    assert tiers.tier(onward.intent, onward, state, CFG) == 1
    # there is no workspace 0: "previous" from workspace 1 goes nowhere safe
    back = Action(Intent.MOVE_TO_WORKSPACE, window=window, workspace="previous")
    assert tiers.tier(back.intent, back, state, CFG) == 2


def test_a_new_workspace_opens_on_the_focused_monitor_so_a_window_elsewhere_is_tier_2():
    elsewhere = win(monitor=1)
    action = Action(Intent.MOVE_TO_WORKSPACE, window=elsewhere, workspace="9")
    assert tiers.tier(action.intent, action, two_monitors(elsewhere), CFG) == 2
    here = win(monitor=0)
    action = Action(Intent.MOVE_TO_WORKSPACE, window=here, workspace="9")
    assert tiers.tier(action.intent, action, two_monitors(here), CFG) == 1


def test_a_move_with_no_destination_is_treated_as_disruptive():
    action = Action(Intent.MOVE_TO_WORKSPACE, window=win())
    assert tiers.tier(action.intent, action, two_monitors(win()), CFG) == 2


def test_typing_is_tier_2_until_the_app_is_allowlisted():
    action = Action(Intent.TYPE_TEXT, window=win("org.gnome.TextEditor"), text="hi")
    assert tiers.tier(action.intent, action, DesktopState(), CFG) == 2
    allowed = replace(CFG, safety=Safety(type_allow_classes=("org.gnome.texteditor",)))
    assert tiers.tier(action.intent, action, DesktopState(), allowed) == 1


# --------------------------------------------------------------------------- verdicts

SURE = Evidence(source="jev", corroborated=True, agree=True, wide_margin=True)


def test_tier_0_acts_and_offers_a_swap_when_there_is_more_than_one_plausible_candidate():
    assert tiers.verdict(0, replace(SURE, plausible=3))[0] is Verdict.ACT_SWAP
    assert tiers.verdict(0, replace(SURE, plausible=1))[0] is Verdict.ACT


def test_tier_0_acts_on_a_thin_margin_because_focus_is_free_to_reverse():
    thin = replace(SURE, wide_margin=False, plausible=2)
    assert tiers.verdict(0, thin)[0] is Verdict.ACT_SWAP


def test_tier_1_needs_a_wide_margin_or_it_blocks_on_hints():
    assert tiers.verdict(1, SURE)[0] is Verdict.ACT
    assert tiers.verdict(1, replace(SURE, wide_margin=False))[0] is Verdict.HINTS


@pytest.mark.parametrize("level", [0, 1])
def test_an_uncorroborated_pick_becomes_hints(level):
    assert tiers.verdict(level, replace(SURE, corroborated=False))[0] is Verdict.HINTS


@pytest.mark.parametrize("level", [0, 1])
def test_disagreement_between_formulations_caps_at_hints(level):
    assert tiers.verdict(level, replace(SURE, agree=False))[0] is Verdict.HINTS


def test_tier_2_without_the_verb_said_is_refused_whatever_jev_thinks():
    evidence = replace(SURE, explicit_target=True, verb_said=False)
    verdict, reason = tiers.verdict(2, evidence)
    assert verdict is Verdict.REFUSE
    assert "verb" in reason


def test_tier_2_with_the_verb_and_an_explicit_target_is_a_countdown():
    evidence = replace(SURE, explicit_target=True, verb_said=True)
    assert tiers.verdict(2, evidence)[0] is Verdict.COUNTDOWN


def test_tier_2_without_an_explicit_target_is_hints():
    evidence = replace(SURE, explicit_target=False, verb_said=True)
    assert tiers.verdict(2, evidence)[0] is Verdict.HINTS


def test_tier_2_never_comes_from_a_late_answer():
    evidence = replace(SURE, explicit_target=True, verb_said=True, late=True)
    assert tiers.verdict(2, evidence)[0] is Verdict.HINTS


def test_a_title_never_authorizes_a_tier_2_target():
    evidence = replace(SURE, explicit_target=True, verb_said=True, by_title=True)
    assert tiers.verdict(2, evidence)[0] is Verdict.HINTS


def test_tier_3_is_a_physical_key_and_only_from_the_grammar():
    assert tiers.verdict(3, Evidence(source="grammar"))[0] is Verdict.CONFIRM_KEY
    assert tiers.verdict(3, SURE)[0] is Verdict.REFUSE
    assert tiers.verdict(3, replace(SURE, source="pick"))[0] is Verdict.REFUSE


def test_the_destructive_verb_lexicon_is_closed():
    assert sorted(tiers.DESTRUCTIVE_VERBS) == ["close", "exit", "kill", "quit"]
    assert tiers.verb_said(Intent.CLOSE_WINDOW, ("please", "kill", "it"))
    assert not tiers.verb_said(Intent.CLOSE_WINDOW, ("get", "rid", "of", "it"))
    # a substring is not the word
    assert not tiers.verb_said(Intent.CLOSE_WINDOW, ("closet",))


# --------------------------------------------------------------------------- typing

EDITOR = win("org.gnome.TextEditor")
DESKTOP = DesktopState(windows=(EDITOR,), active_address=EDITOR.address, locked=False)


def test_typing_into_a_known_ordinary_app_is_allowed():
    assert tiers.typing_refusal(EDITOR, "text editor", DESKTOP, CFG) == ""


def test_typing_with_no_window_is_refused():
    assert "no window" in tiers.typing_refusal(None, "", DESKTOP, CFG)


@pytest.mark.parametrize(
    "cls", ["kitty", "Alacritty", "foot", "org.wezfurlong.wezterm", "com.mitchellh.ghostty"]
)
def test_typing_into_a_terminal_class_is_refused(cls):
    assert "terminal" in tiers.typing_refusal(win(cls), "text editor", DESKTOP, CFG)


def test_typing_into_a_terminal_kind_is_refused_whatever_its_class():
    assert "terminal" in tiers.typing_refusal(win("my-term"), "terminal emulator", DESKTOP, CFG)


@pytest.mark.parametrize("namespace", ["rofi", "walker", "wofi", "fuzzel", "anyrun", "hyprlock"])
def test_typing_is_refused_while_a_launcher_or_lock_layer_is_open(namespace):
    state = replace(DESKTOP, layers=(Layer("waybar", "eDP-1", 2), Layer(namespace, "eDP-1", 3)))
    assert namespace in tiers.typing_refusal(EDITOR, "text editor", state, CFG)


def test_a_bar_or_a_notification_layer_does_not_block_typing():
    state = replace(DESKTOP, layers=(Layer("waybar", "eDP-1", 2), Layer("mako", "eDP-1", 3)))
    assert tiers.typing_refusal(EDITOR, "text editor", state, CFG) == ""


def test_a_configured_locker_namespace_blocks_typing():
    cfg = replace(CFG, safety=Safety(locker_namespaces=("quickshell-lock",)))
    state = replace(DESKTOP, layers=(Layer("quickshell-lock", "eDP-1", 3),))
    assert tiers.typing_refusal(EDITOR, "text editor", state, cfg)


@pytest.mark.parametrize(
    "cls",
    ["hyprpolkitagent", "polkit-gnome-authentication-agent-1", "pinentry-qt", "gcr-prompter"],
)
def test_typing_into_an_auth_agent_is_refused(cls):
    assert "authentication" in tiers.typing_refusal(win(cls), "utility", DESKTOP, CFG)


@pytest.mark.parametrize("kind", ["", "unknown"])
def test_typing_into_an_app_of_unknown_kind_is_refused(kind):
    assert "kind" in tiers.typing_refusal(win("mystery"), kind, DESKTOP, CFG)


def test_every_control_character_is_stripped_not_only_enter():
    dirty = "ls\n rm\r -rf\x1b[2J \x03 ~\t/\x7f \x85 \u2028 \u2029 done"
    clean = tiers.clean_typed_text(dirty, 200)
    assert clean == "ls rm -rf [2J ~ / done"
    assert all(ord(c) >= 32 and not 0x7F <= ord(c) <= 0x9F for c in clean)


def test_invisible_format_characters_are_stripped_from_typed_text():
    # right-to-left override and zero width space: what is typed must be what is seen
    assert tiers.clean_typed_text("pay\u202e100\u200b now", 200) == "pay100 now"


def test_typed_text_is_capped():
    assert len(tiers.clean_typed_text("a" * 500, 200)) == 200
