"""The engine's orchestration, with every collaborator faked.

What is tested here is ordering and restraint: what must not start while locked, what must
never leave the machine, what a countdown and a confirmation wait for, and that one bad
utterance cannot take the daemon down. No microphone, no network, no compositor.
"""

import asyncio
import contextlib
import time
from dataclasses import replace

import pytest

from hyprsay import daemon
from hyprsay.config import Config, Safety
from hyprsay.model import (
    Action,
    Candidate,
    Decision,
    DesktopState,
    Intent,
    Outcome,
    Transcript,
    Verdict,
    Window,
)

WINDOW = Window("0x1", "firefox", "firefox", "a title", 1, "1", 0, focus_rank=0)
OTHER = Window("0x2", "firefox", "firefox", "another", 2, "2", 0, focus_rank=1)
STATE = DesktopState(windows=(WINDOW, OTHER), active_address="0x1", locked=False)
PCM = b"\x01\x00" * 16000  # one second


@pytest.fixture(autouse=True)
def no_journal(monkeypatch):
    monkeypatch.setenv("HYPRSAY_JOURNAL", "off")


class World:
    state = STATE

    def on_custom(self, callback):
        self.custom = callback

    async def run(self):
        await asyncio.Event().wait()


class Ptt:
    def __init__(self):
        self.held = False
        self.forced: list[str] = []

    def feed_custom(self, data):
        pass

    def force_up(self, reason):
        self.forced.append(reason)
        self.held = False

    async def events(self):
        await asyncio.sleep(0.05)  # long enough for the startup tasks to run
        return
        yield  # pragma: no cover - makes this an async generator that ends at once


class Recorder:
    def __init__(self, *, spoke=True, pcm=PCM, fails=False):
        self.spoke, self.pcm, self.fails = spoke, pcm, fails
        self.started = self.stopped = self.aborted = 0

    def start(self):
        if self.fails:
            raise RuntimeError("no input device")
        self.started += 1

    def stop(self):
        self.stopped += 1
        return self.pcm

    def abort(self):
        self.aborted += 1

    def level(self):
        return 0.5

    def voiced_after(self, seconds):
        return self.spoke


class Recognizer:
    name = "fake"

    def __init__(self, text="focus firefox", rescued: str | None = None):
        self.text, self.rescued = text, rescued
        self.calls = self.rescues = self.warmed = 0
        if rescued is None:
            self.rescue = None  # a local-only recognizer has no rescue at all

    async def warm(self):
        self.warmed += 1

    async def transcribe(self, pcm, sample_rate):
        self.calls += 1
        return Transcript(self.text, backend="local:fake")

    async def rescue(self, pcm, sample_rate):  # noqa: F811 - replaced by None when absent
        self.rescues += 1
        return Transcript(self.rescued, backend="gateway:fake", rescued=True)


class Understander:
    last_exchange: dict = {}

    def __init__(self, decisions):
        self.decisions = decisions  # transcript text -> Decision
        self.seen: list[tuple[str, tuple]] = []

    async def understand(self, transcript, state, *, pinned_address="", picking=()):
        self.seen.append((transcript.text, picking))
        return self.decisions[transcript.text]


class Executor:
    def __init__(self, outcome=None, outcomes=None):
        self.outcome = outcome or Outcome(True, "Done.")
        # per intent, for a chain where one clause must fail and the others must not
        self.outcomes = outcomes or {}
        self.executed: list[Action] = []
        self.pins: list[str] = []
        self.undone = 0

    def execute(self, action, *, pinned_address=""):
        self.executed.append(action)
        self.pins.append(pinned_address)
        return self.outcomes.get(action.intent, self.outcome)

    def undo(self):
        self.undone += 1
        return Outcome(True, "Undone.")

    def again(self):
        return Outcome(True, "Again.")


class Latch:
    def __init__(self, locked=False):
        self.value = locked
        self.callback = None

    def locked(self):
        return self.value

    def on_lock(self, callback):
        self.callback = callback


class Hud:
    def __init__(self):
        self.sent: list[dict] = []

    async def start(self):
        return True

    async def close(self):
        pass

    async def send(self, message):
        self.sent.append(message)

    def from_decision(self, decision, state=None):
        return {"t": "state", "state": decision.verdict.value, "text": decision.heard}

    @property
    def states(self):
        return [m["state"] for m in self.sent if m.get("state") != "hearing" or "level" not in m]


FOCUS = Action(Intent.FOCUS_WINDOW, window=WINDOW)
CLOSE = Action(Intent.CLOSE_WINDOW, window=WINDOW)
LOCK = Action(Intent.LOCK_SCREEN)


def engine(decisions=None, *, cfg=None, **parts):
    built = {
        "world": World(),
        "ptt": Ptt(),
        "recorder": Recorder(),
        "recognizer": Recognizer(),
        "understander": Understander(decisions or {"focus firefox": Decision(Verdict.ACT, FOCUS)}),
        "executor": Executor(),
        "latch": Latch(),
        "hud": Hud(),
    }
    built.update(parts)
    return daemon.Engine(cfg or Config(), **built), built


async def utter(e, parts, reason="key"):
    parts["ptt"].held = True
    await e._key_down()
    parts["ptt"].held = False
    await e._key_up(reason)


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------------------ the happy path


def test_a_clear_command_is_heard_understood_and_carried_out():
    e, p = engine()
    run(utter(e, p))
    assert p["recorder"].started == 1 and p["recorder"].stopped == 1
    assert p["executor"].executed == [FOCUS]
    assert p["hud"].states[-1] == "act"


def test_the_window_focused_at_key_down_is_what_the_understander_is_given():
    e, p = engine()

    async def go():
        await e._key_down()
        p["world"].state = replace(STATE, active_address="0x2")  # focus moves mid-utterance
        await e._key_up("key")

    run(go())
    assert e._pinned == "0x1"


def test_the_speech_model_is_loaded_at_startup_not_on_the_first_command():
    # found live: it loaded lazily, so the first command waited 2 to 6 s for the model
    e, p = engine()
    run(e.run())
    assert p["recognizer"].warmed == 1
    assert p["recognizer"].calls == 1  # the throwaway inference that warms the session
    assert p["executor"].executed == []


def test_a_model_that_cannot_load_is_reported_and_the_engine_keeps_running():
    class Broken(Recognizer):
        async def warm(self):
            raise RuntimeError("model files are missing")

    e, p = engine(recognizer=Broken())
    run(e.run())  # must not raise
    assert any("model files are missing" in m.get("text", "") for m in p["hud"].sent)


# ------------------------------------------------------------------------------ locked


def test_a_key_press_while_locked_never_opens_the_microphone():
    e, p = engine(latch=Latch(locked=True))
    run(e._key_down())
    assert p["recorder"].started == 0
    assert p["hud"].sent[-1]["state"] == "refused"


def test_audio_captured_before_a_lock_is_dropped_not_decoded():
    e, p = engine()

    async def go():
        await e._key_down()
        p["latch"].value = True
        await e._key_up("key")

    run(go())
    assert p["recognizer"].calls == 0
    assert p["executor"].executed == []


def test_the_lock_edge_aborts_the_recorder_and_forces_the_key_up():
    e, p = engine()

    async def go():
        await e._key_down()
        e._on_lock()

    run(go())
    assert p["recorder"].aborted == 1
    assert p["ptt"].forced == ["locked"]


# ------------------------------------------------------------------------------ what may leave


def test_a_held_key_with_nobody_talking_decodes_nothing_and_sends_nothing():
    recognizer = Recognizer(rescued="anything")
    e, p = engine(recorder=Recorder(spoke=False), recognizer=recognizer)
    run(utter(e, p))
    assert recognizer.calls == 0 and recognizer.rescues == 0
    assert p["hud"].states[-1] == "hidden"


def test_speech_not_meant_for_the_computer_is_never_sent_to_the_cloud():
    recognizer = Recognizer("so then i told him", rescued="anything")
    decisions = {"so then i told him": Decision(Verdict.NOTHING)}
    e, p = engine(decisions, recognizer=recognizer)
    run(utter(e, p))
    assert recognizer.rescues == 0
    assert p["executor"].executed == []


def test_an_addressed_but_garbled_command_gets_one_cloud_rescue_of_the_same_audio():
    recognizer = Recognizer("focus fire rocks", rescued="focus firefox")
    decisions = {
        # what the understander really returns when it could not place the words
        "focus fire rocks": Decision(Verdict.SUGGEST, reason="not understood", rehearable=True),
        "focus firefox": Decision(Verdict.ACT, FOCUS),
    }
    e, p = engine(decisions, recognizer=recognizer)
    run(utter(e, p))
    assert recognizer.rescues == 1
    assert p["executor"].executed == [FOCUS]


def test_a_rescue_that_hears_the_same_thing_changes_nothing():
    recognizer = Recognizer("blah blah", rescued="Blah blah")
    decisions = {"blah blah": Decision(Verdict.SUGGEST)}
    e, p = engine(decisions, recognizer=recognizer)
    run(utter(e, p))
    assert [text for text, _ in p["understander"].seen] == ["blah blah"]


def test_a_local_only_recognizer_is_never_asked_to_rescue():
    decisions = {"focus firefox": Decision(Verdict.SUGGEST)}
    e, p = engine(decisions, recognizer=Recognizer(rescued=None))
    run(utter(e, p))  # must not raise
    assert p["executor"].executed == []


# ------------------------------------------------------------------------------ ceremony


def fast(countdown=0.05):
    return Config(safety=Safety(countdown_s=countdown))


def test_a_close_waits_out_its_countdown_then_runs():
    e, p = engine({"focus firefox": Decision(Verdict.COUNTDOWN, CLOSE, tier=2)}, cfg=fast())

    async def go():
        await utter(e, p)
        assert p["executor"].executed == []  # not yet
        await e._pending

    run(go())
    assert p["executor"].executed == [CLOSE]


def test_any_key_press_during_the_countdown_cancels_it():
    e, p = engine({"focus firefox": Decision(Verdict.COUNTDOWN, CLOSE, tier=2)}, cfg=fast(0.5))

    async def go():
        await utter(e, p)
        await asyncio.sleep(0.02)
        await e._key_down()  # the cancel gesture
        await asyncio.sleep(0.6)

    run(go())
    assert p["executor"].executed == []


def test_a_countdown_that_expires_into_a_locked_session_does_not_run():
    e, p = engine({"focus firefox": Decision(Verdict.COUNTDOWN, CLOSE, tier=2)}, cfg=fast())

    async def go():
        await utter(e, p)
        p["latch"].value = True
        await e._pending

    run(go())
    assert p["executor"].executed == []


def test_a_session_action_needs_a_short_physical_tap():
    e, p = engine({"focus firefox": Decision(Verdict.CONFIRM_KEY, LOCK, tier=3)})

    async def go():
        await utter(e, p)
        await asyncio.sleep(0)
        await e._key_down()  # tap
        await e._key_up("key")
        await e._pending

    run(go())
    assert p["executor"].executed == [LOCK]
    assert p["recorder"].started == 1  # the confirming tap never opened the microphone


def test_holding_the_key_is_not_a_confirmation(monkeypatch):
    monkeypatch.setattr(daemon, "CONFIRM_TAP_S", 0.01)
    e, p = engine({"focus firefox": Decision(Verdict.CONFIRM_KEY, LOCK, tier=3)})

    async def go():
        await utter(e, p)
        await asyncio.sleep(0)
        await e._key_down()
        await asyncio.sleep(0.05)  # held too long to be a tap
        await e._key_up("key")
        await e._pending

    run(go())
    assert p["executor"].executed == []


def test_an_unconfirmed_session_action_times_out_and_does_not_run(monkeypatch):
    monkeypatch.setattr(daemon, "CONFIRM_WINDOW_S", 0.05)
    e, p = engine({"focus firefox": Decision(Verdict.CONFIRM_KEY, LOCK, tier=3)})

    async def go():
        await utter(e, p)
        await e._pending

    run(go())
    assert p["executor"].executed == []


# ------------------------------------------------------------------------------ badges


def test_swap_badges_arm_the_next_utterance_to_be_a_bare_number():
    rivals = (Candidate("Firefox 1", window=WINDOW), Candidate("Firefox 2", window=OTHER))
    second = Action(Intent.FOCUS_WINDOW, window=OTHER)
    decisions = {
        "focus firefox": Decision(Verdict.ACT_SWAP, FOCUS, rivals),
        "two": Decision(Verdict.ACT, second),
    }
    e, p = engine(decisions)

    async def go():
        await utter(e, p)
        p["recognizer"].text = "two"
        await utter(e, p)

    run(go())
    assert p["understander"].seen[0][1] == ()
    assert p["understander"].seen[1][1] == rivals  # picking mode, with the same candidates
    assert p["executor"].executed == [FOCUS, second]


def test_hints_do_not_act_but_do_arm_picking():
    rivals = (Candidate("Firefox 1", window=WINDOW), Candidate("Firefox 2", window=OTHER))
    e, p = engine({"focus firefox": Decision(Verdict.HINTS, FOCUS, rivals, tier=1)})
    run(utter(e, p))
    assert p["executor"].executed == []
    assert e._picking == rivals


def test_saying_cancel_clears_the_badges():
    rivals = (Candidate("a", window=WINDOW), Candidate("b", window=OTHER))
    decisions = {
        "focus firefox": Decision(Verdict.HINTS, FOCUS, rivals, tier=1),
        "cancel": Decision(Verdict.ACT, Action(Intent.CANCEL), heard="cancel"),
    }
    e, p = engine(decisions)

    async def go():
        await utter(e, p)
        p["recognizer"].text = "cancel"
        await utter(e, p)

    run(go())
    assert e._picking == ()


def test_a_lock_clears_the_badges_and_any_countdown():
    rivals = (Candidate("a", window=WINDOW), Candidate("b", window=OTHER))
    e, p = engine({"focus firefox": Decision(Verdict.HINTS, FOCUS, rivals, tier=1)})

    async def go():
        await utter(e, p)
        e._on_lock()
        await asyncio.sleep(0)  # the lock callback hops onto the loop

    run(go())
    assert e._picking == ()


def test_badges_expire():
    rivals = (Candidate("a", window=WINDOW), Candidate("b", window=OTHER))
    e, p = engine({"focus firefox": Decision(Verdict.HINTS, FOCUS, rivals, tier=1)})

    async def go():
        await utter(e, p)
        e._picking_until = 0.0  # long ago
        await utter(e, p)

    run(go())
    assert p["understander"].seen[1][1] == ()


# ------------------------------------------------------------------------------ robustness


def test_a_refused_action_is_shown_as_refused_not_as_done():
    e, p = engine(executor=Executor(Outcome(False, "the window changed")))
    run(utter(e, p))
    assert p["hud"].sent[-1] == {
        "t": "state",
        "state": "refused",
        "text": "the window changed",
        "ttl_ms": 2500,
    }


def test_a_microphone_that_cannot_open_is_reported_not_raised():
    e, p = engine(recorder=Recorder(fails=True))
    run(e._key_down())
    assert "microphone" in p["hud"].sent[-1]["text"]


def test_undo_goes_to_the_executor_not_through_a_new_action():
    e, p = engine({"focus firefox": Decision(Verdict.ACT, Action(Intent.UNDO), heard="undo")})
    run(utter(e, p))
    assert p["executor"].undone == 1 and p["executor"].executed == []


# ------------------------------------------------------------ what the review found leaking


def test_a_misheard_carrier_still_counts_as_dictation_and_no_audio_is_uploaded():
    # "type my password" heard as "typed my password": the words must not go to a model,
    # and neither must the recording of them
    recognizer = Recognizer("typed my password is hunter2", rescued="type my password")
    decisions = {
        "typed my password is hunter2": Decision(
            Verdict.SUGGEST, reason="that sounded like dictation", dictation=True
        )
    }
    e, p = engine(decisions, recognizer=recognizer)
    run(utter(e, p))
    assert recognizer.rescues == 0
    assert all("hunter2" not in str(m) for m in p["hud"].sent)


def test_a_suggestion_that_rehearing_cannot_fix_does_not_upload_the_clip():
    # "Jev is off", "hyprsay does not do that": the recognizer was never the problem
    recognizer = Recognizer("bring up my notes", rescued="bring up my notes")
    decisions = {
        "bring up my notes": Decision(
            Verdict.SUGGEST, reason="Jev is turned off"
        )  # rehearable False
    }
    e, p = engine(decisions, recognizer=recognizer)
    run(utter(e, p))
    assert recognizer.rescues == 0


def test_the_overlay_never_shows_the_transcript_while_waiting_on_the_cloud():
    recognizer = Recognizer("my bank password is swordfish", rescued="focus firefox")
    decisions = {
        "my bank password is swordfish": Decision(Verdict.SUGGEST, reason="?", rehearable=True),
        "focus firefox": Decision(Verdict.ACT, FOCUS),
    }
    e, p = engine(decisions, recognizer=recognizer)
    run(utter(e, p))
    assert recognizer.rescues == 1
    assert all("swordfish" not in str(m) for m in p["hud"].sent)


def test_a_silent_clip_is_never_uploaded_even_though_the_key_clicked():
    # one voiced frame from the key press is not speech: an empty transcript with no
    # real voice behind it must not become an upload of a second of the room
    recognizer = Recognizer("", rescued="anything")
    e, p = engine(recorder=Recorder(spoke=False), recognizer=recognizer)
    run(utter(e, p))
    assert recognizer.calls == 0 and recognizer.rescues == 0


def test_a_lock_during_decoding_drops_the_utterance_before_it_is_understood():
    class Locking(Recognizer):
        def __init__(self, latch):
            super().__init__()
            self.latch = latch

        async def transcribe(self, pcm, sample_rate):
            self.latch.value = True  # the screen locks while the model is working
            return await super().transcribe(pcm, sample_rate)

    latch = Latch()
    e, p = engine(recognizer=Locking(latch), latch=latch)
    run(utter(e, p))
    assert p["understander"].seen == []  # never reached the model
    assert p["executor"].executed == []
    assert p["hud"].sent[-1]["state"] == "refused"


def test_a_lock_after_understanding_still_stops_the_action():
    class LockOnUnderstand(Understander):
        def __init__(self, decisions, latch):
            super().__init__(decisions)
            self.latch = latch

        async def understand(self, transcript, state, **kwargs):
            decision = await super().understand(transcript, state, **kwargs)
            self.latch.value = True
            return decision

    latch = Latch()
    e, p = engine(
        understander=LockOnUnderstand({"focus firefox": Decision(Verdict.ACT, FOCUS)}, latch),
        latch=latch,
    )
    run(utter(e, p))
    assert p["executor"].executed == []


class SlowExecutor(Executor):
    """Closing a window takes real time, so a cancel can land while it is happening."""

    def execute(self, action, *, pinned_address=""):
        time.sleep(0.15)
        return super().execute(action, pinned_address=pinned_address)


def test_a_cancel_that_lands_while_the_action_runs_says_it_was_too_late():
    # the window really does close: swallowing the cancellation would leave the user
    # believing they stopped it
    e, p = engine(
        {"focus firefox": Decision(Verdict.COUNTDOWN, CLOSE, tier=2)},
        cfg=fast(0.01),
        executor=SlowExecutor(),
    )

    async def go():
        await utter(e, p)
        await asyncio.sleep(0.06)  # the countdown has expired; the close is under way
        e._cancel_pending("key pressed")
        with contextlib.suppress(asyncio.CancelledError):
            await e._pending

    run(go())
    assert p["executor"].executed == [CLOSE]  # it happened
    assert any("too late" in str(m.get("text", "")) for m in p["hud"].sent)  # and was said


def test_a_cancel_before_the_action_starts_still_stops_it():
    e, p = engine(
        {"focus firefox": Decision(Verdict.COUNTDOWN, CLOSE, tier=2)},
        cfg=fast(0.4),
        executor=SlowExecutor(),
    )

    async def go():
        await utter(e, p)
        await asyncio.sleep(0.02)  # well inside the countdown
        e._cancel_pending("key pressed")
        with contextlib.suppress(asyncio.CancelledError):
            await e._pending

    run(go())
    assert p["executor"].executed == []


def test_a_second_press_opens_the_microphone_without_waiting_for_the_first_to_finish():
    """The whole point of push to talk is that the key works when you press it. The
    pipeline used to be awaited inline, so a press during the previous decode opened the
    microphone half a second late and the first word of the next command was lost."""
    from hyprsay.activation import PttEvent

    order: list[str] = []

    class SlowRecognizer(Recognizer):
        async def transcribe(self, pcm, sample_rate):
            if len(pcm) < len(PCM):  # the startup warm-up, not an utterance
                return await super().transcribe(pcm, sample_rate)
            order.append("decode-start")
            await asyncio.sleep(0.2)
            order.append("decode-end")
            return await super().transcribe(pcm, sample_rate)

    class Scripted(Ptt):
        async def events(self):
            yield PttEvent("down", "key", 0.0)
            yield PttEvent("up", "key", 0.1)
            await asyncio.sleep(0.05)  # the first utterance is still decoding
            yield PttEvent("down", "key", 0.2)
            await asyncio.sleep(0.4)  # let both finish before the loop ends

    class Watching(Recorder):
        def start(self):
            order.append("mic-open")
            super().start()

    e, p = engine(recognizer=SlowRecognizer(), ptt=Scripted(), recorder=Watching())
    run(e.run())
    assert order.count("mic-open") == 2
    # the second press opened the microphone while the first was still being decoded
    assert order.index("mic-open", order.index("mic-open") + 1) < order.index("decode-end")
    assert "decode-end" in order  # and the first utterance still ran to completion


# ------------------------------------------------------- one utterance, several commands

LAUNCH = Action(Intent.LAUNCH_APP)
MOVE = Action(Intent.MOVE_TO_WORKSPACE, workspace="3")
MUTE = Action(Intent.VOLUME, verb="mute")
SCROLL = Action(Intent.SCROLL, direction=None)


def chain(*decisions, cfg=None, **parts):
    """One utterance whose first decision carries the rest."""
    first, rest = decisions[0], decisions[1:]
    return engine({"focus firefox": replace(first, rest=rest)}, cfg=cfg, **parts)


def test_a_chain_runs_its_clauses_in_the_order_they_were_spoken():
    e, p = chain(
        Decision(Verdict.ACT, LAUNCH, heard="open firefox"),
        Decision(Verdict.ACT, MOVE),
        Decision(Verdict.ACT, MUTE),
    )
    run(utter(e, p))
    assert p["executor"].executed == [LAUNCH, MOVE, MUTE]


def test_a_chain_stops_the_moment_a_clause_is_refused():
    e, p = chain(
        Decision(Verdict.ACT, LAUNCH),
        Decision(Verdict.REFUSE, reason="that window is gone"),
        Decision(Verdict.ACT, MUTE),
    )
    run(utter(e, p))
    assert p["executor"].executed == [LAUNCH]


def test_a_chain_stops_when_a_clause_only_asks_and_does_not_leave_badges_up():
    """A number answered now could only finish the clause it belongs to, and the rest of
    the utterance would be lost: that is the half-done outcome the chain exists to stop."""
    rivals = (Candidate("a", window=WINDOW), Candidate("b", window=OTHER))
    e, p = chain(
        Decision(Verdict.ACT, LAUNCH),
        Decision(Verdict.HINTS, MOVE, rivals, tier=1, reason="several windows fit"),
        Decision(Verdict.ACT, MUTE),
    )
    run(utter(e, p))
    assert p["executor"].executed == [LAUNCH]
    assert e._picking == ()


def test_a_chain_that_stopped_says_which_part_stopped_it_and_what_already_ran():
    e, p = chain(
        Decision(Verdict.ACT, LAUNCH),
        Decision(Verdict.REFUSE, reason="that window is gone"),
        Decision(Verdict.ACT, MUTE),
    )
    run(utter(e, p))
    said = p["hud"].sent[-1]["text"]
    assert "part 2 of 3" in said
    assert "that window is gone" in said
    assert "already did" in said and "launch app" in said


def test_a_chain_whose_first_clause_fails_says_nothing_was_done():
    e, p = chain(
        Decision(Verdict.REFUSE, reason="session is locked"),
        Decision(Verdict.ACT, MUTE),
    )
    run(utter(e, p))
    assert p["executor"].executed == []
    assert "nothing was done" in p["hud"].sent[-1]["text"]


def test_a_clause_the_executor_refuses_stops_the_rest_of_the_chain():
    executor = Executor(outcomes={Intent.MOVE_TO_WORKSPACE: Outcome(False, "the window changed")})
    e, p = chain(
        Decision(Verdict.ACT, LAUNCH),
        Decision(Verdict.ACT, MOVE),
        Decision(Verdict.ACT, MUTE),
        executor=executor,
    )
    run(utter(e, p))
    assert executor.executed == [LAUNCH, MOVE]


def test_the_lock_is_asked_again_between_two_clauses():
    class LockAfterFirst(Executor):
        def __init__(self, latch):
            super().__init__()
            self.latch = latch

        def execute(self, action, *, pinned_address=""):
            self.latch.value = True  # the screen locks while the first clause runs
            return super().execute(action, pinned_address=pinned_address)

    latch = Latch()
    e, p = chain(
        Decision(Verdict.ACT, LAUNCH),
        Decision(Verdict.ACT, MUTE),
        executor=LockAfterFirst(latch),
        latch=latch,
    )
    run(utter(e, p))
    assert p["executor"].executed == [LAUNCH]
    assert p["hud"].sent[-1]["text"] == "session is locked"


def test_a_clause_with_a_blank_target_is_not_pinned_to_the_window_at_key_down():
    """It means "whatever the clause before it left in front of me". Pinning it to key
    down would aim a scroll at the terminal the key was held over."""
    e, p = chain(Decision(Verdict.ACT, LAUNCH), Decision(Verdict.ACT, SCROLL))
    run(utter(e, p))
    assert p["executor"].executed == [LAUNCH, SCROLL]
    assert p["executor"].pins == ["0x1", ""]


def test_a_clause_that_names_its_own_window_keeps_the_pinned_address():
    typed = Action(Intent.TYPE_TEXT, window=WINDOW, text="hello")
    e, p = chain(Decision(Verdict.ACT, FOCUS), Decision(Verdict.ACT, typed))
    run(utter(e, p))
    assert p["executor"].pins == ["0x1", "0x1"]


def test_a_countdown_inside_a_chain_is_waited_for_before_the_next_clause():
    e, p = chain(
        Decision(Verdict.ACT, LAUNCH),
        Decision(Verdict.COUNTDOWN, CLOSE, tier=2),
        Decision(Verdict.ACT, MUTE),
        cfg=fast(),
    )
    run(utter(e, p))
    assert p["executor"].executed == [LAUNCH, CLOSE, MUTE]


def test_a_countdown_cancelled_mid_chain_stops_everything_after_it():
    e, p = chain(
        Decision(Verdict.ACT, LAUNCH),
        Decision(Verdict.COUNTDOWN, CLOSE, tier=2),
        Decision(Verdict.ACT, MUTE),
        cfg=fast(0.5),
    )

    async def go():
        task = asyncio.ensure_future(utter(e, p))
        await asyncio.sleep(0.05)
        await e._key_down()  # the cancel gesture
        await task

    run(go())
    assert p["executor"].executed == [LAUNCH]


def test_a_chain_that_ran_through_says_what_every_part_did():
    e, p = chain(
        Decision(Verdict.ACT, LAUNCH, heard="open firefox and mute"),
        Decision(Verdict.ACT, MUTE),
    )
    run(utter(e, p))
    chip = p["hud"].sent[-1]["chip"]
    assert "launch app" in chip and "volume mute" in chip


def test_a_single_decision_still_behaves_exactly_as_it_did_before():
    e, p = engine({"focus firefox": Decision(Verdict.ACT, FOCUS)})
    run(utter(e, p))
    assert p["executor"].executed == [FOCUS]
    assert p["executor"].pins == ["0x1"]
    assert p["hud"].states[-1] == "act"
