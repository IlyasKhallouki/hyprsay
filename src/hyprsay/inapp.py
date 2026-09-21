"""In-app reach that needs no accessibility tree: wheel notches and named chords.

v1 could open an application and move its window, and nothing inside it. "Open chrome,
navigate to youtube and look up ltt" needs input INSIDE the window, and the accessibility
tree that would name the controls is not something to build on: on the owner's own desktop
the AT-SPI registry will not even activate (`busctl` answers "Could not activate remote
peer 'org.a11y.atspi.Registry': unit failed"), and 8 of the 10 windows open while this was
written were terminals, which expose nothing through it even when it works. Scrolling,
paging and keystrokes need none of it. They are ordinary input, delivered to the window
under the cursor or to the window that holds focus.

So this module stays thin. It builds a `Gesture`, an `Operation` performs it, and every
delivery goes out through the inherited `hypruse.server` functions, which hold the lock,
confinement, auth and keyboard-grab guards, the dry-run barrier and the journal.

Three things a reader will not expect.

A chord is pinned the way typing is, with `window=`, so the inherited call focuses that
address first and a focus change between authorization and delivery cannot redirect it. A
scroll cannot be pinned that way: `hypruse.server.pointer` has no `window=` parameter,
because a wheel event goes to whatever is under the cursor rather than to whatever holds
focus. So a scroll is pinned geometrically instead, by putting the cursor inside the
authorized window's rectangle and sending the notches there. The cost is visible and
honest: the human's cursor moves, and on a desktop with focus-follows-mouse so does focus.

A gesture is refused on exactly the surfaces typing is refused on, by calling the same
function (`tiers.typing_refusal`). A chord into a terminal is not milder than text into
one: ctrl+c kills the foreground job, ctrl+d ends the shell, and Return runs whatever is
already on the line. A wheel notch is not milder either, because `set -g mouse on` in tmux
and `set mouse=a` in vim turn wheel events into keystrokes the application reads.

And a chord is only ever produced by the grammar. Jev never picks one from a list, so
there is no question file here and no candidates: an unknown name is refused, not guessed.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from hyprsay.config import Config
from hyprsay.model import DesktopState, Direction, Window
from hyprsay.nlu import tiers
from hypruse import server

SCROLL = "scroll"
CHORD = "chord"

# amount 0 tiny .. 4 maximum, the scale the grammar already uses for resize and volume.
# A notch is one wheel click, which most toolkits turn into three lines, so the middle
# step is roughly half a screen in a browser. A guess until someone measures a real page.
SCROLL_NOTCHES: tuple[int, ...] = (1, 2, 4, 8, 16)
DEFAULT_AMOUNT = 2

# The closed set of chords speech can reach, each with the tier code gives it. One table,
# two views: SAFE_CHORDS below is derived from it, because a second hand-written list of
# the same names is a second thing to drift.
CHORD_TIERS: dict[str, int] = {
    # tier 0: the viewport moves and nothing else does. The other key puts it back.
    "Page_Down": 0,
    "Page_Up": 0,
    # the ends of the page, or of the line in a text field. Either way this moves a caret
    # or a scrollbar and writes nothing
    "Home": 0,
    "End": 0,
    # tier 1: navigation and editing. Each leaves the window able to do what it did before.
    "ctrl+l": 1,  # the address bar: it selects the URL, it does not go anywhere
    "ctrl+f": 1,  # the find bar: a search inside the page, nothing leaves the machine
    "ctrl+t": 1,  # a new tab, empty. Nothing of the tab you were on is lost
    "ctrl+r": 1,  # reload. A page that came from a POST asks before it resends anything
    "alt+Left": 1,  # back, and alt+Right returns; neither can lose a page
    "alt+Right": 1,
    "ctrl+Tab": 1,  # another tab is shown; the one you left is still open
    "ctrl+shift+Tab": 1,
    "ctrl+shift+t": 1,  # reopens the tab ctrl+w closed, which is why it is not tier 2
    # tier 2: a countdown, every time. Each of these ends something or commits something.
    "ctrl+w": 2,  # closes the tab. The page comes back, what you typed into it does not
    "ctrl+q": 2,  # quits the application, every tab with it
    "alt+F4": 2,  # the same thing asked of the compositor instead of the application
    "Return": 2,  # submits. It is the key that sends the message and runs the command line
    "Delete": 2,  # deletes the selection, and speech cannot see what is selected
    "BackSpace": 2,  # the same, and in some applications it also navigates back
    "ctrl+s": 2,  # writes the file. No chord unwrites it
}

# what may be sent without a countdown: tier 0 and tier 1, and nothing else
SAFE_CHORDS: frozenset[str] = frozenset(name for name, level in CHORD_TIERS.items() if level <= 1)

# reversing a chord is only offered where the reverse is not worse than the act. "New tab"
# has no entry, because undoing it means ctrl+w, which closes a tab and is tier 2.
INVERSE_CHORDS: dict[str, str] = {
    "Page_Down": "Page_Up",
    "Page_Up": "Page_Down",
    "alt+Left": "alt+Right",
    "alt+Right": "alt+Left",
    "ctrl+Tab": "ctrl+shift+Tab",
    "ctrl+shift+Tab": "ctrl+Tab",
}

_PAGE_KEYS = {Direction.DOWN: "Page_Down", Direction.UP: "Page_Up"}
# hypruse's sign convention (hypruse/wire.py): positive dy scrolls content down, positive
# dx scrolls content right
_AXIS = {
    Direction.DOWN: (0, 1),
    Direction.UP: (0, -1),
    Direction.RIGHT: (1, 0),
    Direction.LEFT: (-1, 0),
}
_DRY_PREFIX = "DRY RUN"  # the opening every inherited tool gives a rehearsal


class GestureError(ValueError):
    """This gesture cannot or did not happen. A ValueError so that `executor.explain`
    already turns it into one plain sentence for the HUD."""


# --------------------------------------------------------------------------- the gesture


@dataclass(frozen=True)
class Gesture:
    """One piece of ordinary input aimed at one window. What an `Operation` performs."""

    kind: str  # SCROLL | CHORD
    keys: str = ""  # the table's spelling of a chord; "" for a scroll
    dy: int = 0  # wheel notches, positive is content down
    dx: int = 0  # wheel notches, positive is content right
    # the address this gesture was authorized against. "" pins nothing yet, and `perform`
    # pins it to the window it is handed.
    window: str = ""

    @property
    def tier(self) -> int:
        """Derived, never stored: a Gesture built by hand cannot claim a cheaper tier."""
        return 0 if self.kind == SCROLL else chord_tier(self.keys)

    def describe(self) -> str:
        """A short phrase for the HUD chip. Never contains anything the window wrote."""
        if self.kind == CHORD:
            return f"press {self.keys}"
        if self.dy:
            return f"scroll {'down' if self.dy > 0 else 'up'}"
        return f"scroll {'right' if self.dx > 0 else 'left'}"


# ---------------------------------------------------------------------------- the chords


def _key(keys: str) -> str:
    parts = [part.strip() for part in keys.split("+") if part.strip()]
    if not parts:
        return ""
    last = parts[-1]
    return "+".join([p.lower() for p in parts[:-1]] + [last if len(last) == 1 else last.lower()])


_INDEX: dict[str, str] = {_key(name): name for name in CHORD_TIERS}


def canonical(keys: str) -> str:
    """The table's spelling of `keys`, or "" when the table has no such chord.

    Modifiers are matched without regard to case, a one-character key is not: in XKB "L"
    is shift and "l" is not, so lowering the key would quietly turn ctrl+L into a
    different keystroke. A longer key name (Page_Down, F4) has no such pair.
    """
    return _INDEX.get(_key(keys), "")


def chord_tier(keys: str) -> int:
    """0 for pure scrolling and paging, 1 for navigation, 2 for anything that can close,
    submit or destroy.

    A name this table has never heard of gets 3, for the reason `tiers.tier` gives 3 to an
    intent it does not know: the strictest tier, not the cheapest. Nothing reaches the
    desktop at tier 3 from here anyway, because `press` refuses to build a gesture for a
    name that is not in the table.
    """
    return CHORD_TIERS.get(canonical(keys), 3)


# ----------------------------------------------------------------------- the constructors


def scroll(direction: Direction, amount: int | None = None, window: str = "") -> Gesture:
    """Wheel notches aimed at `window`.

    Tier 0: a scroll cannot destroy anything, and the same call the other way brings the
    view back, which is what makes acting first and offering a swap safe (PLAN 5.6).
    """
    step = _AXIS.get(direction)
    if step is None:
        raise GestureError("no direction was named to scroll in")
    index = DEFAULT_AMOUNT if amount is None else amount
    notches = SCROLL_NOTCHES[min(max(index, 0), len(SCROLL_NOTCHES) - 1)]
    return Gesture(SCROLL, dy=step[1] * notches, dx=step[0] * notches, window=window)


def press(keys: str, window: str = "") -> Gesture:
    """A gesture for a named chord. A name the table does not hold is refused here, so no
    string from anywhere else reaches the keyboard."""
    name = canonical(keys)
    if not name:
        raise GestureError(f"{keys!r} is not a chord this can send")
    return Gesture(CHORD, keys=name, window=window)


def page(direction: Direction, window: str = "") -> Gesture:
    """A page down or a page up, as the key rather than as the wheel.

    An application decides for itself what one page of itself is, and its own key is the
    only thing that knows where the next screenful starts. Sideways has no such key.
    """
    name = _PAGE_KEYS.get(direction)
    if name is None:
        raise GestureError("paging only goes up and down")
    return press(name, window)


def top(window: str = "") -> Gesture:
    return press("Home", window)


def bottom(window: str = "") -> Gesture:
    return press("End", window)


def inverse(gesture: Gesture) -> Gesture | None:
    """The gesture that puts the view back, or None when there is none worth offering.

    Best effort by nature: scrolling back from the bottom of a page lands where the page
    ends, not where you were. Nothing is lost either way, which is the whole point of
    tier 0.
    """
    if gesture.kind == SCROLL:
        return replace(gesture, dy=-gesture.dy, dx=-gesture.dx)
    name = INVERSE_CHORDS.get(gesture.keys)
    return replace(gesture, keys=name) if name else None


# ---------------------------------------------------------------------------- refusal


def refusal(window: Window | None, kind: str, state: DesktopState, cfg: Config) -> str:
    """Why no gesture may be sent to this window, or "" when one may.

    The rule is the typing rule, called rather than copied: the surfaces that must never
    receive dictated text must never receive keystrokes or wheel notches either. The
    sentence that comes back therefore speaks of typing, and is not reworded here, because
    a second copy of the wording is a second thing to keep true.
    """
    return tiers.typing_refusal(window, kind, state, cfg)


# ---------------------------------------------------------------------------- perform


def _aim(window: Window) -> tuple[float, float]:
    """The point a wheel event has to land on to reach `window`: the middle of it."""
    width, height = window.size
    if width <= 0 or height <= 0:
        raise GestureError("that window has no size, so there is nowhere to aim the wheel")
    return window.at[0] + width / 2, window.at[1] + height / 2


def _said(result: object) -> str:
    return result if isinstance(result, str) else ""


def _dry(result: object) -> str | None:
    text = _said(result)
    return text if text.startswith(_DRY_PREFIX) else None


def perform(gesture: Gesture, window: Window) -> str:
    """Deliver `gesture` to `window` and say in one sentence what happened.

    `window` is the window the gesture was authorized against, re-read fresh (the executor
    resolves the pinned address against a new snapshot before any operation runs). A
    gesture that carries a different address is refused, never redirected.
    """
    if gesture.window and gesture.window != window.address:
        raise GestureError("that gesture was authorized against a different window")
    if gesture.kind not in (SCROLL, CHORD):
        raise GestureError(f"{gesture.kind!r} is not a gesture this can perform")
    # the last line, the one `executor._check_typing` keeps for text: the resolver refused
    # a terminal already, and this refuses it again at the moment of delivery
    if tiers.is_terminal(window):
        raise GestureError("a terminal reads keys and wheel notches as commands")
    if gesture.kind == CHORD:
        # window= makes the inherited lock and keyboard-grab guards REFUSE rather than
        # note, and focuses that address before the keys go out. allow_auth stays at its
        # default, so an authentication dialog refuses the chord as it refuses text.
        result = server.keyboard("key", keys=gesture.keys, window=window.address)
        return _dry(result) or f"Pressed {gesture.keys} in {window.cls}."
    if not (gesture.dy or gesture.dx):
        raise GestureError("a scroll of no notches is not a scroll")
    x, y = _aim(window)
    result = server.pointer("scroll", x=x, y=y, scroll_dy=gesture.dy, scroll_dx=gesture.dx)
    text = _dry(result)
    if text:
        return text
    # a bar, a launcher or a popup over that point takes the notches instead, and the
    # inherited call only NOTES that rather than refusing (a click into a launcher is a
    # legitimate thing to want). Saying "scrolled chrome" here would be a lie. The layer's
    # own namespace is not repeated: it is a string another program chose.
    if "NOTE:" in _said(result):
        return f"A layer was over {window.cls}, so the scroll went to that."
    return f"Scrolled {window.cls}."
