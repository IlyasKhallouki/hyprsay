"""Clicking a control inside an application window, by the name the speaker said.

Everything else hyprsay does aims at a window. This is the one module that reaches
INSIDE one, through the accessibility tree that GTK and Qt publish on the AT-SPI bus,
so "click send" can find the Send button with no screenshot and no pixel guessing.

Two measured facts shape every decision below.

**The tree is far more expensive than it looks.** One busctl round trip takes a median
of 5.8 ms on this machine, and `hypruse.a11y.find_elements` spends at least three of
them on every node it visits and six on every control it keeps. Measured live against
the real bus: KCalc's 48 controls took **16.2 seconds**; a window that publishes nothing
takes 0.4 to 0.55 s to say so; the cached re-read takes 0.02 ms. Three consequences,
all of them load bearing:

- the walk runs only when the utterance actually asks to click something, never
  speculatively, because a speculative one would eat the whole latency budget;
- a walk that overruns its caller's deadline is NOT thrown away. It keeps going on its
  worker and lands in the cache, so the first "click send" answers "say that again in a
  moment" and the second one is instant. At 16 s a walk is far too expensive to repeat;
- the cache has to outlive several utterances, so its TTL is tens of seconds, not the
  couple of seconds that would be right if a read were cheap.

**The tree is fragile.** It was broken on this machine while this was written: four
stray at-spi-bus-launcher processes, and the registry that the bus address pointed at
answering `Could not activate remote peer 'org.a11y.atspi.Registry': unit failed`.
`probe` exists so that this comes back as one sentence naming the command that repairs
it, instead of as an empty list that the caller would read as "there is no such button".
The same live check found google-chrome and com.rtosta.zapzap publishing empty trees,
which is why an empty tree behind a Chrome or Electron class is named for the flag it is
missing rather than reported as an absence of buttons.

THE SAFETY RULE (docs/PLAN.md 5.6). A control's accessible name is written by the
application that draws it, and inside a browser by whatever page is loaded, so it is
exactly as untrusted as a window title. `Control.trusted` is always False and cannot be
set. Two consequences are enforced here rather than left to the caller:

- A control the speaker did not name is never a click target. `best_match` scores only
  against the words actually spoken, so a page that names a button "click the button"
  still needs the speaker to utter a word that names it.
- An untrusted name may raise a tier, never lower one. `click_tier` is at least 1, and
  2 with a countdown for any name that reads destructive. A hostile page can use that
  to make hyprsay more careful about its own buttons, which is the harmless direction.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Protocol

from hyprsay.lexicon import mixed_script, phonetic_key, sanitize, similarity
from hyprsay.model import Window
from hypruse import a11y, server


class ControlsError(Exception):
    """Why a control cannot be read or clicked. The text is one plain sentence for the
    HUD, the same shape as `ops.OpError`.

    It is its own class rather than a subclass of `OpError` so that `ops` may import
    this module without an import cycle. The caller turns it into a REFUSE or a SUGGEST
    with `reason=str(exc)`.
    """


# A name is application-written text on its way to the HUD, so it is cut to a length a
# pill can show. A truncated name still finds its control: hypruse matches a name as a
# substring when it is not exact.
MAX_NAME = 80
MAX_ROLE = 40

# How long a reading stays good. Tens of seconds, not a couple, because a re-read cost
# 16.2 s live: a stale list is a far better answer than making the speaker wait for the
# tree again. What makes that safe is that a stale entry cannot produce a wrong click.
# `click` re-resolves the control BY NAME inside hypruse at the moment of the click, so
# a control that has gone takes the refusal path, not some other control's coordinate.
# A title change drops the entry at once, which covers the staleness that matters: a new
# page or a new document is a new set of buttons.
CACHE_TTL_S = 30.0
# how many windows' readings are kept at once; the daemon runs for weeks
MAX_CACHED_WINDOWS = 16

# Every dry-run result from the inherited server opens with this.
_DRY_PREFIX = "DRY RUN"

_REPAIR = "systemctl --user restart at-spi-dbus-bus"


# --------------------------------------------------------------------------- the control


@dataclass(frozen=True)
class Control:
    """One clickable thing inside a window, as the accessibility tree reports it."""

    id: str  # "<bus service>:<object path>", unique while the widget lives
    name: str  # UNTRUSTED: written by the application, and by a web page inside it
    role: str  # the toolkit's own role name: "push button", "link", "entry"
    enabled: bool
    # global logical pixels (x, y, w, h), the space `Window.at` and the pointer use.
    # For drawing badges only: a click re-resolves the control by name inside the
    # guarded hypruse path, so a stale coordinate can never be clicked.
    bounds: tuple[int, int, int, int]
    window_address: str
    # ALWAYS False, and forced back below. The name of a control is written by the
    # application, and inside a browser by the page, so it is as untrusted as a window
    # title: it may never select or corroborate a target (docs/PLAN.md 5.6). The field
    # exists so that code reading `Candidate.corroborated` and this side by side reads
    # the same way, not because there is a value that would make a label trustworthy.
    trusted: bool = False

    def __post_init__(self) -> None:
        if self.trusted:
            object.__setattr__(self, "trusted", False)


class Reader(Protocol):
    """What this module needs from an accessibility tree.

    `Atspi` is the live one. Tests pass a fake, because a unit test may never touch the
    bus, and because the bus is exactly what is broken when this code matters most.
    """

    def probe(self) -> tuple[bool, str]: ...

    def read(self, window: Window, limit: int) -> list[dict[str, Any]]: ...


def _plain(text: str, limit: int = 120) -> str:
    """A hypruse or busctl message cut down to one plain clause for the HUD."""
    return sanitize(text).split(". ", 1)[0].rstrip(".")[:limit]


class Atspi:
    """The live AT-SPI tree, through `hypruse.a11y` (busctl, no new dependency)."""

    def probe(self) -> tuple[bool, str]:
        """(usable, one sentence). Never raises: probe is how a caller finds out that
        something is wrong, so it may not be another thing that goes wrong.

        Three failures are told apart because they need three different repairs: no bus
        at all, a bus whose registry does not answer (the orphaned-bus case seen on this
        machine), and a registry that answers with nothing registered.
        """
        try:
            address = a11y.bus_address()
        except Exception:
            return False, (
                "no accessibility bus is running, so no application can be asked what its "
                f"buttons are; run {_REPAIR} and start the application again"
            )
        try:
            registered = a11y.apps(a11y.Bus(address))
        except Exception:
            return False, (
                f"the accessibility bus at {address} is there but its registry does not "
                f"answer, which is a bus left over from an older session; run {_REPAIR}"
            )
        if not registered:
            return False, (
                "the accessibility registry is empty, so no application has published its "
                "controls; start the application again, and Chrome or Electron with "
                "--force-renderer-accessibility"
            )
        return True, f"{len(registered)} applications are on the accessibility bus"

    def read(self, window: Window, limit: int) -> list[dict[str, Any]]:
        """The window's actionable elements, in `a11y.find_elements` shape.

        The title is passed to `app_for_pid` and `window_frame` because a multi-process
        toolkit registers under a different pid and a multi-window app has several
        frames. That is untrusted text choosing which subtree to WALK; it authorizes
        nothing, and the click is pinned to the window address either way.
        """
        try:
            bus = a11y.connect()
            app = a11y.app_for_pid(bus, window.pid, window.title)
            if app is None:
                return []
            frame = a11y.window_frame(bus, app[0], app[1], window.title, window.size)
            elements, _ = a11y.find_elements(bus, frame[0], frame[1], max_results=limit)
        except a11y.A11yError as exc:
            what = window.cls or "that window"
            raise ControlsError(
                f"the controls of {what} could not be read: {_plain(str(exc))}"
            ) from exc
        return elements


def probe(reader: Reader | None = None) -> tuple[bool, str]:
    """Can a control be clicked by name at all, and if not, what should the owner run?"""
    return (reader or Atspi()).probe()


# --------------------------------------------------------------------------- reading


@dataclass(frozen=True)
class _Cached:
    title: str
    cls: str
    stamp: float
    controls: tuple[Control, ...]


_CACHE: dict[str, _Cached] = {}
# keyed by (address, title, class), not by address alone: a walk that is still running
# when the title changes described a window that no longer exists, and the next
# utterance must not be handed its buttons
_PENDING: dict[tuple[str, str, str], Future] = {}
_LOCK = threading.Lock()
_POOL: ThreadPoolExecutor | None = None


def _now() -> float:
    return time.monotonic()


def _pool() -> ThreadPoolExecutor:
    """One worker, created on first use. A second concurrent walk would only queue
    behind the first on the same bus, and the daemon has one executor thread anyway."""
    global _POOL
    if _POOL is None:
        _POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hyprsay-controls")
    return _POOL


def invalidate(address: str = "") -> None:
    """Drop one window's cached controls, or all of them.

    The daemon calls this on a title change, a close, or anything else that means the
    window is no longer what was read. `controls_for` checks the title itself, so this
    is belt and braces for the events that arrive without one.
    """
    with _LOCK:
        if address:
            _CACHE.pop(address, None)
        else:
            _CACHE.clear()


def _fresh(window: Window) -> tuple[Control, ...] | None:
    with _LOCK:
        cached = _CACHE.get(window.address)
        if cached is None:
            return None
        # a new title means the document or the page changed, and with it every button.
        # a new class means the address has been handed to a different window.
        stale = cached.title != window.title or cached.cls != window.cls
        if stale or _now() - cached.stamp > CACHE_TTL_S:
            del _CACHE[window.address]
            return None
        return cached.controls


def _control(window: Window, element: dict[str, Any]) -> Control | None:
    """One element of a tree walk as a Control, or None when it is not clickable there."""
    extent = tuple(element.get("extent") or ())
    if len(extent) != 4:
        return None
    try:
        ex, ey, ew, eh = (int(v) for v in extent)
    except (TypeError, ValueError):
        return None
    ax, ay = window.at
    aw, ah = window.size
    x, y = ax + ex, ay + ey
    # the window rectangle is authoritative, exactly as in hypruse's `ui`: a toolkit
    # reports absurd origins for widgets on a tab page it has not rendered, and a click
    # there would land in some other window. A window of unknown size keeps everything,
    # since there is then nothing to check against.
    centre = (x + ew // 2, y + eh // 2)
    if aw and ah and not (ax <= centre[0] < ax + aw and ay <= centre[1] < ay + ah):
        return None
    return Control(
        id=f"{element.get('svc', '')}:{element.get('path', '')}",
        name=sanitize(str(element.get("name", "")))[:MAX_NAME],
        role=sanitize(str(element.get("role", "")))[:MAX_ROLE],
        enabled=bool(element.get("clickable", False)),
        bounds=(x, y, ew, eh),
        window_address=window.address,
    )


def _read_controls(window: Window, limit: int, reader: Reader) -> list[Control]:
    found = (_control(window, element) for element in reader.read(window, limit))
    return [control for control in found if control is not None][:limit]


def _key(window: Window) -> tuple[str, str, str]:
    return (window.address, window.title, window.cls)


def _forget(window: Window, future: Future) -> None:
    with _LOCK:
        if _PENDING.get(_key(window)) is future:
            del _PENDING[_key(window)]


def _settle(window: Window, future: Future, found: Sequence[Control]) -> None:
    """Retire a finished walk: forget that it is running, keep what it found.

    An EMPTY result is cached too. "This window publishes nothing" took 0.4 to 0.55 s to
    establish live, and a terminal will still publish nothing in a second's time, so the
    next utterance should get that answer for free rather than pay for it again.
    """
    _forget(window, future)
    with _LOCK:
        _CACHE[window.address] = _Cached(window.title, window.cls, _now(), tuple(found))
        if len(_CACHE) > MAX_CACHED_WINDOWS:
            # nothing expires by itself, and a window that is never spoken to again
            # would otherwise sit here for the life of the daemon
            del _CACHE[min(_CACHE, key=lambda address: _CACHE[address].stamp)]


def _store(window: Window, future: Future) -> None:
    """The done callback of a walk nobody is waiting for any more, because it ran past
    its caller's budget. Its result is still worth having: it is what the next utterance
    about this window reads instead of walking the tree again."""
    if not _succeeded(future):
        # a walk that failed caches nothing, not even an emptiness: a bus that broke
        # mid-walk is not the same statement as "this window has no buttons"
        _forget(window, future)
        return
    _settle(window, future, future.result(timeout=0))


def _start(window: Window, limit: int, reader: Reader) -> Future:
    with _LOCK:
        running = _PENDING.get(_key(window))
        # a finished walk that failed is worth repeating; one that succeeded is not,
        # even in the moment between its result landing and its callback running
        if running is not None and (not running.done() or _succeeded(running)):
            return running
        future = _pool().submit(_read_controls, window, limit, reader)
        _PENDING[_key(window)] = future
    future.add_done_callback(lambda done: _store(window, done))
    return future


def _succeeded(future: Future) -> bool:
    return not future.cancelled() and future.exception(timeout=0) is None


# Classes that publish nothing until they are started with a flag. Chrome and every
# Electron shell built on it expose an empty tree rather than no tree, which reads as
# "no such button" unless it is named for what it is.
CHROMIUM_MARKERS: tuple[str, ...] = (
    "chrom",
    "electron",
    "brave",
    "vivaldi",
    "edge",
    "opera",
    "slack",
    "discord",
    "spotify",
    "signal",
    "zapzap",
    "whatsapp",
    "obsidian",
    "notion",
    "teams",
    "vscode",
    "code-oss",
    "codium",
)
ACCESSIBILITY_FLAG = "--force-renderer-accessibility"


def looks_chromium(window: Window) -> bool:
    classes = f"{window.cls} {window.initial_class}".lower()
    return any(marker in classes for marker in CHROMIUM_MARKERS)


def _nothing_exposed(window: Window) -> str:
    what = window.cls or "that window"
    if looks_chromium(window):
        return (
            f"{what} is Chrome or Electron, and those publish no controls at all unless they "
            f"were started with {ACCESSIBILITY_FLAG}, so this is not 'no such button', it is "
            "'no buttons at all'"
        )
    return (
        f"{what} publishes no controls on the accessibility bus, so nothing in it can be "
        "clicked by name; a terminal and a canvas app never will"
    )


def controls_for(
    window: Window,
    limit: int = 60,
    timeout: float = 1.5,
    reader: Reader | None = None,
) -> list[Control]:
    """The clickable controls of one window. Call this only when the utterance asks to
    click something: it is a walk over a D-Bus tree, not a lookup.

    Raises `ControlsError`, whose text is one plain sentence, rather than returning an
    empty list, because every reason for an empty list ("the bus is broken", "Chrome was
    started without the flag", "still reading") is a different thing to say and none of
    them means "there is no such button".

    A walk that runs past `timeout` is not abandoned. It keeps going on its worker and
    fills the cache, so the next utterance gets it for nothing. On a real window that is
    the normal path, not the exception: KCalc's 48 controls took 16.2 s live, so at the
    default budget the first "click send" after a window is touched will nearly always
    answer "say that again in a moment" and the one after it will be instant.
    """
    found = _fresh(window)
    if found is None:
        reader = reader or Atspi()
        usable, why = probe(reader)
        if not usable:
            raise ControlsError(why)
        future = _start(window, limit, reader)
        try:
            walked: list[Control] = future.result(timeout=max(timeout, 0.0))
        except TimeoutError as exc:
            raise ControlsError(
                f"reading the controls of {window.cls or 'that window'} is taking longer than "
                f"{timeout:.1f} seconds; it is still going, so say that again in a moment"
            ) from exc
        except ControlsError:
            raise
        except Exception as exc:
            # everything this function raises is a ControlsError carrying one plain
            # sentence, so one `except` in the caller covers a broken bus, a Reader
            # nobody anticipated, and a bug in the walk alike
            what = window.cls or "that window"
            raise ControlsError(
                f"the controls of {what} could not be read: {_plain(str(exc))}"
            ) from exc
        _settle(window, future, walked)
        found = tuple(walked)
    if not found:
        raise ControlsError(_nothing_exposed(window))
    return list(found)


# --------------------------------------------------------------------------- matching

_TOKEN = re.compile(r"[^\W_]+")


def _wordset(block: str) -> frozenset[str]:
    return frozenset(block.split())


# Words that carry the request to click, not the name of the thing clicked. Only these
# are stripped from what the speaker said. `lexicon.STOPWORDS` is deliberately NOT
# reused here: it removes "ok", "open", "close", "play" and "send", which on a button
# are not command words at all, they are the whole name.
CARRIER: frozenset[str] = _wordset(
    """
    click clicking press pressing tap hit push punch select choose pick
    the a an this that these those it its there here please now then just hey
    on in at of to for with and my your
    button buttons control controls option options item items entry field box
    link tab menu icon toggle thing one
    """
)

# A spoken word must reach this to count as naming a word of a control's name. This is
# the lexicon's CORROBORATION floor, not its lower matching floor, because here the
# spoken word is the only thing that authorizes the click: there is no trusted field
# behind it to agree with. At the matching floor "send" reaches a button called "Spend"
# (0.82); at this one it does not.
SAID_FLOOR = 0.85
# and a whole control must reach this to be offered at all. Below it, the overlap is
# one word out of many and the speaker was talking about something else.
MATCH_FLOOR = 0.5
# shorter than this, only an exact word counts: every three-letter word sounds like
# every other one, and "ok" must not answer to "up"
MIN_FUZZY_LETTERS = 4


def _words(text: str) -> list[str]:
    return _TOKEN.findall(sanitize(text).casefold())


def _singular(token: str) -> str:
    return token[:-1] if len(token) > 3 and token.endswith("s") else token


def spoken_words(phrase: str) -> list[str]:
    """What the speaker said, minus the words that carry the click itself."""
    return [w for w in _words(phrase) if w not in CARRIER and not mixed_script(w)]


def _said(spoken: str, word: str) -> float:
    """How well one spoken word names one word of a control's name: 0, or SAID_FLOOR to 1."""
    if spoken == word or _singular(spoken) == _singular(word):
        return 1.0
    if min(len(spoken), len(word)) < MIN_FUZZY_LETTERS:
        return 0.0
    if phonetic_key(spoken)[:1] != phonetic_key(word)[:1]:
        return 0.0
    heard = similarity(spoken, word)
    return heard if heard >= SAID_FLOOR else 0.0


def score(phrase: str, control: Control) -> float:
    """How well a phrase names this control, 0 to 1. Zero when nothing was said that
    names it, and zero is not a candidate."""
    spoken = spoken_words(phrase)
    # a name token that mixes scripts is a homoglyph, never a word: "сlose" with a
    # Cyrillic c would otherwise answer to "close" with four of its five letters
    name = [w for w in _words(control.name) if not mixed_script(w)]
    if not spoken or not name:
        return 0.0
    hit = sum(max((_said(s, w) for s in spoken), default=0.0) for w in name)
    # divided by the longer side, so "Send" beats "Send later" for "send", and
    # "Send message" beats "Send" for "send message"
    return round(hit / max(len(spoken), len(name)), 4)


def best_match(
    phrase: str, controls: Sequence[Control], limit: int = 5
) -> list[tuple[Control, float]]:
    """The controls a phrase could name, best first. Pass the target words only.

    THE RULE: a control whose name matches nothing the speaker literally said is never
    returned, whatever else is on screen and however alone it is.

    The defence: a control name is written by the application, and in a browser by the
    page, so it is untrusted text exactly like a window title (docs/PLAN.md 5.6). If a
    click could be aimed at the nearest control rather than a named one, a page would
    only have to be open at the moment someone in the room says "click" to receive it,
    and a page that names a button "click the button" would receive every such
    utterance. Scoring only against the words the speaker actually used means the page
    cannot supply the evidence that selects it; the speaker has to. That the resulting
    click is still at least tier 1, and tier 2 for a destructive-sounding name, is the
    second half of the same rule and lives in `click_tier`.

    Deterministic: equal scores are broken by name and then by tree id, never by walk
    order, so the same desktop and the same words always produce the same list.
    """
    scored = [(control, score(phrase, control)) for control in controls]
    found = [(control, value) for control, value in scored if value >= MATCH_FLOOR]
    found.sort(key=lambda pair: (-pair[1], pair[0].name.casefold(), pair[0].id))
    return found[:limit]


# --------------------------------------------------------------------------- the tier

# Words that make a click tier 2 with a countdown, whatever the control actually does.
#
# This is a safety net, not a guess at meaning. The tree says what a control is CALLED
# and where it is; nothing in AT-SPI says what it will do, and there is no version of
# this module that can know. So the one thing that can be done cheaply is done: when the
# name reads like the operations people regret, the click gets the same cancellable
# countdown that closing a window gets. It is wrong in both directions by design. "Send"
# on a chat message earns a countdown it does not need, and a button called "Proceed"
# that wipes a disk earns none. The list is closed and auditable for the same reason the
# tier 2 verb lexicon is: a rule that can be read in one screen is a rule that can be
# checked, and the cost of a false countdown is 1.5 seconds.
DESTRUCTIVE_CONTROL_WORDS: frozenset[str] = frozenset(
    {
        "delete",
        "remove",
        "buy",
        "pay",
        "send",
        "confirm",
        "sign",
        "allow",
        "permit",
        "install",
        "uninstall",
        "format",
        "erase",
        "quit",
    }
)
# "close" alone is the most common harmless button there is, one per dialog, so it is
# not a word above. "Close account" is a different thing and is matched as the phrase.
DESTRUCTIVE_CONTROL_PHRASES: tuple[tuple[str, ...], ...] = (("close", "account"),)


def _has_phrase(words: Sequence[str], phrase: Sequence[str]) -> bool:
    size = len(phrase)
    return any(tuple(words[i : i + size]) == tuple(phrase) for i in range(len(words) - size + 1))


def click_tier(control: Control) -> int:
    """What a click on this control costs: 1 normally, 2 when the name reads destructive.

    Clicking is never tier 0. Tier 0 is for what is free to reverse, and nothing inside
    an application is: a click can send a message, spend money or delete a file, and
    hyprsay has no inverse for any of it, so `ops` can offer no undo.

    Reading an untrusted name to RAISE a tier is safe, and is the only use an untrusted
    name has here. The window's owner, a hostile page included, can use this to give its
    own buttons a countdown. It cannot use it to take one away.
    """
    words = [w for w in _words(control.name) if not mixed_script(w)]
    if any(_reads_destructive(w) for w in words):
        return 2
    if any(_has_phrase(words, phrase) for phrase in DESTRUCTIVE_CONTROL_PHRASES):
        return 2
    return 1


def _reads_destructive(word: str) -> bool:
    """Does this word of a control's name read as destructive, as loosely as the matcher
    reads it?

    The two sides have to use the same eye. The matcher accepted a control when a spoken
    word merely SOUNDED like one of its words, at SAID_FLOOR, while this test demanded an
    exact string. A page could therefore name a button "Sende": "click send" matched it
    fuzzily, ahead of the app's own "Send message", and then walked past the countdown
    because "sende" is not literally in the list. Reading an untrusted name may only ever
    raise the tier, so when in doubt it raises it.
    """
    if {word, _singular(word)} & DESTRUCTIVE_CONTROL_WORDS:
        return True
    # compared directly, without the matcher's minimum length and initial-sound guards:
    # those exist to stop false MATCHES, and a false match costs a wrong click, while a
    # false reading here costs only a countdown on a button that did not need one. So
    # "payy" and "sende" are read as "pay" and "send".
    return any(
        similarity(word, dangerous) >= SAID_FLOOR or word.startswith(dangerous)
        for dangerous in DESTRUCTIVE_CONTROL_WORDS
    )


# --------------------------------------------------------------------------- clicking


def click(control: Control, window: Window) -> str:
    """Click the control, through the inherited `hypruse.server.click_ui`, pinned to
    `window`. Returns one plain sentence; raises `ControlsError` with one.

    Through click_ui and not through `pointer` at `control.bounds`, for the reason
    `ops` gives for every other verb: the inherited function holds the confinement
    check, the authentication-dialog refusal, the session-lock refusal, the covering
    layer check, the dry-run barrier and the journal entry. It also re-resolves the
    control by name at the moment of the click, so a coordinate read seconds ago can
    never be the thing that is clicked.

    `window` is the window the utterance was pinned to, and the control must belong to
    it. An ambiguous name is refused rather than guessed: two buttons of one name are a
    tie, and a tie is a question for the speaker, exactly as it is for two windows.

    The caller has already taken this through `click_tier` and the verdict table. This
    function is only the HOW.
    """
    if control.window_address != window.address:
        raise ControlsError("that control belongs to a different window, so it was not clicked")
    if not control.name.strip():
        raise ControlsError("that control has no name, so nothing you said can have named it")
    if not control.enabled:
        raise ControlsError(
            f"{control.name!r} is not active right now, so clicking it would do nothing"
        )
    result = server.click_ui(name=control.name, window=window.address)
    if not isinstance(result, str):
        # click_ui answers an ambiguous name with the list of candidates
        raise ControlsError(
            f"more than one control in {window.cls} is called {control.name!r}, so say which one"
        )
    if result.startswith(_DRY_PREFIX):
        return result
    if not result.startswith("clicked "):
        # every other string click_ui returns is a fall-back-to-vision note
        raise ControlsError(f"{control.name!r} could not be clicked: {_plain(result)}")
    return f"Clicked {control.name!r} in {window.cls}."
