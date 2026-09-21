"""The shared vocabulary of hyprsay. Every module speaks these types and nothing else.

Flow: an `Utterance` (audio) becomes a `Transcript` (text), which becomes a `Parse`
(an intent plus slots, from the grammar or from Jev), which is resolved against a
`DesktopState` into a `Decision`. A Decision is the only thing the executor and the
HUD ever see. It says what to do (`Verdict`), with what (`Action`), how sure we are
(`tier`, `reason`), and what else it might have been (`candidates`).

Two rules are encoded here rather than left to convention:

- Everything is frozen. State is replaced, never mutated, so a Decision can never
  drift away from the desktop snapshot it was made against.
- `trusted` travels with every `App` and every `Candidate`. A window title is
  written by whoever owns the window, including a hostile web page. Only trusted
  fields may authorize an action (see docs/PLAN.md section 5.6).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

# --------------------------------------------------------------------------- desktop


@dataclass(frozen=True)
class Window:
    address: str  # always "0x..." form, the only safe dispatch target
    cls: str  # Hyprland `class`
    initial_class: str
    title: str  # UNTRUSTED: set by the window's owner
    workspace_id: int
    workspace_name: str
    monitor: int
    floating: bool = False
    fullscreen: bool = False
    pinned: bool = False
    hidden: bool = False
    pid: int = 0
    # Hyprland's focusHistoryID: 0 is the focused window, 1 the one before it
    focus_rank: int = 99
    at: tuple[int, int] = (0, 0)
    size: tuple[int, int] = (0, 0)


@dataclass(frozen=True)
class Workspace:
    id: int
    name: str
    monitor: str = ""
    windows: int = 0

    @property
    def special(self) -> bool:
        return self.id < 0 or self.name.startswith("special")


@dataclass(frozen=True)
class Monitor:
    id: int
    name: str
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    scale: float = 1.0
    focused: bool = False
    active_workspace_id: int = 0


@dataclass(frozen=True)
class Layer:
    namespace: str
    monitor: str
    level: int  # 0 background, 1 bottom, 2 top, 3 overlay


@dataclass(frozen=True)
class DesktopState:
    windows: tuple[Window, ...] = ()
    workspaces: tuple[Workspace, ...] = ()
    monitors: tuple[Monitor, ...] = ()
    layers: tuple[Layer, ...] = ()
    active_address: str = ""
    active_workspace_id: int = 0
    # fail closed: a state we could not read is a locked state
    locked: bool = True
    provider: str = "hyprlang"  # "hyprlang" | "lua"
    stamp: float = 0.0  # time.monotonic() when the snapshot was taken

    @property
    def focused(self) -> Window | None:
        return self.by_address(self.active_address)

    def by_address(self, address: str) -> Window | None:
        return next((w for w in self.windows if w.address == address), None)


@dataclass(frozen=True)
class App:
    """One installed application, from a .desktop file."""

    id: str  # desktop file id without ".desktop"
    name: str
    generic_name: str = ""
    # a short human phrase derived from Categories/GenericName: "web browser"
    kind: str = ""
    keywords: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    # Exec with field codes removed, split into an argument vector. Never a shell line.
    exec_argv: tuple[str, ...] = ()
    wm_classes: tuple[str, ...] = ()  # StartupWMClass plus the file stem
    # True only for files in root-owned system directories, or approved by hash
    trusted: bool = False
    source: str = ""


# --------------------------------------------------------------------------- language


class Intent(StrEnum):
    FOCUS_WINDOW = "focus_window"
    CLOSE_WINDOW = "close_window"
    MOVE_TO_WORKSPACE = "move_to_workspace"
    SWITCH_WORKSPACE = "switch_workspace"
    FULLSCREEN = "fullscreen"
    TOGGLE_FLOATING = "toggle_floating"
    LAUNCH_APP = "launch_app"
    RESIZE_WINDOW = "resize_window"
    MOVE_WINDOW = "move_window"  # move within the layout, by direction
    FOCUS_DIRECTION = "focus_direction"
    VOLUME = "volume"
    MEDIA = "media"
    TYPE_TEXT = "type_text"  # grammar only, never offered to Jev
    LOCK_SCREEN = "lock_screen"  # grammar only, never offered to Jev
    UNDO = "undo"
    AGAIN = "again"
    CANCEL = "cancel"
    HELP = "help"
    PICK = "pick"  # a bare number while hints or swap badges are showing
    NONE = "none"


# intents Jev is allowed to propose. Everything else is reachable only by exact grammar.
JEV_INTENTS: frozenset[Intent] = frozenset(
    {
        Intent.FOCUS_WINDOW,
        Intent.CLOSE_WINDOW,
        Intent.MOVE_TO_WORKSPACE,
        Intent.SWITCH_WORKSPACE,
        Intent.FULLSCREEN,
        Intent.TOGGLE_FLOATING,
        Intent.LAUNCH_APP,
        Intent.RESIZE_WINDOW,
        Intent.MOVE_WINDOW,
        Intent.FOCUS_DIRECTION,
        Intent.VOLUME,
        Intent.MEDIA,
        Intent.NONE,
    }
)


class Direction(StrEnum):
    LEFT = "left"
    RIGHT = "right"
    UP = "up"
    DOWN = "down"


@dataclass(frozen=True)
class Slots:
    # the words the speaker used for a window or app ("the browser"), verbatim from
    # the normalized utterance; None when nothing was said
    window_ref: str | None = None
    app_ref: str | None = None
    # the speaker pointed instead of naming: "this", "it", "here"
    deictic: bool = False
    # "3", "next", "previous", or "special:<name>"
    workspace: str | None = None
    direction: Direction | None = None
    # 0 tiny .. 4 maximum; None when no size word was used
    amount: int | None = None
    # volume: "up" | "down" | "mute" | "unmute" | "set"; media: "play_pause" | "next" | "previous"
    verb: str | None = None
    number: int | None = None  # a percentage, or the badge number for PICK
    text: str | None = None  # dictated text, cut from the RAW transcript


@dataclass(frozen=True)
class Parse:
    intent: Intent
    slots: Slots = field(default_factory=Slots)
    source: str = "grammar"  # "grammar" | "jev"
    # grammar: 1.0 for an exact parse. jev: min over the answers the intent read.
    confidence: float = 1.0
    utterance: str = ""  # normalized
    raw: str = ""  # as the recognizer produced it


@dataclass(frozen=True)
class Transcript:
    text: str
    backend: str  # "local:parakeet-110m", "gateway:fish-audio/transcribe-1", ...
    ms: float = 0.0
    rescued: bool = False  # True when the cloud was asked after local led nowhere


@dataclass(frozen=True)
class Utterance:
    pcm: bytes  # signed 16 bit little endian mono
    sample_rate: int = 16000
    # the window focused at key DOWN. Typing and deictic targets are pinned to it.
    pinned_address: str = ""
    started: float = 0.0
    ended: float = 0.0

    @property
    def seconds(self) -> float:
        return len(self.pcm) / 2 / self.sample_rate if self.sample_rate else 0.0


# --------------------------------------------------------------------------- decisions


class Verdict(StrEnum):
    ACT = "act"
    ACT_SWAP = "act_swap"  # tier 0 only: act now, show numbered badges to re-target
    HINTS = "hints"  # do not act; show numbered badges and wait for a number
    COUNTDOWN = "countdown"  # tier 2: act after a cancellable countdown
    CONFIRM_KEY = "confirm_key"  # tier 3: act only after a physical key press
    REFUSE = "refuse"  # a rule forbids it; say why
    SUGGEST = "suggest"  # not understood, but here is what was close
    NOTHING = "nothing"  # not addressed to us; stay silent


@dataclass(frozen=True)
class Candidate:
    label: str  # what the badge and the chip show: "Firefox (workspace 2)"
    window: Window | None = None
    app: App | None = None
    score: float = 0.0
    # anchored in the transcript by a TRUSTED field (docs/PLAN.md 5.6)
    corroborated: bool = False


@dataclass(frozen=True)
class Action:
    intent: Intent
    window: Window | None = None
    app: App | None = None
    workspace: str | None = None
    direction: Direction | None = None
    amount: int | None = None
    verb: str | None = None
    number: int | None = None
    text: str | None = None

    def describe(self) -> str:
        """A short human phrase for the HUD chip. Never includes dictated text."""
        what = self.intent.value.replace("_", " ")
        target = self.window.cls if self.window else self.app.name if self.app else ""
        where = f" -> {self.workspace}" if self.workspace else ""
        how = f" {self.direction.value}" if self.direction else f" {self.verb}" if self.verb else ""
        return f"{what}{how} {target}{where}".strip()


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    action: Action | None = None
    candidates: tuple[Candidate, ...] = ()
    tier: int = 0
    reason: str = ""  # one plain sentence; shown in `inspect` and on REFUSE
    suggestions: tuple[str, ...] = ()  # example phrases for SUGGEST
    heard: str = ""


@dataclass(frozen=True)
class Outcome:
    ok: bool
    message: str = ""
    # the Action that undoes this one, when one exists
    inverse: Action | None = None


# --------------------------------------------------------------------------- seams


class Evaluator(Protocol):
    """What the NLU needs from Jev. `hyprsay.jev.JevClient` satisfies it; tests use a fake."""

    async def evaluate(self, state: Any, questions: dict[str, Any]) -> Any: ...


class Recognizer(Protocol):
    name: str

    async def transcribe(self, pcm: bytes, sample_rate: int) -> Transcript: ...


class LockLatch(Protocol):
    """Fail closed: any doubt is `True`."""

    def locked(self) -> bool: ...
