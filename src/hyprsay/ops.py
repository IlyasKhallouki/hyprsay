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

`Intent.SERVICE` is the one operation that does not write the desktop at all: it hands a
capability back to the adapter that offered it (`adapters/base.py`). It is in this table
rather than beside it so that an adapter write goes through exactly the checks every
other write goes through, in the same order, from the same function.

Every inverse is computed from the desktop as it was BEFORE the write, and is itself an
ordinary `Action`, so "undo" goes through the same checks as anything else.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from hyprsay import controls, inapp, recipes
from hyprsay.adapters import base
from hyprsay.config import Config
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
def perform_service(adapter: str, capability: str, handle: str, state: DesktopState) -> str:
    """Hand one capability to the adapter that minted its handle, and say what happened.

    Journaled here and not left to `daemon._act`, for the reason the three wrappers above
    this one are journaled: an adapter writes down its own socket or its own bus, so
    nothing in `hypruse.server` sees the call and nothing else would record that it
    happened. `hyprsay say --act` has no daemon behind it at all, and that is exactly the
    surface an adapter is easiest to test from.

    The adapter is looked up by NAME rather than handed in, so the journal entry is three
    plain strings instead of an object repr, and so this function can be replayed.
    """
    found = base.by_name(_adapters(), adapter)
    if found is None:
        raise OpError(f"{adapter} is not running in this process, so nothing was done.")
    context = base.Context(state=state, heard="", now=time.monotonic())
    try:
        outcome = found.perform(capability, handle, context)
    except base.AdapterError as exc:
        # the adapter's own sentence, word for word: it names the repair, and it may
        # quote text an application wrote, which `Affordance` has already sanitized
        raise OpError(str(exc)) from exc
    if not outcome.ok:
        raise OpError(outcome.message or f"{adapter} did not do that.")
    return outcome.message or f"{capability.replace('_', ' ')} done."


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


# ------------------------------------------------------------------- reaching inside


def _engine() -> tuple[Config, object | None]:
    """The running engine's configuration and lexicon, or fail-closed stand-ins.

    Read the way `recipes._through_executor` reads it rather than threaded down through
    five layers, because the operation table is a mapping of plain functions. With no
    engine there is no lexicon, every window's kind reads as unknown, and the refusal
    below therefore refuses: the strict direction.
    """
    from hyprsay.executor import current

    engine = current()
    if engine is None:
        return Config(), None
    return engine.cfg, engine.lexicon


def _adapters() -> tuple[Any, ...]:
    """The adapters the running engine holds, read the same way `_engine` reads a lexicon.

    The SAME objects the understander harvested from, which is the whole reason they are
    built once in `daemon.build` and handed to both: a handle is only ever honoured by
    the `adapters.base.Minted` that issued it, so a second set of adapters would refuse
    every option the first set offered. With no engine there are none, and `_service`
    then refuses rather than finding some other way to a bus.
    """
    from hyprsay.executor import current

    engine = current()
    return () if engine is None else tuple(engine.adapters)


def _authorize_gesture(
    window: Window, before: DesktopState, gesture: str = inapp.CHORD, keys: str = ""
) -> str:
    """Refuse a gesture the fresh desktop forbids, and return the window's kind.

    The understander already ran `inapp.refusal` against the window it could see. It
    runs again here because this is the first moment a chained clause knows its target
    at all, and because the inherited pointer call only NOTES a launcher covering the
    point it aims at: this is the only thing that stops a scroll reaching rofi.
    """
    cfg, lexicon = _engine()
    kind = lexicon.kind_of(window) if lexicon is not None else ""
    refusal = inapp.refusal(window, kind, before, cfg, gesture, keys)
    if refusal:
        # word for word, not recapitalized: the sentence may quote a layer namespace,
        # which is a string another program chose
        raise OpError(refusal)
    return kind


def _scroll(action: Action, before: DesktopState) -> str:
    window = _require_window(action)
    _address(window)
    _authorize_gesture(window, before, inapp.SCROLL)
    if action.direction is None:
        raise OpError("No direction was named to scroll in.")
    return inapp.perform(inapp.scroll(action.direction, action.amount, window.address), window)


def _press_chord(action: Action, before: DesktopState) -> str:
    window = _require_window(action)
    _address(window)
    chord = _CHORD_SPELLING.get(action.verb or "", action.verb or "")
    # the chord name matters here: a tier 0 chord moves a view and can cause nothing, so
    # it is allowed where a scroll is, and refusing it at this last step after the
    # understander allowed it would be a refusal the user could never predict
    _authorize_gesture(window, before, inapp.CHORD, chord)
    return inapp.perform(inapp.press(chord, window.address), window)


def _run_recipe(action: Action, before: DesktopState) -> str:
    """One named key sequence from `recipes.RECIPES`, rendered by code and performed.

    The recipe is looked up again here, against the window as it is now: a name the
    understander read against a browser must not run against whatever holds that address
    at the moment of the write.
    """
    window = _require_window(action)
    _address(window)
    kind = _authorize_gesture(window, before)
    recipe = _recipe_for(window, kind, action.verb or "")
    refusal = recipes.refusal_for(recipe, window, kind)
    if refusal:
        raise OpError(refusal)
    if journal.dry_run():
        return _would(f"run the {recipe.name.replace('_', ' ')} sequence in {window.cls}")
    safety.touch("run_recipe")
    try:
        recipes.execute(_spelled(recipes.render(recipe, action.text, window)), window)
    except recipes.RecipeError as exc:
        raise OpError(str(exc)) from exc
    return f"{recipe.description[:1].upper()}{recipe.description[1:]}."


# `recipes` writes "enter"; `inapp`'s closed chord table spells the same physical key
# "Return", the XKB keysym name, and refuses every name it does not hold, so a recipe
# that ends in Enter could not be delivered at all. Neither file is ours to change, so
# the spelling is translated here, and ONLY the spelling: a name that is not the very
# same key is not in this map and `inapp.press` still refuses it.
_CHORD_SPELLING: dict[str, str] = {"enter": "Return"}


def _spelled(steps: list[recipes.Step]) -> list[recipes.Step]:
    return [
        replace(step, keys=_CHORD_SPELLING[step.keys])
        if step.kind is recipes.StepKind.PRESS and step.keys in _CHORD_SPELLING
        else step
        for step in steps
    ]


def _recipe_for(window: Window, kind: str, name: str):
    found = next((r for r in recipes.for_window(window, kind) if r.name == name), None)
    if found is None:
        raise OpError(f"{window.cls or 'That window'} has no {name.replace('_', ' ')}.")
    return found


def _click_control(action: Action, before: DesktopState) -> str:
    """Click the control the understander named, re-found in the tree as it is now.

    The controls are read again rather than carried along: `Action` has no room for one,
    the reading is cached for tens of seconds so this is nearly always free, and the name
    the understander authorized has to still be the best match or nothing is clicked. An
    untrusted name may not change under us between the countdown and the click.
    """
    window = _require_window(action)
    _address(window)
    _authorize_gesture(window, before)
    name = action.verb or ""
    try:
        found = controls.controls_for(window)
    except controls.ControlsError as exc:
        raise OpError(str(exc)) from exc
    ranked = controls.best_match(action.text or "", found)
    if not ranked or ranked[0][0].name != name:
        raise OpError("The controls changed since you spoke, so nothing was clicked.")
    try:
        return controls.click(ranked[0][0], window)
    except controls.ControlsError as exc:
        raise OpError(str(exc)) from exc


# ------------------------------------------------------------------- through an adapter


def _service(action: Action, fresh: DesktopState) -> str:
    """One adapter capability, carried out by the adapter that offered it.

    THE GUARDS, in the order they run, and why each one is here and not somewhere else.
    The executor has already taken the lock latch, refused read-only mode and re-read the
    desktop (`executor._run`); `fresh` is that re-read, and it is what every check below
    is made against. What is left is the part only this function can do:

    1. the adapter still exists in this process. An option minted by an adapter that is
       gone is a handle nobody can honour (`adapters.base.Minted`), and the honest answer
       to one is a refusal, never a different adapter with the same capability id.
    2. the capability is still one that adapter offers RIGHT NOW. Asked of the adapter
       against fresh state rather than remembered from the harvest, which is
       `adapters.base.capability_of`'s whole reason for existing: a player that closed
       between the answer and the act takes its capability with it.
    3. the tier has not gone up. `adapters.base.tier_for` lets an untrusted label RAISE
       what an option costs, and that rule is worth nothing if it is applied once, before
       the model answers, and never again. So the live tier is recomputed from the live
       capability and the label the option carried, and a capability that now costs more
       than it did when it was offered is refused rather than performed at the old price.
    4. tier 3 is not reachable at all, by construction (`base.MAX_OFFERABLE_TIER`), and it
       is checked anyway: this is the last code that looks before an application acts.

    `perform` itself checks the one thing only it can: that the handle is one it minted.
    """
    service = action.service
    if service is None:
        raise OpError("There is nothing for an adapter to do here.")
    adapters = _adapters()
    found = base.by_name(adapters, service.adapter)
    if found is None:
        raise OpError(f"{service.adapter} is not available, so nothing was done.")
    context = base.Context(state=fresh, heard="", now=time.monotonic())
    try:
        live = base.capability_of(found, service.capability, context)
    except Exception as exc:
        raise OpError(f"{service.adapter} could not be asked what it can do now.") from exc
    if live is None:
        raise OpError(f"{service.adapter} cannot do that any more, so nothing was done.")
    try:
        now = base.tier_for(live, service.label)
    except base.AdapterError as exc:
        raise OpError(str(exc)) from exc
    if now > service.tier or now > base.MAX_OFFERABLE_TIER:
        raise OpError("That costs more now than when it was offered, so nothing was done.")
    if journal.dry_run():
        return _would(f"ask {service.adapter} to {service.capability.replace('_', ' ')}")
    safety.touch("service")
    return perform_service(service.adapter, service.capability, service.handle, fresh)


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


def _scroll_back(action: Action, _before: DesktopState) -> Action | None:
    # the same notches the other way, which is what makes a scroll free to reverse
    return replace(action, direction=_OPPOSITE[action.direction]) if action.direction else None


def _chord_back(action: Action, _before: DesktopState) -> Action | None:
    """The chord that puts the view back, where `inapp` says one exists.

    Nothing is invented here: Home has no undo, and "new tab" is not undone by ctrl+w,
    because closing a tab is a tier 2 act and an undo may never cost more than the thing
    it undoes.
    """
    gesture = inapp.inverse(inapp.press(action.verb, "")) if action.verb else None
    return replace(action, verb=gesture.keys) if gesture is not None else None


# The recipes whose opposite is another recipe and nothing worse. Everything else has
# none: a search, a navigation and a find leave the page somewhere code cannot name, and
# a closed tab is exactly the case `inapp.INVERSE_CHORDS` refuses to pretend about.
_RECIPE_BACK: dict[str, str] = {
    "back": "forward",
    "forward": "back",
    "next_tab": "previous_tab",
    "previous_tab": "next_tab",
    "zoom_in": "zoom_out",
    "zoom_out": "zoom_in",
}


def _recipe_back(action: Action, _before: DesktopState) -> Action | None:
    name = _RECIPE_BACK.get(action.verb or "")
    return replace(action, verb=name, text=None) if name else None


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
        Operation(Intent.SCROLL, _scroll, _scroll_back, needs_window=True),
        Operation(Intent.PRESS_CHORD, _press_chord, _chord_back, needs_window=True),
        # "again" re-runs the last action WITHOUT re-tiering it, and a recipe may be
        # tier 2 (it types, it closes a tab, it saves over a file), so it is not repeated
        Operation(
            Intent.RUN_RECIPE, _run_recipe, _recipe_back, needs_window=True, repeatable=False
        ),  # fmt: skip
        # a click can send a message or spend money and has no inverse at all
        Operation(
            Intent.CLICK_CONTROL, _click_control, _none, needs_window=True, repeatable=False
        ),  # fmt: skip
        # No window: an adapter option names its own target in its handle, so it is never
        # aimed at whatever has focus. No inverse: what was playing before is not
        # recoverable from here and an undo would be a second guess (`waytify.perform`).
        # Not repeatable: "again" replays an action WITHOUT re-tiering it, and a handle
        # goes stale, so the second run would be a fresh write at a price nobody checked.
        Operation(Intent.SERVICE, _service, _none, repeatable=False),
    )
}
