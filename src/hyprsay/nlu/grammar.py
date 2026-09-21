"""The fast path: an exact, table-driven grammar from normalized text to a `Parse`.

A spoken command that the grammar recognizes never touches the network, so this is the
path that makes the latency budget. It is exact on purpose. Every rule is a full match
over the whole utterance; there is no scoring and no partial credit. `None` is the
useful answer for everything else, because `None` is what sends the utterance to Jev,
and a grammar that guesses would take that chance away. Referring words are capped at
six, so ordinary talk that happens to start with a verb does not parse either.

What the grammar does NOT do is decide which window "the browser" is. Referring words
are copied verbatim into `window_ref` or `app_ref` and the resolver matches them
against trusted lexicon fields (docs/PLAN.md 5.6). "focus firefox" is always a
`window_ref`; whether to offer a launch when no such window exists is the resolver's
call, made with desktop state the grammar never sees.

Three rules are about safety, not language:

- Dictated text is reachable only behind a carrier verb ("type", "say", "dictate",
  "write") and is cut from the RAW transcript by token offsets. The normalizer lowercases,
  drops "please" and turns "two" into "2"; none of that may leak into what is typed.
- A variant is the normalizer's guess at a misheard word. A guess may supply a harmless
  verb ("taggle" read as "toggle") but never a disruptive operation: close, type and
  lock need their verb literally in the transcript (PLAN 5.6, tier 2 and 3), and when
  the verb is literal the literal text has already matched, so those intents are simply
  refused from variants. The literal text always wins over a variant: "open firefuck"
  parses with `app_ref` "firefuck", and deciding that this means Firefox is the
  resolver's job, with its own thresholds. A parse that did come from a variant carries
  that variant as `utterance`, so the repair stays visible downstream.
- While badges are showing (`picking=True`) a bare number means a badge, and only
  pick, cancel and undo are live. Anything else said in that window is not a command.

Slot conventions the model leaves open: resize uses `verb` "grow" or "shrink", and
`direction` as the axis (RIGHT is width, DOWN is height, None is both), the signs
Hyprland's resizeactive uses. Volume up or down may carry `number` ("up by 10") or
`amount`. Launch may carry `workspace` ("open firefox on workspace 3").

Cost: about forty anchored regexes, tried in order over the text and up to three
variants. A failed full match usually dies on the first character, so a twelve word
utterance that matches nothing costs tens of microseconds.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from hyprsay.model import Direction, Intent, Parse, Slots
from hyprsay.nlu.normalize import Normalized

# --------------------------------------------------------------------------- fragments

_REF = r"\S+(?: \S+){0,5}"
_REF_LAZY = r"\S+(?: \S+){0,5}?"
_WS = r"(?:workspace|desktop)"
_N = r"[1-9]\d?"
_REL = r"next|previous|prev|prior"
_GO = (
    r"(?:(?:go|switch|change|jump|head|hop|take me|bring me)(?: over)?(?: to)?"
    r"|show(?: me)?|open)"
)
_MOVE = r"(?:move|send|put|throw|shift|push)"
_ONTO = r"(?:over )?(?:to|on|onto|in|into)"
_WS_TARGET = (
    rf"(?:(?:the )?{_WS} (?:number )?(?P<n>{_N})|(?:number )?(?P<n2>{_N})"
    rf"|the (?P<n3>{_N}) {_WS}|(?:the )?(?P<rel>{_REL}) {_WS})"
)
_DIR = r"(?P<dir>left|right|up|down|upwards?|downwards?|above|below)"
_SOUND = r"(?:volume|sound|audio)"
_LAUNCH = r"(?:open up|open|launch|start up|start|run|fire up|boot up)"
_PLAYING = r"(?:music|song|track|audio|media|video|playback|it|this|that)"

_AMOUNTS = {
    "slightly": 0,
    "a tiny bit": 0,
    "a touch": 0,
    "a hair": 0,
    "barely": 0,
    "a bit": 1,
    "a little": 1,
    "a little bit": 1,
    "a tad": 1,
    "somewhat": 1,
    "some": 2,
    "more": 2,
    "moderately": 2,
    "a fair bit": 2,
    "a lot": 3,
    "a whole lot": 3,
    "a bunch": 3,
    "lots": 3,
    "much": 3,
    "way": 3,
    "far": 3,
    "significantly": 3,
    "all the way": 4,
    "as much as possible": 4,
    "to the max": 4,
    "completely": 4,
    "fully": 4,
}
_AMT = "|".join(sorted(_AMOUNTS, key=len, reverse=True))

_DIRECTIONS = {
    "left": Direction.LEFT,
    "right": Direction.RIGHT,
    "up": Direction.UP,
    "upward": Direction.UP,
    "upwards": Direction.UP,
    "above": Direction.UP,
    "down": Direction.DOWN,
    "downward": Direction.DOWN,
    "downwards": Direction.DOWN,
    "below": Direction.DOWN,
}
# (verb, axis). The axis is a Direction: RIGHT is width, DOWN is height, None is both.
_SIZES: dict[str, tuple[str, Direction | None]] = {
    "bigger": ("grow", None),
    "larger": ("grow", None),
    "grow": ("grow", None),
    "enlarge": ("grow", None),
    "expand": ("grow", None),
    "smaller": ("shrink", None),
    "shrink": ("shrink", None),
    "reduce": ("shrink", None),
    "wider": ("grow", Direction.RIGHT),
    "narrower": ("shrink", Direction.RIGHT),
    "thinner": ("shrink", Direction.RIGHT),
    "taller": ("grow", Direction.DOWN),
    "shorter": ("shrink", Direction.DOWN),
}
_RELATIVE = {"next": "next", "previous": "previous", "prev": "previous", "prior": "previous"}

# the speaker pointed instead of naming
_DEICTIC = re.compile(
    r"this|that|it|here"
    r"|(?:(?:the|this|that|my) )?(?:(?:current|active|focused) )?(?:window|app|application)"
    r"|(?:the|this|that) (?:(?:current|active|focused) )?one"
    r"|(?:the )?(?:current|active|focused)"
)
_AFTER_CARRIER = " \t\r\n,:;.-"

# the verb of these must be literally in the transcript, never supplied by a variant
_LITERAL_VERB = frozenset({Intent.CLOSE_WINDOW, Intent.TYPE_TEXT, Intent.LOCK_SCREEN})
_LIVE_WHILE_PICKING = frozenset({Intent.PICK, Intent.CANCEL, Intent.UNDO})

Build = Callable[[dict[str, str]], Slots | None]


@dataclass(frozen=True)
class _Rule:
    intent: Intent
    pattern: re.Pattern[str]
    build: Build
    examples: tuple[str, ...]


def _said(said: dict[str, str], name: str) -> str | None:
    """A capture by name. Alternatives of one pattern number their groups: n, n2, n3."""
    return said.get(name) or said.get(name + "2") or said.get(name + "3")


def _slots(target: str | None = None, number: str = "workspace", **fixed: Any) -> Build:
    """A slot builder. `target` is "window", "app" or None for intents with no target."""

    def build(said: dict[str, str]) -> Slots | None:
        slots: dict[str, Any] = dict(fixed)
        ref = _said(said, "ref")
        pointed = ref is None or _DEICTIC.fullmatch(ref) is not None
        if target == "app":
            if pointed:
                return None
            slots["app_ref"] = ref
        elif target == "window" and pointed:
            slots["deictic"] = True
        elif target == "window":
            slots["window_ref"] = ref
        digits = _said(said, "n")
        if digits and number == "workspace":
            slots["workspace"] = str(int(digits))
        elif digits:
            if int(digits) > 100:
                return None
            slots["number"] = int(digits)
        if rel := _said(said, "rel"):
            slots["workspace"] = _RELATIVE[rel]
        if direction := _said(said, "dir"):
            slots["direction"] = _DIRECTIONS[direction]
        if "size" in said:
            slots["verb"], slots["direction"] = _SIZES[said["size"]]
        if amount := _said(said, "amt"):
            slots["amount"] = _AMOUNTS[amount]
        return Slots(**slots)

    return build


def _rule(intent: Intent, pattern: str, build: Build, *examples: str) -> _Rule:
    return _Rule(intent, re.compile(pattern), build, examples)


_NOTHING = _slots()
_WINDOW = _slots("window")

# Order matters: the first rule that matches wins, so exact phrases come before rules
# that capture free referring words, and "focus <anything>" comes last.
_RULES: tuple[_Rule, ...] = (
    # ----------------------------------------------------------------- session words
    _rule(
        Intent.HELP,
        r"help(?: me)?|what can i (?:say|do)|what can you do"
        r"|what (?:are|were) (?:the|my) (?:commands|options)"
        r"|(?:show|list)(?: me)?(?: the)? (?:help|commands)|commands",
        _NOTHING,
        "what can i say",
        "help",
        "show me the commands",
    ),
    _rule(
        Intent.UNDO,
        r"undo(?: (?:that|it|this|the last (?:one|thing|action|command)))?|nope|go back"
        r"|revert(?: (?:that|it))?|(?:put|take) (?:it|that) back",
        _NOTHING,
        "undo",
        "undo that",
        "nope",
        "go back",
    ),
    _rule(
        Intent.AGAIN,
        r"again|(?:do|run) (?:that|it|this) again|repeat(?: (?:that|it|the last (?:one|command)))?"
        r"|(?:1|one) more time|once more|same again",
        _NOTHING,
        "again",
        "do that again",
        "repeat",
    ),
    _rule(
        Intent.CANCEL,
        r"cancel(?: (?:that|it|this))?|never ?mind|forget (?:it|that)|abort"
        r"|stop(?: (?:that|it|listening))?|scratch that",
        _NOTHING,
        "cancel",
        "never mind",
        "stop",
    ),
    _rule(
        Intent.LOCK_SCREEN,
        r"lock(?: (?:the |my |this )?(?:screen|computer|session|desktop|pc|machine|laptop))?",
        _NOTHING,
        "lock the screen",
        "lock screen",
    ),
    # the builder is replaced in `_try`: dictated text needs the raw transcript
    _rule(
        Intent.TYPE_TEXT,
        r"(?:type|say|dictate|write)(?: .+)?",
        _NOTHING,
        "type hello world",
        "say see you at five",
        "dictate dear team",
    ),
    # ----------------------------------------------------------------- workspaces
    _rule(
        Intent.SWITCH_WORKSPACE,
        rf"(?:{_GO} )?(?:the )?{_WS} (?:number )?(?:to )?(?P<n>{_N})",
        _NOTHING,
        "workspace 3",
        "go to workspace 3",
        "desktop 3",
        "switch to workspace three",
    ),
    _rule(
        Intent.SWITCH_WORKSPACE,
        rf"(?:{_GO} )?(?:the )?(?P<n>{_N}) {_WS}",
        _NOTHING,
        "go to the third workspace",
    ),
    _rule(
        Intent.SWITCH_WORKSPACE,
        rf"(?:{_GO} )?(?:the )?(?P<rel>{_REL}) {_WS}|{_WS} (?P<rel2>{_REL})",
        _NOTHING,
        "next workspace",
        "previous workspace",
        "go to the next desktop",
    ),
    _rule(
        Intent.SWITCH_WORKSPACE,
        rf"(?:go|switch|jump|change)(?: over)? to (?:number )?(?P<n>{_N})",
        _NOTHING,
        "go to 3",
        "switch to four",
    ),
    _rule(
        Intent.MOVE_TO_WORKSPACE,
        rf"{_MOVE} {_ONTO} {_WS_TARGET}",
        _WINDOW,
        "move to workspace 2",
        "send to the next workspace",
    ),
    _rule(
        Intent.MOVE_TO_WORKSPACE,
        rf"{_MOVE} (?P<ref>{_REF_LAZY}) {_ONTO} {_WS_TARGET}",
        _WINDOW,
        "move this to workspace 2",
        "send firefox to 3",
        "put the browser on workspace three",
        "move window to workspace two",
    ),
    # ----------------------------------------------------------------- directions
    _rule(
        Intent.FOCUS_DIRECTION,
        rf"(?:focus|go|switch|move focus|switch focus)(?: to)?(?: the)? {_DIR}",
        _NOTHING,
        "focus left",
        "go to the right",
        "move focus up",
    ),
    _rule(
        Intent.FOCUS_DIRECTION,
        rf"(?:(?:focus|focus on|go to|switch to|show) )?(?:the )?"
        rf"(?:window (?:(?:on|to|at) the )?{_DIR}|(?P<dir2>left|right) window)",
        _NOTHING,
        "window to the right",
        "focus the window on the left",
        "the window below",
    ),
    _rule(
        Intent.MOVE_WINDOW,
        rf"(?:move|push|shift|nudge)(?: over)?(?: to)?(?: the)? {_DIR}",
        _WINDOW,
        "move left",
        "move to the right",
    ),
    _rule(
        Intent.MOVE_WINDOW,
        rf"(?:move|push|shift|nudge) (?P<ref>{_REF_LAZY})(?: over)?(?: to)?(?: the)? {_DIR}",
        _WINDOW,
        "move this left",
        "move firefox to the right",
        "push this window up",
    ),
    # ----------------------------------------------------------------- volume
    _rule(
        Intent.VOLUME,
        rf"(?:set |put |turn |change |make )?(?:the )?{_SOUND}(?: (?:to|at))? (?P<n>\d{{1,3}})"
        r"(?: percent)?",
        _slots(number="number", verb="set"),
        "set volume to 40 percent",
        "volume 40",
    ),
    _rule(
        Intent.VOLUME,
        rf"(?:(?P<amt>{_AMT}) )?(?:(?:turn|crank) (?:it |(?:the )?{_SOUND} )?up(?: the {_SOUND})?"
        rf"|{_SOUND} up|(?:increase|raise) (?:the )?{_SOUND}|(?:make it )?louder)"
        rf"(?: (?P<amt2>{_AMT}))?(?: (?:by )?(?P<n>\d{{1,3}})(?: percent)?)?",
        _slots(number="number", verb="up"),
        "volume up",
        "louder",
        "turn it up a bit",
        "volume up by 10",
    ),
    _rule(
        Intent.VOLUME,
        rf"(?:(?P<amt>{_AMT}) )?(?:turn (?:it |(?:the )?{_SOUND} )?down(?: the {_SOUND})?"
        rf"|{_SOUND} down|(?:decrease|lower|reduce) (?:the )?{_SOUND}"
        r"|(?:make it )?(?:quieter|softer))"
        rf"(?: (?P<amt2>{_AMT}))?(?: (?:by )?(?P<n>\d{{1,3}})(?: percent)?)?",
        _slots(number="number", verb="down"),
        "volume down",
        "quieter",
        "lower the volume a lot",
    ),
    _rule(
        Intent.VOLUME,
        rf"mute(?: (?:it|this|that|(?:the )?(?:{_SOUND}|music|speakers?)))?|silence|{_SOUND} off",
        _slots(verb="mute"),
        "mute",
        "mute the sound",
    ),
    _rule(
        Intent.VOLUME,
        rf"unmute(?: (?:it|this|that|(?:the )?(?:{_SOUND}|music|speakers?)))?"
        rf"|{_SOUND} (?:back )?on",
        _slots(verb="unmute"),
        "unmute",
        "sound back on",
    ),
    # ----------------------------------------------------------------- media
    _rule(
        Intent.MEDIA,
        rf"(?:play pause|play|pause|resume|unpause)(?: (?:the |my |some )?{_PLAYING})?"
        r"|stop (?:the |my )?(?:music|song|track|playback|video)",
        _slots(verb="play_pause"),
        "play",
        "pause",
        "pause the music",
    ),
    _rule(
        Intent.MEDIA,
        r"(?:(?:play|go to|skip to) )?(?:the )?next (?:song|track|video)"
        r"|skip(?: (?:it|this|that|(?:(?:this|that|the) )?(?:song|track|one)))?",
        _slots(verb="next"),
        "next song",
        "skip this song",
    ),
    _rule(
        Intent.MEDIA,
        r"(?:(?:play|go to|go back to) )?(?:the )?(?:previous|prev|last) (?:song|track|video)"
        r"|go back a (?:song|track)",
        _slots(verb="previous"),
        "previous track",
        "play the previous song",
    ),
    # ----------------------------------------------------------------- window state
    _rule(
        Intent.FULLSCREEN,
        r"(?:(?:toggle|go|enter|exit|leave|switch to|go to|make(?: it| this| that)?) )?"
        r"fullscreen(?: mode)?|unfullscreen",
        _WINDOW,
        "fullscreen",
        "toggle full screen",
        "make it fullscreen",
    ),
    _rule(
        Intent.FULLSCREEN,
        rf"(?:make|put|get) (?P<ref>{_REF_LAZY}) (?:go |in |into |to )?fullscreen(?: mode)?",
        _WINDOW,
        "make firefox fullscreen",
    ),
    _rule(
        Intent.FULLSCREEN,
        rf"(?:toggle )?fullscreen (?:on |for )?(?P<ref>{_REF})|maximi[sz]e(?: (?P<ref2>{_REF}))?",
        _WINDOW,
        "fullscreen firefox",
        "maximize this",
        "maximize",
    ),
    _rule(
        Intent.TOGGLE_FLOATING,
        r"(?:toggle )?(?:float|floating|tile|tiled|tiling|unfloat)(?: mode)?",
        _WINDOW,
        "toggle floating",
        "float",
    ),
    _rule(
        Intent.TOGGLE_FLOATING,
        rf"(?:toggle )?(?:float|floating|tile|unfloat)(?: on| for)? (?P<ref>{_REF})",
        _WINDOW,
        "float this",
        "tile it",
        "float the terminal",
    ),
    _rule(
        Intent.TOGGLE_FLOATING,
        rf"(?:make|let|set) (?P<ref>{_REF_LAZY}) (?:float|floating|tile|tiled|tiling)",
        _WINDOW,
        "make this float",
        "make kitty floating",
    ),
    # ----------------------------------------------------------------- resize
    _rule(
        Intent.RESIZE_WINDOW,
        rf"(?:(?:make|get|resize) (?P<ref>{_REF_LAZY}) )?(?:(?P<amt>{_AMT}) )?"
        rf"(?P<size>bigger|larger|smaller|wider|narrower|thinner|taller|shorter)"
        rf"(?: (?P<amt2>{_AMT}))?",
        _WINDOW,
        "make it bigger",
        "wider",
        "narrower",
        "make this a lot smaller",
        "slightly taller",
    ),
    _rule(
        Intent.RESIZE_WINDOW,
        rf"(?P<size>grow|enlarge|expand|shrink|reduce)(?: (?P<amt>{_AMT}))?",
        _WINDOW,
        "shrink",
        "grow a bit",
    ),
    _rule(
        Intent.RESIZE_WINDOW,
        rf"(?P<size>grow|enlarge|expand|shrink|reduce) (?P<ref>{_REF_LAZY})(?: (?P<amt>{_AMT}))?",
        _WINDOW,
        "shrink this a lot",
        "grow the terminal all the way",
    ),
    # ----------------------------------------------------------------- close, launch, focus
    _rule(
        Intent.CLOSE_WINDOW,
        rf"close|(?:close|quit) (?P<ref>{_REF})",
        _WINDOW,
        "close this",
        "close the window",
        "close firefox",
        "quit spotify",
    ),
    _rule(
        Intent.LAUNCH_APP,
        rf"{_LAUNCH} (?P<ref>{_REF_LAZY}) (?:on|in) (?:the )?{_WS} (?:number )?(?P<n>{_N})",
        _slots("app"),
        "open firefox on workspace 3",
    ),
    _rule(
        Intent.LAUNCH_APP,
        rf"{_LAUNCH} (?P<ref>{_REF})",
        _slots("app"),
        "open firefox",
        "launch the file manager",
        "start a terminal",
    ),
    _rule(
        Intent.FOCUS_WINDOW,
        r"(?:focus on|focus|switch to|go to|jump to|show me|show|bring up|take me to"
        rf"|activate|raise) (?P<ref>{_REF})",
        _WINDOW,
        "focus firefox",
        "go to the terminal",
        "switch to kitty",
        "show me spotify",
    ),
)

_PICK = _rule(
    Intent.PICK,
    r"(?:(?:pick|choose|select|take) )?(?:(?:number|option|badge) )?(?:the )?(?P<n>[1-9])"
    r"(?: one)?",
    _slots(number="number"),
    "2",
    "number two",
    "the second one",
)


def _dictated(norm: Normalized) -> Slots | None:
    """The words after the carrier verb, cut from the raw transcript as they were heard."""
    carrier = norm.text.split(" ", 1)[0]
    if norm.tokens and norm.tokens[0].text == carrier:
        start = norm.tokens[0].end
    else:
        # a Normalized built by hand, without tokens
        found = re.search(rf"\b{re.escape(carrier)}\b", norm.raw, re.IGNORECASE)
        if found is None:
            return None
        start = found.end()
    text = norm.raw[start:].lstrip(_AFTER_CARRIER).rstrip()
    # every recognizer ends an utterance with a full stop the speaker never said
    if text.endswith(".") and not text.endswith(".."):
        text = text[:-1].rstrip()
    return Slots(text=text) if text else None


class Grammar:
    """Exact parses only. `None` means "ask Jev", never "probably this"."""

    def __init__(self) -> None:
        self._rules = _RULES
        self._picking = (_PICK, *(r for r in _RULES if r.intent in _LIVE_WHILE_PICKING))

    def parse(self, norm: Normalized, *, picking: bool = False) -> Parse | None:
        rules = self._picking if picking else self._rules
        for text in dict.fromkeys((norm.text, *norm.variants)):
            parsed = self._try(text, norm, rules)
            if parsed is not None:
                return parsed
        return None

    def examples(self, intent: Intent) -> tuple[str, ...]:
        rules = (*self._rules, _PICK)
        return tuple(e for r in rules if r.intent == intent for e in r.examples)

    def intents(self) -> tuple[Intent, ...]:
        return tuple(dict.fromkeys(r.intent for r in (*self._rules, _PICK)))

    def _try(self, text: str, norm: Normalized, rules: tuple[_Rule, ...]) -> Parse | None:
        if not text:
            return None
        literal = text == norm.text
        for rule in rules:
            match = rule.pattern.fullmatch(text)
            if match is None:
                continue
            said = {k: v for k, v in match.groupdict().items() if v is not None}
            slots = _dictated(norm) if rule.intent is Intent.TYPE_TEXT else rule.build(said)
            if slots is None:
                continue
            if not literal and rule.intent in _LITERAL_VERB:
                return None
            return Parse(rule.intent, slots, "grammar", 1.0, utterance=text, raw=norm.raw)
        return None
