"""The engine: key down, speak, key up, and the thing happens.

One asyncio loop owns the conversation with the user. Everything slow or blocking is
pushed off it: the recognizer decodes in a thread, Jev is awaited, and every write to
the desktop runs on ONE worker thread because the inherited hypruse core keeps
process-global state.

The order of checks is the safety design, not an implementation detail:

    key down   -> locked? refuse. Pin the focused window. Open the mic. Warm Jev.
    key up     -> locked? drop the audio. Decode. Understand. (Cloud rescue, once.)
    decision   -> stamp it with the desktop it was decided against
    act        -> consume the stamp, refuse a moved desktop above tier 1, then the
                  executor re-checks the lock and the target against FRESH state

A session lock at any point drops the utterance, cancels any countdown, closes the
microphone and zeroes the buffer. Hyprland skips ordinary binds while locked, so the
key-up never arrives in that case; the lock latch forces it.

The stamp is `nlu/context.fingerprint`, and it guards the gap this file owns and nobody
else can see: between the snapshot an utterance is understood against and the write, a
decode takes 93 to 165 ms, a Jev fan-out about 315 ms and a tier 2 countdown another
1.5 s, all measured, so roughly two seconds in which the desktop can become a different
desktop. It is consumed before anything is written, so one decision can only ever act
once (docs/STRATEGY.md 2.5). It is not a confidence gate and never replaces one: it
proves the world has not moved, and says nothing about whether the choice was right.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from . import journal, ops
from .config import Config
from .model import Action, Candidate, Decision, Intent, Outcome, Transcript, Verdict
from .nlu import context

log = logging.getLogger("hyprsay")

# shorter than this is a stray tap, not speech
MIN_UTTERANCE_S = 0.25
# a confirming tap for tier 3 must be this short, so it cannot be a new utterance
CONFIRM_TAP_S = 0.5
CONFIRM_WINDOW_S = 4.0
LOCK_POLL_S = 0.25


class Engine:
    def __init__(self, cfg: Config, *, world, ptt, recorder, recognizer, understander, executor,
                 latch, hud, jev=None) -> None:  # fmt: skip
        self.cfg = cfg
        self.world, self.ptt, self.recorder = world, ptt, recorder
        self.recognizer, self.understander = recognizer, understander
        self.executor, self.latch, self.hud, self.jev = executor, latch, hud, jev
        # one thread: the inherited core is not safe to call concurrently
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hyprsay-act")
        self._pinned = ""
        self._down_at = 0.0
        # badges on screen: the next utterance may be a bare number
        self._picking: tuple[Candidate, ...] = ()
        self._picking_until = 0.0
        # the rest of an utterance whose chain paused on a question (`_ask_on`). It lives
        # and dies with the badges, because the number is the only thing that resumes it
        self._resume: tuple | None = None
        # the fingerprint of the desktop the decision in hand was made against, cleared
        # the moment it is used. Empty means nothing is waiting to be carried out.
        self._stamp = ""
        self._pending: asyncio.Task | None = None  # a countdown or a confirm wait
        self._meter: asyncio.Task | None = None
        self._confirm: asyncio.Future | None = None
        self._lock_watch: asyncio.Task | None = None
        self._cancelled = False
        self._running: set[asyncio.Task] = set()
        self._pipeline = asyncio.Lock()

    # ------------------------------------------------------------------ lifecycle

    async def run(self) -> None:
        self.latch.on_lock(self._on_lock)
        self.world.on_custom(self.ptt.feed_custom)
        tasks = [
            asyncio.create_task(self.world.run()),
            asyncio.create_task(self.hud.start()),
            asyncio.create_task(self._warm_speech()),
        ]
        try:
            async for event in self.ptt.events():
                try:
                    if event.kind == "down":
                        await self._key_down()
                    else:
                        # the rest of the utterance (decode, Jev, act) takes half a
                        # second or more. Awaiting it here meant the next key press
                        # opened the microphone that much late and the first word of
                        # the next command was lost, so it runs alongside instead.
                        self._track(self._key_up(event.reason))
                except Exception:  # one bad utterance must never kill the daemon
                    log.exception("utterance failed")
                    await self._show("refused", text="internal error, see the log")
        finally:
            for task in (*tasks, *self._running):
                task.cancel()
            self._worker.shutdown(wait=False, cancel_futures=True)
            with contextlib.suppress(Exception):
                await self.hud.close()

    def _track(self, coro) -> asyncio.Task:
        """Run an utterance to its end without holding up the next key press."""

        async def guarded() -> None:
            # one at a time: the executor has a single worker, and two pipelines racing
            # would interleave their overlay states
            async with self._pipeline:
                try:
                    await coro
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("utterance failed")
                    await self._show("refused", text="internal error, see the log")

        task = asyncio.create_task(guarded())
        self._running.add(task)
        task.add_done_callback(self._running.discard)
        return task

    async def _warm_speech(self) -> None:
        """Load the speech model now, not on the first command.

        Found on the live system: the model loaded lazily inside the first decode, so the
        first thing anyone said waited 2 to 6 s, and a SIGTERM during that load hung the
        shutdown. One throwaway inference follows the load because the first real decode
        is the slow one too (450 ms measured, against about 165 ms after it).
        """
        warm = getattr(self.recognizer, "warm", None)
        if warm is None:
            return
        started = time.perf_counter()
        try:
            await warm()
            await self.recognizer.transcribe(b"\0\0" * 3200, 16000)
        except Exception as exc:
            log.warning("speech model is not ready: %s", exc)
            await self._show("refused", text=f"speech model: {exc}", ttl_ms=6000)
            return
        log.info("speech model ready in %.1f s", time.perf_counter() - started)

    def _on_lock(self) -> None:
        """The session locked. Runs from whatever thread noticed; touch nothing async here."""
        self.recorder.abort()
        self.ptt.force_up("locked")
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(self._forget_everything, "session locked")

    _loop: asyncio.AbstractEventLoop | None = None

    def _forget_everything(self, why: str) -> None:
        self._cancel_pending(why, forget_badges=True)

    def _cancel_pending(self, why: str, *, forget_badges: bool = False) -> None:
        if self._pending and not self._pending.done():
            self._pending.cancel()
            log.info("pending action cancelled: %s", why)
        self._cancelled = True
        if forget_badges:
            # NOT on an ordinary key press: the key has to go down to say the number, so
            # clearing here would make every numbered hint and swap impossible to answer
            self._picking = ()
            self._resume = None
            self._stamp = ""
            # the badges and what the last commands did are one memory with one lifetime
            # (nlu/context.py), so whatever clears one clears the other
            forget = getattr(self.understander, "forget", None)
            if forget is not None:
                forget()

    # ------------------------------------------------------------------ key down

    async def _key_down(self) -> None:
        self._loop = asyncio.get_running_loop()
        now = time.monotonic()
        # a key press while a tier 3 action waits for its confirming tap
        if self._confirm is not None and not self._confirm.done():
            self._down_at = now
            return
        # any new press cancels a running countdown: that IS the cancel gesture
        self._cancel_pending("key pressed")
        if self.latch.locked():
            await self._show("refused", text="session is locked", ttl_ms=1500)
            return
        self._pinned = self.world.state.active_address
        self._down_at = now
        try:
            # opening the device blocks while PortAudio negotiates; keep the loop free
            await asyncio.to_thread(self.recorder.start)
        except Exception as exc:
            await self._show("refused", text=f"microphone: {exc}", ttl_ms=3000)
            return
        if self.jev is not None:
            # speech outlasts a handshake: 526 ms -> 325 ms on the first call (measured)
            asyncio.create_task(self.jev.warm())
        await self._show("hearing", level=0.0)
        self._meter = asyncio.create_task(self._run_meter())
        self._ensure_lock_watch()

    def _ensure_lock_watch(self) -> None:
        if self._lock_watch is None or self._lock_watch.done():
            self._lock_watch = asyncio.create_task(self._watch_lock())

    async def _watch_lock(self) -> None:
        """Hyprland emits no lock event, and the latch only notices a lock when asked.

        So while there is anything a lock must interrupt (a held key, a countdown, a
        pending confirmation) ask it a few times a second. The latch fires `_on_lock` on
        the edge. It can shell out to loginctl, so it runs off the loop.
        """
        with contextlib.suppress(asyncio.CancelledError):
            while self.ptt.held or (self._pending is not None and not self._pending.done()):
                await asyncio.to_thread(self.latch.locked)
                await asyncio.sleep(LOCK_POLL_S)

    async def _run_meter(self) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            while True:
                await asyncio.sleep(1 / 30)
                await self.hud.send(
                    {"t": "state", "state": "hearing", "level": self.recorder.level()}
                )

    # ------------------------------------------------------------------ key up

    async def _key_up(self, reason: str) -> None:
        if self._meter:
            self._meter.cancel()
            self._meter = None
        held = time.monotonic() - self._down_at
        if self._confirm is not None and not self._confirm.done():
            # only a short, deliberate tap confirms; holding the key is a new utterance
            self._confirm.set_result(held <= CONFIRM_TAP_S)
            return
        # ask BEFORE stopping: stop() wipes the recorder's own buffers
        spoke = self.recorder.voiced_after(0.0)
        pcm = self.recorder.stop()
        # captured now: a second key press may repin before this utterance is understood
        pinned = self._pinned
        if reason == "locked" or self.latch.locked():
            await self._show("refused", text="session is locked", ttl_ms=1500)
            return
        seconds = len(pcm) / 2 / 16000
        if seconds < MIN_UTTERANCE_S or not spoke:
            # a stray press, or a held key with nobody talking: nothing to decode, and
            # above all nothing to send anywhere
            await self._show("hidden")
            return

        await self._show("thinking")
        started = time.perf_counter()
        transcript = await self.recognizer.transcribe(pcm, 16000)
        # decoding takes a sixth of a second and Jev another third: the screen can lock
        # inside that window, and nothing about this utterance may travel afterwards
        if await self._locked_now():
            return await self._drop_for_lock()
        decision = await self._understand(transcript, pinned)
        if await self._locked_now():
            return await self._drop_for_lock()

        # the local transcript led nowhere: ask the cloud about the SAME audio, once.
        # Cloud models held 8 of 8 on degraded audio where local fell to 6 or 7 (measured).
        # Only what hearing the words again could fix. SUGGEST alone does not mean the
        # recognizer failed: it is also how "Jev is off", "that sounded like dictation"
        # and "hyprsay does not do that" come back, and uploading the clip for those
        # sends audio the speaker had every reason to think stayed here.
        rescue = getattr(self.recognizer, "rescue", None)
        failed_locally = decision.rehearable or (not transcript.text.strip() and spoke)
        if rescue is not None and failed_locally and not decision.dictation:
            # no text on the overlay either: it may be the words being dictated
            await self._show("still_thinking")
            with contextlib.suppress(Exception):
                second = await rescue(pcm, 16000)
                if (
                    second.text.strip()
                    and second.text.strip().lower() != transcript.text.strip().lower()
                ):
                    transcript, decision = second, await self._understand(second, pinned)

        if await self._locked_now():
            return await self._drop_for_lock()
        journal.record(
            "utterance",
            seconds=round(seconds, 2),
            # the words of a dictation are the speaker's, not a command: the journal keeps
            # their length and a hash, the same as it does for the text that gets typed
            transcript=replace(transcript, text=journal.redact_text(transcript.text))
            if decision.dictation
            else transcript,
            decision=decision,
            ms=round((time.perf_counter() - started) * 1000),
            exchange=getattr(self.understander, "last_exchange", None),
        )
        await self._carry_out(decision, pinned)

    async def _locked_now(self) -> bool:
        """Ask the latch, off the loop: it may shell out to loginctl."""
        return await asyncio.to_thread(self.latch.locked)

    async def _drop_for_lock(self) -> None:
        self._forget_everything("session locked")
        await self._show("refused", text="session is locked", ttl_ms=1500)

    async def _understand(self, transcript: Transcript, pinned: str = "") -> Decision:
        live = time.monotonic() < self._picking_until
        picking = self._picking if live else ()
        # the clauses waiting on an answer wait exactly as long as the badges do. Once
        # they are gone the question went unanswered, and the rest of that utterance is
        # not something to run against a desktop several commands later
        if not live:
            self._resume = None
        # one snapshot, read once: the stamp has to be the fingerprint of the very state
        # the decision is made against, and `world.state` moves under a running daemon
        state = self.world.state
        self._stamp = context.fingerprint(state)
        return await self.understander.understand(
            transcript, state, pinned_address=pinned or self._pinned, picking=picking
        )

    # ------------------------------------------------------------------ decisions

    async def _carry_out(self, d: Decision, pinned: str | None = None) -> None:
        self._picking = ()
        # consumed before anything is written, so one decision can only act once: a
        # retry, a second key press or a chain restarting finds nothing to spend
        stamp, self._stamp = self._stamp, ""
        resume, self._resume = self._resume, None
        stale = self._stale(d, stamp)
        if stale:
            await self._show("refused", text=stale, ttl_ms=3000)
            return
        if d.rest:
            await self._chain(d, pinned)
            return
        if resume is None:
            await self._one(d, pinned)
            return
        await self._resumed(d, resume)

    async def _resumed(self, d: Decision, resume: tuple) -> None:
        """This decision answered a question a chain stopped on, so it IS that chain's
        next step: it is carried out as one, and the clauses behind it follow."""
        rest, done, offset, total, heard, pinned = resume
        if not await self._one(d, pinned, chained=True):
            # still not answered: keep the question up rather than dropping the rest
            if d.verdict is Verdict.HINTS and d.candidates:
                await self._ask_on(d, rest, done, offset - 1, total, heard, pinned)
            else:
                await self._stopped(offset - 1, total, d, done)
            return
        done.append(_chip(d))
        await self._steps(rest, pinned, done=done, offset=offset, heard=heard)

    def _stale(self, d: Decision, stamp: str) -> str:
        """Why this decision may no longer be acted on, or "" when it may.

        Only for what cannot be taken back. A focus or a workspace switch is free to
        reverse and the executor already re-checks its own target against fresh state
        (`executor._check_window`); a close, a type or a session action is not, and it is
        decided against a whole desktop rather than against one window. So above tier 1
        the desktop it was decided against has to still be there.

        The check is on the whole utterance, before its first step, because that is the
        only moment at which nothing has happened yet. After that the chain itself is
        what moves the desktop, which is why the stamp is spent here rather than re-read
        between steps: a step is not stale because the step before it did its job.
        """
        if max((step.tier for step in (d, *d.rest)), default=0) < 2:
            return ""
        if not stamp:
            return "that was already carried out, so nothing was done"
        if context.moved(stamp, self.world.state):
            return "the desktop changed while that was being decided, so nothing was done"
        return ""

    async def _chain(self, first: Decision, pinned: str | None) -> None:
        """One step at a time, on the one worker, stopping the moment one does not act.

        A step is a clause the seam Booleans cut out of the utterance, or a slot of the
        ordered plan (`nlu/plan.py`); this file cannot tell them apart and does not need
        to, because both arrive as `Decision.rest` and both were understood against the
        desktop the step before them leaves.

        A later clause is an order that assumes the earlier one happened: "open firefox
        and move it to workspace 3" has nothing to move if the launch was refused. So a
        refusal, a cancelled countdown or an unconfirmed key ends the chain, and the
        overlay says which part stopped it and what had already run. Running on and
        hoping would aim the rest of the utterance at whatever the desktop happens to
        look like, which is exactly the failure v1 had, only faster.

        A question is not one of those. See `_ask_on`.
        """
        await self._steps((first, *first.rest), pinned, done=[], offset=0, heard=first.heard)

    async def _steps(
        self,
        clauses: tuple[Decision, ...],
        pinned: str | None,
        *,
        done: list[str],
        offset: int,
        heard: str,
    ) -> None:
        """The clauses still to run. `offset` is how many ran before this call, so that
        "part 2 of 3" keeps meaning the same part after a chain pauses for an answer."""
        total = offset + len(clauses)
        for index, clause in enumerate(clauses):
            part = offset + index
            # the screen can lock between two clauses as easily as during one
            if part and await self._locked_now():
                await self._drop_for_lock()
                return
            if not await self._one(clause, pinned, chained=True):
                if clause.verdict is Verdict.HINTS and clause.candidates:
                    rest = clauses[index + 1 :]
                    await self._ask_on(clause, rest, done, part, total, heard, pinned)
                else:
                    await self._stopped(part, total, clause, done)
                return
            done.append(_chip(clause))
        await self._show("heard", text=heard, chip="; ".join(done), ttl_ms=2500)

    async def _ask_on(
        self,
        clause: Decision,
        rest: tuple[Decision, ...],
        done: list[str],
        part: int,
        total: int,
        heard: str,
        pinned: str | None,
    ) -> None:
        """Ask the question a clause raised, and keep the rest of the utterance waiting.

        Badges used to be hidden mid-chain, because a number could only finish the clause
        it belonged to and would leave the rest of the utterance unsaid. That reasoning
        was sound and its conclusion was not: it meant "find the channel and scroll down"
        with two browsers open showed nothing at all and did nothing at all. The answer
        instead finishes the utterance. The clauses after this one were understood knowing
        their window would only be settled at run time (`nlu/understand._carried`), so
        they are aimed at whichever window the number chooses.
        """
        self._arm_picking(clause.candidates, self.cfg.hud.hint_timeout_s)
        # resolved now: `self._pinned` is the window focused at key down, and answering
        # takes a key press of its own, which would move it
        held = self._pinned if pinned is None else pinned
        self._resume = (rest, list(done), part + 1, total, heard, held)
        await self.hud.send(self.hud.from_decision(clause, self.world.state))

    async def _stopped(self, index: int, total: int, clause: Decision, done: list[str]) -> None:
        ran = f"already did {', '.join(done)}" if done else "nothing was done"
        why = clause.reason or clause.verdict.value
        await self._show(
            "refused", text=f"stopped at part {index + 1} of {total}: {why}; {ran}", ttl_ms=4000
        )

    async def _one(self, d: Decision, pinned: str | None, *, chained: bool = False) -> bool:
        """Carry out one decision, and say whether it acted on the desktop.

        `chained` turns off the badges. A number answered mid-chain could only finish the
        clause it belongs to and would leave the rest of the utterance unsaid, which is
        the half-done outcome the chain exists to avoid.
        """
        action = d.action
        if action is not None and action.intent in (
            Intent.UNDO,
            Intent.AGAIN,
            Intent.CANCEL,
            Intent.HELP,
        ):
            return await self._meta(action.intent, d)

        if d.verdict is Verdict.ACT and action:
            return await self._act(action, d, pinned=self._pin(action, pinned))
        if d.verdict is Verdict.ACT_SWAP and action:
            # free to reverse: act now, and let a number re-target it (docs/PLAN.md 5.6)
            badges = () if chained else d.candidates
            if not await self._act(action, d, badges=badges, pinned=self._pin(action, pinned)):
                return False
            if badges:
                self._arm_picking(d.candidates, self.cfg.hud.swap_timeout_s)
            return True
        if d.verdict is Verdict.HINTS:
            if not chained:
                self._arm_picking(d.candidates, self.cfg.hud.hint_timeout_s)
                await self.hud.send(self.hud.from_decision(d, self.world.state))
            return False
        if d.verdict is Verdict.COUNTDOWN and action:
            return await self._ceremony(
                self._countdown(action, d, self._pin(action, pinned)), chained
            )
        if d.verdict is Verdict.CONFIRM_KEY and action:
            return await self._ceremony(self._confirm_by_key(action, d), chained)
        if d.verdict is Verdict.NOTHING:
            await self._show("hidden")
            return False
        if not chained:  # REFUSE, SUGGEST
            await self.hud.send(self.hud.from_decision(d, self.world.state))
        return False

    async def _ceremony(self, coro, chained: bool) -> bool:
        """A countdown or a confirming tap runs as `self._pending`, so a key press cancels
        it. Only a chain waits for the answer, because the clause after it may not start
        until this one has really happened. On its own the utterance is finished here, and
        waiting would hold the pipeline shut while the next key press wants it.
        """
        self._pending = asyncio.create_task(coro)
        self._ensure_lock_watch()
        if not chained:
            return True
        try:
            return bool(await self._pending)
        except asyncio.CancelledError:
            return False

    def _pin(self, action: Action, pinned: str | None) -> str | None:
        """A clause whose target is blank means "whatever the clause before it left in
        front of me", so it must NOT be pinned to the window focused at key down."""
        needs = ops.OPERATIONS.get(action.intent)
        if action.window is None and needs is not None and needs.needs_window:
            return ""
        return pinned

    def _arm_picking(self, candidates: tuple[Candidate, ...], seconds: float) -> None:
        self._picking = candidates
        self._picking_until = time.monotonic() + seconds

    async def _act(
        self,
        action: Action,
        d: Decision,
        badges: tuple[Candidate, ...] = (),
        *,
        pinned: str | None = None,
    ) -> bool:
        loop = asyncio.get_running_loop()
        target = self._pinned if pinned is None else pinned
        outcome: Outcome = await loop.run_in_executor(
            self._worker,
            lambda: self.executor.execute(action, pinned_address=target),
        )
        journal.record("action", action=action, ok=outcome.ok, message=outcome.message)
        # what the next utterance is told about this one: a verdict of ACT is a
        # permission, and the write can still refuse it (nlu/context.Turn.ok)
        acted = getattr(self.understander, "acted", None)
        if acted is not None:
            acted(outcome.ok)
        if not outcome.ok:
            await self._show("refused", text=outcome.message, ttl_ms=2500)
            return False
        shown = replace(d, verdict=Verdict.ACT_SWAP if badges else Verdict.ACT)
        await self.hud.send(self.hud.from_decision(shown, self.world.state))
        return True

    async def _countdown(self, action: Action, d: Decision, pinned: str | None = None) -> bool:
        """Tier 2. Any key press during the countdown cancels it (see _key_down)."""
        self._cancelled = False
        await self.hud.send(self.hud.from_decision(d, self.world.state))
        try:
            await asyncio.sleep(self.cfg.safety.countdown_s)
        except asyncio.CancelledError:
            await self._show("refused", text="cancelled", ttl_ms=1200)
            raise
        if await self._locked_now() or self._cancelled:
            return False
        # past this point the window really does close, so a cancel that arrives now is
        # too late to stop it. Shield the call rather than let the cancellation unwind
        # silently: the user must be told what happened, and the journal must have it.
        running = asyncio.ensure_future(self._act(action, d, pinned=pinned))
        try:
            return await asyncio.shield(running)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await running
            await self._show("heard", text="too late to cancel", chip=action.describe())
            raise

    async def _confirm_by_key(self, action: Action, d: Decision) -> bool:
        """Tier 3. A spoken 'confirm' would share the channel it authenticates, so the
        confirmation is a short physical tap of the push-to-talk key."""
        self._confirm = asyncio.get_running_loop().create_future()
        await self.hud.send(
            {**self.hud.from_decision(d, self.world.state), "text": "tap the key to confirm"}
        )
        try:
            confirmed = await asyncio.wait_for(self._confirm, CONFIRM_WINDOW_S)
        except (TimeoutError, asyncio.CancelledError):
            confirmed = False
        finally:
            self._confirm = None
        if confirmed and not self.latch.locked():
            return await self._act(action, d)
        await self._show("refused", text="not confirmed", ttl_ms=1200)
        return False

    async def _meta(self, intent: Intent, d: Decision) -> bool:
        """Undo, again, cancel and help. Returns whether the desktop changed, which is
        what a later clause in a chain is allowed to build on: cancel and help change
        nothing, so a clause that assumed they did must not run."""
        loop = asyncio.get_running_loop()
        if intent is Intent.CANCEL:
            self._cancel_pending("cancel", forget_badges=True)
            await self._show("hidden")
            return False
        if intent is Intent.HELP:
            await self.hud.send(self.hud.from_decision(d, self.world.state))
            return False
        call = self.executor.undo if intent is Intent.UNDO else self.executor.again
        outcome: Outcome = await loop.run_in_executor(self._worker, call)
        state = "heard" if outcome.ok else "refused"
        await self._show(state, text=d.heard, chip=outcome.message or intent.value, ttl_ms=1500)
        return outcome.ok

    async def _show(self, state: str, **fields) -> None:
        await self.hud.send({"t": "state", "state": state, **fields})


def _chip(decision: Decision) -> str:
    """What one clause did, for the line that says what already ran. Never its text: a
    recipe's spoken span is the speaker's words, exactly as dictation is."""
    return decision.action.describe() if decision.action is not None else decision.verdict.value


def build(cfg: Config) -> Engine:
    """Wire the real parts together. Imports are local so `hyprsay doctor` can report a
    missing piece instead of the whole CLI failing to import."""
    from . import a11ybus
    from . import adapters as adapter_pkg
    from . import jev as jev_pkg
    from .activation import PushToTalk
    from .audio import Recorder
    from .executor import Executor, boot
    from .executor import use as executor_registry
    from .hudproto import HudServer
    from .lexicon import Lexicon
    from .lock import Latch
    from .nlu.grammar import Grammar
    from .nlu.understand import Understander
    from .stt.hybrid import make_recognizer
    from .world import HyprSocket, WorldModel, use_socket_transport

    boot()
    sock = HyprSocket()
    use_socket_transport(sock)  # 6 to 22 ms per hyprctl spawn becomes 0.1 to 0.3 ms
    # and the same swap under the accessibility tree: reading one 48 control window was
    # 14.5 s through spawned busctl calls and is 1.8 s over one held connection
    a11ybus.use_fast_transport()
    world = WorldModel(sock)

    key = ""
    client = None
    try:
        key = jev_pkg.load_key()
    except jev_pkg.JevAuthError as exc:
        log.warning("no gateway key, running grammar-only and local speech only: %s", exc)
    if key and cfg.jev.enabled:
        client = jev_pkg.JevClient(
            key,
            route=cfg.jev.route,
            model=cfg.jev.model,
            deadline=cfg.jev.deadline_s,
            token_cap=cfg.jev.token_cap,
            zero_data_retention=cfg.jev.zero_data_retention,
        )

    lexicon = Lexicon.scan(cfg)
    latch = Latch(cfg, lambda: world.state, sock.query)
    acts = Executor(cfg, lambda: world.state, latch, lexicon)
    # recipes reach the executor through this, so a recipe cannot become a second way to
    # type that skips the pin, the lock latch and the character cap
    executor_registry(acts)
    return Engine(
        cfg,
        world=world,
        ptt=PushToTalk(cfg),
        recorder=Recorder(cfg),
        recognizer=make_recognizer(cfg, key),
        # the adapters are built ONCE and held: building them per utterance would open
        # the session bus on every word and throw away the handles an adapter minted for
        # the utterance before, which is the half of the contract that makes a handle
        # safe to be opaque (adapters/base.Minted)
        understander=Understander(cfg, lexicon, Grammar(), client, adapters=adapter_pkg.live(cfg)),
        executor=acts,
        latch=latch,
        hud=HudServer(hud=cfg.hud, countdown_s=cfg.safety.countdown_s),
        jev=client,
    )


def main(cfg: Config) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    engine = build(cfg)

    async def serve() -> None:
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, task.cancel)
        with contextlib.suppress(asyncio.CancelledError):
            await engine.run()

    asyncio.run(serve())
    return 0
