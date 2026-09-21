"""The engine: key down, speak, key up, and the thing happens.

One asyncio loop owns the conversation with the user. Everything slow or blocking is
pushed off it: the recognizer decodes in a thread, Jev is awaited, and every write to
the desktop runs on ONE worker thread because the inherited hypruse core keeps
process-global state.

The order of checks is the safety design, not an implementation detail:

    key down   -> locked? refuse. Pin the focused window. Open the mic. Warm Jev.
    key up     -> locked? drop the audio. Decode. Understand. (Cloud rescue, once.)
    decision   -> the verdict says how much ceremony the action needs
    act        -> the executor re-checks the lock and the target against FRESH state

A session lock at any point drops the utterance, cancels any countdown, closes the
microphone and zeroes the buffer. Hyprland skips ordinary binds while locked, so the
key-up never arrives in that case; the lock latch forces it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from . import journal
from .config import Config
from .model import Action, Candidate, Decision, Intent, Outcome, Transcript, Verdict

log = logging.getLogger("hyprsay")

# shorter than this is a stray tap, not speech
MIN_UTTERANCE_S = 0.25
# a confirming tap for tier 3 must be this short, so it cannot be a new utterance
CONFIRM_TAP_S = 0.5
CONFIRM_WINDOW_S = 4.0


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
        self._pending: asyncio.Task | None = None  # a countdown or a confirm wait
        self._meter: asyncio.Task | None = None
        self._confirm: asyncio.Future | None = None

    # ------------------------------------------------------------------ lifecycle

    async def run(self) -> None:
        self.latch.on_lock(self._on_lock)
        self.world.on_custom(self.ptt.feed_custom)
        tasks = [asyncio.create_task(self.world.run()), asyncio.create_task(self.hud.start())]
        try:
            async for event in self.ptt.events():
                try:
                    if event.kind == "down":
                        await self._key_down()
                    else:
                        await self._key_up(event.reason)
                except Exception:  # one bad utterance must never kill the daemon
                    log.exception("utterance failed")
                    await self._show("refused", text="internal error, see the log")
        finally:
            for task in tasks:
                task.cancel()
            self._worker.shutdown(wait=False, cancel_futures=True)
            with contextlib.suppress(Exception):
                await self.hud.close()

    def _on_lock(self) -> None:
        """The session locked. Runs from whatever thread noticed; touch nothing async here."""
        self.recorder.abort()
        self.ptt.force_up("locked")
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(self._cancel_pending, "session locked")

    _loop: asyncio.AbstractEventLoop | None = None

    def _cancel_pending(self, why: str) -> None:
        if self._pending and not self._pending.done():
            self._pending.cancel()
            log.info("pending action cancelled: %s", why)
        self._picking = ()

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
            self.recorder.start()
        except Exception as exc:
            await self._show("refused", text=f"microphone: {exc}", ttl_ms=3000)
            return
        if self.jev is not None:
            # speech outlasts a handshake: 526 ms -> 325 ms on the first call (measured)
            asyncio.create_task(self.jev.warm())
        await self._show("hearing", level=0.0)
        self._meter = asyncio.create_task(self._run_meter())

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
        pcm = self.recorder.stop()
        if reason == "locked" or self.latch.locked():
            await self._show("refused", text="session is locked", ttl_ms=1500)
            return
        seconds = len(pcm) / 2 / 16000
        if seconds < MIN_UTTERANCE_S:
            await self._show("hidden")
            return

        await self._show("thinking")
        started = time.perf_counter()
        transcript = await self.recognizer.transcribe(pcm, 16000)
        decision = await self._understand(transcript)

        # the local transcript led nowhere: ask the cloud about the SAME audio, once.
        # Cloud models held 8 of 8 on degraded audio where local fell to 6 or 7 (measured).
        rescue = getattr(self.recognizer, "rescue", None)
        if rescue is not None and decision.verdict in (Verdict.SUGGEST, Verdict.NOTHING):
            await self._show("still_thinking", text=transcript.text)
            with contextlib.suppress(Exception):
                second = await rescue(pcm, 16000)
                if (
                    second.text.strip()
                    and second.text.strip().lower() != transcript.text.strip().lower()
                ):
                    transcript, decision = second, await self._understand(second)

        journal.record(
            "utterance",
            seconds=round(seconds, 2),
            transcript=transcript,
            decision=decision,
            ms=round((time.perf_counter() - started) * 1000),
            exchange=getattr(self.understander, "last_exchange", None),
        )
        await self._carry_out(decision)

    async def _understand(self, transcript: Transcript) -> Decision:
        picking = self._picking if time.monotonic() < self._picking_until else ()
        return await self.understander.understand(
            transcript, self.world.state, pinned_address=self._pinned, picking=picking
        )

    # ------------------------------------------------------------------ decisions

    async def _carry_out(self, d: Decision) -> None:
        self._picking = ()
        action = d.action
        if action is not None and action.intent in (
            Intent.UNDO,
            Intent.AGAIN,
            Intent.CANCEL,
            Intent.HELP,
        ):
            await self._meta(action.intent, d)
            return

        if d.verdict is Verdict.ACT and action:
            await self._act(action, d)
        elif d.verdict is Verdict.ACT_SWAP and action:
            # free to reverse: act now, and let a number re-target it (docs/PLAN.md 5.6)
            if await self._act(action, d, badges=d.candidates):
                self._arm_picking(d.candidates, self.cfg.hud.swap_timeout_s)
        elif d.verdict is Verdict.HINTS:
            self._arm_picking(d.candidates, self.cfg.hud.hint_timeout_s)
            await self.hud.send(self.hud.from_decision(d, self.world.state))
        elif d.verdict is Verdict.COUNTDOWN and action:
            self._pending = asyncio.create_task(self._countdown(action, d))
        elif d.verdict is Verdict.CONFIRM_KEY and action:
            self._pending = asyncio.create_task(self._confirm_by_key(action, d))
        elif d.verdict is Verdict.NOTHING:
            await self._show("hidden")
        else:  # REFUSE, SUGGEST
            await self.hud.send(self.hud.from_decision(d, self.world.state))

    def _arm_picking(self, candidates: tuple[Candidate, ...], seconds: float) -> None:
        self._picking = candidates
        self._picking_until = time.monotonic() + seconds

    async def _act(self, action: Action, d: Decision, badges: tuple[Candidate, ...] = ()) -> bool:
        loop = asyncio.get_running_loop()
        outcome: Outcome = await loop.run_in_executor(
            self._worker, lambda: self.executor.execute(action, pinned_address=self._pinned)
        )
        journal.record("action", action=action, ok=outcome.ok, message=outcome.message)
        if not outcome.ok:
            await self._show("refused", text=outcome.message, ttl_ms=2500)
            return False
        shown = replace(d, verdict=Verdict.ACT_SWAP if badges else Verdict.ACT)
        await self.hud.send(self.hud.from_decision(shown, self.world.state))
        return True

    async def _countdown(self, action: Action, d: Decision) -> None:
        """Tier 2. Any key press during the countdown cancels it (see _key_down)."""
        total = self.cfg.safety.countdown_s
        await self.hud.send(
            {**self.hud.from_decision(d, self.world.state), "countdown_ms": int(total * 1000)}
        )
        try:
            await asyncio.sleep(total)
        except asyncio.CancelledError:
            await self._show("refused", text="cancelled", ttl_ms=1200)
            raise
        # the executor checks the lock again; this avoids even showing a success chip
        if self.latch.locked():
            return
        await self._act(action, d)

    async def _confirm_by_key(self, action: Action, d: Decision) -> None:
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
            await self._act(action, d)
        else:
            await self._show("refused", text="not confirmed", ttl_ms=1200)

    async def _meta(self, intent: Intent, d: Decision) -> None:
        loop = asyncio.get_running_loop()
        if intent is Intent.CANCEL:
            self._cancel_pending("cancel")
            await self._show("hidden")
        elif intent is Intent.HELP:
            await self.hud.send(self.hud.from_decision(d, self.world.state))
        else:
            call = self.executor.undo if intent is Intent.UNDO else self.executor.again
            outcome: Outcome = await loop.run_in_executor(self._worker, call)
            state = "heard" if outcome.ok else "refused"
            await self._show(state, text=d.heard, chip=outcome.message or intent.value, ttl_ms=1500)

    async def _show(self, state: str, **fields) -> None:
        await self.hud.send({"t": "state", "state": state, **fields})


def build(cfg: Config) -> Engine:
    """Wire the real parts together. Imports are local so `hyprsay doctor` can report a
    missing piece instead of the whole CLI failing to import."""
    from . import jev as jev_pkg
    from .activation import PushToTalk
    from .audio import Recorder
    from .executor import Executor, boot
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
    return Engine(
        cfg,
        world=world,
        ptt=PushToTalk(cfg),
        recorder=Recorder(cfg),
        recognizer=make_recognizer(cfg, key),
        understander=Understander(cfg, lexicon, Grammar(), client),
        executor=Executor(cfg, lambda: world.state, latch, lexicon),
        latch=latch,
        hud=HudServer(),
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
