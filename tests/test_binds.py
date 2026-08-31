import pytest

from hypruse import hyprctl


@pytest.mark.parametrize(
    "mask,names",
    [
        (0, []),
        (64, ["SUPER"]),
        (5, ["CTRL", "SHIFT"]),
        (65, ["SUPER", "SHIFT"]),
        (72, ["SUPER", "ALT"]),
    ],
)
def test_modmask_decode(mask, names):
    assert hyprctl.modmask_to_names(mask) == names


RAW = [
    {
        "modmask": 64,
        "key": "Q",
        "dispatcher": "exec",
        "arg": "kitty",
        "has_description": True,
        "description": "open terminal",
        "submap": "",
        "mouse": False,
        "keycode": 0,
    },
    {
        "modmask": 64,
        "key": "mouse:272",
        "dispatcher": "movewindow",
        "arg": "",
        "submap": "",
        "mouse": True,
        "keycode": 0,
    },
    {
        "modmask": 65,
        "key": "",
        "dispatcher": "movetoworkspace",
        "arg": "2",
        "submap": "",
        "mouse": False,
        "keycode": 10,
    },
    {
        "modmask": 0,
        "key": "escape",
        "dispatcher": "submap",
        "arg": "reset",
        "submap": "resize",
        "mouse": False,
        "keycode": 0,
    },
]


def test_parse_binds_shapes():
    parsed = hyprctl.parse_binds(RAW)
    combos = [b["combo"] for b in parsed]
    assert combos == ["SUPER+Q", "SUPER+SHIFT+code:10", "escape"]
    assert parsed[0]["description"] == "open terminal"
    assert parsed[0]["action"] == "exec" and parsed[0]["arg"] == "kitty"
    assert "description" not in parsed[1]
    assert parsed[2]["submap"] == "resize"


def test_parse_binds_drops_mouse_binds():
    parsed = hyprctl.parse_binds(RAW)
    assert not any("mouse" in b["combo"] for b in parsed)


# A Lua config binds closures, not named dispatchers: Hyprland reports every
# one as `__lua` with a registry index, and never sets the `mouse` flag.
LUA_RAW = [
    {
        "modmask": 64,
        "key": "Q",
        "dispatcher": "__lua",
        "arg": "37",
        "has_description": True,
        "description": "open terminal",
        "submap": "",
        "mouse": False,
        "keycode": 0,
    },
    {
        "modmask": 64,
        "key": "mouse:272",
        "dispatcher": "__lua",
        "arg": "41",
        "submap": "",
        "mouse": False,  # the Lua manager never sets this
        "keycode": 0,
    },
    {
        "modmask": 0,
        "key": "switch:on:Lid",
        "dispatcher": "__lua",
        "arg": "42",
        "submap": "",
        "mouse": False,
        "keycode": 0,
    },
    {
        "modmask": 64,
        "key": "mouse_down",  # scroll, not a button: still a real bind
        "dispatcher": "__lua",
        "arg": "43",
        "submap": "",
        "mouse": False,
        "keycode": 0,
    },
]


def test_parse_binds_marks_lua_closures_and_drops_their_registry_index():
    parsed = hyprctl.parse_binds(LUA_RAW)
    assert parsed == [
        {"combo": "SUPER+Q", "action": "lua", "description": "open terminal"},
        {"combo": "SUPER+mouse_down", "action": "lua"},
    ]
    # the index is a pointer into the compositor's own Lua state: it means
    # nothing here, and offering it would invite an agent to send it back
    assert "arg" not in parsed[0]


def test_parse_binds_drops_pointer_and_switch_binds_a_lua_config_did_not_flag():
    combos = [b["combo"] for b in hyprctl.parse_binds(LUA_RAW)]
    assert "SUPER+mouse:272" not in combos and "switch:on:Lid" not in combos


def test_parse_binds_keeps_scroll_binds():
    # mouse_up/mouse_down are scroll, not buttons: on a hyprlang config they
    # carry a real dispatcher and use_bind runs them, so dropping them by a
    # bare "mouse" prefix would take a working bind away
    scroll = [
        {"modmask": 64, "key": "mouse_down", "dispatcher": "workspace", "arg": "e+1",
         "submap": "", "mouse": False, "keycode": 0}
    ]
    assert hyprctl.parse_binds(scroll) == [
        {"combo": "SUPER+mouse_down", "action": "workspace", "arg": "e+1"}
    ]
