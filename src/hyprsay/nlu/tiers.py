"""What an action may do, decided in code. "Jev proposes, the tier function disposes."

A tier is a function of (intent, action, state, cfg), not a constant on the intent
(docs/PLAN.md 5.6). Moving a window to the next workspace on the same monitor is tier 1;
the same verb aimed at a special workspace or another monitor makes the window vanish
from view and is tier 2. Typing into an allowlisted editor is tier 1; typing anywhere
else is tier 2; typing into a terminal is refused outright.

| tier | meaning          | rule                                                         |
| 0    | free to reverse  | corroborated and both formulations agree: act, offer a swap  |
| 1    | reversible       | also needs a wide margin; otherwise blocking hints           |
| 2    | disruptive       | never on Jev alone: verb literally said, explicit or deictic |
|      |                  | target, cancellable countdown, never from a late answer      |
| 3    | session          | grammar only, confirmed by a physical key                    |

Why the verb must be literally present for tier 2: Jev is not deterministic (the pick
flipped 4 times in 30 identical requests on a near-tie), and a recognizer can turn
anything into anything. "close" spoken and recognized as "close" is evidence from the
speaker; `intent = close_window` at P=0.8 is an opinion.

Typing is the dangerous operation (PLAN 5.6 and section 14): the first review found that
a hostile window could take focus and receive dictated text, and that shells execute on
more than Enter. So the target is pinned to the window focused at key DOWN, terminals,
launchers, lock surfaces, auth agents and unknown kinds are refused, and every control
character is stripped, not only newline.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from hyprsay.config import Config
from hyprsay.model import Action, DesktopState, Intent, Verdict, Window

TIER0 = frozenset(
    {
        Intent.FOCUS_WINDOW,
        Intent.SWITCH_WORKSPACE,
        Intent.FOCUS_DIRECTION,
        Intent.HELP,
        # these change nothing on the desktop by themselves
        Intent.CANCEL,
        Intent.PICK,
        Intent.NONE,
    }
)
TIER1 = frozenset(
    {
        Intent.LAUNCH_APP,
        Intent.MOVE_TO_WORKSPACE,  # raised to 2 by context, see `tier`
        Intent.MOVE_WINDOW,
        Intent.RESIZE_WINDOW,
        Intent.TOGGLE_FLOATING,
        Intent.FULLSCREEN,
        Intent.VOLUME,
        Intent.MEDIA,
        # grammar only. The executor re-tiers whatever action they replay (PLAN 5.8).
        Intent.UNDO,
        Intent.AGAIN,
    }
)
TIER2 = frozenset({Intent.CLOSE_WINDOW, Intent.TYPE_TEXT})

# the closed lexicon of words that may authorize a tier 2 action, per intent
LITERAL_VERBS: dict[Intent, frozenset[str]] = {
    Intent.CLOSE_WINDOW: frozenset({"close", "quit", "kill", "exit"}),
    Intent.MOVE_TO_WORKSPACE: frozenset({"move", "send", "put", "bring", "take", "throw"}),
    Intent.TYPE_TEXT: frozenset({"type", "write", "dictate", "enter"}),
}
DESTRUCTIVE_VERBS = LITERAL_VERBS[Intent.CLOSE_WINDOW]

TERMINAL_CLASSES = frozenset(
    {
        "kitty",
        "alacritty",
        "foot",
        "footclient",
        "wezterm",
        "org.wezfurlong.wezterm",
        "konsole",
        "org.kde.konsole",
        "gnome-terminal",
        "gnome-terminal-server",
        "org.gnome.terminal",
        "org.gnome.console",
        "kgx",
        "org.gnome.ptyxis",
        "ptyxis",
        "xterm",
        "uxterm",
        "urxvt",
        "rxvt",
        "st",
        "st-256color",
        "ghostty",
        "com.mitchellh.ghostty",
        "tilix",
        "terminator",
        "xfce4-terminal",
        "lxterminal",
        "qterminal",
        "cool-retro-term",
        "rio",
        "contour",
        "warp",
        "dev.warp.warp",
        "tabby",
        "hyper",
        "blackbox",
        "com.raggesilver.blackbox",
        "yakuake",
        "guake",
        "tmux",
    }
)
# prefixes, matched like hypruse's layer_kind(): "rofi - drun" is still rofi
LAUNCHER_NAMESPACES = (
    "rofi",
    "walker",
    "wofi",
    "fuzzel",
    "anyrun",
    "tofi",
    "bemenu",
    "dmenu",
    "ulauncher",
    "albert",
    "kickoff",
    "sherlock",
    "launcher",
)
LOCK_NAMESPACES = ("hyprlock", "swaylock", "gtklock", "waylock", "lockscreen")
# substrings: auth agents come in many spellings and a miss here types a password aloud
AUTH_MARKERS = ("polkit", "policykit", "pinentry", "keyring", "gcr-prompter", "askpass", "kwallet")
UNKNOWN_KINDS = frozenset({"", "unknown", "application", "other"})


def is_terminal(window: Window, kind: str = "") -> bool:
    classes = {window.cls.lower(), window.initial_class.lower()}
    return bool(classes & TERMINAL_CLASSES) or "terminal" in kind.lower()


def _is_auth(window: Window) -> bool:
    classes = (window.cls.lower(), window.initial_class.lower())
    return any(marker in cls for cls in classes for marker in AUTH_MARKERS)


def grabbing_layer(state: DesktopState, cfg: Config) -> str:
    """The namespace of an open launcher or lock surface, or "" when there is none."""
    extra = tuple(n.lower() for n in cfg.safety.locker_namespaces if n)
    for layer in state.layers:
        namespace = layer.namespace.lower()
        if namespace.startswith(LAUNCHER_NAMESPACES + LOCK_NAMESPACES + extra):
            return layer.namespace
    return ""


# --------------------------------------------------------------------------- the tier


def tier(intent: Intent, action: Action | None, state: DesktopState, cfg: Config) -> int:
    if intent in TIER0:
        return 0
    if intent is Intent.MOVE_TO_WORKSPACE:
        return 1 if action is not None and _stays_in_view(action, state) else 2
    if intent is Intent.TYPE_TEXT:
        return 1 if action is not None and _type_allowlisted(action.window, cfg) else 2
    if intent in TIER1:
        return 1
    if intent in TIER2:
        return 2
    # lock, and anything this table has never heard of: the strictest tier
    return 3


def _type_allowlisted(window: Window | None, cfg: Config) -> bool:
    if window is None:
        return False
    allowed = {c.lower() for c in cfg.safety.type_allow_classes}
    return window.cls.lower() in allowed


def _stays_in_view(action: Action, state: DesktopState) -> bool:
    """True when the destination is an ordinary workspace on the window's own monitor."""
    target = (action.workspace or "").strip().lower()
    if not target or target.startswith("special"):
        return False
    window = action.window or state.focused
    if window is None:
        return False
    if target in {"next", "previous"}:
        number = state.active_workspace_id + (1 if target == "next" else -1)
    elif target.isdigit():
        number = int(target)
    else:
        named = next((w for w in state.workspaces if w.name.lower() == target), None)
        if named is None:
            return False
        number = named.id
    if number < 1:
        return False
    names = {m.id: m.name for m in state.monitors}
    workspace = next((w for w in state.workspaces if w.id == number), None)
    if workspace is not None:
        if workspace.special:
            return False
        return len(names) <= 1 or workspace.monitor == names.get(window.monitor)
    # a workspace that does not exist yet is created on the focused monitor
    focused = next((m for m in state.monitors if m.focused), None)
    return len(names) <= 1 or (focused is not None and focused.id == window.monitor)


# --------------------------------------------------------------------------- the verdict


@dataclass(frozen=True)
class Evidence:
    """Everything the verdict may depend on. Built by the understander, judged here."""

    source: str = "grammar"  # "grammar" | "jev" | "pick"
    # the chosen entity is anchored in the transcript by a TRUSTED field, or the intent
    # names no entity at all
    corroborated: bool = False
    # R2's argmax is R2b's highest boolean, and that boolean clears the presence gate.
    # Trivially true when no window had to be chosen by Jev.
    agree: bool = True
    wide_margin: bool = True
    # the tier 2 verb is literally in the normalized utterance
    verb_said: bool = False
    # the target was named by a trusted field, or pointed at ("this"), or picked by number
    explicit_target: bool = False
    # a title token broke the tie. Titles never authorize tier 2 (PLAN 5.6).
    by_title: bool = False
    late: bool = False
    plausible: int = 1  # how many candidates were plausible, the chosen one included


def verdict(level: int, evidence: Evidence) -> tuple[Verdict, str]:
    """The verdict for a fully formed action, and one plain sentence saying why."""
    if level >= 3:
        if evidence.source != "grammar":
            return Verdict.REFUSE, "session actions are only accepted as exact commands"
        return Verdict.CONFIRM_KEY, "press the confirm key to continue"
    if level == 2:
        return _disruptive(evidence)
    if not evidence.corroborated:
        return Verdict.HINTS, "nothing you said names that target, so pick a number"
    if not evidence.agree:
        return Verdict.HINTS, "two readings of the command disagree, so pick a number"
    if level == 1:
        if not evidence.wide_margin:
            return Verdict.HINTS, "that was a close call, so pick a number"
        return Verdict.ACT, ""
    # tier 0 is free to reverse: act now, and let a number re-target (PLAN 5.6)
    if evidence.plausible > 1:
        return Verdict.ACT_SWAP, "say a number to switch to another candidate"
    return Verdict.ACT, ""


def _disruptive(evidence: Evidence) -> tuple[Verdict, str]:
    if not evidence.verb_said:
        return Verdict.REFUSE, "a disruptive action needs its verb said out loud, like close"
    if evidence.late:
        return Verdict.HINTS, "the answer came late, so nothing disruptive happens by itself"
    if not evidence.explicit_target or evidence.by_title:
        return Verdict.HINTS, "say which window, or pick a number"
    if not (evidence.corroborated and evidence.agree and evidence.wide_margin):
        return Verdict.HINTS, "not sure enough of the target for that, so pick a number"
    return Verdict.COUNTDOWN, "cancel within the countdown to stop this"


def verb_said(intent: Intent, tokens: tuple[str, ...] | list[str]) -> bool:
    return bool(LITERAL_VERBS.get(intent, frozenset()) & set(tokens))


# --------------------------------------------------------------------------- typing


def typing_refusal(window: Window | None, kind: str, state: DesktopState, cfg: Config) -> str:
    """Why dictated text may not go to this window, or "" when it may."""
    if window is None:
        return "there is no window to type into"
    layer = grabbing_layer(state, cfg)
    if layer:
        return f"{layer} is open and would receive the keys"
    if is_terminal(window, kind):
        return "typing into a terminal is not allowed: a shell runs what it receives"
    if _is_auth(window):
        return "typing into an authentication dialog is not allowed"
    if kind.strip().lower() in UNKNOWN_KINDS:
        return f"it is not known what kind of app {window.cls or 'this'} is, so typing is off"
    return ""


def clean_typed_text(text: str, max_chars: int) -> str:
    """Strip every C0 and C1 control, format code point, and Unicode line or paragraph
    separator, then cap. Tab is a control too: in a form it moves focus."""
    kept = "".join(
        " " if unicodedata.category(c) in {"Cc", "Zl", "Zp"} else c
        for c in text
        if unicodedata.category(c) not in {"Cf", "Cs", "Co"}
    )
    return " ".join(kept.split())[:max_chars].rstrip()
