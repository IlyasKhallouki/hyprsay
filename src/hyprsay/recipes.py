"""Named key sequences that reach inside an application, one table per app KIND.

Why this file exists: v1 can focus, move and launch windows, but "open chrome, navigate
to youtube and look up ltt" launched Chrome and dropped every word after it. Reaching
inside a window normally means an accessibility tree, and there is none to rely on here.
Measured on the development machine: the AT-SPI registry will not even activate
(`busctl --address=... org.a11y.atspi.Registry` answers "Could not activate remote peer
... unit failed"), and the only browser window open reports class `google-chrome`, which
publishes no tree at all without `--force-renderer-accessibility` (docs/hypruse-README.md:
"canvas, games, terminals, and Electron/Chrome without a flag expose little or nothing").
What every one of those applications does honour is its keyboard. So an in-app action
here is a short, reviewed, named sequence of keystrokes, not a hunt for a button.

Why the table is keyed by KIND and never by window class or title. This machine has 234
files in /usr/share/applications; 3 of them declare `WebBrowser`, 5 `TerminalEmulator`,
5 `TextEditor` or `IDE`, 2 `FileManager`. Keying on class would need one entry per
browser and would still miss the next browser the owner installs; keying on the kind that
`Lexicon.kind_of` already derives from root-owned `Categories` needs one entry for all of
them. Titles are never consulted: a title is written by whoever owns the window, a
hostile page included (docs/PLAN.md 5.6).

Why this is Python and not a `recipes.toml`. A recipe is policy, not data: each entry
carries a tier and decides what a dictated span is allowed to become. A TOML file beside
the config is user-writable, and anything that can write it could add a tier 0 recipe
that presses ctrl+s, or turn a search into a navigation to a host of its choosing. That
is the same argument `lexicon.py` makes for distrusting user-writable .desktop files, so
the table lives in reviewed code and changes only through a diff.

What a recipe may not do:

- **Type anything a model chose.** Every `TYPE` step carries a `fill` naming the code
  rule that turns the grammar's dictated span into its text, plus a template written
  here. A URL template starts with `https://`, so even a weakened `normalize_host` could
  not make a spoken "javascript colon ..." into the scheme of what gets typed.
- **Press a key into a terminal.** `for_window` offers a terminal nothing but scrolling
  and `render` refuses outright. A shell runs what it receives, and ctrl+s there is XOFF:
  it freezes the terminal and looks exactly like a crash.
- **Decide its own authorization.** `Recipe.tier` is a floor. A recipe that types is
  exactly as dangerous as typing, so the caller raises it with `tier_for` to whatever
  `nlu.tiers.tier(Intent.TYPE_TEXT, ...)` says about that window, and still runs
  `nlu.tiers.typing_refusal` for the checks that need the live desktop.
"""

from __future__ import annotations

import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol
from urllib.parse import quote

from hyprsay.model import Action, Intent, Window
from hyprsay.nlu.tiers import UNKNOWN_KINDS, clean_typed_text, is_terminal

# the same cap the typing rules use (docs/PLAN.md 5.6)
MAX_TEXT_CHARS = 200

# A chord that opens a bar (ctrl+l, ctrl+f) has to be processed by the application
# before the text arrives, and this is the one place where being wrong types the query
# into the page instead of into the bar. The value is [A] until someone measures the
# round trip on a loaded machine; it is deliberately longer than it probably needs to be.
BAR_SETTLE_S = 0.15

# scroll notches. A "sweep" is what one spoken "top" is worth: no chord reaches the top
# of every view (Home moves the caret inside a text field), so scrolling far is the only
# gesture that means the same thing in a browser, a chat log and an image viewer.
SCROLL_NOTCHES = 3
SWEEP_NOTCHES = 30


class RecipeError(Exception):
    """This recipe cannot run, or this span cannot become what the recipe needs.

    The text is one plain sentence, ready for the HUD, like `ops.OpError`.
    """


# --------------------------------------------------------------------------- app kinds

BROWSER = "browser"
EDITOR = "editor"
FILE_MANAGER = "file manager"
TERMINAL = "terminal"
GENERIC = "generic"

# `Lexicon.kind_of(window)` -> the table that applies. A kind that is absent here gets
# the generic table only, and an unknown kind gets only the recipes that press no key.
KIND_TABLE: dict[str, str] = {
    "web browser": BROWSER,
    "code editor": EDITOR,
    "word processor": EDITOR,
    "file manager": FILE_MANAGER,
    "terminal": TERMINAL,
}


# --------------------------------------------------------------------------- the steps


class StepKind(StrEnum):
    FOCUS = "focus"  # the window the utterance was pinned to, through the guarded hypr
    PRESS = "press"  # one chord, in hypruse's `parse_combo` spelling: "ctrl+shift+t"
    TYPE = "type"  # a literal built by code from the dictated span
    SCROLL = "scroll"
    SETTLE = "settle"  # wait for the bar a chord just opened


class Fill(StrEnum):
    """Which code rule turns the dictated span into a `TYPE` step's text.

    There is no value meaning "whatever was said": every typed character passes through
    one of these, and each one is a function in this file that can refuse.
    """

    NONE = "none"  # not a TYPE step, or a TYPE step that has already been rendered
    HOST = "host"  # `normalize_host`: a plausible host, or a refusal
    QUERY = "query"  # percent-encoded into a search URL, never typed as words
    LITERAL = "literal"  # the words themselves, controls stripped, capped


_PLACEHOLDER: dict[Fill, str] = {
    Fill.HOST: "{host}",
    Fill.QUERY: "{q}",
    Fill.LITERAL: "{text}",
}


@dataclass(frozen=True)
class Step:
    kind: StepKind
    keys: str = ""  # PRESS
    text: str = ""  # TYPE: the template before `render`, the literal after it
    fill: Fill = Fill.NONE  # TYPE
    direction: str = ""  # SCROLL: "up" | "down"
    amount: int = 0  # SCROLL
    seconds: float = 0.0  # SETTLE

    def describe(self) -> str:
        """One phrase for the HUD and the journal.

        Dictated text never appears here, for the same reason it never reaches Jev: the
        words are the speaker's, not a command (docs/PLAN.md 7). A rendered TYPE step is
        counted, not quoted.
        """
        if self.kind is StepKind.PRESS:
            return f"press {self.keys}"
        if self.kind is StepKind.TYPE:
            if self.fill is Fill.NONE:
                return f"type {len(self.text)} characters"
            return f"type the {self.fill.value}"
        if self.kind is StepKind.SCROLL:
            return f"scroll {self.direction} {self.amount}"
        if self.kind is StepKind.SETTLE:
            return f"wait {self.seconds:g}s"
        return "focus the window"


def _focus() -> Step:
    return Step(StepKind.FOCUS)


def _press(keys: str) -> Step:
    return Step(StepKind.PRESS, keys=keys)


def _type(fill: Fill, template: str) -> Step:
    return Step(StepKind.TYPE, text=template, fill=fill)


def _scroll(direction: str, amount: int = SCROLL_NOTCHES) -> Step:
    return Step(StepKind.SCROLL, direction=direction, amount=amount)


def _settle() -> Step:
    return Step(StepKind.SETTLE, seconds=BAR_SETTLE_S)


# -------------------------------------------------------------------------- the recipe


@dataclass(frozen=True)
class Recipe:
    name: str
    kind: str  # one of the app kinds above
    steps: tuple[Step, ...]
    tier: int  # a FLOOR; see `tier_for`
    needs_text: bool  # true exactly when a step types the dictated span
    description: str  # one phrase, for the HUD chip and `hyprsay help`

    def __post_init__(self) -> None:
        """Every invariant this file relies on, checked at import.

        These are cheap and they are the reason a reviewer can read one entry of the
        table and know what it can do: a recipe that types is marked and never tier 0, a
        recipe never types a literal of its own devising, and a URL is always https.
        """
        if not 0 <= self.tier <= 3:
            raise ValueError(f"{self.name}: tier {self.tier} is not a tier")
        if not self.steps or self.steps[0].kind is not StepKind.FOCUS:
            raise ValueError(f"{self.name}: a recipe starts by focusing its window")
        typed = [s for s in self.steps if s.kind is StepKind.TYPE]
        for step in typed:
            if step.fill is Fill.NONE:
                raise ValueError(f"{self.name}: code does not type literals of its own")
            if _PLACEHOLDER[step.fill] not in step.text:
                raise ValueError(f"{self.name}: the template has no {step.fill.value} in it")
            if step.fill in (Fill.HOST, Fill.QUERY) and not step.text.startswith("https://"):
                raise ValueError(f"{self.name}: a typed URL is written https:// by code")
        if bool(typed) is not self.needs_text:
            raise ValueError(f"{self.name}: needs_text does not match what the steps type")
        if typed and self.tier < 1:
            raise ValueError(f"{self.name}: typing is never free to reverse")
        if self.kind == TERMINAL and self.presses_keys:
            raise ValueError(f"{self.name}: a terminal runs what it receives")

    @property
    def presses_keys(self) -> bool:
        """True when any step delivers a keystroke. The terminal rule keys on this and
        not on `needs_text`, because a bare chord is a keystroke too."""
        return any(s.kind in (StepKind.PRESS, StepKind.TYPE) for s in self.steps)


# ------------------------------------------------------------------- spoken text rules

# A bare spoken label is ambiguous, so it is looked up rather than guessed. The naive
# rule ("append .com") is wrong for three of the entries below already, and for a word
# that is not a site at all it would silently navigate somewhere real: "open ltt" would
# fetch ltt.com instead of searching. Anything not in this table has to be said in full.
KNOWN_SITES: dict[str, str] = {
    "youtube": "youtube.com",
    "github": "github.com",
    "gitlab": "gitlab.com",
    "google": "google.com",
    "gmail": "mail.google.com",
    "reddit": "reddit.com",
    "wikipedia": "wikipedia.org",
    "twitch": "twitch.tv",
    "amazon": "amazon.com",
    "netflix": "netflix.com",
    "stackoverflow": "stackoverflow.com",
    "chatgpt": "chatgpt.com",
    "claude": "claude.ai",
}

# what a recognizer writes when the speaker dictates punctuation
_SPOKEN_PUNCTUATION: dict[str, str] = {
    "dot": ".",
    "period": ".",
    "dash": "-",
    "hyphen": "-",
}
_GLUE = frozenset(_SPOKEN_PUNCTUATION.values())
# a word that can only mean a path, a query or a scheme. "colon" is the important one:
# `javascript:` typed into an address bar runs script in the page that is already open,
# with that page's cookies, so a sentence spoken by anyone in the room (or by a video)
# would become code running as the user. `data:` and `file:` are the same shape of
# problem. None of them is a site, so none of them is ever assembled here.
_NOT_A_HOST_WORDS = frozenset({"colon", "slash", "backslash", "question", "hash", "percent", "at"})
# the same list as characters, for a recognizer that wrote the punctuation itself
_NOT_A_HOST_CHARS = ":/\\?#%@"

# The engine, rather than the address bar. An omnibox decides for itself whether what it
# was given is a host or a query, so a search for "ltt.com" would navigate instead of
# searching; percent-encoding the words into the engine's own URL removes the guess, and
# the query cannot escape the parameter it is encoded into.
SEARCH_URLS: dict[str, str] = {
    "": "https://duckduckgo.com/?q={q}",
    "youtube": "https://www.youtube.com/results?search_query={q}",
    "wikipedia": "https://en.wikipedia.org/w/index.php?search={q}",
}


def normalize_host(spoken: str) -> str:
    """The host a dictated span means, or a refusal.

    The rule, in full: strip control characters, lower case, turn the spoken punctuation
    words into their punctuation ("github dot com" -> "github.com"), and refuse anything
    that is left with a space in it, because a phrase is a search and not an address
    ("linus tech tips" must not become linustechtips.com). A single label with no dot is
    looked up in `KNOWN_SITES` ("youtube" -> youtube.com) and refused when it is not
    there. What survives must be plain ASCII and shaped like a domain.
    """
    text = unicodedata.normalize("NFKC", clean_typed_text(spoken, MAX_TEXT_CHARS)).strip().lower()
    if not text:
        raise RecipeError("nothing was said to open")
    words = text.split()
    if any(w in _NOT_A_HOST_WORDS for w in words) or any(c in text for c in _NOT_A_HOST_CHARS):
        raise RecipeError("only a plain site name can be opened, never a scheme or a path")
    host = _glue(words)
    if " " in host:
        raise RecipeError("that sounded like a phrase rather than an address, so try a search")
    if "." not in host:
        known = KNOWN_SITES.get(host)
        if known is None:
            raise RecipeError(f"{host} is not a site I know: say it in full, like github dot com")
        host = known
    _check_host(host)
    return host


def _glue(words: list[str]) -> str:
    """Join spoken words into one candidate, keeping a space wherever nothing said to
    close it up. The space is what later tells a phrase apart from an address."""
    out: list[str] = []
    for word in words:
        piece = _SPOKEN_PUNCTUATION.get(word, word)
        if out and piece not in _GLUE and out[-1] not in _GLUE:
            out.append(" ")
        out.append(piece)
    return "".join(out).strip()


def _check_host(host: str) -> None:
    # nothing here checks the 253 characters a host may have: the span was already
    # capped at 200 by `clean_typed_text` and gluing only ever shortens it, so such a
    # test could never fire, and a guard that cannot fire is worse than none
    if not host.isascii():
        raise RecipeError("an address spelled with another alphabet is refused")
    labels = host.split(".")
    for label in labels:
        if label.startswith("xn--"):
            # punycode is how a homograph arrives in ASCII, and no one dictates it
            raise RecipeError("an encoded address is refused, so say the plain name")
        if not 1 <= len(label) <= 63 or label.startswith("-") or label.endswith("-"):
            raise RecipeError("that does not look like a web address")
        if any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in label):
            raise RecipeError("that does not look like a web address")
    if len(labels[-1]) < 2 or not labels[-1].isalpha():
        # also what refuses a bare IP address: 192.168.1.1 ends in digits
        raise RecipeError("that address has no domain ending, like com or org")


def search_url(query: str, site: str = "") -> str:
    """A search for `query` on `site` ("" is the default engine), percent-encoded."""
    template = SEARCH_URLS.get(site)
    if template is None:
        raise RecipeError(f"there is no search recipe for {site}")
    return _fill_query(template, query)


def _fill_query(template: str, spoken: str) -> str:
    clean = clean_typed_text(spoken, MAX_TEXT_CHARS)
    if not clean:
        raise RecipeError("there was nothing to search for")
    # safe="" so a slash or a colon inside the words is encoded too: the result has to be
    # one parameter, not a path the engine might read as something else
    return template.replace("{q}", quote(clean, safe=""))


def _fill_literal(template: str, spoken: str) -> str:
    clean = clean_typed_text(spoken, MAX_TEXT_CHARS)
    if not clean:
        raise RecipeError("there was nothing to type")
    return template.replace("{text}", clean)


# --------------------------------------------------------------------------- the table

_URL = "https://{host}/"

_BROWSER: tuple[Recipe, ...] = (
    # ctrl+t focuses the address bar of the new tab by itself; ctrl+l after it is what
    # makes that true whatever the owner's new-tab page is, so the URL cannot land in a
    # search box that page happens to draw.
    Recipe(
        name="open_url",
        kind=BROWSER,
        steps=(
            _focus(),
            _press("ctrl+t"),
            _settle(),
            _press("ctrl+l"),
            _settle(),
            _type(Fill.HOST, _URL),
            _press("enter"),
        ),
        tier=1,
        needs_text=True,
        description="open a site in a new tab",
    ),
    # the same keys aimed at the tab you are looking at. It is a tier higher than
    # open_url for one reason: the page that is there goes away, and it may be a form
    # halfway filled in. A new tab loses nothing, which is why it is the default.
    Recipe(
        name="go_to_url",
        kind=BROWSER,
        steps=(
            _focus(),
            _press("ctrl+l"),
            _settle(),
            _type(Fill.HOST, _URL),
            _press("enter"),
        ),
        tier=2,
        needs_text=True,
        description="leave this page and go to a site in the tab you are on",
    ),
    Recipe(
        name="search_web",
        kind=BROWSER,
        steps=(
            _focus(),
            _press("ctrl+t"),
            _settle(),
            _press("ctrl+l"),
            _settle(),
            _type(Fill.QUERY, SEARCH_URLS[""]),
            _press("enter"),
        ),
        tier=1,
        needs_text=True,
        description="search the web in a new tab",
    ),
    Recipe(
        name="search_youtube",
        kind=BROWSER,
        steps=(
            _focus(),
            _press("ctrl+t"),
            _settle(),
            _press("ctrl+l"),
            _settle(),
            _type(Fill.QUERY, SEARCH_URLS["youtube"]),
            _press("enter"),
        ),
        tier=1,
        needs_text=True,
        description="search YouTube in a new tab",
    ),
    Recipe(
        name="search_wikipedia",
        kind=BROWSER,
        steps=(
            _focus(),
            _press("ctrl+t"),
            _settle(),
            _press("ctrl+l"),
            _settle(),
            _type(Fill.QUERY, SEARCH_URLS["wikipedia"]),
            _press("enter"),
        ),
        tier=1,
        needs_text=True,
        description="search Wikipedia in a new tab",
    ),
    # no Enter: the find bar highlights as the text arrives, and Enter would jump to the
    # next match. Escape is the undo, which is why this stays at tier 1 while it types.
    Recipe(
        name="find",
        kind=BROWSER,
        steps=(_focus(), _press("ctrl+f"), _settle(), _type(Fill.LITERAL, "{text}")),
        tier=1,
        needs_text=True,
        description="find words on this page",
    ),
    Recipe(
        name="new_tab",
        kind=BROWSER,
        steps=(_focus(), _press("ctrl+t")),
        tier=1,
        needs_text=False,
        description="open a new tab",
    ),
    # ctrl+shift+t reopens it, but a tab can hold a form nobody can retype, so this
    # earns its countdown the same way close_window does
    Recipe(
        name="close_tab",
        kind=BROWSER,
        steps=(_focus(), _press("ctrl+w")),
        tier=2,
        needs_text=False,
        description="close this tab",
    ),
    Recipe(
        name="next_tab",
        kind=BROWSER,
        steps=(_focus(), _press("ctrl+pgdn")),
        tier=0,
        needs_text=False,
        description="show the next tab",
    ),
    Recipe(
        name="previous_tab",
        kind=BROWSER,
        steps=(_focus(), _press("ctrl+pgup")),
        tier=0,
        needs_text=False,
        description="show the previous tab",
    ),
    Recipe(
        name="back",
        kind=BROWSER,
        steps=(_focus(), _press("alt+left")),
        tier=1,
        needs_text=False,
        description="go back a page",
    ),
    Recipe(
        name="forward",
        kind=BROWSER,
        steps=(_focus(), _press("alt+right")),
        tier=1,
        needs_text=False,
        description="go forward a page",
    ),
    # a reload of a page that came from a form re-sends it, which is why this is not
    # filed with the tier 0 view changes below
    Recipe(
        name="reload",
        kind=BROWSER,
        steps=(_focus(), _press("ctrl+r")),
        tier=1,
        needs_text=False,
        description="reload this page",
    ),
    # ctrl+equal rather than ctrl+plus: plus needs shift on a US layout, equal does not,
    # and every browser accepts it
    Recipe(
        name="zoom_in",
        kind=BROWSER,
        steps=(_focus(), _press("ctrl+equal")),
        tier=0,
        needs_text=False,
        description="make this page bigger",
    ),
    Recipe(
        name="zoom_out",
        kind=BROWSER,
        steps=(_focus(), _press("ctrl+minus")),
        tier=0,
        needs_text=False,
        description="make this page smaller",
    ),
    Recipe(
        name="zoom_reset",
        kind=BROWSER,
        steps=(_focus(), _press("ctrl+0")),
        tier=0,
        needs_text=False,
        description="put this page back to its normal size",
    ),
)

_EDITOR: tuple[Recipe, ...] = (
    Recipe(
        name="find",
        kind=EDITOR,
        steps=(_focus(), _press("ctrl+f"), _settle(), _type(Fill.LITERAL, "{text}")),
        tier=1,
        needs_text=True,
        description="find words in this document",
    ),
    # Save is tier 2, and it is the entry worth arguing about. It looks like the safest
    # thing on this page: one chord, no text, and the editor's own undo still holds the
    # buffer. What it actually does is overwrite the file on disk, and THAT has no undo
    # inside the editor: the previous contents are gone unless the app keeps backups.
    # Voice is unauthenticated, so "save" spoken by anyone in the room, or by a video,
    # would commit whatever is on screen at that moment, including the paragraph the
    # owner was about to discard. A countdown costs a second and is the whole defence.
    # It is also why this recipe is per kind: ctrl+s in a browser opens a file dialog,
    # and in a terminal it is XOFF, which freezes it.
    Recipe(
        name="save",
        kind=EDITOR,
        steps=(_focus(), _press("ctrl+s")),
        tier=2,
        needs_text=False,
        description="save this file over the one on disk",
    ),
)

_FILE_MANAGER: tuple[Recipe, ...] = (
    # no Enter here either, and for a sharper reason than in a browser: in a file manager
    # Enter opens whatever the search selected, which runs it
    Recipe(
        name="find",
        kind=FILE_MANAGER,
        steps=(_focus(), _press("ctrl+f"), _settle(), _type(Fill.LITERAL, "{text}")),
        tier=1,
        needs_text=True,
        description="find a file in this folder",
    ),
)

_GENERIC: tuple[Recipe, ...] = (
    # offered for any window whose kind is known and has no table of its own. ctrl+f is
    # find in nearly every toolkit, but "nearly" is why this one is a tier above the
    # browser's: we do not know this application, so the countdown is the review.
    Recipe(
        name="find",
        kind=GENERIC,
        steps=(_focus(), _press("ctrl+f"), _settle(), _type(Fill.LITERAL, "{text}")),
        tier=2,
        needs_text=True,
        description="find words in this window",
    ),
    Recipe(
        name="scroll_down",
        kind=GENERIC,
        steps=(_focus(), _scroll("down")),
        tier=0,
        needs_text=False,
        description="scroll down",
    ),
    Recipe(
        name="scroll_up",
        kind=GENERIC,
        steps=(_focus(), _scroll("up")),
        tier=0,
        needs_text=False,
        description="scroll up",
    ),
    Recipe(
        name="to_top",
        kind=GENERIC,
        steps=(_focus(), _scroll("up", SWEEP_NOTCHES)),
        tier=0,
        needs_text=False,
        description="scroll to the top",
    ),
    Recipe(
        name="to_bottom",
        kind=GENERIC,
        steps=(_focus(), _scroll("down", SWEEP_NOTCHES)),
        tier=0,
        needs_text=False,
        description="scroll to the bottom",
    ),
)


def _table(recipes: tuple[Recipe, ...]) -> dict[str, Recipe]:
    return {recipe.name: recipe for recipe in recipes}


RECIPES: dict[str, dict[str, Recipe]] = {
    BROWSER: _table(_BROWSER),
    EDITOR: _table(_EDITOR),
    FILE_MANAGER: _table(_FILE_MANAGER),
    # deliberately empty, and not missing: a terminal gets the pointer-only generic
    # recipes and nothing else. See the module docstring.
    TERMINAL: {},
    GENERIC: _table(_GENERIC),
}

# The words that must be literally in the utterance before a tier 2 recipe may run, the
# same rule `nlu.tiers.LITERAL_VERBS` applies to disruptive intents and for the same
# reason: "save" spoken and recognized as "save" is evidence from the speaker, while a
# model's opinion that this is a save is not. Keyed by recipe NAME, because the name is
# what a chained utterance selects.
SPOKEN_VERBS: dict[str, frozenset[str]] = {
    "go_to_url": frozenset({"go", "navigate", "open", "visit"}),
    "close_tab": frozenset({"close", "quit"}),
    "save": frozenset({"save"}),
    "find": frozenset({"find", "search", "look"}),
}


def verb_said(recipe: Recipe, tokens: tuple[str, ...] | list[str]) -> bool:
    return bool(SPOKEN_VERBS.get(recipe.name, frozenset()) & set(tokens))


# ------------------------------------------------------------------------- the offering


def recipe_kind(kind: str) -> str:
    """The table that applies to a lexicon kind, or "" when none does."""
    return KIND_TABLE.get(kind.strip().lower(), "")


def for_window(window: Window, kind: str) -> list[Recipe]:
    """Every recipe that may be offered for this window, most specific first.

    `kind` is what `Lexicon.kind_of(window)` returned, which is "" for an app whose
    .desktop file is not trusted. That case and a terminal get the same treatment: the
    recipes that press no key at all. An unknown kind is the same condition that turns
    typing off in `tiers.typing_refusal`, and the reasoning carries over to chords, since
    what ctrl+s does in an application nobody has identified is not knowable.
    """
    offered: list[Recipe] = []
    seen: set[str] = set()
    for table in (recipe_kind(kind), GENERIC):
        for recipe in RECIPES.get(table, {}).values():
            # a kind's own entry shadows the generic one of the same name: both do the
            # same thing, and the specific one carries the right tier and description
            if recipe.name in seen:
                continue
            seen.add(recipe.name)
            offered.append(recipe)
    if _keys_refused(window, kind):
        return [recipe for recipe in offered if not recipe.presses_keys]
    return offered


def _keys_refused(window: Window, kind: str) -> bool:
    return is_terminal(window, kind) or kind.strip().lower() in UNKNOWN_KINDS


def refusal_for(recipe: Recipe, window: Window, kind: str) -> str:
    """Why this recipe may not run on this window, or "" when it may.

    This covers only what can be decided from the window and its kind. A recipe that
    types must still go through `nlu.tiers.typing_refusal`, which is the one that knows
    about a launcher grabbing the keyboard and about auth dialogs, and its tier must
    still go through `tier_for`.
    """
    if recipe.presses_keys and is_terminal(window, kind):
        return "a terminal runs what it receives, so no key sequence is sent to one"
    if recipe.presses_keys and kind.strip().lower() in UNKNOWN_KINDS:
        return f"it is not known what kind of app {window.cls or 'this'} is, so keys are off"
    wanted = recipe_kind(kind)
    if recipe.kind not in (GENERIC, wanted):
        return f"{recipe.name.replace('_', ' ')} is for a {recipe.kind}, and this is not one"
    return ""


def tier_for(recipe: Recipe, typing_tier: int = 0) -> int:
    """The tier the caller must authorize against.

    A recipe that types is exactly as dangerous as typing, so its tier is never below
    what `nlu.tiers.tier(Intent.TYPE_TEXT, action, state, cfg)` returned for the same
    window. The recipe's own tier is a floor, never a ceiling.
    """
    return max(recipe.tier, typing_tier) if recipe.needs_text else recipe.tier


# --------------------------------------------------------------------------- rendering


def render(recipe: Recipe, text: str | None, window: Window) -> list[Step]:
    """The steps to perform, with every template filled in by code.

    `text` is the grammar's dictated span and nothing else: Jev never sees a recipe and
    never supplies one of these words. What comes back carries no placeholder, so a step
    cannot be filled twice or filled by whoever runs it.

    The terminal check is repeated here even though `for_window` already made it. This is
    the last line before the keys go out, and the executor may hold a recipe chosen a
    moment ago against a window that has since been replaced by a terminal.
    """
    if recipe.presses_keys and is_terminal(window):
        raise RecipeError("a terminal runs what it receives, so no key sequence is sent to one")
    if not recipe.needs_text:
        # a recipe with no TYPE step has nowhere to put a span, so any words that came
        # with it are simply not performed
        return list(recipe.steps)
    spoken = text or ""
    return [
        replace(step, text=_filled(step, spoken), fill=Fill.NONE)
        if step.kind is StepKind.TYPE
        else step
        for step in recipe.steps
    ]


def _filled(step: Step, spoken: str) -> str:
    if step.fill is Fill.HOST:
        return step.text.replace("{host}", normalize_host(spoken))
    if step.fill is Fill.QUERY:
        return _fill_query(step.text, spoken)
    if step.fill is Fill.LITERAL:
        return _fill_literal(step.text, spoken)
    raise RecipeError("that step has nothing to type")


# --------------------------------------------------------------------------- performing


class Keys(Protocol):
    """What `hyprsay.inapp` provides. A test passes a fake with the same names."""

    def press(self, keys: str, window: str = ...) -> object: ...

    def scroll(self, direction: str, amount: int, window: str = ...) -> object: ...


def execute(
    steps: list[Step],
    window: Window,
    keys: Keys | None = None,
    focus: Callable[[Window], object] | None = None,
    typist: Callable[[str, Window], object] | None = None,
) -> list[str]:
    """Perform rendered steps against one window. Returns a line per step, for the
    journal, with no dictated text in it.

    Focusing and typing do not go through `inapp`: they go through the operations that
    already carry their guards. `ops` sanitizes the text again and passes `window=`, which
    is what makes the inherited keyboard guard REFUSE instead of note when a lock screen
    or a launcher has the keyboard (docs/PLAN.md 5.6). Chords and scrolling have no such
    operation, which is what `inapp` is for.
    """
    hands = keys if keys is not None else _inapp()
    focus = focus or _focus_through_ops
    typist = typist or _type_through_ops
    done: list[str] = []
    for step in steps:
        if step.fill is not Fill.NONE:
            raise RecipeError("that step still needs its text, so the recipe was never rendered")
        if step.kind is StepKind.FOCUS:
            focus(window)
        elif step.kind is StepKind.PRESS:
            # press() only BUILDS the gesture; perform() is what sends it. Calling the
            # constructor alone meant the bar a chord was meant to open never opened and
            # the text landed in whatever had focus, which for "navigate to youtube"
            # is the page rather than the address bar.
            hands.perform(hands.press(step.keys, window=window.address), window)
        elif step.kind is StepKind.TYPE:
            typist(step.text, window)
        elif step.kind is StepKind.SCROLL:
            hands.perform(hands.scroll(step.direction, step.amount, window=window.address), window)
        elif step.kind is StepKind.SETTLE:
            # this runs on the executor's single worker thread, the same one a launch
            # already blocks for two seconds, so a blocking wait here costs nothing new
            time.sleep(step.seconds)
        done.append(step.describe())
    return done


def _inapp() -> Keys:
    # imported here and not at the top because it is written in parallel with this file,
    # and because a recipe can be read, offered and rendered with no input backend at all
    from hyprsay import inapp

    return inapp


def _through_executor(action: Action, window: Window) -> None:
    """Every write a recipe makes goes where every other write goes.

    Calling `Operation.perform` directly reached the keyboard with none of the checks the
    executor makes first: the window still being the one focused when the key went down,
    the session not having locked since, the read-only gate, and the configured limit on
    how much may be typed. A recipe must not be a second, unguarded way to type.
    """
    from hyprsay.executor import current

    executor = current()
    if executor is None:
        raise RecipeError("recipes can only run inside the engine, which owns the guards")
    outcome = executor.execute(action, pinned_address=window.address)
    if not outcome.ok:
        raise RecipeError(outcome.message)


def _focus_through_ops(window: Window) -> None:
    _through_executor(Action(Intent.FOCUS_WINDOW, window=window), window)


def _type_through_ops(text: str, window: Window) -> None:
    _through_executor(Action(Intent.TYPE_TEXT, window=window, text=text), window)
