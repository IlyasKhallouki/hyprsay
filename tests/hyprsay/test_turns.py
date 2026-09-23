"""The second turn: fetch what could not exist yet, then decide again over it.

Nothing here opens a socket, a bus or the network. Most of it drives a fake adapter that
records what it was asked and hands back candidates a test wrote; the last pair drives the
REAL `adapters.waytify` over a recorded socket, all the way to a real `Executor`, because
the waytify daemon is not running on this machine and cannot be started from a test. Jev
is the shipped fake, scripted per question id, and the clock is injected, so "the deadline
ran out" is a test that takes no time at all.

Each test states one rule of `nlu/turns.py` as a sentence. The two that matter most are
the two the owner asked for: the loop is BOUNDED, and it can STOP.
"""

import asyncio
import json
import time
from dataclasses import replace
from pathlib import Path

import pytest

from hyprsay import executor
from hyprsay.adapters import waytify
from hyprsay.adapters.base import (
    Adapter,
    AdapterError,
    Affordance,
    Capability,
    Minted,
    Param,
    Trust,
    tier_for,
)
from hyprsay.config import Config
from hyprsay.executor import Executor
from hyprsay.jev.fake import FakeEvaluator
from hyprsay.model import (
    App,
    DesktopState,
    Intent,
    Monitor,
    Outcome,
    Transcript,
    Verdict,
    Window,
    Workspace,
)
from hyprsay.nlu import turns
from hyprsay.nlu.understand import Understander
from hypruse import safety as hypruse_safety
from hypruse import server

# --------------------------------------------------------------------------- the desktop


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


TERMINAL = win("0x1", "kitty", 0, "fish")
FOX = win("0x2", "firefox", 1, "YouTube", workspace=2)


def desktop(*windows, active="0x1") -> DesktopState:
    windows = windows or (TERMINAL, FOX)
    return DesktopState(
        windows=windows,
        workspaces=tuple(
            Workspace(i, str(i), "eDP-1") for i in sorted({w.workspace_id for w in windows})
        ),  # fmt: skip
        monitors=(Monitor(0, "eDP-1", focused=True, active_workspace_id=1),),
        active_address=active,
        active_workspace_id=1,
        locked=False,
    )


DESKTOP = desktop()


def app(id, name, kind) -> App:
    return App(id=id, name=name, kind=kind, wm_classes=(id,), trusted=True, exec_argv=(id,))


APPS = (app("firefox", "Firefox", "web browser"), app("kitty", "kitty", "terminal"))


class FakeLexicon:
    def __init__(self, apps=APPS) -> None:
        self.apps = tuple(apps)

    @staticmethod
    def _anchors(one: App) -> set[str]:
        return set(one.name.lower().split()) | set(one.kind.lower().split())

    def _names(self, text, one) -> bool:
        return one.trusted and bool(set(text.lower().split()) & self._anchors(one))

    def app_for_window(self, window):
        return next((a for a in self.apps if window.cls.lower() in a.wm_classes), None)

    def kind_of(self, window):
        one = self.app_for_window(window)
        return one.kind if one else ""

    def words(self):
        return frozenset(w for a in self.apps for w in self._anchors(a))

    def match_apps(self, phrase, limit=5):
        return [(a, 1.0) for a in self.apps if self._names(phrase, a)][:limit]

    def match_windows(self, phrase, state, limit=5):
        found = [
            w
            for w in sorted(state.windows, key=lambda w: w.focus_rank)
            if (one := self.app_for_window(w)) is not None and self._names(phrase, one)
        ]
        return [(w, 1.0) for w in found][:limit]

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
    def parse(self, norm, *, picking=False):
        return None

    def examples(self, intent):
        return (f"{intent.value} example",)


class Clock:
    """The injected clock, so a wall clock deadline is testable in no time at all."""

    def __init__(self, now=1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


# --------------------------------------------------------------------------- the adapter

TRACK = Capability(
    id="fake.play_track",
    summary="play a track on Spotify, found by name",
    tier=1,
    kind="spotify track",
    params=(Param("query", "text", required=True),),
    needs_query=True,
)
# a second capability that also has to be fetched, for the "turn two asked to fetch
# again" rule. Two of them in the list is also what the real waytify adapter offers.
ALBUM = replace(TRACK, id="fake.play_album", summary="play an album on Spotify, found by name")
# a capability that needs nothing from the utterance, so `base.offer` resolves it eagerly
# and the harvest carries real candidates with real labels. This is what MPRIS looks like:
# one option per live player, each named by what that player is playing.
PAUSE = Capability(
    id="fake.pause",
    summary="pause a media player",
    tier=1,
    kind="media player",
)
# tier 2, to prove that an adapter may SHOW one and may never have one authorized
RISKY = Capability(
    id="fake.wipe",
    summary="wipe the queue on Spotify",
    tier=2,
    kind="spotify queue",
    params=(Param("query", "text", required=True),),
    needs_query=True,
)

FOUND = (
    ("Bohemian Rhapsody - Queen", "uri:1"),
    ("Bohemian Rhapsody, live at Wembley - Queen", "uri:2"),
    ("Bohemian Rhapsody, live in Montreal - Queen", "uri:3"),
)


class FakeService(Adapter):
    """One adapter, with a list of results a test wrote and no socket behind it."""

    name = "fake"

    def __init__(self, found=FOUND, *, caps=(TRACK,), error=None, seconds=0.0) -> None:
        self.found = tuple(found)
        self.caps = tuple(caps)
        self.error = error
        self.seconds = seconds
        self.asked: list[tuple[str, dict]] = []
        self.performed: list[tuple[str, str]] = []
        self._minted = Minted()

    def probe(self):
        return True, "the fake adapter is fine"

    def can_serve(self, context):
        return True

    def capabilities(self, context):
        return self.caps

    def resolve(self, capability, slots, context):
        self.asked.append((capability, dict(slots)))
        if self.seconds:
            time.sleep(self.seconds)
        if self.error is not None:
            raise self.error
        declared = next(c for c in self.caps if c.id == capability)
        return tuple(
            Affordance(
                id=f"{capability}#{n}",
                label=label,
                kind=declared.kind,
                tier=tier_for(declared, label),
                handle=self._minted.mint(handle, label),
                adapter=self.name,
                capability=capability,
                trust=Trust.UNTRUSTED,
            )
            for n, (label, handle) in enumerate(self.found, 1)
        )

    def perform(self, capability, handle, context):
        self.performed.append((capability, handle))
        return Outcome(True, f"Playing {handle}.")


# --------------------------------------------------------------------------- the harness

# the path these tests were written against; the single request has its own section
FANOUT = Config(jev=replace(Config().jev, path="fanout"))

ADDRESSED = {"addressed": 0.95}
SAID = "play the second live version of bohemian rhapsody"


def build(script=None, *, adapters=None, cfg=None, clock=None):
    jev = FakeEvaluator({**ADDRESSED, "intent": "none", **(script or {})})
    understander = Understander(
        cfg or FANOUT,
        FakeLexicon(),
        FakeGrammar(),
        jev,
        normalizer=normalizer,
        clock=clock or Clock(),
        adapters=tuple(adapters if adapters is not None else (FakeService(),)),
    )
    return understander, jev


def say(understander, text=SAID, state=DESKTOP):
    return asyncio.run(understander.understand(Transcript(text, "test"), state))


def picking(second):
    """Turn one takes the fetchable capability, turn two takes `second`.

    One callable serves both turns, because Jev answers per question id and both turns
    ask `step_1`. Which turn this is, is read off the OPTIONS: only the second list holds
    ids under `turns.FETCHED`, so every test that uses this is also asserting that the
    second list really is a different list from the first.
    """

    def choose(question, state):
        chosen = second(question) if callable(second) else second
        has_fetched = any(key.startswith(turns.FETCHED) for key in question.options)
        return chosen if has_fetched else "x1"

    return choose


# --------------------------------------------------------------------------- the query cut


@pytest.mark.parametrize(
    ("said", "query"),
    [
        ("play bohemian rhapsody", "bohemian rhapsody"),
        ("play the second live version of bohemian rhapsody", "live version of bohemian rhapsody"),
        ("put on some miles davis", "miles davis"),
        ("search for kind of blue on spotify", "kind of blue"),
        ("look up hyprland", "hyprland"),
        ("listen to the third one", ""),
        ("play", ""),
        ("play something on spotify please", "something"),
        ("find me a podcast about rust", "podcast about rust"),
    ],
)
def test_the_query_is_the_speakers_words_with_the_asking_cut_off(said, query):
    assert turns.strip_carrier(said) == query


def test_an_ordinal_never_reaches_the_search_because_spotify_has_no_second():
    """ "The second one" says which RESULT, not what to look for. Leaving it in the query
    poisons every result that comes back, and it is the one word the second turn needs."""
    assert "second" not in turns.strip_carrier(SAID)
    assert "live" in turns.strip_carrier(SAID)


def test_an_ordinal_the_normalizer_turned_into_a_digit_is_cut_too():
    """Measured on the live path: "play the second live version" reaches the fetch as
    "play the 2 live version", so a table of ordinal WORDS alone left the digit in the
    query that went to Spotify."""
    said, literal = "play the 2 live version of bohemian rhapsody", SAID.split()
    assert turns.strip_carrier(said, literal) == "live version of bohemian rhapsody"


@pytest.mark.parametrize(
    ("said", "literal"),
    [("play 2 unlimited", ("play", "2", "unlimited")), ("play 99 luftballons", None)],
)
def test_a_leading_number_the_speaker_did_not_say_as_an_ordinal_stays_in_the_query(said, literal):
    """A leading number is usually the thing itself. "2 Unlimited" and "99 Luftballons"
    are bands, and the digit is all that tells them apart from anything else, so it is
    only dropped when the recognizer's own tokens held the ordinal word."""
    assert turns.strip_carrier(said, literal or said.split()) == said.removeprefix("play ")


def test_the_service_named_mid_sentence_is_cut_and_the_word_after_it_only_then():
    """Measured live: "search spotify for X" left "spotify for X" going to Spotify as a
    search query, because `TRAILING` only reaches the tail. The "for" goes with it, and
    ONLY after a service name, so "play for whom the bell tolls" keeps its own first word.
    """
    said = "search spotify for the 2 live version of bohemian rhapsody"
    assert turns.strip_carrier(said, SAID.split()) == "live version of bohemian rhapsody"
    assert turns.strip_carrier("play for whom the bell tolls") == "for whom the bell tolls"
    assert turns.strip_carrier("put on spotify bohemian rhapsody") == "bohemian rhapsody"


def test_a_query_is_cut_to_the_length_the_adapter_accepts():
    long = "play " + ("la " * 200)
    assert len(turns.strip_carrier(long)) <= turns.MAX_QUERY


def test_a_capability_whose_required_slot_cannot_be_filled_is_never_fetched():
    assert turns.slots_for(TRACK, "play") is None
    assert turns.slots_for(TRACK, "play miles davis") == {"query": "miles davis"}


# --------------------------------------------------------------------------- the bounds


def test_the_budget_allows_exactly_one_more_turn_and_then_says_out_of_turns():
    clock = Clock()
    budget = turns.Budget(clock=clock).started()
    assert budget.stop() is None
    assert budget.taken().stop() is turns.Stopped.OUT_OF_TURNS


def test_the_budget_expires_on_the_wall_clock_however_many_turns_are_left():
    clock = Clock()
    budget = turns.Budget(clock=clock).started()
    clock.now += turns.LOOP_S + 0.01
    assert budget.stop() is turns.Stopped.OUT_OF_TIME
    assert budget.left == 0.0


def test_the_fetch_never_gets_more_than_what_is_left_of_the_whole_turn():
    """Two ceilings that each look reasonable multiply. The fetch shares one deadline
    with the question that has to follow it, so it may never eat the whole thing."""
    clock = Clock()
    budget = turns.Budget(clock=clock).started()
    assert budget.fetch_seconds() == turns.FETCH_S
    clock.now += turns.LOOP_S - 0.1
    assert budget.fetch_seconds() == pytest.approx(0.1, abs=1e-6)


def test_two_is_the_ceiling_written_down_rather_than_a_habit():
    assert turns.MAX_TURNS == 2


# --------------------------------------------------------------------------- the loop


def test_a_capability_that_has_to_be_asked_is_fetched_and_then_chosen_among():
    """The owner's case. Jev cannot write a track URI, so the utterance takes two turns:
    it names the capability, code searches, and the second turn picks a real result."""
    service = FakeService()
    u, jev = build({"step_1": picking("s2")}, adapters=(service,))
    decision = say(u)
    assert service.asked == [("fake.play_track", {"query": "live version of bohemian rhapsody"})]
    assert decision.verdict is Verdict.ACT
    assert decision.action.intent is Intent.SERVICE
    assert decision.action.service.handle == "uri:2"
    assert u.last_exchange["path"] == "second turn"
    assert u.last_exchange["turn"]["fetched"] == 3


def test_the_fetched_candidates_keep_the_order_the_adapter_ranked_them_in():
    """ "The second live version" is an index into the list the service returned, and a
    list this repository reshuffled is one no ordinal can name."""
    service = FakeService()
    u, jev = build({"step_1": picking("s1")}, adapters=(service,))
    say(u)
    _, questions = jev.request_with("step_1")
    second = [q for _, q in jev.calls if "s1" in getattr(q.get("step_1"), "options", {})]
    options = list(second[0]["step_1"].options)
    fetched = [key for key in options if key.startswith(turns.FETCHED)]
    assert fetched == ["s1", "s2", "s3"]
    labels = [second[0]["step_1"].options[key]["label"] for key in fetched]
    assert labels == [label for label, _ in FOUND]


def test_exactly_two_requests_carry_the_step_question_and_never_a_third():
    """docs/STRATEGY.md 3.2: one request per utterance is the target, two is the ceiling,
    and the only legitimate second is fetching what could not exist before the first."""
    service = FakeService()
    u, jev = build({"step_1": picking("s2")}, adapters=(service,))
    say(u)
    carried = [q for _, q in jev.calls if "step_1" in q]
    assert len(carried) == 2
    assert len(service.asked) == 1


def test_the_second_turn_asks_one_choice_and_not_three():
    """Three Choices carry the option list three times and about seven candidates fit;
    one carries it once and thirty or more do (`requests.plan_request`)."""
    u, jev = build({"step_1": picking("s2")}, adapters=(FakeService(),))
    say(u)
    second = [q for _, q in jev.calls if "s1" in getattr(q.get("step_1"), "options", {})][0]
    assert set(second) == {"step_1"}


def test_the_second_turn_asks_which_result_and_not_which_step():
    """Measured on the live gateway: asked as a plan step, the catch-all
    `media_play_pause` won 7 of 8 identical requests at a margin code refuses, so the
    utterance did nothing. Asked as "which of these results did you mean", the right
    track won 8 of 8 (`bank.chosen_result` carries both numbers)."""
    u, jev = build({"step_1": picking("s2")}, adapters=(FakeService(),))
    say(u)
    first, second = (q["step_1"] for _, q in jev.calls if "step_1" in q)
    assert "first thing the speaker asked for" in first.instructions["ask"]
    assert "already been looked up" in second.instructions["ask"]
    assert "count them in the order they are given" in second.instructions["order"]


def test_the_second_list_still_carries_the_machine_so_the_model_has_somewhere_else_to_go():
    """A forced Choice always crowns a winner, which this project measured the hard way.
    A list of nothing but search results would therefore always produce a track."""
    u, jev = build({"step_1": picking("s2")}, adapters=(FakeService(),))
    say(u)
    second = [q for _, q in jev.calls if "s1" in getattr(q.get("step_1"), "options", {})][0]
    options = set(second["step_1"].options)
    assert {"s1", "s2", "s3"} <= options
    assert any(key.startswith("w") for key in options)


# --------------------------------------------------------------------------- stopping


def test_turn_two_choosing_something_the_first_list_already_held_stops_and_says_so():
    """Turn one saw that option and chose to search instead. Taking it now would overrule
    turn one with evidence turn one already had."""
    service = FakeService()
    u, _ = build({"step_1": picking("w1")}, adapters=(service,))
    decision = say(u)
    assert decision.verdict is Verdict.SUGGEST
    assert decision.action is None
    assert u.last_exchange["turn"]["stopped"] == turns.Stopped.NO_BETTER.value
    assert service.performed == []


def test_turn_two_asking_for_another_fetch_stops_rather_than_looping():
    service = FakeService(caps=(TRACK, ALBUM))
    u, _ = build({"step_1": picking(lambda q: "x2" if "x2" in q.options else "x1")})
    u.adapters = (service,)
    decision = say(u)
    assert decision.verdict is Verdict.SUGGEST
    assert u.last_exchange["turn"]["stopped"] == turns.Stopped.OUT_OF_TURNS.value
    assert len(service.asked) == 1


def test_a_second_turn_that_names_nothing_at_all_ends_the_utterance_with_no_write():
    service = FakeService()
    u, _ = build({"step_1": picking("none")}, adapters=(service,))
    decision = say(u)
    assert decision.verdict is Verdict.SUGGEST
    assert u.last_exchange["turn"]["stopped"] == turns.Stopped.NO_BETTER.value
    assert service.performed == []


def test_an_adapter_that_refuses_hands_back_its_own_sentence_and_its_own_repair():
    """`probe`'s rule, applied to a fetch: an empty list reads as "there is nothing to
    play", and the sentence that names the repair is the only useful answer."""
    service = FakeService(error=AdapterError("the waytify daemon is not running; start it"))
    u, _ = build({"step_1": picking("s1")}, adapters=(service,))
    decision = say(u)
    assert decision.verdict is Verdict.SUGGEST
    assert "waytify daemon is not running" in decision.reason
    assert u.last_exchange["turn"]["stopped"] == turns.Stopped.FETCH_FAILED.value


def test_a_fetch_that_comes_back_empty_says_nothing_came_back_rather_than_acting():
    service = FakeService(found=())
    u, _ = build({"step_1": picking("s1")}, adapters=(service,))
    decision = say(u)
    assert u.last_exchange["turn"]["stopped"] == turns.Stopped.NOTHING_FOUND.value
    assert decision.action is None


def test_a_fetch_that_overruns_its_deadline_is_abandoned_and_nothing_is_done():
    """The deadline abandons the thread rather than killing it: `resolve` is read only by
    contract, so a fetch nobody wants any more costs a thread and changes nothing."""
    service = FakeService(seconds=turns.FETCH_S + 0.3)
    u, _ = build({"step_1": picking("s1")}, adapters=(service,))
    decision = say(u)
    assert u.last_exchange["turn"]["stopped"] == turns.Stopped.OUT_OF_TIME.value
    assert decision.action is None
    assert service.performed == []


def test_an_utterance_that_says_what_to_do_but_not_what_to_find_is_asked_again():
    service = FakeService()
    u, _ = build({"step_1": picking("s1")}, adapters=(service,))
    decision = say(u, "play")
    assert u.last_exchange["turn"]["stopped"] == turns.Stopped.NO_QUERY.value
    assert service.asked == []
    assert decision.rehearable is True


def test_an_option_whose_adapter_is_not_in_this_process_is_never_fetched_from():
    """The harvest offered it and the adapter was gone by the time the answer came back.
    A handle is only ever honoured by the `Minted` that issued it, so the answer to that
    is a refusal and never a different adapter that happens to know the same id."""
    service = FakeService()
    u, jev = build(adapters=(service,))

    def vanish(question, state):
        u.adapters = ()
        return "x1"

    jev.script("step_1", vanish)
    decision = say(u)
    assert decision.verdict is Verdict.SUGGEST
    assert u.last_exchange["turn"]["stopped"] == turns.Stopped.NO_ADAPTER.value
    assert service.asked == []


# --------------------------------------------------------------------------- the tier


def test_the_second_turn_is_judged_by_the_tier_function_exactly_like_the_first():
    """A turn is not a licence. A tier 2 capability may be SHOWN by an adapter and may
    never be authorized from a list, because picking an id is not a verb said out loud."""
    service = FakeService(caps=(RISKY,))
    u, _ = build({"step_1": picking("s1")}, adapters=(service,))
    decision = say(u, "wipe the queue on spotify")
    assert decision.verdict is not Verdict.ACT
    assert service.performed == []


def test_a_chosen_track_carries_the_tier_the_adapter_declared_for_its_capability():
    u, _ = build({"step_1": picking("s2")}, adapters=(FakeService(),))
    decision = say(u)
    assert decision.tier == TRACK.tier
    assert decision.action.service.tier == TRACK.tier


def test_a_homoglyph_in_a_track_name_raises_what_it_costs_and_so_refuses_it():
    """An untrusted label may RAISE a tier and may never lower one. A name spelled with a
    Cyrillic letter is a string built to be read as something it is not, and at tier 2 an
    option chosen from a list has no spoken verb behind it."""
    hostile = (("Bohemiаn Rhapsody - Queen", "uri:1"),)
    service = FakeService(found=hostile)
    u, _ = build({"step_1": picking("s1")}, adapters=(service,))
    decision = say(u)
    assert decision.verdict is not Verdict.ACT
    assert service.performed == []


def test_an_untrusted_label_puts_an_option_in_the_list_and_still_never_authorizes_it():
    """The whole trust rule in one utterance.

    A track name ranks its own option into the list, because ranking is not authorization
    (`affordances.claims`). It may not then be the evidence that acts on it: the tier
    rests on the capability's SUMMARY, which is one line written in this repository. Here
    the speaker says only words the track label holds, so the option is offered, chosen,
    and refused for lack of anything the speaker said that names what it does.
    """
    player = FakeService(caps=(PAUSE,))
    u, _ = build({"step_1": "x2"}, adapters=(player,))
    decision = say(u, "bohemian rhapsody live at wembley")
    assert u.last_exchange["path"] == "service"
    assert decision.verdict is not Verdict.ACT
    assert player.performed == []


def test_a_capability_the_speaker_did_name_is_acted_on_through_the_adapter_that_owns_it():
    """The same option, said the way the summary is written: "pause a media player"."""
    player = FakeService(caps=(PAUSE,))
    u, _ = build({"step_1": "x2"}, adapters=(player,))
    decision = say(u, "pause bohemian rhapsody live at wembley")
    assert decision.verdict is Verdict.ACT
    assert decision.action.intent is Intent.SERVICE
    assert decision.action.service.adapter == "fake"
    assert decision.action.service.handle == "uri:2"


# --------------------------------------------------------------------------- privacy


def test_no_handle_ever_reaches_a_request_and_no_query_reaches_the_journal_note():
    service = FakeService()
    u, jev = build({"step_1": picking("s2")}, adapters=(service,))
    say(u)
    sent = jev.sent_text()
    assert "uri:1" not in sent and "uri:2" not in sent and "uri:3" not in sent
    note = u.last_exchange["turn"]
    assert note["query_chars"] == len("live version of bohemian rhapsody")
    assert "bohemian" not in str(note)


# ------------------------------------------------- the whole way, over the real protocol
#
# Everything above drives a fake adapter. These two drive the REAL `adapters.waytify`
# class, with nothing faked but the socket, and then the real `Executor` and `ops._service`
# on the far side. That is as close to the daemon as this machine can get: the waytify
# daemon is not running here, and it cannot be started or logged in from a test.


def hello():
    return {"type": "hello", "protocol": waytify.PROTOCOL, "version": "0.1.0"}


def found(name, subtitle, uri, kind="track"):
    return {"name": name, "subtitle": subtitle, "uri": uri, "kind": kind}


RHAPSODY_URI = "spotify:track:6rqhFgbbKwnb9MLmUQDhG6"
LIVE_URI = "spotify:track:3z8h0TU7ReDPLIbEnYhWZb"
RHAPSODY = [
    found("Bohemian Rhapsody", "Queen", RHAPSODY_URI),
    found("Bohemian Rhapsody, live at Wembley", "Queen", LIVE_URI),
    found("A Night at the Opera", "Queen", "spotify:album:1GbtB4zTqAsyfZEsm1RZfx", "album"),
]


def spotify_state(rows=()):
    return {"type": "state", "state": {"spotify": {"authorized": True, "search": list(rows)}}}


class RecordedLink:
    """One recorded conversation. Every line the adapter writes is kept, so what was
    asked of the daemon is something a test can assert on."""

    def __init__(self, frames) -> None:
        self.pending = list(frames)
        self.sent: list[dict] = []

    def send(self, line):
        self.sent.append(json.loads(line))

    def readline(self, timeout):
        return json.dumps(self.pending.pop(0)) if self.pending else ""

    def close(self):
        return None


class RecordedDial:
    """A socket that is always there, one recorded script per connect."""

    def __init__(self, *scripts) -> None:
        self.scripts = [list(s) for s in scripts]
        self.links: list[RecordedLink] = []

    def __call__(self, path, timeout):
        script = self.scripts.pop(0) if self.scripts else []
        self.links.append(RecordedLink(script))
        return self.links[-1]


def daemon(*scripts, clock=None):
    """The real `Waytify`, with a recorded socket and the filesystem question answered."""
    dial = RecordedDial(*scripts)
    adapter = waytify.Waytify(dial, path=Path("/run/waytify/sock"), clock=clock)
    adapter.listening = lambda: True  # the socket itself is a filesystem question
    adapter.recorded = dial
    return adapter


@pytest.fixture
def acting(monkeypatch):
    """A real `Executor` with the beacon and the journal out of the way."""
    monkeypatch.setattr(hypruse_safety, "touch", lambda action: None)
    monkeypatch.setattr(server, "READONLY", False)
    monkeypatch.setenv("HYPRSAY_JOURNAL", "off")
    monkeypatch.delenv("HYPRUSE_DRYRUN", raising=False)
    yield
    executor.use(None)


def test_the_owners_case_all_the_way_through_the_real_waytify_protocol(acting):
    """ "play the second live version": harvest, choose the capability, SEARCH over the
    real protocol, harvest again, choose a real track, and perform it through the executor.

    The search leg is three frames because the ack proves nothing: waytify answers
    `Search` immediately and publishes a new `State` only once the list has really
    changed, so the adapter waits for a state whose search list differs from the one it
    subscribed with. Both of those frames are here, in that order.
    """
    clock = Clock()
    service = daemon(
        [hello(), spotify_state(), spotify_state(RHAPSODY)],  # subscribe, then the answer
        [hello(), {"type": "ack"}],  # the play, on its own connection
        clock=clock,
    )
    u, _ = build({"step_1": picking("s2")}, adapters=(service,), clock=clock)
    decision = say(u)

    assert decision.verdict is Verdict.ACT
    assert decision.action.intent is Intent.SERVICE
    assert decision.action.service.capability == "waytify.play_track"
    assert decision.action.service.handle == LIVE_URI

    asked = service.recorded.links[0].sent
    assert asked == [
        {"cmd": "subscribe", "scope": "full"},
        {"cmd": "search", "query": "live version of bohemian rhapsody"},
    ]

    acts = Executor(Config(), lambda: DESKTOP, Unlocked(), None, adapters=(service,))
    executor.use(acts)
    outcome = acts.execute(decision.action)
    assert outcome.ok, outcome.message
    assert outcome.message == "Playing Bohemian Rhapsody, live at Wembley - Queen."
    assert service.recorded.links[1].sent == [{"cmd": "play_track", "uri": LIVE_URI}]


def test_a_daemon_that_is_not_there_says_how_to_start_it_and_plays_nothing(acting):
    """The case on this machine, and the reason the test above has a recorded socket at
    all: `hyprsay probe` reports the daemon down, and so does a spoken command."""
    service = waytify.Waytify(path=Path("/run/user/nobody/waytify/does-not-exist"))
    u, _ = build({"step_1": picking("s1")}, adapters=(service,))
    decision = say(u)
    # the harvest never offers a capability from an adapter whose socket is not there,
    # so the utterance never reaches a second turn at all
    assert decision.action is None or decision.action.intent is not Intent.SERVICE
    usable, why = service.probe()
    assert usable is False
    assert "waytify daemon is not running" in why


class Unlocked:
    def locked(self) -> bool:
        return False


# --------------------------------------------------------------------------- the single request

SINGLE = Config(jev=replace(Config().jev, path="single"))


def test_on_the_single_path_the_words_answer_is_the_query():
    service = FakeService()
    # w6_8 is "bohemian rhapsody": the second turn asks step_1 over what came back
    u, _ = build({"action": "x1", "words": "w6_8", "step_1": "s2"}, adapters=(service,), cfg=SINGLE)
    decision = say(u)
    assert service.asked == [("fake.play_track", {"query": "bohemian rhapsody"})]
    assert decision.action is not None and decision.action.service.handle == "uri:2"


def test_on_the_single_path_an_unsure_words_answer_falls_back_to_cutting_the_verb():
    service = FakeService()
    words = {"w6_8": 0.3, "w3_8": 0.3, "none": 0.4}
    u, _ = build({"action": "x1", "words": words, "step_1": "s2"}, adapters=(service,), cfg=SINGLE)
    say(u)
    assert service.asked == [("fake.play_track", {"query": "live version of bohemian rhapsody"})]


def test_asking_to_search_names_a_capability_that_finds_by_name():
    service = FakeService()
    u, _ = build({"action": "x1", "words": "w2_4", "step_1": "s1"}, adapters=(service,), cfg=SINGLE)
    decision = say(u, "search for bohemian rhapsody")
    assert service.asked == [("fake.play_track", {"query": "bohemian rhapsody"})]
    assert decision.verdict is Verdict.ACT


class PageLike(FakeService):
    """A page: its links are harvested eagerly, and a find returns links of the same kind."""

    CLICK = Capability(id="fake.click", summary="click something on this page", tier=1,
                       kind="page")  # fmt: skip
    FIND = Capability(id="fake.find", summary="find and play something on this site by name",
                      tier=1, kind="page search", params=(Param("query", "text", required=True),),
                      needs_query=True, replaces=("fake.click",))  # fmt: skip

    def __init__(self):
        super().__init__(caps=(self.FIND, self.CLICK))

    def resolve(self, capability, slots, context):
        self.asked.append((capability, dict(slots)))
        labels = (
            [label for label, _ in FOUND]
            if capability == "fake.find"
            else [f"result {n}: {label}" for n, (label, _) in enumerate(FOUND, 1)]
        )
        return tuple(
            Affordance(id=f"c{n}", label=label, kind="page", tier=1,
                       handle=self._minted.mint(f"click {n}", label), adapter=self.name,
                       capability=capability, trust=Trust.UNTRUSTED)
            for n, label in enumerate(labels, 1)
        )  # fmt: skip


def test_turn_two_offers_what_the_find_brought_back_and_not_the_pages_raw_links_beside_it():
    page = PageLike()

    def first_the_find(question, state):
        if any(key.startswith(turns.FETCHED) for key in question.options):
            return "s1"
        return next(k for k, v in question.options.items()
                    if isinstance(v, dict) and "find and play" in v.get("label", ""))  # fmt: skip

    u, jev = build({"step_1": first_the_find}, adapters=(page,))
    say(u, "play bohemian rhapsody")
    second = [q for _, q in jev.calls if "s1" in getattr(q.get("step_1"), "options", {})][0]
    labels = [v.get("label", "") for v in second["step_1"].options.values() if isinstance(v, dict)]
    assert not any(label.startswith("result ") for label in labels)
