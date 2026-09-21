"""The executor: the only place in hyprsay that writes to the desktop.

A `Decision` was made against a snapshot that is already old by the time it gets here:
Jev took up to 0.9 s, a countdown another 1.5 s, and the human kept using the mouse the
whole time. So nothing is trusted from the Decision except the intent and the address.
Before every write, in this order:

1. the lock latch. Locked, or unsure, means nothing happens and nothing is even read.
2. a FRESH desktop state, which can itself say locked.
3. for a window target: that address must still exist and still have the same class.
   Addresses are heap pointers and get reused, so an address that now belongs to a
   different application is "the window changed", not a match.
4. for typing: the focused address must still be the one pinned at key DOWN, with the
   class it had then (docs/PLAN.md 5.6). Text is sanitized again here, whatever the
   grammar already did, because this is the last code that sees it.

Then the operation runs through `hyprsay.ops`, which goes through the inherited hypruse
functions and their guards. Whatever those raise becomes an `Outcome` with one plain
sentence. `execute`, `undo` and `again` never raise: the caller is a worker thread
with a HUD to update, not a place to handle a traceback.

hypruse keeps its state in module globals and `os.environ`, so the daemon runs every
call on ONE worker thread (hypruse-map.md section 7). The lock here is insurance
against a second caller, not a licence for one.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from hyprsay import ops
from hyprsay.config import Config
from hyprsay.model import Action, DesktopState, Intent, LockLatch, Outcome, Window
from hypruse import hyprctl, journal, safety, server, session, trust, wire
from hypruse import input as hinput

LOCKED = "The screen is locked, so nothing was done."
UNREADABLE = "The desktop could not be read, so nothing was done."
WINDOW_CHANGED = "The window changed, so nothing was done."
NO_WINDOW = "There is no window to act on."
FOCUS_MOVED = "Focus moved since you started speaking, so nothing was typed."
READ_ONLY = "hyprsay is in read-only mode, so nothing was done."

_booted = False


def boot() -> None:
    """The inherited boot sequence, for a process that uses hypruse as a library.

    Call once, from the MAIN thread, before any GLib or asyncio loop starts: the kill
    path installs a SIGTERM handler, and `signal.signal` works nowhere else.

    `safety.arm()` and not `safety.init()`: init raises the beacon at the fixed path
    `$XDG_RUNTIME_DIR/hypruse/state.json`, which this user's hypruse MCP servers also
    write, and it takes no directory argument. arm installs the same shutdown path
    (atexit plus SIGTERM, running the cleanups) without fighting over that file.
    """
    global _booted
    if _booted:
        return
    # the strict seat guard refuses when the mouse moved since the last look, and the
    # human always moves the mouse while speaking
    os.environ.pop("HYPRUSE_STRICT", None)
    session.ensure_session_env()
    server.use_plain_blocks()
    safety.arm()
    # a held drag button must be released on the kill path, exactly as server.main does
    safety.on_shutdown(hinput.release_held)
    _booted = True


class Executor:
    def __init__(
        self,
        cfg: Config,
        state_provider: Callable[[], DesktopState],
        latch: LockLatch,
        lexicon: Any = None,
    ) -> None:
        self.cfg = cfg
        self.lexicon = lexicon
        self._state_provider = state_provider
        self._latch = latch
        self._serial = threading.RLock()
        self._last: tuple[Action, str] | None = None
        self._inverse: Action | None = None

    # ------------------------------------------------------------------ public

    def execute(self, action: Action, *, pinned_address: str = "") -> Outcome:
        with self._serial:
            if action.intent is Intent.UNDO:
                return self.undo()
            if action.intent is Intent.AGAIN:
                return self.again()
            outcome = self._guarded(action, pinned_address)
            if outcome.ok:
                self._last = (action, pinned_address)
                self._inverse = outcome.inverse
            return outcome

    def undo(self) -> Outcome:
        with self._serial:
            if self._inverse is None:
                return Outcome(False, "There is nothing to undo.")
            outcome = self._guarded(self._inverse, "")
            if not outcome.ok:
                return outcome
            # no redo: a second "undo" must not flip the desktop back and forth
            self._inverse = None
            return Outcome(True, f"Undone. {outcome.message}".strip())

    def again(self) -> Outcome:
        with self._serial:
            if self._last is None:
                return Outcome(False, "There is nothing to repeat.")
            action, pinned_address = self._last
            operation = ops.OPERATIONS.get(action.intent)
            if operation is None or not operation.repeatable:
                return Outcome(False, "That one is not repeated. Say it in full.")
            outcome = self._guarded(action, pinned_address)
            if outcome.ok:
                self._inverse = outcome.inverse
            return outcome

    def forget(self) -> None:
        """Drop the undo and again memory, for example when the screen locks."""
        with self._serial:
            self._last = None
            self._inverse = None

    # ----------------------------------------------------------------- private

    def _guarded(self, action: Action, pinned_address: str) -> Outcome:
        try:
            return self._run(action, pinned_address)
        except Exception as exc:
            return Outcome(False, explain(exc))

    def _run(self, action: Action, pinned_address: str) -> Outcome:
        operation = ops.OPERATIONS.get(action.intent)
        if operation is None:
            return Outcome(False, "There is nothing to do for that.")
        if self._is_locked():
            return Outcome(False, LOCKED)
        if server.READONLY:
            return Outcome(False, READ_ONLY)
        try:
            fresh = self._state_provider()
        except Exception:
            return Outcome(False, UNREADABLE)
        if fresh.locked:
            return Outcome(False, LOCKED)

        if action.intent is Intent.TYPE_TEXT:
            checked = self._check_typing(action, fresh, pinned_address)
        elif operation.needs_window:
            checked = self._check_window(action, fresh, pinned_address)
        else:
            checked = action
        if isinstance(checked, Outcome):
            return checked

        inverse = _quietly(operation.inverse, checked, fresh)
        message = operation.perform(checked, fresh)
        if message.startswith("DRY RUN"):
            inverse = None  # nothing happened, so there is nothing to undo
        return Outcome(True, message, inverse)

    def _is_locked(self) -> bool:
        try:
            return bool(self._latch.locked())
        except Exception:
            return True

    def _check_window(
        self, action: Action, fresh: DesktopState, pinned_address: str
    ) -> Action | Outcome:
        """The action with its window replaced by the fresh record of the same window."""
        if action.window is None:
            # "make this fullscreen": the pinned window, else whatever has focus now
            current = fresh.by_address(pinned_address) if pinned_address else fresh.focused
            return replace(action, window=current) if current else Outcome(False, NO_WINDOW)
        current = fresh.by_address(action.window.address)
        if current is None or not _same_class(current, action.window):
            return Outcome(False, WINDOW_CHANGED)
        return replace(action, window=current)

    def _check_typing(
        self, action: Action, fresh: DesktopState, pinned_address: str
    ) -> Action | Outcome:
        pinned = action.window
        if not pinned_address or pinned is None or pinned.address != pinned_address:
            return Outcome(False, "Typing needs the window that was focused at key down.")
        focused = fresh.focused
        if focused is None or focused.address != pinned_address:
            return Outcome(False, FOCUS_MOVED)
        if not _same_class(focused, pinned):
            return Outcome(False, WINDOW_CHANGED)
        if ops.is_terminal(focused.cls) or ops.is_terminal(focused.initial_class):
            return Outcome(False, "Typing into a terminal is refused.")
        text = ops.sanitize_text(action.text or "", self.cfg.safety.type_max_chars)
        if not text:
            return Outcome(False, "There is nothing to type.")
        return replace(action, window=focused, text=text)


# ----------------------------------------------------------------------------- helpers


def _same_class(now: Window, then: Window) -> bool:
    return now.cls.lower() == then.cls.lower()


def _quietly(
    inverse: Callable[[Action, DesktopState], Action | None], action: Action, fresh: DesktopState
) -> Action | None:
    """An inverse that cannot be worked out is a missing undo, never a failed action."""
    try:
        return inverse(action, fresh)
    except Exception:
        return None


def _first_sentence(exc: BaseException, limit: int = 160) -> str:
    # hypruse messages are written for an LLM agent and carry tool advice after the
    # first sentence; they can also quote a window class or a layer namespace, which the
    # window's owner chose, so control characters go too
    text = ops.sanitize_text(str(exc))
    head = text.split(". ", 1)[0].rstrip(".")
    return head[:limit]


def explain(exc: BaseException) -> str:
    """One plain sentence for the HUD, for anything the inherited code can raise."""
    if isinstance(exc, ops.OpError):
        return str(exc)
    if isinstance(exc, trust.TrustError):
        return f"Refused: {_first_sentence(exc)}."
    if isinstance(exc, journal.DryRunError):
        return "Dry run: nothing was delivered."
    if isinstance(exc, hyprctl.HyprctlError):
        return f"Hyprland did not accept that: {_first_sentence(exc)}."
    if isinstance(exc, hinput.InputError | wire.WireError):
        return f"The text could not be delivered: {_first_sentence(exc)}."
    if isinstance(exc, ValueError | TypeError):
        return f"That could not be done: {_first_sentence(exc)}."
    return f"That failed ({type(exc).__name__}), so it may not have happened."
