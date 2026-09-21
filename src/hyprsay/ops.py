"""The operation table: for every `Intent`, how to do it, how to undo it, what it needs.

Nothing here decides WHETHER to act. That was the resolver's job, and the executor's
checks (lock, fresh window, pinned focus) have already passed by the time `perform` runs.
This module only knows HOW, and the rule for how is: go through the inherited hypruse
function wherever one exists, because `hypruse.server` holds the argument validation,
the confinement and auth guards, the dry-run barrier and the journal. Reimplementing a
dispatch next to it would silently drop all of them.

Three verbs have no inherited function (resize, move within the layout, focus by
direction). hypruse-map.md is blunt about calling `hyprctl.dispatch` raw: it skips
`safety.touch`, the journal and every trust guard. So each of those gets a small guarded,
journaled wrapper below instead of a bare dispatch.

Targets are always `address:0x...` (docs/PLAN.md 5.8). That is why resize uses
`resizewindowpixel VECTOR,address:...` rather than `resizeactive`: the legacy
`resizeactive` string can only hit whatever is focused at the instant it lands, and the
human keeps using the mouse while speaking. `movewindow` has no addressed form on
0.56.2 (hypr-ipc.md), so it focuses the named window first through the guarded `hypr`.

Volume, media and the screen lock are not compositor business. They run `wpctl`,
`playerctl` and `loginctl` with an argument vector, never a shell line.

Every inverse is computed from the desktop as it was BEFORE the write, and is itself an
ordinary `Action`, so "undo" goes through the same checks as anything else.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, replace

from hyprsay.model import Action, DesktopState, Direction, Intent, Window
from hypruse import hyprctl, journal, safety, server, trust

# amount 0 tiny .. 4 maximum. None (no size word) means the middle one.
RESIZE_PX: tuple[int, ...] = (20, 40, 80, 160, 320)
VOLUME_PERCENT: tuple[int, ...] = (2, 5, 10, 20, 30)
DEFAULT_AMOUNT = 2
DEFAULT_VOLUME_STEP = 5
SINK = "@DEFAULT_AUDIO_SINK@"

# `launch` blocks until the new window opens. Returning early loses nothing but the
# "window opened" confirmation, and the single worker thread is not held for 8 s by a
# slow application while the next command waits.
LAUNCH_WAIT_S = 2.0
TOOL_TIMEOUT_S = 3.0

# Typing into a terminal is typing into a shell. Refused here regardless of the allow
# list (docs/PLAN.md 5.6); the resolver refuses earlier, this is the last line.
TERMINAL_CLASSES: frozenset[str] = frozenset(
    {
        "kitty",
        "alacritty",
        "foot",
        "footclient",
        "wezterm",
        "ghostty",
        "konsole",
        "gnome-terminal",
        "gnome-terminal-server",
        "ptyxis",
        "kgx",
        "console",
        "xterm",
        "uxterm",
        "urxvt",
        "st",
        "tilix",
        "terminator",
        "contour",
        "rio",
        "warp",
        "cool-retro-term",
        "xfce4-terminal",
        "lxterminal",
        "qterminal",
        "blackbox",
        "tabby",
        "hyper",
    }
)

_ADDRESS = re.compile(r"^0x[0-9a-fA-F]+$")
_LETTER = {Direction.LEFT: "l", Direction.RIGHT: "r", Direction.UP: "u", Direction.DOWN: "d"}
_OPPOSITE = {
    Direction.LEFT: Direction.RIGHT,
    Direction.RIGHT: Direction.LEFT,
    Direction.UP: Direction.DOWN,
    Direction.DOWN: Direction.UP,
}
_SHRINK_VERBS = frozenset({"shrink", "smaller"})
# whitespace-like controls become a space so the words on either side stay apart
_SPACE_LIKE = frozenset("\t\n\v\f\r\x85\u2028\u2029")
_DRY_PREFIX = "DRY RUN"
# an operation that ran but had no effect. The executor drops the inverse for these.
NOTHING_CHANGED = "Nothing changed"


class OpError(Exception):
    """The operation cannot or did not happen. The text is a plain sentence for the HUD."""


@dataclass(frozen=True)
class Operation:
    intent: Intent
    # returns one plain sentence saying what happened; raises to say what did not
    perform: Callable[[Action, DesktopState], str]
    # the Action that undoes it, from the state BEFORE the write; None when there is none
    inverse: Callable[[Action, DesktopState], Action | None]
    needs_window: bool = False
    # "again" never repeats an operation that earned a countdown or a key press
    repeatable: bool = True


# ------------------------------------------------------------------------ helpers


def sanitize_text(text: str, max_chars: int | None = None) -> str:
    """Dictated text with every control character gone, not just Enter.

    All of C0 and C1 (Unicode category Cc) and the line and paragraph separators are
    removed, so nothing typed can submit a form, run a command line or move the caret.
    """
    kept = []
    for ch in text:
        if ch in _SPACE_LIKE:
            kept.append(" ")
        elif unicodedata.category(ch) not in ("Cc", "Zl", "Zp"):
            kept.append(ch)
    clean = " ".join("".join(kept).split())
    return clean if max_chars is None else clean[: max(0, max_chars)].rstrip()


def is_terminal(cls: str) -> bool:
    lowered = cls.lower()
    # reverse-DNS app ids: org.wezfurlong.wezterm, com.mitchellh.ghostty, org.kde.konsole
    return lowered in TERMINAL_CLASSES or lowered.rsplit(".", 1)[-1] in TERMINAL_CLASSES


def workspace_selector(spoken: str) -> str:
    """The Hyprland selector for a spoken workspace. `e+1` walks existing workspaces
    only, so "next" never lands on an empty one past the end (hypr-ipc.md section 7)."""
    return {"next": "e+1", "previous": "e-1"}.get(spoken, spoken)


def workspace_of(window: Window) -> str | None:
    """A selector that names where `window` is, or None when it cannot be named."""
    if window.workspace_id > 0:
        return str(window.workspace_id)
    return window.workspace_name or None


def _would(plan: str) -> str:
    # the same opening the inherited tools use, so one check recognizes every dry run
    return f"{_DRY_PREFIX}, nothing was delivered: would {plan}"


def _said(result: object) -> str:
    return result if isinstance(result, str) else ""


def _dry(result: object) -> str | None:
    text = _said(result)
    return text if text.startswith(_DRY_PREFIX) else None


def _require_window(action: Action) -> Window:
    if action.window is None:
        raise OpError("There is no window to act on.")
    return action.window


def _address(window: Window) -> str:
    if not _ADDRESS.match(window.address):
        raise OpError("That window has no usable address.")
    return f"address:{window.address}"


def _refuse_if_confined(what: str) -> None:
    # the same reasoning as trust.guard_use_bind: no scope can contain "whatever is there"
    if os.environ.get("HYPRUSE_CONFINE"):
        raise trust.TrustError(f"{what} cannot be confined to a window, so it is refused")


def _run_tool(argv: list[str], missing: str) -> str:
    """Run a small desktop tool with an argument vector. Returns its stdout."""
    if shutil.which(argv[0]) is None:
        raise OpError(missing)
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=TOOL_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OpError(f"{argv[0]} could not be run.") from exc
    if proc.returncode != 0:
        raise OpError(f"{argv[0]} refused the request.")
    return proc.stdout


# ------------------------------------------------ guarded wrappers for raw dispatchers


@journal.journaled("act")
def resize_window(address: str, dx: int, dy: int) -> str:
    safety.touch("resize_window")
    trust.guard_window(address)
    if journal.dry_run():
        return _would(f"resize {address} by {dx} {dy}")
    hyprctl.dispatch("resizewindowpixel", f"{dx} {dy},address:{address}")
    return f"resized {address} by {dx} {dy}"


@journal.journaled("act")
def move_in_layout(address: str, letter: str) -> str:
    safety.touch("move_in_layout")
    trust.guard_window(address)
    if journal.dry_run():
        return _would(f"move {address} {letter}")
    hyprctl.dispatch("movewindow", letter)
    return f"moved {address} {letter}"


@journal.journaled("act")
def focus_direction(letter: str) -> str:
    safety.touch("focus_direction")
    _refuse_if_confined("moving focus by direction")
    if journal.dry_run():
        return _would(f"move focus {letter}")
    hyprctl.dispatch("movefocus", letter)
    return f"focus moved {letter}"


# --------------------------------------------------------------------------- perform


def _focus_window(action: Action, _before: DesktopState) -> str:
    window = _require_window(action)
    _address(window)
    result = server.hypr("focus_window", target=window.address)
    return _dry(result) or f"Focused {window.cls}."


def _close_window(action: Action, _before: DesktopState) -> str:
    window = _require_window(action)
    _address(window)
    result = _said(server.hypr("close_window", target=window.address))
    if _dry(result):
        return result
    # closewindow only ASKS, and an editor with unsaved changes answers with a dialog
    if "still open" in result:
        raise OpError(f"Asked {window.cls} to close, but it is still open. It may want to save.")
    verb = "Closed" if result.startswith("closed") else "Asked to close"
    return f"{verb} {window.cls}."


def _move_to_workspace(action: Action, _before: DesktopState) -> str:
    window = _require_window(action)
    _address(window)
    if not action.workspace:
        raise OpError("No workspace was named.")
    result = server.hypr(
        "move_window", target=window.address, workspace=workspace_selector(action.workspace)
    )
    return _dry(result) or f"Moved {window.cls} to workspace {action.workspace}."


def _switch_workspace(action: Action, _before: DesktopState) -> str:
    if not action.workspace:
        raise OpError("No workspace was named.")
    result = server.hypr("workspace", workspace=workspace_selector(action.workspace))
    return _dry(result) or f"Workspace {action.workspace}."


def _fullscreen(action: Action, _before: DesktopState) -> str:
    window = _require_window(action)
    _address(window)
    result = server.hypr("fullscreen", target=window.address)
    return _dry(result) or f"Fullscreen toggled on {window.cls}."


def _toggle_floating(action: Action, _before: DesktopState) -> str:
    window = _require_window(action)
    _address(window)
    result = server.hypr("toggle_floating", target=window.address)
    return _dry(result) or f"Floating toggled on {window.cls}."


def _launch_app(action: Action, _before: DesktopState) -> str:
    app = action.app
    if app is None:
        raise OpError("No application was named.")
    if not app.trusted:
        raise OpError(f"{app.name} comes from a desktop file that is not approved yet.")
    if not app.exec_argv:
        raise OpError(f"{app.name} has no command to run.")
    # the argument vector from the .desktop file, quoted back into the one line Hyprland's
    # exec takes. No word of it comes from speech.
    command = " ".join(shlex.quote(arg) for arg in app.exec_argv)
    workspace = workspace_selector(action.workspace) if action.workspace else ""
    result = server.launch(command, workspace=workspace, wait_s=LAUNCH_WAIT_S)
    if _dry(result):
        return _said(result)
    if isinstance(result, dict):
        return f"Opened {app.name}."
    return f"Started {app.name}. Its window has not appeared yet."


def resize_delta(action: Action) -> tuple[int, int]:
    """The pixel vector for a resize, in the signs Hyprland's resize dispatchers use.

    The grammar's convention: `verb` is "grow" or "shrink" and `direction` is the axis
    (RIGHT is width, DOWN is height, None is both). So "narrower" arrives as shrink
    RIGHT and must come out as a negative x. LEFT and UP are the same axes mirrored, so
    a bare direction with no verb still moves "along the direction".
    """
    amount = DEFAULT_AMOUNT if action.amount is None else action.amount
    px = RESIZE_PX[min(max(amount, 0), len(RESIZE_PX) - 1)]
    sign = -1 if (action.verb or "") in _SHRINK_VERBS else 1
    x, y = {
        None: (1, 1),
        Direction.RIGHT: (1, 0),
        Direction.LEFT: (-1, 0),
        Direction.DOWN: (0, 1),
        Direction.UP: (0, -1),
    }[action.direction]
    return sign * px * x, sign * px * y


def _resize_window(action: Action, _before: DesktopState) -> str:
    window = _require_window(action)
    _address(window)
    dx, dy = resize_delta(action)
    return _dry(resize_window(window.address, dx, dy)) or f"Resized {window.cls}."


def _move_window(action: Action, before: DesktopState) -> str:
    window = _require_window(action)
    _address(window)
    if action.direction is None:
        raise OpError("No direction was named.")
    # `movewindow l` only ever moves the focused window
    if before.active_address != window.address:
        result = server.hypr("focus_window", target=window.address)
        if _dry(result):
            return _said(result)
    result = move_in_layout(window.address, _LETTER[action.direction])
    return _dry(result) or f"Moved {window.cls} {action.direction.value}."


def _focus_direction(action: Action, _before: DesktopState) -> str:
    if action.direction is None:
        raise OpError("No direction was named.")
    result = focus_direction(_LETTER[action.direction])
    return _dry(result) or f"Focus moved {action.direction.value}."


def volume_step(action: Action) -> int:
    if action.number is not None:
        return min(max(action.number, 1), 100)
    if action.amount is not None:
        return VOLUME_PERCENT[min(max(action.amount, 0), len(VOLUME_PERCENT) - 1)]
    return DEFAULT_VOLUME_STEP


def volume_argv(action: Action) -> list[str]:
    verb = action.verb or ""
    if verb in ("up", "down"):
        sign = "+" if verb == "up" else "-"
        # -l 1.0: "louder" said five times must not drive the sink past 100 percent
        return ["wpctl", "set-volume", "-l", "1.0", SINK, f"{volume_step(action)}%{sign}"]
    if verb == "set":
        if action.number is None:
            raise OpError("No volume level was named.")
        return ["wpctl", "set-volume", "-l", "1.0", SINK, f"{min(max(action.number, 0), 100)}%"]
    if verb in ("mute", "unmute", "toggle"):
        return ["wpctl", "set-mute", SINK, {"mute": "1", "unmute": "0", "toggle": "toggle"}[verb]]
    raise OpError("That is not something the volume can do.")


def _volume(action: Action, _before: DesktopState) -> str:
    argv = volume_argv(action)
    if journal.dry_run():
        return _would(f"run {' '.join(argv)}")
    safety.touch("volume")
    was = current_volume_percent()
    _run_tool(argv, "wpctl is not installed, so the volume cannot be changed.")
    if action.verb not in ("up", "down", "set"):
        return f"Volume {action.verb}."
    now = current_volume_percent()
    if was is not None and now == was:
        # say so: "done" for a change that did not happen is a lie, and an undo of it
        # would move the volume the other way (seen live: up at 100 percent, then down)
        return f"{NOTHING_CHANGED}: the volume is already at {now} percent."
    return f"Volume {now} percent." if now is not None else f"Volume {action.verb}."


_MEDIA = {"play_pause": "play-pause", "next": "next", "previous": "previous"}


def _media(action: Action, _before: DesktopState) -> str:
    command = _MEDIA.get(action.verb or "")
    if command is None:
        raise OpError("That is not something the player can do.")
    argv = ["playerctl", command]
    if journal.dry_run():
        return _would(f"run {' '.join(argv)}")
    safety.touch("media")
    _run_tool(argv, "playerctl is not installed, so media control is unavailable.")
    return f"Media {command}."


def _type_text(action: Action, _before: DesktopState) -> str:
    window = _require_window(action)
    _address(window)
    text = sanitize_text(action.text or "")
    if not text:
        raise OpError("There is nothing to type.")
    # `window=` makes the inherited lock and keyboard-grab guards REFUSE instead of
    # noting: text aimed at a window must not end up in a launcher or a lock prompt.
    # allow_auth stays at its default, and no key is ever pressed, so never Enter.
    result = server.keyboard("type", text=text, window=window.address)
    return _dry(result) or f"Typed {len(text)} characters into {window.cls}."


def _lock_screen(_action: Action, _before: DesktopState) -> str:
    # "auto" resolves the graphical session even for a daemon that belongs to none
    argv = ["loginctl", "lock-session", os.environ.get("XDG_SESSION_ID") or "auto"]
    if journal.dry_run():
        return _would(f"run {' '.join(argv)}")
    safety.touch("lock_screen")
    _run_tool(argv, "loginctl is not installed, so the screen cannot be locked.")
    return "Locked."


# --------------------------------------------------------------------------- inverses


def _none(_action: Action, _before: DesktopState) -> Action | None:
    return None


def _same(action: Action, _before: DesktopState) -> Action | None:
    return action


def _refocus_previous(_action: Action, before: DesktopState) -> Action | None:
    previous = before.focused
    return Action(Intent.FOCUS_WINDOW, window=previous) if previous else None


def _previous_workspace(action: Action, before: DesktopState) -> Action | None:
    # a special workspace toggles, so "go back" has no selector that is safe to guess
    if (action.workspace or "").startswith("special") or before.active_workspace_id <= 0:
        return None
    return Action(Intent.SWITCH_WORKSPACE, workspace=str(before.active_workspace_id))


def _move_back(action: Action, before: DesktopState) -> Action | None:
    window = before.by_address(action.window.address) if action.window else None
    origin = workspace_of(window) if window else None
    return Action(Intent.MOVE_TO_WORKSPACE, window=window, workspace=origin) if origin else None


def _opposite_resize(action: Action, _before: DesktopState) -> Action | None:
    # the verb carries the sign on every axis, so flipping it negates the whole vector
    shrinking = (action.verb or "") in _SHRINK_VERBS
    return replace(action, verb="grow" if shrinking else "shrink")


def _opposite_move(action: Action, _before: DesktopState) -> Action | None:
    # best effort: a dwindle tree does not promise that right undoes left
    return replace(action, direction=_OPPOSITE[action.direction]) if action.direction else None


def current_volume_percent() -> int | None:
    """Read-only. `wpctl get-volume` prints "Volume: 0.45" or "Volume: 0.45 [MUTED]"."""
    try:
        out = _run_tool(["wpctl", "get-volume", SINK], "wpctl is not installed.")
        return round(float(out.split()[1]) * 100)
    except (OpError, IndexError, ValueError):
        return None


def _opposite_volume(action: Action, _before: DesktopState) -> Action | None:
    verb = action.verb or ""
    flipped = {"up": "down", "down": "up", "mute": "unmute", "unmute": "mute", "toggle": "toggle"}
    if verb in ("up", "down", "set"):
        # restore the level that was there, not "step the other way": at the cap a step
        # up changes nothing, and stepping down to undo it would lower the volume. The
        # executor asks for the inverse BEFORE performing, so this reads the old level.
        was = current_volume_percent()
        if was is not None:
            return replace(action, verb="set", number=was)
    if verb in flipped:
        return replace(action, verb=flipped[verb])
    return None


def _opposite_media(action: Action, _before: DesktopState) -> Action | None:
    # "previous" usually restarts the track, so "next" would not undo it
    return {"play_pause": action, "next": replace(action, verb="previous")}.get(action.verb or "")


# ----------------------------------------------------------------------------- table

OPERATIONS: dict[Intent, Operation] = {
    op.intent: op
    for op in (
        Operation(Intent.FOCUS_WINDOW, _focus_window, _refocus_previous, needs_window=True),
        Operation(Intent.CLOSE_WINDOW, _close_window, _none, needs_window=True, repeatable=False),
        Operation(Intent.MOVE_TO_WORKSPACE, _move_to_workspace, _move_back, needs_window=True),
        Operation(Intent.SWITCH_WORKSPACE, _switch_workspace, _previous_workspace),
        Operation(Intent.FULLSCREEN, _fullscreen, _same, needs_window=True),
        Operation(Intent.TOGGLE_FLOATING, _toggle_floating, _same, needs_window=True),
        Operation(Intent.LAUNCH_APP, _launch_app, _none),
        Operation(Intent.RESIZE_WINDOW, _resize_window, _opposite_resize, needs_window=True),
        Operation(Intent.MOVE_WINDOW, _move_window, _opposite_move, needs_window=True),
        Operation(Intent.FOCUS_DIRECTION, _focus_direction, _refocus_previous),
        Operation(Intent.VOLUME, _volume, _opposite_volume),
        Operation(Intent.MEDIA, _media, _opposite_media),
        Operation(Intent.TYPE_TEXT, _type_text, _none, needs_window=True, repeatable=False),
        Operation(Intent.LOCK_SCREEN, _lock_screen, _none, repeatable=False),
    )
}
