"""The engine's half of the HUD protocol: NDJSON over one Unix socket.

The HUD is a separate process because PyGObject exists only as a system package and
the speech model only as a pip wheel, so the two cannot share an interpreter
(docs/PLAN.md section 4). That makes the HUD something that can be absent, slow,
restarting or a different program altogether (a Quickshell frontend is planned), and
the rules here follow from that:

- The ENGINE is the server. It owns `$XDG_RUNTIME_DIR/hyprsay/hud.sock`, mode 0600 in a
  0700 directory, and takes exactly one client whose SO_PEERCRED uid is its own. What
  goes over this socket is what the speaker just said, so nobody else may read it.
- The client's first line must be `{"t":"hello","proto":1}`. Anything else is answered
  with `{"t":"refused","reason":...}`, closed, logged, and kept in `last_refusal` for
  `hyprsay doctor`.
- `send()` never raises and never waits. No HUD, a dead HUD, a wedged HUD, a malformed
  message: all of them end in a dropped message and a log line, never in a failed
  voice command.
- The engine owns the state machine. Every message is checked against `next_state`
  before it is written, so a frontend may simply draw what it is told.

Wire format, engine to HUD, one JSON object per line:

    {"t":"state","state":"hints","text":"focus firefox","chip":"say a number",
     "level":0.0,"badges":[{"n":1,"label":"Firefox","x":7,"y":7,"w":950,"h":1066}],
     "countdown_ms":0,"suggestions":[],"ttl_ms":4000}

A message that names a NEW state is always complete. A message that names the CURRENT
state is an update and may carry only what changed; the level meter is exactly that,
`{"t":"state","state":"hearing","level":0.42}`, thirty times a second at most.
Badge rectangles are Hyprland global logical pixels, the same space as `Window.at`. A
badge with `w == 0` is a candidate that is not on screen (another workspace, or an app
that is not running): it keeps its number, because the number is what the speaker says,
but there is no rectangle to draw it over. `ttl_ms` asks the HUD to hide by itself after
that long; 0 means stay until told. `countdown_ms == 0` in the countdown state means
"waiting for a key, no timer" (tier 3, docs/PLAN.md 5.6).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
import stat
import struct
import time
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from .config import HUD
from .model import Candidate, Decision, DesktopState, Intent, Verdict, Window

log = logging.getLogger(__name__)

PROTO = 1
RUNTIME_SUBDIR = "hyprsay"
SOCKET_NAME = "hud.sock"

STATES = (
    "hidden",
    "hearing",
    "thinking",
    "still_thinking",
    "heard",
    "swap",
    "hints",
    "countdown",
    "refused",
    "suggest",
    "help",
)

# Reachable from anywhere: the engine can always give up, a key press always starts a
# new utterance, and a refusal (locked, no microphone, internal error) can come at any time.
ALWAYS = frozenset({"hidden", "hearing", "refused"})
# what an utterance can end in
RESULTS = frozenset({"heard", "swap", "hints", "countdown", "suggest", "help"})
# A state may always repeat itself: that is how an update is sent.
TRANSITIONS: dict[str, frozenset[str]] = {
    # `hyprsay help` from a shell opens the overlay without an utterance
    "hidden": frozenset({"help"}),
    # the grammar can answer at key release, with no thinking in between
    "hearing": RESULTS | {"thinking"},
    "thinking": RESULTS | {"still_thinking"},
    "still_thinking": RESULTS,
    "heard": frozenset(),
    # a spoken number re-targets the action that already ran
    "swap": frozenset({"heard"}),
    "hints": frozenset({"heard"}),
    # the countdown ran out, or the confirming tap came, and the action ran
    "countdown": frozenset({"heard", "swap"}),
    "refused": frozenset(),
    "suggest": frozenset(),
    "help": frozenset(),
}
# The speaker answers badges by pressing the key and saying a number, so the badges
# have to stay up through that next utterance until it produces a result of its own.
CARRIES_BADGES = frozenset({"hearing", "thinking", "still_thinking"})

LEVEL_HZ = 30
# a single spoken digit picks a badge
MAX_BADGES = 9
MAX_SUGGESTIONS = 8
TEXT_CHARS = 160
CHIP_CHARS = 60
LABEL_CHARS = 48

HEARD_TTL_MS = 1500
REFUSED_TTL_MS = 2500
SUGGEST_TTL_MS = 6000
HELP_TTL_MS = 12000
CONFIRM_TEXT = "tap the key to confirm"
# A HELP decision carries no examples of its own (the understander leaves `suggestions`
# empty for bare intents), and an empty help overlay would be the dead end PLAN 6 forbids.
HELP_EXAMPLES = (
    "focus firefox",
    "close this",
    "move this to workspace 3",
    "workspace 2",
    "open terminal",
    "fullscreen",
    "volume up",
    "undo",
)

HELLO_TIMEOUT_S = 2.0
LINE_LIMIT = 4096
# a HUD that stopped reading (SIGSTOP, a hung main loop) must not grow our memory
BACKLOG_LIMIT = 256 * 1024

_DEFAULTS: dict[str, Any] = {
    "text": "",
    "chip": "",
    "level": 0.0,
    "badges": [],
    "countdown_ms": 0,
    "suggestions": [],
    "ttl_ms": 0,
}
_LEVEL_KEYS = frozenset({"t", "state", "level"})


class HudError(OSError):
    """The socket could not be set up safely. The message is a plain sentence."""


def socket_path(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    runtime = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime) / RUNTIME_SUBDIR / SOCKET_NAME


# --------------------------------------------------------------------------- messages


def _clean(value: Any, limit: int) -> str:
    """One displayable line. Labels can carry a window title, which a hostile page
    controls: no control or format characters (newlines, bidi overrides), bounded length."""
    text = "".join(
        " " if ch in "\n\r\t" else ch
        for ch in str(value)
        if ch in "\n\r\t" or unicodedata.category(ch) not in ("Cc", "Cf")
    )
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "\u2026"


def _int(value: Any, floor: int | None = 0) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return number if floor is None else max(floor, number)


def _level(value: Any) -> float:
    try:
        level = float(value)
    except (TypeError, ValueError):
        return 0.0
    # NaN fails both comparisons and would otherwise reach json.dumps as a bare NaN
    return round(min(1.0, max(0.0, level)), 3) if level == level else 0.0


def _badge(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping) or _int(raw.get("n")) < 1:
        return None
    return {
        "n": _int(raw.get("n")),
        "label": _clean(raw.get("label", ""), LABEL_CHARS),
        "x": _int(raw.get("x"), floor=None),
        "y": _int(raw.get("y"), floor=None),
        "w": _int(raw.get("w")),
        "h": _int(raw.get("h")),
    }


def _field(name: str, value: Any) -> Any:
    if name == "text":
        return _clean(value, TEXT_CHARS)
    if name == "chip":
        return _clean(value, CHIP_CHARS)
    if name == "level":
        return _level(value)
    if name == "badges":
        items = value if isinstance(value, list | tuple) else ()
        return [b for b in map(_badge, items) if b is not None][:MAX_BADGES]
    if name == "suggestions":
        items = value if isinstance(value, list | tuple) else ()
        return [_clean(s, CHIP_CHARS) for s in items if str(s).strip()][:MAX_SUGGESTIONS]
    return _int(value)


def state_message(
    state: str,
    *,
    text: str = "",
    chip: str = "",
    level: float = 0.0,
    badges: Iterable[Mapping[str, Any]] = (),
    countdown_ms: int = 0,
    suggestions: Iterable[str] = (),
    ttl_ms: int = 0,
) -> dict[str, Any]:
    """A complete state message. Raises on an unknown state; `send` is what never raises."""
    if state not in STATES:
        raise ValueError(f"unknown HUD state {state!r}")
    given = {
        "text": text,
        "chip": chip,
        "level": level,
        "badges": list(badges),
        "countdown_ms": countdown_ms,
        "suggestions": list(suggestions),
        "ttl_ms": ttl_ms,
    }
    return {"t": "state", "state": state, **{k: _field(k, v) for k, v in given.items()}}


HIDDEN = state_message("hidden")


def level_update(level: float) -> dict[str, Any]:
    """The smallest legal message: the meter moved and nothing else did."""
    return {"t": "state", "state": "hearing", "level": _level(level)}


def is_level_update(message: Mapping[str, Any]) -> bool:
    return message.get("state") == "hearing" and "level" in message and set(message) <= _LEVEL_KEYS


# --------------------------------------------------------------------------- the state machine


def next_state(current: str, message: Mapping[str, Any]) -> str:
    """The state the HUD is in after `message`, which is `current` when the message is
    malformed or asks for a transition that is not legal.

    Legal transitions:

    - any state -> `hidden`, `hearing`, `refused` (see ALWAYS)
    - any state -> itself (an update: new level, a speculative transcript, fresh badges)
    - `hidden` -> `help`
    - `hearing` -> `thinking`, or straight to a result when the grammar answers at once
    - `thinking` -> `still_thinking`, or a result
    - `still_thinking` -> a result
    - `swap`, `hints` -> `heard` (a number was spoken and acted on)
    - `countdown` -> `heard` or `swap` (it ran out, or the confirming tap came)
    - results are `heard`, `swap`, `hints`, `countdown`, `suggest`, `help`

    Everything else is a bug in the caller: `thinking` out of nowhere, a second result
    on top of a first, `still_thinking` going back to `thinking`.
    """
    if not isinstance(message, Mapping) or message.get("t") != "state":
        return current
    target = message.get("state")
    if not isinstance(target, str) or target not in STATES:
        return current
    if target == current or target in ALWAYS or target in TRANSITIONS.get(current, ()):
        return target
    return current


def compose(shown: Mapping[str, Any], message: Mapping[str, Any]) -> dict[str, Any] | None:
    """The complete message the HUD shows after `message` arrives on top of `shown`,
    or None when the message is malformed or the transition is illegal.

    Same state: fields the message leaves out keep their value. New state: they start
    from their defaults, except that badges survive into the utterance that answers them.
    """
    current = str(shown.get("state", "hidden"))
    if not isinstance(message, Mapping) or message.get("t") != "state":
        return None
    target = message.get("state")
    if next_state(current, message) != target:
        return None
    base = dict(shown) if target == current else {**HIDDEN, "state": target}
    if target != current and target in CARRIES_BADGES:
        base["badges"] = list(shown.get("badges", ()))
    for name in _DEFAULTS:
        if name in message:
            base[name] = _field(name, message[name])
    return base


# --------------------------------------------------------------------------- from decisions


def _on_screen(window: Window, state: DesktopState) -> bool:
    if window.hidden or window.size[0] <= 0 or window.size[1] <= 0:
        return False
    visible = {m.active_workspace_id for m in state.monitors} or {state.active_workspace_id}
    if window.workspace_id not in visible and not window.pinned:
        return False
    # a fullscreen window is the only thing visible on its workspace
    return window.fullscreen or not any(
        other.fullscreen and other.workspace_id == window.workspace_id
        for other in state.windows
        if other.address != window.address
    )


def badges_for(
    candidates: Iterable[Candidate], state: DesktopState | None = None
) -> list[dict[str, Any]]:
    """Numbered badges, one per candidate, in candidate order.

    The number is the candidate's position and nothing else, because a spoken "two" is
    resolved against the same tuple. So a candidate that cannot be drawn is never
    skipped; it gets an empty rectangle instead. Geometry comes from the live snapshot
    when there is one: the candidate's own Window is as old as the Decision.
    """
    badges = []
    for n, candidate in enumerate(list(candidates)[:MAX_BADGES], start=1):
        window = candidate.window
        if window is not None and state is not None:
            window = state.by_address(window.address)
            if window is not None and not _on_screen(window, state):
                window = None
        x, y, w, h = (*window.at, *window.size) if window is not None else (0, 0, 0, 0)
        badges.append(_badge({"n": n, "label": candidate.label, "x": x, "y": y, "w": w, "h": h}))
    return [b for b in badges if b is not None]


def from_decision(
    decision: Decision,
    state: DesktopState | None = None,
    *,
    hud: HUD | None = None,
    countdown_s: float = 1.5,
) -> dict[str, Any]:
    """What the HUD shows for a Decision. `state` is the live desktop snapshot, used
    for badge geometry; without it the candidates' own windows are trusted as they are."""
    hud = hud or HUD()
    action = decision.action
    chip = action.describe() if action is not None else ""
    heard = decision.heard
    verdict = decision.verdict
    if action is not None and action.intent is Intent.HELP and verdict is Verdict.ACT:
        return state_message(
            "help",
            text="what you can say",
            suggestions=decision.suggestions or HELP_EXAMPLES,
            ttl_ms=HELP_TTL_MS,
        )
    if verdict is Verdict.ACT:
        return state_message("heard", text=heard, chip=chip, ttl_ms=HEARD_TTL_MS)
    if verdict is Verdict.ACT_SWAP:
        return state_message(
            "swap",
            text=heard,
            chip=chip,
            badges=badges_for(decision.candidates, state),
            ttl_ms=int(hud.swap_timeout_s * 1000),
        )
    if verdict is Verdict.HINTS:
        return state_message(
            "hints",
            text=heard,
            chip="say a number",
            badges=badges_for(decision.candidates, state),
            ttl_ms=int(hud.hint_timeout_s * 1000),
        )
    if verdict is Verdict.COUNTDOWN:
        return state_message(
            "countdown", text=heard, chip=chip, countdown_ms=int(countdown_s * 1000)
        )
    if verdict is Verdict.CONFIRM_KEY:
        # no timer of ours: the engine decides how long it waits for the tap
        return state_message("countdown", text=CONFIRM_TEXT, chip=chip)
    if verdict is Verdict.REFUSE:
        return state_message("refused", text=decision.reason or "refused", ttl_ms=REFUSED_TTL_MS)
    if verdict is Verdict.SUGGEST:
        return state_message(
            "suggest",
            text=heard or "not understood",
            chip=decision.reason,
            suggestions=decision.suggestions,
            ttl_ms=SUGGEST_TTL_MS,
        )
    return dict(HIDDEN)


class RateLimit:
    """At most `per_second` events in any second, by refusing anything that comes
    sooner than one interval after the last one that was allowed."""

    def __init__(
        self, per_second: float = LEVEL_HZ, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._interval = 1.0 / per_second
        self._clock = clock
        self._last = float("-inf")

    def allow(self) -> bool:
        now = self._clock()
        if now - self._last < self._interval:
            return False
        self._last = now
        return True

    def reset(self) -> None:
        self._last = float("-inf")


# --------------------------------------------------------------------------- the server


def _peer_uid(sock: Any) -> int | None:
    try:
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", raw)
    except (OSError, struct.error, AttributeError):
        return None  # fail closed: a peer we cannot identify is not ours
    return uid


def _prepare_directory(directory: Path) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise HudError(f"{directory} is not a directory owned by this user")
    if stat.S_IMODE(info.st_mode) != 0o700:
        directory.chmod(0o700)


def _clear_stale(path: Path) -> None:
    """Remove a socket file nobody listens on. A live one belongs to another engine."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(info.st_mode):
        raise HudError(f"{path} exists and is not a socket")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.5)
        probe.connect(str(path))
    except OSError:
        path.unlink(missing_ok=True)
    else:
        raise HudError(f"another engine is already serving {path}")
    finally:
        probe.close()


class HudServer:
    """Serves one HUD. Construct anywhere; `start`, `send` and `close` belong to one loop."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        hud: HUD | None = None,
        countdown_s: float = 1.5,
        expected_uid: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.path = path or socket_path()
        self.last_refusal = ""
        self._hud = hud or HUD()
        self._countdown_s = countdown_s
        self._uid = os.getuid() if expected_uid is None else expected_uid
        self._clock = clock
        self._levels = RateLimit(LEVEL_HZ, clock)
        self._server: asyncio.Server | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._shown: dict[str, Any] = dict(HIDDEN)
        self._expires = 0.0  # 0: never

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> bool:
        """Listen. False (and a log line) when the socket cannot be had; the engine runs
        without a HUD rather than not at all."""
        if self._server is not None:
            return True
        try:
            _prepare_directory(self.path.parent)
            _clear_stale(self.path)
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.bind(str(self.path))
                # the 0700 directory already covers the moment between bind and chmod
                os.chmod(self.path, 0o600)
                self._server = await asyncio.start_unix_server(
                    self._on_client, sock=sock, limit=LINE_LIMIT
                )
            except BaseException:
                sock.close()
                raise
        except OSError as exc:
            log.error("HUD socket unavailable, running without a HUD: %s", exc)
            return False
        log.info("HUD socket listening at %s", self.path)
        return True

    async def close(self) -> None:
        server, self._server = self._server, None
        self._drop_client()
        if server is None:
            return
        server.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(server.wait_closed(), 1.0)
        with contextlib.suppress(OSError):
            self.path.unlink()

    @property
    def connected(self) -> bool:
        return self._writer is not None

    @property
    def state(self) -> str:
        return str(self._current()["state"])

    # ------------------------------------------------------------------ sending

    def from_decision(self, decision: Decision, state: DesktopState | None = None) -> dict:
        return from_decision(decision, state, hud=self._hud, countdown_s=self._countdown_s)

    async def send(self, message: Mapping[str, Any]) -> None:
        """Show this, if anyone is looking. Never raises, never waits on the HUD."""
        self.send_nowait(message)

    def send_nowait(self, message: Mapping[str, Any]) -> bool:
        """True when a line was handed to a connected HUD."""
        try:
            return self._send(message)
        except Exception:  # whatever went wrong, the voice command goes on
            log.exception("HUD message dropped")
            self._drop_client()
            return False

    def _current(self) -> dict[str, Any]:
        """What the HUD is showing now: a state with a ttl hides itself over there."""
        if self._expires and self._clock() >= self._expires:
            self._shown, self._expires = dict(HIDDEN), 0.0
        return self._shown

    def _send(self, message: Mapping[str, Any]) -> bool:
        shown = self._current()
        full = compose(shown, message)
        if full is None:
            wanted = message.get("state") if isinstance(message, Mapping) else message
            log.warning("HUD message dropped: %r is not legal from %r", wanted, shown["state"])
            return False
        meter_only = full["state"] == shown["state"] and is_level_update(message)
        if meter_only and not self._levels.allow():
            return False
        self._shown = full
        if not meter_only:
            self._expires = self._clock() + full["ttl_ms"] / 1000 if full["ttl_ms"] else 0.0
        return self._write(level_update(full["level"]) if meter_only else full)

    def _write(self, message: Mapping[str, Any]) -> bool:
        writer = self._writer
        if writer is None or writer.is_closing():
            return False
        if writer.transport.get_write_buffer_size() > BACKLOG_LIMIT:
            log.warning("HUD is not reading; disconnecting it")
            self._drop_client()
            return False
        line = json.dumps(message, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        writer.write(line.encode() + b"\n")
        return True

    def _drop_client(self) -> None:
        writer, self._writer = self._writer, None
        if writer is not None:
            with contextlib.suppress(Exception):
                writer.close()

    # ------------------------------------------------------------------ the client

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            refusal = await self._admit(reader, writer)
            if refusal:
                self._refuse(writer, refusal)
                # Closing a socket with unread input is reported to the peer as a reset,
                # and the reason can be lost with it (seen live: the HUD retried three
                # times before it got to read why). So swallow, unparsed, what it sent.
                with contextlib.suppress(Exception):
                    await writer.drain()
                    await asyncio.wait_for(reader.read(LINE_LIMIT), 0.2)
                return
            self._writer = writer
            log.info("HUD connected")
            shown = self._current()
            if shown["state"] != "hidden":
                # a HUD that restarts in the middle of an utterance catches up
                remaining = int((self._expires - self._clock()) * 1000) if self._expires else 0
                self._write({**shown, "ttl_ms": max(remaining, 0)})
            # the HUD has nothing more to say; reading is how we learn that it left
            with contextlib.suppress(OSError, ValueError, asyncio.LimitOverrunError):
                while await reader.readline():
                    pass
        finally:
            if self._writer is writer:
                self._writer = None
                log.info("HUD disconnected")
            with contextlib.suppress(Exception):
                writer.close()

    async def _admit(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> str:
        """Empty when the client may stay, otherwise the reason it may not."""
        uid = _peer_uid(writer.get_extra_info("socket"))
        if uid != self._uid:
            return f"peer uid {uid} is not the engine's uid {self._uid}"
        try:
            line = await asyncio.wait_for(reader.readline(), HELLO_TIMEOUT_S)
            hello = json.loads(line)
        except (TimeoutError, ValueError, OSError, asyncio.LimitOverrunError):
            return "no hello: the first line must be a JSON hello message"
        if not isinstance(hello, dict) or hello.get("t") != "hello":
            return "no hello: the first line must be a JSON hello message"
        if hello.get("proto") != PROTO or isinstance(hello.get("proto"), bool):
            # the number is the client's own and short; nothing else of the line is echoed
            theirs = hello.get("proto") if isinstance(hello.get("proto"), int) else "unknown"
            return f"protocol mismatch: engine speaks {PROTO}, HUD speaks {theirs}"
        if self._writer is not None and not self._writer.is_closing():
            return "a HUD is already connected"
        return ""

    def _refuse(self, writer: asyncio.StreamWriter, reason: str) -> None:
        self.last_refusal = reason
        log.warning("HUD client refused: %s", reason)
        with contextlib.suppress(Exception):
            writer.write(json.dumps({"t": "refused", "reason": reason}).encode() + b"\n")
