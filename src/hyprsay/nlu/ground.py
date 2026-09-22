"""Going to the application you already have open, or starting another copy of it.

Reproduced live on this machine before this module existed: Chrome open on workspace 8,
the speaker on workspace 6, "open chrome" started a SECOND Chrome, while "go to chrome"
went to the one that was there. The second-instance rate on that utterance was 100%.

The cause was not the model. `grammar.py` bound every launch verb to `Intent.LAUNCH_APP`
and `Grammar._try` returns the first full match, so "open" could never reach the
`FOCUS_WINDOW` rule that follows it; `understand._launch` then built the launch with the
desktop in scope and never looked at it. So the verb stops deciding the action and only
declares a preference (`Prefer`), and `reach()` is the single place that chooses.

Matching, in trust order, and what is refused on purpose:

- `App.wm_classes` against `Window.cls` and `Window.initial_class`, then the lexicon's
  own window-to-app map. Both are compositor fields, the same ones `resolve_window` has
  always selected windows by.
- NEVER by title. A title is written by whoever owns the window (docs/PLAN.md 5.6).
- NEVER by pid. Measured here: `hyprctl -j clients` gives a Chrome PWA and both
  `google-chrome` windows the same pid 1325064, and `/proc/1325064/cmdline` is the bare
  `/opt/google/chrome/chrome`. For Chromium-family applications a pid is not weak
  evidence, it is false evidence, so that signal is dropped rather than weighted.

One note on tiers, because this branch can make an utterance cost less. Going to a window
is tier 0 and starting an application is tier 1, so a match that turns a launch into a
focus LOWERS what an utterance costs, and a window's class is chosen by that window's
owner exactly like its title. That is docs/PLAN.md 5.6 read backwards, so a class match
alone is no longer allowed to answer. `matches()` grades every match: a window is
CONFIRMED only when `lexicon.app_for_window` routes it back to this same trusted
application, and a class-only match is offered as a candidate rather than acted on. The
safe direction is always the launch, because starting a second copy costs a window and
arriving at an impostor costs whatever that impostor is pretending to be.

What that grading does NOT close, stated plainly because the docstring is the only place
it would be recorded: the lexicon's class map is built from the very `wm_classes` this
module matches against, so a window that names a class a trusted `.desktop` file declares
resolves home by construction. A local process that sets its class to "Spotify" while
Spotify is installed is still indistinguishable from Spotify here, because the compositor
offers no identity a window's owner does not write. Closing that needs a field the owner
cannot forge, and the only one on offer is `/proc/<pid>/exe`, which this module refuses
for the measured reason above. Grading catches the cheaper half: a window whose two class
fields name two different applications, and a class no trusted entry claims at all.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from hyprsay.config import Gates
from hyprsay.model import App, DesktopState, Window
from hyprsay.nlu.resolve import LexiconLike


class Prefer(StrEnum):
    """What the words asked for. The verb declares this; it no longer decides."""

    EXISTING = "existing"  # a focus verb: "go to chrome", "show me chrome"
    EITHER = "either"  # a launch verb: "open chrome"
    NEW = "new"  # a launch verb beside a novelty word: "open another chrome"


class Reached(StrEnum):
    LAUNCH = "launch"
    FOCUS = "focus"
    AMBIGUOUS = "ambiguous"  # several windows fit, so the caller's hints decide


@dataclass(frozen=True)
class Reach:
    """The branch, plus the windows the caller needs to carry it out."""

    verdict: Reached
    window: Window | None = None  # set only for FOCUS
    windows: tuple[Window, ...] = ()  # every matched window, most recently used first
    reason: str = ""  # one plain sentence, for the HUD when the caller shows hints


# All three lists are closed, for the reason `understand.DICTATION_OPENERS` is closed:
# similarity scoring over an open vocabulary swallows words it should not. The French
# half is here because the owner speaks French and the cloud recognizer writes it;
# "autre" after a launch verb reads as "another", while "the other one" is a correction
# and is answered by the pick grammar while badges are showing.
LAUNCH_VERBS: frozenset[str] = frozenset({"open", "launch", "start", "run", "fire", "boot"})
FOCUS_VERBS: frozenset[str] = frozenset(
    {"focus", "switch", "go", "jump", "show", "bring", "activate", "raise", "take"}
)
NOVELTY: frozenset[str] = frozenset(
    {
        "new", "another", "second", "third", "extra", "fresh", "more", "additional",
        "nouveau", "nouvelle", "autre", "deuxieme", "troisieme", "seconde", "supplementaire",
    }
)  # fmt: skip

# Indefinite articles, in both languages the owner speaks. "the" is deliberately absent:
# "a second chrome" asks for one more, while "the second chrome" points at one that is
# already there, and a pointing phrase must not turn into a launch.
ARTICLES: frozenset[str] = frozenset({"a", "an", "un", "une"})


def _digit_novelty(words: Sequence[str]) -> bool:
    """A digit standing where a novelty word was spoken.

    `normalize._numbers` rewrites every ordinal to a digit before the grammar runs, so
    "open a second chrome" reaches this module as "open a 2 chrome" and "second",
    "third" and "seconde" in NOVELTY above can never match a real utterance. They were
    dead words from the day this module was written.

    Reading `Parse.raw` instead was the other candidate fix and it was rejected: it
    repairs only the grammar path. `understand._launch_remote` passes `heard.text`,
    which is the normalized utterance, and has no raw string in scope at all, so the
    Jev path would keep the bug. The novelty test has to work on the spelling both paths
    actually carry, which is the normalized one.

    The position is what keeps this closed rather than "any digit anywhere": a digit is
    novelty only immediately after a launch verb or an indefinite article, so "open
    chrome on workspace 3" and "open 1password" are untouched.
    """
    return any(
        word.isdigit() and (before in LAUNCH_VERBS or before in ARTICLES)
        for before, word in zip(words, words[1:], strict=False)
    )


def preference(heard: str | tuple[str, ...] | list[str], workspace: str | None = None) -> Prefer:
    """What the utterance asked for, read from the words it actually used.

    The words, and not a flag on the parse, because `model.Slots` has no room for one and
    the words are already there: `Parse.utterance` records the text a rule matched. A
    parse that recorded no words declares nothing, and declaring nothing is NEW, which is
    what naming `Intent.LAUNCH_APP` meant everywhere before this module existed.

    A named workspace is a placement instruction, and a window that already exists
    somewhere else cannot honour it: "open firefox in a new workspace" asks for a firefox
    there, not a trip to the firefox on workspace 8. So a workspace reads as NEW too.
    """
    if workspace:
        return Prefer.NEW
    ordered = list(heard.split() if isinstance(heard, str) else heard)
    words = set(ordered)
    if not words:
        return Prefer.NEW
    if words & NOVELTY or _digit_novelty(ordered):
        return Prefer.NEW
    if words & FOCUS_VERBS and not words & LAUNCH_VERBS:
        return Prefer.EXISTING
    return Prefer.EITHER


@dataclass(frozen=True)
class Matched:
    """Every window that answers to an application, graded by who vouched for it.

    `confirmed` are the windows the lexicon routes back to this same trusted entry, so
    two readings of the machine agree about them. `every` also holds the windows that
    matched nothing but a class their own owner wrote. Both are most recently used first.
    """

    every: tuple[Window, ...] = ()
    confirmed: tuple[Window, ...] = ()

    @property
    def class_only(self) -> tuple[Window, ...]:
        addresses = {w.address for w in self.confirmed}
        return tuple(w for w in self.every if w.address not in addresses)


def confirms(window: Window, app: App, lexicon: LexiconLike) -> bool:
    """Does the lexicon's own window-to-app map route this window home to `app`?

    This is the second reading, and the only one that is not simply "the class this
    window declares is one of the classes this application declares". It disagrees
    exactly where it matters: a window whose `class` and `initialClass` name two
    different applications resolves to whichever one owns `class`, and a class no
    trusted `.desktop` entry claims resolves to nothing at all.
    """
    owner = lexicon.app_for_window(window)
    return owner is not None and owner.trusted and owner.id == app.id


def matches(app: App, state: DesktopState, lexicon: LexiconLike) -> Matched:
    """The live windows that belong to `app`, graded, most recently used first.

    Exact class matching only. A near match would be a title match wearing a different
    field name, and the windows this returns can lower what an utterance costs.
    """
    classes = {c.casefold() for c in app.wm_classes if c}
    every: list[Window] = []
    confirmed: list[Window] = []
    for window in sorted(state.windows, key=lambda w: w.focus_rank):
        own = {c.casefold() for c in (window.cls, window.initial_class) if c}
        vouched = confirms(window, app, lexicon)
        if not vouched and not own & classes:
            continue
        every.append(window)
        if vouched:
            confirmed.append(window)
    return Matched(tuple(every), tuple(confirmed))


def windows_of(app: App, state: DesktopState, lexicon: LexiconLike) -> tuple[Window, ...]:
    """Every window that answers to `app` at all, whoever vouched for it.

    Kept as the plain question, for callers that only want to know whether anything of
    this application is on screen. Deciding what to DO reads `matches` instead, because
    the grade is the whole safety argument.
    """
    return matches(app, state, lexicon).every


def reach(
    app: App,
    prefer: Prefer,
    state: DesktopState,
    lexicon: LexiconLike,
    gates: Gates | None = None,
) -> Reach:
    """Go to a window of `app`, or start one. The single entry point.

    `gates` is in the signature because every resolver in this package takes one and
    callers pass it positionally. Nothing here reads it, and that is the point: which
    branch to take is a rule over live state, not a score with a threshold under it.

    The order of the tests is load bearing twice over. The already-in-front test comes
    before the single-window test, because with one kitty window and the speaker inside
    it "open a terminal" used to focus the window the speaker was already typing in and
    do nothing at all; the EITHER preference never reached the branch that would have
    read it as a request for the next one. And the grade test comes before both, because
    a window nothing but its own class vouches for may be offered and may never be acted
    on (docs/PLAN.md 5.6).
    """
    if prefer is Prefer.NEW:
        return Reach(Reached.LAUNCH, reason="another one was asked for")
    found = matches(app, state, lexicon)
    if not found.every:
        return Reach(Reached.LAUNCH, reason=f"no {app.name} window is open")
    if prefer is Prefer.EITHER and any(w.address == state.active_address for w in found.every):
        # already looking at one of them, so "open a terminal" said in a terminal is a
        # request for the next one. A focus verb means the opposite and falls through to
        # the tie rule, which takes the most recent window that is NOT the focused one.
        # Asked of every match and not only the vouched-for ones on purpose: a launch is
        # the safe answer, so an unvouched window may push towards it and never away.
        return Reach(
            Reached.LAUNCH, windows=found.every, reason=f"a {app.name} is already in front"
        )
    if not found.confirmed:
        # every window that answers to this name does so by a class its own owner chose,
        # and nothing else agrees. Offering them is fine, because the speaker decides;
        # acting on one would let a local process capture "open spotify" at tier 0 with
        # no badge and no countdown, which is the untrusted-input rule inverted.
        return Reach(
            Reached.AMBIGUOUS,
            windows=found.every,
            reason=f"nothing but its own class says that window is {app.name}, so pick a number",
        )
    if len(found.every) == 1:
        window = found.every[0]
        return Reach(Reached.FOCUS, window, found.every, f"{app.name} is already open")
    return Reach(
        Reached.AMBIGUOUS,
        windows=found.every,
        reason=f"{len(found.every)} {app.name} windows are open, so pick a number",
    )
