"""What the machine looks like, what the last command did, and the hash of both.

Three rules are stated here as sentences, one per test: what a fingerprint must and must
not notice, what a turn may remember and what it must forget, and what the context may
tell a model about a desktop. Nothing here reads a compositor, a bus, a microphone or the
network: the desktop is a fixture, Jev is the shipped fake, and the clock is injected.
"""

import asyncio
import json
from dataclasses import replace

import pytest

from hyprsay import daemon
from hyprsay.config import Config, Safety
from hyprsay.jev.fake import FakeEvaluator
from hyprsay.model import (
    Action,
    App,
    Candidate,
    Decision,
    DesktopState,
    Direction,
    Intent,
    Monitor,
    Outcome,
    Parse,
    Slots,
    Transcript,
    Verdict,
    Window,
    Workspace,
)
from hyprsay.nlu import context
from hyprsay.nlu.understand import Understander

# --------------------------------------------------------------------------- fixtures


def win(address, cls, rank, title="", workspace=1) -> Window:
    return Window(
        address=address,
        cls=cls,
        initial_class=cls,
        title=title,
        workspace_id=workspace,
        workspace_name=str(workspace),
        monitor=0,
        focus_rank=rank,
    )


TERMINAL = win("0x1", "kitty", 0, "zsh: ~/secret-project")
FOX = win("0x2", "firefox", 1, "YouTube - cats compilation", workspace=2)
MUSIC = win("0x4", "spotify", 2, "Artist - Song", workspace=3)
STRANGER = win("0x9", "some-daemon", 3, "whatever it likes", workspace=3)


def desktop(*windows, active="0x1", locked=False) -> DesktopState:
    windows = windows or (TERMINAL, FOX, MUSIC)
    return DesktopState(
        windows=windows,
        workspaces=tuple(
            Workspace(i, str(i), "eDP-1") for i in sorted({w.workspace_id for w in windows})
        ),  # fmt: skip
        monitors=(Monitor(0, "eDP-1", focused=True, active_workspace_id=1),),
        active_address=active,
        active_workspace_id=1,
        locked=locked,
    )


DESKTOP = desktop()


def app(id, name, kind, trusted=True) -> App:
    return App(id=id, name=name, kind=kind, wm_classes=(id,), trusted=trusted, exec_argv=(id,))


FIREFOX = app("firefox", "Firefox", "web browser")
SPOTIFY = app("spotify", "Spotify", "music player")
KITTY = app("kitty", "kitty", "terminal")
GIMP = app("gimp", "GIMP", "image editor")
EVIL = app("some-daemon", "Evil Tool", "system utility", trusted=False)
APPS = (FIREFOX, SPOTIFY, KITTY, GIMP, EVIL)


class FakeLexicon:
    """Trusted fields only: a thing is named by a word of its name or of its kind."""

    def __init__(self, apps=APPS) -> None:
        self.apps = tuple(apps)

    @staticmethod
    def _anchors(one: App) -> set[str]:
        return set(one.name.lower().split()) | set(one.kind.lower().split())

    def _names(self, text: str, one: App) -> bool:
        return one.trusted and bool(set(text.lower().split()) & self._anchors(one))

    def app_for_window(self, window):
        return next((a for a in self.apps if window.cls.lower() in a.wm_classes), None)

    def kind_of(self, window):
        one = self.app_for_window(window)
        return one.kind if one and one.trusted else ""

    def words(self):
        return frozenset(w for a in self.apps if a.trusted for w in self._anchors(a))

    def match_apps(self, phrase, limit=5):
        named = [a for a in self.apps if self._names(phrase, a)]
        return [(a, 1.0) for a in named][:limit]

    def match_windows(self, phrase, state, limit=5):
        hits = [
            w
            for w in sorted(state.windows, key=lambda w: w.focus_rank)
            if (one := self.app_for_window(w)) is not None and self._names(phrase, one)
        ]
        return [(w, 1.0) for w in hits][:limit]

    def title_discriminates(self, residual, candidates):
        return None

    def corroborates(self, utterance, *, app=None, window=None):
        owner = app if app is not None else self.app_for_window(window)
        return owner is not None and self._names(utterance, owner)


class Norm:
    def __init__(self, text, raw) -> None:
        self.text, self.raw = text, raw
        self.tokens, self.variants = tuple(text.split()), ()


def normalizer(raw, vocabulary=(), aliases=None) -> Norm:
    return Norm(" ".join(raw.lower().replace(".", " ").split()), raw)


class FakeGrammar:
    def __init__(self, parses=None) -> None:
        self.parses = parses or {}

    def parse(self, norm, *, picking=False):
        return self.parses.get(norm.text)

    def examples(self, intent):
        return (f"{intent.value} example",)


class Clock:
    """The injected clock, so a 30 second memory is testable in a millisecond."""

    def __init__(self, now=1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


ADDRESSED = {"addressed": 0.95}


def build(script=None, *, parses=None, cfg=None, clock=None, apps=APPS):
    jev = FakeEvaluator(script)
    understander = Understander(
        cfg or Config(),
        FakeLexicon(apps),
        FakeGrammar(parses),
        jev,
        normalizer=normalizer,
        clock=clock or Clock(),
    )
    return understander, jev


def say(understander, text, state=DESKTOP, **kwargs):
    return asyncio.run(understander.understand(Transcript(text, "test"), state, **kwargs))


def focus_of(window) -> Decision:
    return Decision(Verdict.ACT, Action(Intent.FOCUS_WINDOW, window=window))


# --------------------------------------------------------------------------- fingerprint


def test_the_same_desktop_always_fingerprints_the_same():
    assert context.fingerprint(DESKTOP) == context.fingerprint(desktop())


def test_a_window_that_opens_changes_the_fingerprint():
    more = desktop(TERMINAL, FOX, MUSIC, STRANGER)
    assert context.fingerprint(more) != context.fingerprint(DESKTOP)


def test_a_window_that_moves_to_another_workspace_changes_the_fingerprint():
    moved = desktop(TERMINAL, replace(FOX, workspace_id=7), MUSIC)
    assert context.fingerprint(moved) != context.fingerprint(DESKTOP)


def test_moving_the_focus_does_not_change_the_fingerprint():
    """Hyprland follows the mouse by default, so the focused window and the active
    workspace change while the speaker is doing nothing but moving the pointer. A
    freshness gate that noticed would refuse a close because the mouse crossed a window.
    """
    elsewhere = replace(DESKTOP, active_address="0x2", active_workspace_id=2)
    assert context.fingerprint(elsewhere) == context.fingerprint(DESKTOP)


def test_a_window_that_rewrites_its_title_does_not_change_the_fingerprint():
    """A title is written by whoever owns the window, including a page that rewrites it
    every second, so it may not decide whether an action is still allowed to happen."""
    renamed = desktop(TERMINAL, replace(FOX, title="Spotify - Premium"), MUSIC)
    assert context.fingerprint(renamed) == context.fingerprint(DESKTOP)


def test_a_locked_session_fingerprints_differently_from_an_unlocked_one():
    assert context.fingerprint(desktop(locked=True)) != context.fingerprint(DESKTOP)


def test_an_empty_stamp_never_calls_the_desktop_moved():
    """A decision nobody fingerprinted is judged by the tier gates alone, as before."""
    assert context.moved("", DESKTOP) is False
    assert context.moved(context.fingerprint(DESKTOP), DESKTOP) is False
    assert context.moved(context.fingerprint(DESKTOP), desktop(TERMINAL)) is True


# --------------------------------------------------------------------------- turns


def remember(decision, said="go to spotify", now=1000.0):
    return context.remembered(decision, said=said, now=now, stamp="abc", lexicon=FakeLexicon())


def test_only_a_decision_that_will_change_the_desktop_is_remembered():
    assert remember(focus_of(MUSIC)) is not None
    assert remember(Decision(Verdict.HINTS, Action(Intent.FOCUS_WINDOW))) is None
    assert remember(Decision(Verdict.REFUSE, reason="no")) is None
    assert remember(Decision(Verdict.NOTHING)) is None
    assert remember(Decision(Verdict.ACT)) is None  # a verdict with no action at all


def test_a_turn_remembers_what_it_targeted_by_its_trusted_name():
    turn = remember(focus_of(MUSIC))
    assert (turn.app, turn.kind, turn.intent) == ("Spotify", "music player", "focus_window")
    assert turn.address == "0x4"
    assert turn.said == "go to spotify"


def test_a_turn_that_carried_the_speakers_own_text_remembers_no_words():
    """Dictation and a recipe's spoken span are the speaker's words, not a command, so
    the turn records that something was typed and never what."""
    typed = Decision(
        Verdict.ACT, Action(Intent.TYPE_TEXT, window=TERMINAL, text="my password is hunter2")
    )
    assert remember(typed, said="type my password is hunter2").said == ""
    recipe = Decision(Verdict.ACT, Action(Intent.RUN_RECIPE, window=FOX, verb="open_url"))
    assert remember(recipe, said="go to youtube dot com").said == ""


def test_an_untrusted_desktop_file_names_nothing_in_a_turn():
    """The same rule `Lexicon.kind_of` already applies: a user-writable entry must not be
    able to relabel anything."""
    turn = remember(focus_of(STRANGER))
    assert (turn.app, turn.kind) == ("", "")
    assert turn.address == "0x9"


def test_a_turn_older_than_the_ttl_is_not_in_force():
    turn = remember(focus_of(MUSIC), now=1000.0)
    assert turn.live(1000.0 + context.TTL_S - 1) is True
    assert turn.live(1000.0 + context.TTL_S + 1) is False
    assert turn.seconds_ago(1006.4) == 6


def test_only_the_last_three_commands_are_kept_and_the_newest_is_first():
    ctx = context.Context()
    for index in range(5):
        ctx = ctx.remember(context.Turn(said=f"command {index}", at=1000.0))
    assert [turn.said for turn in ctx.recent] == ["command 4", "command 3", "command 2"]
    assert len(ctx.recent) == context.MAX_TURNS


def test_an_expired_turn_is_dropped_when_the_next_utterance_arrives():
    ctx = context.Context().remember(context.Turn(said="go to spotify", at=1000.0))
    assert ctx.about(DESKTOP, now=1020.0).recent
    assert not ctx.about(DESKTOP, now=1040.0).recent


def test_the_executors_answer_lands_on_the_newest_turn_only():
    ctx = context.Context()
    ctx = ctx.remember(context.Turn(said="one", at=1000.0))
    ctx = ctx.remember(context.Turn(said="two", at=1001.0)).did(False)
    assert [turn.ok for turn in ctx.recent] == [False, None]
    assert context.Context().did(True).recent == ()


def test_forgetting_drops_the_turns_and_the_badges_together():
    ctx = context.Context(
        recent=(context.Turn(said="one", at=1000.0),),
        picking=(Candidate("Firefox", window=FOX),),
        correction=True,
    )
    assert ctx.forgotten() == replace(ctx, recent=(), picking=(), correction=False)


# --------------------------------------------------------------------------- corrections


def with_history(tokens, now=1000.0):
    ctx = context.Context().remember(context.Turn(said="go to spotify", at=1000.0))
    return ctx.about(DESKTOP, now=now, tokens=tokens).correction


def test_a_refusal_at_the_front_of_an_utterance_is_a_correction():
    assert with_history(("no", "the", "other", "one"))
    assert with_history(("nope",))
    assert with_history(("wrong", "window"))


def test_the_other_one_and_instead_are_corrections():
    assert with_history(("the", "other", "one"))
    assert with_history(("open", "firefox", "instead"))
    assert with_history(("not", "that", "one"))


def test_an_ordinary_command_that_merely_says_no_is_not_a_correction():
    """ "no" is also a word in the middle of a command, which is why the openers are read
    in the first two words only and the pairs have to be both words."""
    assert not with_history(("move", "it", "to", "the", "one", "with", "no", "terminal"))
    assert not with_history(("go", "to", "the", "browser"))


def test_a_correction_of_nothing_is_not_a_correction():
    plain = context.Context().about(DESKTOP, now=1000.0, tokens=("no", "not", "that"))
    assert plain.correction is False


def test_a_correction_of_something_that_has_expired_is_not_a_correction():
    assert not with_history(("no", "the", "other", "one"), now=1100.0)


# --------------------------------------------------------------------------- the payload


def shown(ctx) -> dict:
    return ctx.for_request()


def context_for(*, now=1000.0, tokens=(), state=DESKTOP, turn=None):
    ctx = context.Context(lexicon=FakeLexicon())
    if turn is not None:
        ctx = ctx.remember(turn)
    return ctx.about(state, now=now, tokens=tokens, lexicon=FakeLexicon())


def test_the_context_names_the_applications_that_are_open_most_recent_first():
    block = shown(context_for())
    assert block["open_apps"] == ["kitty", "Firefox", "Spotify"]
    assert block["current_workspace"] == "1"
    assert block["focused"] == {"app": "kitty", "kind": "terminal"}


def test_a_window_no_trusted_desktop_file_claims_is_not_named_at_all():
    """Naming it from its own class would let a window name itself to the model."""
    block = shown(context_for(state=desktop(TERMINAL, STRANGER)))
    assert block["open_apps"] == ["kitty"]


def test_the_same_application_twice_is_named_once():
    twice = desktop(TERMINAL, FOX, replace(FOX, address="0x3", focus_rank=4))
    assert shown(context_for(state=twice))["open_apps"] == ["kitty", "Firefox"]


def test_nothing_focused_means_nothing_is_said_about_what_is_focused():
    assert "focused" not in shown(context_for(state=desktop(TERMINAL, active="0xdead")))


def test_no_title_no_address_and_no_untrusted_name_is_in_the_context():
    turn = remember(focus_of(MUSIC), said="go to spotify")
    busy = desktop(TERMINAL, FOX, MUSIC, STRANGER)
    payload = json.dumps(shown(context_for(turn=turn, state=busy)))
    for secret in ("0x1", "0x4", "secret-project", "cats compilation", "Artist - Song", "Evil"):
        assert secret not in payload


def test_the_previous_command_is_reported_with_how_long_ago_it_was():
    turn = remember(focus_of(MUSIC), said="go to spotify", now=994.0)
    previous = shown(context_for(turn=turn))["previous"]
    assert previous == {
        "did": "focus_window",
        "seconds_ago": 6,
        "utterance": "go to spotify",
        "on": {"app": "Spotify", "kind": "music player"},
    }


def test_what_the_previous_command_acted_on_is_dropped_once_that_window_has_gone():
    """A window that has gone is not context, it is a wrong answer waiting to happen."""
    turn = remember(focus_of(MUSIC), said="go to spotify", now=994.0)
    previous = shown(context_for(turn=turn, state=desktop(TERMINAL, FOX)))["previous"]
    assert "on" not in previous and previous["did"] == "focus_window"


def test_a_command_the_executor_refused_is_reported_as_failed():
    turn = replace(remember(focus_of(MUSIC), now=994.0), ok=False)
    assert shown(context_for(turn=turn))["previous"]["outcome"] == "failed"
    working = replace(turn, ok=True)
    assert "outcome" not in shown(context_for(turn=working))["previous"]


def test_a_correction_is_reported_only_when_there_is_something_to_correct():
    turn = remember(focus_of(MUSIC), now=994.0)
    assert shown(context_for(turn=turn, tokens=("no", "not", "that")))["correcting"] is True
    assert "correcting" not in shown(context_for(tokens=("no", "not", "that")))


# --------------------------------------------------------------------------- understanding


def test_every_utterance_is_stamped_with_the_desktop_it_was_decided_against():
    u, _ = build({**ADDRESSED, "intent": "none"})
    say(u, "whatever")
    assert u.context.fingerprint == context.fingerprint(DESKTOP)
    assert u.last_exchange["fingerprint"] == context.fingerprint(DESKTOP)


def test_an_action_is_remembered_and_a_question_is_not():
    parses = {"go to spotify": Parse(Intent.FOCUS_WINDOW, Slots(window_ref="spotify"))}
    u, _ = build(parses=parses)
    assert say(u, "go to spotify").verdict is Verdict.ACT
    assert [turn.app for turn in u.context.recent] == ["Spotify"]
    say(u, "mumble mumble")
    assert [turn.app for turn in u.context.recent] == ["Spotify"]  # nothing new was added


def test_the_executors_answer_reaches_the_memory_of_the_utterance_that_earned_it():
    parses = {"go to spotify": Parse(Intent.FOCUS_WINDOW, Slots(window_ref="spotify"))}
    u, _ = build(parses=parses)
    say(u, "go to spotify")
    u.acted(False)
    assert u.context.recent[0].ok is False


def test_forgetting_clears_the_pending_template_and_the_memory_together():
    parses = {"go to spotify": Parse(Intent.FOCUS_WINDOW, Slots(window_ref="spotify"))}
    u, _ = build(parses=parses)
    say(u, "go to spotify")
    u.pending = Action(Intent.FOCUS_WINDOW)
    u.forget()
    assert u.pending is None and u.context.recent == ()


def test_the_context_block_is_built_for_every_fan_out_and_is_still_not_sent():
    """docs/STRATEGY.md 4.1 wants this evidence in the request. What leaves the machine
    is pinned by docs/PRIVACY.md and by a test in test_understand.py, so until those
    change together the block is built and journalled and the request is unchanged.
    """
    u, jev = build({**ADDRESSED, "intent": "focus_window", "window": "w1", "is_w1": 0.9})
    say(u, "go to the browser")
    assert u.last_exchange["context"]["open_apps"] == ["kitty", "Firefox", "Spotify"]
    for state, _ in jev.calls:
        assert set(state) == {"utterance", "variants", "note"}


# --------------------------------------------------------------------------- the plan


PLAN_DESKTOP = desktop(TERMINAL, FOX, MUSIC)


def plan_script(**extra):
    """Everything R1 needs to reach a decision at all, plus the step answers."""
    return {**ADDRESSED, "intent": "none", **extra}


def test_a_two_step_answer_becomes_one_decision_that_carries_the_rest():
    """The bug this path exists for: "open gimp go to workspace two" has no connective in
    it, so `nlu/clauses.py` proposes no seam and nothing above could ever cut it."""
    u, _ = build(plan_script(step_1="a1", step_2="ws2", step_3="none"))
    decision = say(u, "open gimp go to workspace two", PLAN_DESKTOP)
    assert decision.action.intent is Intent.LAUNCH_APP
    assert decision.action.app.id == "gimp"
    assert len(decision.rest) == 1
    assert decision.rest[0].action.intent is Intent.SWITCH_WORKSPACE
    assert decision.rest[0].action.workspace == "2"
    assert u.last_exchange["path"] == "plan"


def test_a_one_step_answer_is_left_to_the_paths_that_already_answer_it():
    """A single target goes through R2 and R2b, two formulations that have to agree, and
    a step Choice has no equivalent of that agreement."""
    u, _ = build(plan_script(step_1="ws2", step_2="none", step_3="none"))
    decision = say(u, "workspace two", PLAN_DESKTOP)
    assert u.last_exchange["path"] != "plan"
    assert decision.rest == ()


def test_a_layout_step_after_another_step_leaves_its_target_to_the_write():
    """The handle names the window focused when the machine was harvested, and by the
    time a second step runs the first one has moved the desktop. A blank target is
    settled against fresh state at the moment of the write."""
    u, _ = build(plan_script(step_1="w2", step_2="fullscreen", step_3="none"))
    decision = say(u, "go to firefox fullscreen", PLAN_DESKTOP)
    assert decision.action.intent is Intent.FOCUS_WINDOW
    assert decision.action.window.address == "0x2"
    assert decision.rest[0].action.intent is Intent.FULLSCREEN
    assert decision.rest[0].action.window is None


def test_a_step_naming_an_option_code_never_offered_drops_the_whole_plan():
    """An id nobody minted is a broken answer rather than a finished one."""
    u, _ = build(plan_script(step_1="w2", step_2="w99", step_3="none"))
    decision = say(u, "go to firefox fullscreen", PLAN_DESKTOP)
    assert u.last_exchange["path"] != "plan"
    assert decision.rest == ()


def test_a_step_that_needs_a_direction_nobody_answered_drops_the_plan():
    """`move_window` needs a direction and a step Choice carries none, so the plan is
    dropped rather than aimed somewhere nobody asked for."""
    u, _ = build(plan_script(step_1="w2", step_2="move_window", step_3="none"))
    decision = say(u, "go to firefox move it", PLAN_DESKTOP)
    assert u.last_exchange["path"] != "plan"
    assert decision.rest == ()


def test_the_plan_rides_in_one_request_that_is_never_split():
    u, jev = build(plan_script(step_1="a1", step_2="ws2", step_3="none"))
    say(u, "open gimp go to workspace two", PLAN_DESKTOP)
    plans = [r for r in u.last_exchange["requests"] if r["name"].startswith("rp")]
    assert len(plans) == 1
    assert set(plans[0]["questions"]) == {"step_1", "step_2", "step_3"}
    assert plans[0]["estimated_tokens"] <= Config().jev.token_cap


def test_no_option_ever_carries_an_address_a_title_or_a_handle():
    """The model answers an id; code dereferences it. There is never a dereferenceable
    thing in a request (docs/PLAN.md section 7)."""
    u, _ = build(plan_script(step_1="a1", step_2="ws2", step_3="none"))
    say(u, "open gimp go to workspace two", PLAN_DESKTOP)
    sent = json.dumps(u.last_exchange["requests"])
    for secret in ("0x1", "0x2", "0x4", "secret-project", "cats compilation"):
        assert secret not in sent


def test_talk_that_was_not_addressed_to_the_computer_never_becomes_a_plan():
    """The plan is read before the intent is, so the one gate that keeps a conversation
    in the room from moving windows has to be read with it."""
    u, _ = build(plan_script(addressed=0.1, step_1="a1", step_2="ws2", step_3="none"))
    decision = say(u, "so then i told him to open gimp", PLAN_DESKTOP)
    assert u.last_exchange["path"] != "plan"
    assert decision.rest == () and decision.verdict is Verdict.NOTHING


def test_something_hyprsay_does_not_do_never_becomes_a_plan():
    """An option list cannot take a screenshot, so a plan would name the nearest thing it
    can do instead, which is the worst possible answer."""
    u, _ = build(
        plan_script(unsupported_kind="screenshot", step_1="a1", step_2="ws2", step_3="none")
    )
    decision = say(u, "take a screenshot of gimp on workspace two", PLAN_DESKTOP)
    assert u.last_exchange["path"] != "plan"
    assert decision.verdict is Verdict.SUGGEST and decision.rest == ()


def test_a_plan_that_was_never_answered_leaves_the_utterance_exactly_as_it_was():
    """A request that failed is v1's behaviour and the safe one."""
    u, _ = build(plan_script(step_1="none", step_2="none", step_3="none"))
    decision = say(u, "open gimp go to workspace two", PLAN_DESKTOP)
    assert u.last_exchange["plan"]["cut"] == "none"
    assert decision.rest == ()


def test_the_grammar_fast_path_asks_for_no_plan_at_all():
    """A canonical command still costs about a millisecond and no network."""
    parses = {"go to spotify": Parse(Intent.FOCUS_WINDOW, Slots(window_ref="spotify"))}
    u, jev = build(plan_script(step_1="w1"), parses=parses)
    assert say(u, "go to spotify", PLAN_DESKTOP).verdict is Verdict.ACT
    assert jev.calls == []
    assert "plan" not in u.last_exchange


def test_a_plan_is_only_read_when_the_seams_did_not_already_cut_the_utterance():
    """Two mechanisms cutting one utterance would each obey half of it."""
    u, _ = build(
        plan_script(step_1="a1", step_2="ws2", step_3="none", separates_0=0.95),
        parses={
            "open gimp": Parse(Intent.LAUNCH_APP, Slots(app_ref="gimp")),
            "go to workspace two": Parse(Intent.SWITCH_WORKSPACE, Slots(workspace="2")),
        },
    )
    decision = say(u, "open gimp and go to workspace two", PLAN_DESKTOP)
    assert u.last_exchange["path"] == "chain"
    assert len(decision.rest) == 1


def test_a_clause_of_a_chain_is_never_planned_a_second_time():
    """Two mechanisms cutting one utterance is the failure, so the one that ran second
    does not run at all: a clause never harvests and never asks for a plan of its own."""
    u, jev = build(
        plan_script(step_1="a1", step_2="ws2", step_3="none", separates_0=0.95, intent="none"),
        parses={"open gimp": Parse(Intent.LAUNCH_APP, Slots(app_ref="gimp"))},
    )
    say(u, "open gimp and mumble mumble", PLAN_DESKTOP)
    assert u.last_exchange["path"] == "chain"
    asked = [questions for _, questions in jev.calls if "step_1" in questions]
    assert len(asked) == 1


# --------------------------------------------------------------------------- the daemon


@pytest.fixture(autouse=True)
def no_journal(monkeypatch):
    monkeypatch.setenv("HYPRSAY_JOURNAL", "off")


PCM = b"\x01\x00" * 16000


class World:
    def __init__(self, state=DESKTOP) -> None:
        self.state = state

    def on_custom(self, callback):
        pass

    async def run(self):
        await asyncio.Event().wait()


class Ptt:
    def __init__(self) -> None:
        self.held = False

    def feed_custom(self, data):
        pass

    def force_up(self, reason):
        self.held = False

    async def events(self):
        await asyncio.sleep(0)
        return
        yield


class Recorder:
    def start(self):
        pass

    def stop(self):
        return PCM

    def abort(self):
        pass

    def level(self):
        return 0.5

    def voiced_after(self, seconds):
        return True


class Recognizer:
    name = "fake"
    rescue = None

    def __init__(self, text="do the thing") -> None:
        self.text = text

    async def warm(self):
        pass

    async def transcribe(self, pcm, sample_rate):
        return Transcript(self.text, backend="local:fake")


class Understanding:
    """Returns one scripted decision, and may move the desktop while it thinks, which is
    what really happens during a 93 ms decode and a 315 ms round trip."""

    last_exchange: dict = {}

    def __init__(self, decision, world=None, moves=None) -> None:
        self.decision, self.world, self.moves = decision, world, moves
        self.forgot = 0
        self.outcomes: list[bool] = []

    async def understand(
        self, transcript, state, *, pinned_address="", picking=(), provisional=False
    ):
        if self.moves is not None and self.world is not None:
            self.world.state = self.moves
        return self.decision

    def acted(self, ok):
        self.outcomes.append(ok)

    def forget(self):
        self.forgot += 1


class Executor:
    def __init__(self) -> None:
        self.executed: list[Action] = []

    def execute(self, action, *, pinned_address=""):
        self.executed.append(action)
        return Outcome(True, "Done.")

    def undo(self):
        return Outcome(True, "Undone.")

    def again(self):
        return Outcome(True, "Again.")


class Latch:
    def __init__(self) -> None:
        self.value = False

    def locked(self):
        return self.value

    def on_lock(self, callback):
        self.callback = callback


class Hud:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def start(self):
        return True

    async def close(self):
        pass

    async def send(self, message):
        self.sent.append(message)

    def from_decision(self, decision, state=None):
        return {"t": "state", "state": decision.verdict.value, "text": decision.reason}


def engine(decision, *, moves=None, cfg=None):
    world = World()
    parts = {
        "world": world,
        "ptt": Ptt(),
        "recorder": Recorder(),
        "recognizer": Recognizer(),
        "understander": Understanding(decision, world, moves),
        "executor": Executor(),
        "latch": Latch(),
        "hud": Hud(),
    }
    return daemon.Engine(cfg or Config(safety=Safety(countdown_s=0.01)), **parts), parts


async def utter(e, parts):
    parts["ptt"].held = True
    await e._key_down()
    parts["ptt"].held = False
    await e._key_up("key")
    if e._pending is not None:
        await e._pending


CLOSE = Decision(Verdict.COUNTDOWN, Action(Intent.CLOSE_WINDOW, window=FOX), tier=2)
FOCUS = Decision(Verdict.ACT, Action(Intent.FOCUS_WINDOW, window=FOX), tier=0)


def test_a_disruptive_action_is_refused_when_the_desktop_moved_while_it_was_decided():
    """Between the snapshot and the write there is a decode, a round trip and a 1.5 s
    countdown. A close decided against a desktop that no longer exists asks again."""
    e, parts = engine(CLOSE, moves=desktop(TERMINAL, FOX, MUSIC, STRANGER))
    asyncio.run(utter(e, parts))
    assert parts["executor"].executed == []
    assert "desktop changed" in parts["hud"].sent[-1]["text"]


def test_a_focus_still_runs_when_the_desktop_moved_because_it_is_free_to_reverse():
    """The executor already re-checks a focus target against fresh state, and making the
    speaker say everything twice is the worse failure (docs/PLAN.md 5.6)."""
    e, parts = engine(FOCUS, moves=desktop(TERMINAL, FOX, MUSIC, STRANGER))
    asyncio.run(utter(e, parts))
    assert parts["executor"].executed == [FOCUS.action]


def test_a_disruptive_action_runs_when_the_desktop_has_not_moved():
    e, parts = engine(CLOSE)
    asyncio.run(utter(e, parts))
    assert parts["executor"].executed == [CLOSE.action]


def test_a_decision_is_consumed_before_the_write_so_it_cannot_act_twice():
    """`jev-ultrafast`'s rule, verbatim: consume once, before any mutation, so a retry
    cannot double-act (docs/STRATEGY.md 2.5)."""
    e, parts = engine(CLOSE)

    async def twice():
        await utter(e, parts)
        await e._carry_out(CLOSE)
        if e._pending is not None:
            await e._pending

    asyncio.run(twice())
    assert parts["executor"].executed == [CLOSE.action]
    assert "already carried out" in parts["hud"].sent[-1]["text"]


def test_the_stamp_is_spent_so_a_later_step_is_not_stale_for_doing_its_job():
    """A chain moves the desktop by hand, and the step after it must not be refused for
    the change the step before it made."""
    chained = replace(FOCUS, rest=(CLOSE,))
    e, parts = engine(chained)
    asyncio.run(utter(e, parts))
    assert parts["executor"].executed == [FOCUS.action, CLOSE.action]


def test_the_executors_answer_is_reported_back_to_the_memory():
    e, parts = engine(FOCUS)
    asyncio.run(utter(e, parts))
    assert parts["understander"].outcomes == [True]


def test_a_lock_forgets_the_badges_and_what_the_last_commands_did_together():
    e, parts = engine(FOCUS)

    async def go():
        await utter(e, parts)
        e._picking = (Candidate("Firefox", window=FOX),)
        e._on_lock()
        await asyncio.sleep(0)

    asyncio.run(go())
    assert e._picking == () and e._stamp == ""
    assert parts["understander"].forgot == 1


def test_an_understander_with_no_memory_at_all_still_works():
    """The daemon asks for `acted` and `forget` and does without them: a caller may hold
    an understander that has neither, and an utterance must not fail over it."""

    class Bare(Understanding):
        acted = None
        forget = None

    e, parts = engine(FOCUS)
    parts["understander"] = Bare(FOCUS)
    engine_without = daemon.Engine(Config(), **parts)
    asyncio.run(utter(engine_without, parts))
    assert parts["executor"].executed == [FOCUS.action]


def test_an_action_with_a_direction_is_untouched_by_any_of_this():
    """A guard that changed what an ordinary command does would be a worse bug than the
    one it fixes."""
    moving = Decision(
        Verdict.ACT, Action(Intent.MOVE_WINDOW, window=FOX, direction=Direction.LEFT), tier=1
    )
    e, parts = engine(moving)
    asyncio.run(utter(e, parts))
    assert parts["executor"].executed == [moving.action]
