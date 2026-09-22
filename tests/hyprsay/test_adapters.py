"""The adapter contract, and the three adapters that pay for it. No bus, no network.

A unit test may never touch the session bus, the Spotify Web API, an account or a
running daemon, and on this machine `spotify_player` has never been authenticated and
waytify is not running anyway, which is exactly the state the two probes exist to name.
So the D-Bus transport, the process call and the Unix socket are all seams, and every
test below drives them from a fake: recorded frames for waytify, recorded stdout for the
CLI, a dict for the bus.
"""

import json

import pytest

from hyprsay.adapters import base, live
from hyprsay.adapters.base import (
    MAX_CANDIDATES,
    MAX_LABEL,
    AdapterError,
    Affordance,
    Capability,
    Context,
    Minted,
    Param,
    Trust,
)
from hyprsay.adapters.mpris import DBUS, PLAYER, PREFIX, PROXY, ROOT, Mpris
from hyprsay.adapters.spotify import CREDENTIALS, Spotify, identifier
from hyprsay.adapters.waytify import PROTOCOL, Waytify, socket_path
from hyprsay.config import Config, Privacy

CONTEXT = Context()


# --------------------------------------------------------------------------- fakes


class FakeBus:
    """A session bus that answers from a dict and remembers what was asked of it."""

    def __init__(self, properties=None, *, names=None, get_all=True, gone=()):
        self.properties = properties or {}
        self.names = list(names) if names is not None else list(self.properties)
        self.get_all = get_all
        self.gone = set(gone)
        self.calls = []
        self.refuse = set()

    def call(self, destination, path, interface, member, signature="", args=()):
        self.calls.append((destination, interface, member, tuple(args)))
        if destination == DBUS and member == "ListNames":
            return "as", [[n for n in self.names if n not in self.gone]]
        if destination in self.refuse:
            raise RuntimeError("the player is not answering")
        published = self.properties.get(destination, {})
        if member == "GetAll":
            if not self.get_all:
                raise RuntimeError("cannot parse the D-Bus type")
            return "a{sv}", [dict(published.get(args[0], {}))]
        if member == "Get":
            values = published.get(args[0], {})
            if args[1] not in values:
                raise RuntimeError(f"no such property {args[1]}")
            return "v", [values[args[1]]]
        return "", []


class FakeRunner:
    """A `spotify_player` that answers from recorded output and never runs anything."""

    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.argv = []

    def run(self, argv, timeout):
        self.argv.append(list(argv))
        if not self.replies:
            return 0, "{}", ""
        reply = self.replies.pop(0)
        return reply() if callable(reply) else reply


class Clock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


def player_properties(
    identity="Chrome", *, status="Paused", can=(), entry="google-chrome", metadata=None
):
    flags = {
        "CanControl": True,
        "CanPlay": True,
        "CanPause": True,
        "CanGoNext": False,
        "CanGoPrevious": False,
        "CanSeek": True,
    }
    flags.update({name: True for name in can})
    root = {"Identity": identity, "CanRaise": True}
    if entry:
        root["DesktopEntry"] = entry
    player = {"PlaybackStatus": status, **flags}
    if metadata is not None:
        player["Metadata"] = metadata
    return {ROOT: root, PLAYER: player}


CHROME = PREFIX + "chromium.instance1325064"
PLASMA = PREFIX + "plasma-browser-integration"

# the exact shape docs/PRIVACY.md is about: what is playing says what the owner is doing
EPISODE = {
    "xesam:title": "Dr Smith explains my HIV test results",
    "xesam:artist": ["A Podcast"],
}
OTHER = {"xesam:title": "Nocturne in E flat", "xesam:artist": ["Chopin"]}


def two_chromiums(first=None, second=None):
    """Two players the bus cannot tell apart by name, which is what a track is for."""
    return FakeBus(
        {
            CHROME: player_properties("Chrome", metadata=first or EPISODE),
            PLASMA: player_properties("Chrome", metadata=second or OTHER),
        },
        names=[CHROME, PLASMA],
    )


SEARCH_JSON = json.dumps(
    {
        "tracks": [
            {
                "id": "6rqhFgbbKwnb9MLmUQDhG6",
                "name": "Bohemian Rhapsody",
                "artists": [{"name": "Queen"}],
                "album": {"name": "A Night at the Opera"},
            },
            {
                "id": "spotify:track:3z8h0TU7ReDPLIbEnYhWZb",
                "name": "Bohemian Rhapsody - Live Aid",
                "artists": [{"name": "Queen"}],
            },
            {
                "id": "https://open.spotify.com/track/7tFiyTwD0nx5a1eklYtX2J?si=abc",
                "name": "Bohemian Rhapsody - Remastered 2011",
                "artists": [{"name": "Queen"}],
            },
            {"id": "not-an-id", "name": "Bohemian Rhapsody (karaoke)", "artists": []},
        ],
        "albums": [
            {
                "id": "1GbtB4zTqAsyfZEsm1RZfx",
                "name": "A Night at the Opera",
                "artists": [{"name": "Queen"}],
            }
        ],
        "artists": [{"id": "1dfeR4HaWDbWqFHLkxsg1d", "name": "Queen"}],
        "playlists": [
            {
                "id": "37i9dQZF1DX0XUsuxWHRQd",
                "name": "RapCaviar",
                "owner": {"display_name": "Spotify"},
            }
        ],
    }
)


def authenticated(tmp_path):
    for name in CREDENTIALS:
        (tmp_path / name).write_text("{}")
    return tmp_path


def spotify(tmp_path, replies=None, clock=None):
    return Spotify(
        FakeRunner(replies),
        binary="/usr/bin/spotify_player",
        cache=authenticated(tmp_path),
        clock=clock or Clock(),
    )


# --------------------------------------------------------------------------- the shape


def test_an_affordance_carries_the_six_fields_the_strategy_specifies():
    found = Affordance(
        id="mpris.next#1",
        label="Next Chrome",
        kind="media player",
        tier=1,
        handle=PREFIX + "chromium",
        adapter="mpris",
        capability="mpris.next",
    )
    assert (found.id, found.label, found.kind, found.tier) == (
        "mpris.next#1",
        "Next Chrome",
        "media player",
        1,
    )
    assert found.handle == PREFIX + "chromium"
    assert found.trust is Trust.UNTRUSTED


def test_a_label_written_by_an_application_is_truncated_to_what_the_token_budget_fits():
    found = Affordance(
        id="x.y#1",
        label="A" * 400,
        kind="spotify track",
        tier=1,
        handle="h",
        adapter="x",
        capability="x.y",
    )
    assert len(found.label) == MAX_LABEL


def test_a_label_written_by_an_application_is_stripped_of_control_characters():
    found = Affordance(
        id="x.y#1",
        label="Bohemian​\nRhapsody\u0007",
        kind="spotify track",
        tier=1,
        handle="h",
        adapter="x",
        capability="x.y",
    )
    assert found.label == "Bohemian Rhapsody"


def test_a_label_that_is_nothing_but_invisible_characters_falls_back_to_its_kind():
    found = Affordance(
        id="x.y#1",
        label="​​",
        kind="spotify track",
        tier=1,
        handle="h",
        adapter="x",
        capability="x.y",
    )
    assert found.label == "spotify track"


def test_an_affordance_at_the_session_tier_cannot_be_built_because_it_is_never_offered():
    with pytest.raises(AdapterError):
        Affordance(
            id="x.y#1",
            label="log out",
            kind="session",
            tier=3,
            handle="h",
            adapter="x",
            capability="x.y",
        )


def test_an_option_sent_to_the_model_never_carries_the_handle():
    found = Affordance(
        id="x.y#1",
        label="Bohemian Rhapsody",
        kind="spotify track",
        tier=2,
        handle="6rqhFgbbKwnb9MLmUQDhG6",
        adapter="x",
        capability="x.y",
    )
    option = found.as_option()
    assert option == {
        "label": "Bohemian Rhapsody",
        "kind": "spotify track",
        "trust": "untrusted",
    }
    assert "6rqhFgbbKwnb9MLmUQDhG6" not in json.dumps(option)


def test_a_capability_outside_the_tier_model_is_refused_when_it_is_declared():
    with pytest.raises(AdapterError):
        Capability(id="x.y", summary="do a thing", tier=4)


def test_a_capability_id_that_would_not_survive_as_an_option_key_is_refused():
    with pytest.raises(AdapterError):
        Capability(id="", summary="do a thing", tier=0)


# --------------------------------------------------------------------------- the guards


class Toy:
    """One adapter, written the way an adapter should not be, so the guards can be seen
    doing their work."""

    name = "toy"

    def __init__(self, found=(), *, serves=True, explode=False):
        self.found = list(found)
        self.serves = serves
        self.explode = explode
        self.asked = []

    def probe(self):
        return True, "fine"

    def can_serve(self, context):
        if self.explode:
            raise RuntimeError("the toy is broken")
        return self.serves

    def capabilities(self, context):
        return (
            Capability(id="toy.act", summary="do a thing", tier=1, kind="toy"),
            Capability(
                id="toy.find",
                summary="find a thing by name",
                tier=1,
                kind="toy",
                params=(Param("query"),),
                needs_query=True,
            ),
        )

    def resolve(self, capability, slots, context):
        self.asked.append((capability, dict(slots)))
        return tuple(self.found)

    def perform(self, capability, handle, context):
        raise AssertionError("not reached")


def affordance(id_="toy.act#1", **kw):
    fields = {
        "id": id_,
        "label": "a thing",
        "kind": "toy",
        "tier": 0,
        "handle": "h",
        "adapter": "toy",
        "capability": "toy.act",
    }
    fields.update(kw)
    return Affordance(**fields)


def test_a_candidate_is_forced_untrusted_however_the_adapter_marked_it():
    toy = Toy([affordance(trust=Trust.TRUSTED)])
    found = base.candidates(toy, toy.capabilities(CONTEXT)[0], {}, CONTEXT)
    assert found[0].trust is Trust.UNTRUSTED


def test_a_candidate_can_never_cost_less_than_the_capability_it_came_from():
    toy = Toy([affordance(tier=0)])
    found = base.candidates(toy, toy.capabilities(CONTEXT)[0], {}, CONTEXT)
    assert found[0].tier == 1


def test_a_candidate_keeps_a_tier_the_adapter_raised():
    toy = Toy([affordance(tier=2)])
    found = base.candidates(toy, toy.capabilities(CONTEXT)[0], {}, CONTEXT)
    assert found[0].tier == 2


def test_a_repeated_id_is_dropped_because_options_are_a_dict():
    toy = Toy([affordance(), affordance(label="another thing")])
    found = base.candidates(toy, toy.capabilities(CONTEXT)[0], {}, CONTEXT)
    assert len(found) == 1


def test_the_candidate_list_is_capped_for_the_token_budget():
    toy = Toy([affordance(f"toy.act#{n}") for n in range(1, 200)])
    found = base.candidates(toy, toy.capabilities(CONTEXT)[0], {}, CONTEXT)
    assert len(found) == MAX_CANDIDATES


def test_the_capability_behind_a_chosen_option_is_asked_of_the_machine_again():
    bus = FakeBus({CHROME: player_properties()})
    adapter = Mpris(bus)
    assert base.capability_of(adapter, "mpris.play_pause", CONTEXT).tier == 1
    # the player closed between the harvest and the second question
    bus.gone.add(CHROME)
    adapter.forget()
    assert base.capability_of(adapter, "mpris.play_pause", CONTEXT) is None


def test_a_candidate_is_routed_back_to_the_adapter_that_minted_it():
    toy = Toy([affordance()])
    found = base.candidates(toy, toy.capabilities(CONTEXT)[0], {}, CONTEXT)[0]
    assert base.find([toy], found) is toy
    assert base.find([], found) is None


def test_a_capability_that_needs_words_is_offered_as_itself_and_not_resolved_early():
    toy = Toy([affordance()])
    found = base.offer([toy], CONTEXT)
    ids = [a.id for a in found]
    assert "toy.find" in ids
    assert [asked[0] for asked in toy.asked] == ["toy.act"]


def test_the_capability_option_is_the_one_kind_of_affordance_that_is_trusted():
    toy = Toy()
    found = {a.id: a for a in base.offer([toy], CONTEXT)}
    assert found["toy.find"].trust is Trust.TRUSTED
    assert found["toy.find"].label == "find a thing by name"


def test_an_adapter_that_cannot_serve_is_never_asked_what_it_can_do():
    toy = Toy([affordance()], serves=False)
    assert base.offer([toy], CONTEXT) == ()
    assert toy.asked == []


def test_one_broken_adapter_does_not_cost_the_whole_harvest():
    broken = Toy(explode=True)
    working = Toy([affordance()])
    found = base.offer([broken, working], CONTEXT)
    assert [a.id for a in found] == ["toy.act#1", "toy.find"]


def test_untrusted_text_may_raise_a_tier():
    capability = Capability(id="toy.act", summary="do a thing", tier=1)
    # a Cyrillic "е" inside a Latin word: the same pixels, a different string
    assert base.tier_for(capability, "Spotifу Premium") == 2


def test_untrusted_text_may_never_lower_a_tier():
    capability = Capability(id="toy.act", summary="do a thing", tier=2)
    assert base.tier_for(capability, "an entirely ordinary label") == 2


def test_a_raised_tier_stops_below_the_tier_the_model_is_never_offered():
    capability = Capability(id="toy.act", summary="do a thing", tier=2)
    assert base.tier_for(capability, "Spotifу Premium") == 2


def test_a_session_tier_capability_is_never_turned_into_an_option():
    capability = Capability(id="toy.out", summary="log out", tier=3)
    with pytest.raises(AdapterError):
        base.tier_for(capability, "log out")
    assert base.candidates(Toy([affordance()]), capability, {}, CONTEXT) == ()


def test_a_handle_the_adapter_never_minted_is_not_remembered():
    minted = Minted()
    minted.mint("a", "Track A")
    assert "a" in minted
    assert "b" not in minted
    assert minted.label_for("a") == "Track A"


def test_minted_handles_are_bounded_because_the_daemon_runs_for_weeks():
    minted = Minted(limit=2)
    for handle in ("a", "b", "c"):
        minted.mint(handle)
    assert len(minted) == 2
    assert "a" not in minted
    assert "c" in minted


# --------------------------------------------------------------------------- mpris


def test_only_mpris_names_are_read_from_the_bus():
    bus = FakeBus({CHROME: player_properties()}, names=[CHROME, "org.freedesktop.Notifications"])
    assert [p.bus_name for p in Mpris(bus).players()] == [CHROME]


def test_playerctld_is_dropped_because_it_duplicates_every_real_player():
    bus = FakeBus(
        {CHROME: player_properties(), PROXY: player_properties("Google Chrome")},
        names=[CHROME, PROXY],
    )
    assert [p.bus_name for p in Mpris(bus).players()] == [CHROME]


def test_a_player_reads_the_same_whether_the_transport_can_unmarshal_a_dict_or_not():
    properties = {CHROME: player_properties(can=("CanGoNext",))}
    through_get_all = Mpris(FakeBus(properties), get_all=True).players()
    through_get = Mpris(FakeBus(properties, get_all=False), get_all=False).players()
    assert through_get_all == through_get
    assert "CanGoNext" in through_get[0].can


def test_a_property_the_player_does_not_implement_never_costs_the_others():
    properties = {CHROME: player_properties(entry="")}
    player = Mpris(FakeBus(properties, get_all=False), get_all=False).players()[0]
    assert player.identity == "Chrome"
    assert player.desktop_entry == ""


def test_a_property_this_transport_could_not_parse_is_never_asked_for():
    # asking costs a reconnect, because a parse failure reaches `Connection.call` as a
    # lost connection and it reopens the socket, SASL and Hello included
    bus = FakeBus({CHROME: player_properties(metadata={"xesam:title": "x"})}, get_all=False)
    player = Mpris(bus, get_all=False).players()[0]
    assert player.playing == ""
    assert not any("Metadata" in call[3] for call in bus.calls)


def test_the_bus_is_swept_once_per_harvest_and_not_once_per_question():
    bus = FakeBus({CHROME: player_properties()})
    adapter = Mpris(bus, clock=Clock())
    adapter.can_serve(CONTEXT)
    adapter.capabilities(CONTEXT)
    adapter.resolve("mpris.play_pause", {}, CONTEXT)
    assert sum(1 for call in bus.calls if call[2] == "ListNames") == 1


def test_the_reading_expires_so_a_player_that_appeared_is_found():
    clock = Clock()
    bus = FakeBus({CHROME: player_properties()})
    adapter = Mpris(bus, clock=clock)
    adapter.players()
    bus.names.append(PLASMA)
    bus.properties[PLASMA] = player_properties("Google Chrome")
    clock.now += 5.0
    assert len(adapter.players()) == 2


def test_a_verb_the_player_will_not_honour_is_never_offered():
    bus = FakeBus({CHROME: player_properties()})
    adapter = Mpris(bus)
    ids = [cap.id for cap in adapter.capabilities(CONTEXT)]
    assert "mpris.play_pause" in ids
    assert "mpris.next" not in ids
    assert adapter.resolve("mpris.next", {}, CONTEXT) == ()


def test_a_player_that_takes_no_orders_at_all_is_offered_nothing():
    properties = {CHROME: player_properties()}
    properties[CHROME][PLAYER]["CanControl"] = False
    adapter = Mpris(FakeBus(properties))
    assert adapter.capabilities(CONTEXT) == ()
    assert adapter.resolve("mpris.play_pause", {}, CONTEXT) == ()


def test_quitting_a_player_is_never_offered_because_closing_has_its_own_guards():
    bus = FakeBus({CHROME: player_properties(can=("CanQuit",))})
    ids = [cap.id for cap in Mpris(bus).capabilities(CONTEXT)]
    assert not any("quit" in found for found in ids)


def test_every_media_option_costs_what_the_media_intent_already_costs():
    bus = FakeBus({CHROME: player_properties()})
    adapter = Mpris(bus)
    for capability in adapter.capabilities(CONTEXT):
        for found in adapter.resolve(capability.id, {}, CONTEXT):
            assert found.tier == 1
            assert found.trust is Trust.UNTRUSTED


def test_a_player_that_is_playing_is_offered_as_pause_rather_than_play():
    bus = FakeBus({CHROME: player_properties(status="Playing")})
    found = Mpris(bus).resolve("mpris.play_pause", {}, CONTEXT)[0]
    assert found.label.startswith("Pause ")


def test_one_option_per_player_so_the_speaker_can_name_which_one():
    bus = FakeBus(
        {CHROME: player_properties("Chrome"), PLASMA: player_properties("Google Chrome")},
        names=[CHROME, PLASMA],
    )
    found = Mpris(bus).resolve("mpris.play_pause", {}, CONTEXT)
    assert [a.label for a in found] == ["Play Chrome", "Play Google Chrome"]
    assert [a.id for a in found] == ["mpris.play_pause#1", "mpris.play_pause#2"]


def test_what_is_playing_is_read_when_the_transport_can_unmarshal_a_dict():
    """Two players wearing one name is the case a track is needed for, and it is the
    case it is carried for."""
    found = Mpris(two_chromiums(), get_all=True).resolve("mpris.play_pause", {}, CONTEXT)
    assert "A Podcast" in found[0].label
    assert "Nocturne" in found[1].label


# ------------------------------------------------------------------ mpris and what plays


def test_one_player_is_named_by_its_player_and_never_by_what_it_is_playing():
    """docs/PRIVACY.md. A track title says what the owner is watching, which is the same
    sentence a window title carries, and with one Chromium on the bus there is nothing it
    could be telling apart. "Pause Chromium" is the whole label."""
    bus = FakeBus({CHROME: player_properties(metadata=EPISODE)})
    found = Mpris(bus, get_all=True).resolve("mpris.play_pause", {}, CONTEXT)[0]
    assert found.label == "Play Chrome"
    assert "HIV" not in found.label


def test_no_track_is_kept_anywhere_when_the_owner_said_no_title_ever_leaves():
    """privacy.titles = "never". The gate is at the read, so there is no string to leak
    into a request body, an option, a handle, an outcome or the journal."""
    bus = FakeBus({CHROME: player_properties(metadata=EPISODE)}, names=[CHROME, PLASMA])
    bus.properties[PLASMA] = player_properties("Chrome", metadata=OTHER)
    adapter = Mpris(bus, get_all=True, privacy=Privacy(titles="never"))
    assert [p.playing for p in adapter.players()] == ["", ""]
    found = adapter.resolve("mpris.play_pause", {}, CONTEXT)
    assert [a.label for a in found] == ["Play Chrome", "Play Chrome"]
    for a in found:
        assert "HIV" not in a.label and "Nocturne" not in a.label
        assert "HIV" not in a.handle


def test_a_track_that_names_itself_private_is_dropped_even_when_it_would_tell_two_apart():
    bus = two_chromiums(first={"xesam:title": "Private session recording"})
    found = Mpris(bus, get_all=True).resolve("mpris.play_pause", {}, CONTEXT)
    assert found[0].label == "Play Chrome"
    assert "Nocturne" in found[1].label


def test_a_player_on_the_redaction_list_is_never_described_by_what_it_is_playing():
    bus = two_chromiums()
    adapter = Mpris(bus, get_all=True, privacy=Privacy(redact_classes=("Chrome",)))
    assert [p.playing for p in adapter.players()] == ["", ""]


def test_an_identity_written_by_the_application_cannot_grow_past_the_label_budget():
    bus = FakeBus({CHROME: player_properties("Z" * 300)})
    found = Mpris(bus).resolve("mpris.play_pause", {}, CONTEXT)[0]
    assert len(found.label) == MAX_LABEL


def test_the_method_that_is_sent_is_the_one_the_capability_names():
    bus = FakeBus({CHROME: player_properties(can=("CanGoNext",))})
    adapter = Mpris(bus)
    handle = adapter.resolve("mpris.next", {}, CONTEXT)[0].handle
    outcome = adapter.perform("mpris.next", handle, CONTEXT)
    assert outcome.ok
    assert (CHROME, PLAYER, "Next", ()) in bus.calls


def test_raising_a_player_goes_to_the_root_interface_and_not_to_the_player_one():
    bus = FakeBus({CHROME: player_properties()})
    adapter = Mpris(bus)
    handle = adapter.resolve("mpris.raise", {}, CONTEXT)[0].handle
    adapter.perform("mpris.raise", handle, CONTEXT)
    assert (CHROME, ROOT, "Raise", ()) in bus.calls


def test_a_bus_name_this_adapter_never_offered_is_refused():
    bus = FakeBus({CHROME: player_properties()})
    adapter = Mpris(bus)
    adapter.resolve("mpris.play_pause", {}, CONTEXT)
    with pytest.raises(AdapterError):
        adapter.perform("mpris.play_pause", PREFIX + "somebody.else", CONTEXT)
    assert not any(call[2] == "PlayPause" for call in bus.calls)


def test_a_handle_that_is_not_a_bus_name_at_all_is_refused():
    bus = FakeBus({CHROME: player_properties()})
    adapter = Mpris(bus)
    adapter._minted.mint("org.mpris.MediaPlayer2.x;rm -rf /")
    with pytest.raises(AdapterError):
        adapter.perform("mpris.play_pause", "org.mpris.MediaPlayer2.x;rm -rf /", CONTEXT)


def test_a_player_that_closed_between_the_decision_and_the_act_is_a_refusal():
    bus = FakeBus({CHROME: player_properties()})
    adapter = Mpris(bus)
    handle = adapter.resolve("mpris.play_pause", {}, CONTEXT)[0].handle
    bus.gone.add(CHROME)
    with pytest.raises(AdapterError) as caught:
        adapter.perform("mpris.play_pause", handle, CONTEXT)
    assert "gone" in str(caught.value)
    assert not any(call[2] == "PlayPause" for call in bus.calls)


def test_a_player_that_refuses_the_method_becomes_one_plain_sentence():
    bus = FakeBus({CHROME: player_properties()})
    adapter = Mpris(bus)
    handle = adapter.resolve("mpris.play_pause", {}, CONTEXT)[0].handle
    bus.refuse.add(CHROME)
    with pytest.raises(AdapterError) as caught:
        adapter.perform("mpris.play_pause", handle, CONTEXT)
    assert "Chrome" in str(caught.value)


def test_a_capability_no_player_has_is_refused_rather_than_guessed():
    adapter = Mpris(FakeBus({CHROME: player_properties()}))
    with pytest.raises(AdapterError):
        adapter.resolve("mpris.teleport", {}, CONTEXT)
    with pytest.raises(AdapterError):
        adapter.perform("mpris.teleport", CHROME, CONTEXT)


def test_a_bus_that_cannot_be_asked_is_one_sentence_and_not_an_empty_list():
    class Dead:
        def call(self, *a, **kw):
            raise OSError("no bus")

    adapter = Mpris(Dead())
    usable, why = adapter.probe()
    assert not usable
    assert "session bus" in why
    assert adapter.can_serve(CONTEXT) is False


def test_a_bus_with_no_player_says_so_rather_than_reporting_a_broken_bus():
    usable, why = Mpris(FakeBus({}, names=[])).probe()
    assert not usable
    assert "no media player" in why


def test_the_probe_says_when_track_names_cannot_be_read_on_this_transport():
    bus = FakeBus({CHROME: player_properties()})
    usable, why = Mpris(bus, get_all=False).probe()
    assert usable
    assert "track names" in why


def test_the_transport_self_test_needs_no_bus_and_agrees_with_itself():
    from hyprsay.adapters.mpris import _dicts_readable

    assert _dicts_readable() is _dicts_readable()
    assert isinstance(_dicts_readable(), bool)


# --------------------------------------------------------------------------- spotify


def test_the_probe_names_the_package_when_the_binary_is_not_installed(tmp_path):
    adapter = Spotify(FakeRunner(), binary="not-a-real-binary", cache=tmp_path)
    usable, why = adapter.probe()
    assert not usable
    assert "spotify-player package" in why


def test_the_probe_names_the_one_time_login_when_it_has_never_been_run(tmp_path):
    adapter = Spotify(FakeRunner(), binary="/usr/bin/spotify_player", cache=tmp_path)
    usable, why = adapter.probe()
    assert not usable
    assert "spotify_player authenticate" in why
    assert "Premium" in why


def test_the_probe_says_which_of_the_two_logins_is_missing(tmp_path):
    (tmp_path / CREDENTIALS[0]).write_text("{}")
    adapter = Spotify(FakeRunner(), binary="/usr/bin/spotify_player", cache=tmp_path)
    usable, why = adapter.probe()
    assert not usable
    assert CREDENTIALS[1] in why


def test_the_probe_is_usable_once_both_credentials_are_cached(tmp_path):
    usable, why = spotify(tmp_path).probe()
    assert usable
    assert "authenticated" in why


def test_the_probe_never_raises_whatever_the_filesystem_does(tmp_path):
    adapter = Spotify(FakeRunner(), binary="/usr/bin/spotify_player", cache=tmp_path / "gone")
    assert adapter.probe()[0] is False


def test_deciding_whether_spotify_can_serve_never_spawns_anything(tmp_path):
    adapter = spotify(tmp_path)
    adapter.can_serve(CONTEXT)
    adapter.capabilities(CONTEXT)
    assert adapter._runner.argv == []


def test_an_utterance_with_nothing_to_search_for_asks_for_words(tmp_path):
    adapter = spotify(tmp_path)
    with pytest.raises(AdapterError) as caught:
        adapter.resolve("spotify.play_track", {"query": "   "}, CONTEXT)
    assert "say what to play" in str(caught.value)
    assert adapter._runner.argv == []


def test_a_search_becomes_ranked_candidates_in_the_order_spotify_ranked_them(tmp_path):
    adapter = spotify(tmp_path, [(0, SEARCH_JSON, "")])
    found = adapter.resolve("spotify.play_track", {"query": "bohemian rhapsody"}, CONTEXT)
    assert [a.label for a in found] == [
        "Bohemian Rhapsody - Queen",
        "Bohemian Rhapsody - Live Aid - Queen",
        "Bohemian Rhapsody - Remastered 2011 - Queen",
    ]
    assert adapter._runner.argv == [["/usr/bin/spotify_player", "search", "bohemian rhapsody"]]


def test_candidate_ids_are_one_based_so_an_ordinal_indexes_a_real_list(tmp_path):
    adapter = spotify(tmp_path, [(0, SEARCH_JSON, "")])
    found = adapter.resolve("spotify.play_track", {"query": "bohemian"}, CONTEXT)
    assert [a.id for a in found] == [
        "spotify.play_track#1",
        "spotify.play_track#2",
        "spotify.play_track#3",
    ]


def test_an_id_written_as_a_uri_or_a_url_is_read_as_the_id_it_contains():
    assert identifier("spotify:track:3z8h0TU7ReDPLIbEnYhWZb") == "3z8h0TU7ReDPLIbEnYhWZb"
    assert identifier("https://open.spotify.com/track/7tFiyTwD0nx5a1eklYtX2J?si=x") == (
        "7tFiyTwD0nx5a1eklYtX2J"
    )
    assert identifier({"uri": "spotify:album:1GbtB4zTqAsyfZEsm1RZfx"}) == "1GbtB4zTqAsyfZEsm1RZfx"


def test_anything_that_is_not_a_spotify_id_never_becomes_a_candidate():
    assert identifier("not-an-id") == ""
    assert identifier("6rqhFgbbKwnb9MLmUQDhG6; rm -rf /") == ""
    assert identifier(None) == ""


def test_a_result_with_an_unreadable_id_is_dropped_and_the_rest_are_kept(tmp_path):
    adapter = spotify(tmp_path, [(0, SEARCH_JSON, "")])
    found = adapter.resolve("spotify.play_track", {"query": "bohemian"}, CONTEXT)
    assert len(found) == 3
    assert all(len(a.handle) == 22 for a in found)


def test_every_spotify_candidate_is_untrusted_because_the_internet_wrote_it(tmp_path):
    adapter = spotify(tmp_path, [(0, SEARCH_JSON, "")])
    found = adapter.resolve("spotify.play_track", {"query": "bohemian"}, CONTEXT)
    assert {a.trust for a in found} == {Trust.UNTRUSTED}


def test_a_track_is_started_by_its_id_and_never_by_its_name(tmp_path):
    adapter = spotify(tmp_path, [(0, SEARCH_JSON, ""), (0, "", "")])
    found = adapter.resolve("spotify.play_track", {"query": "bohemian"}, CONTEXT)
    outcome = adapter.perform("spotify.play_track", found[1].handle, CONTEXT)
    assert outcome.ok
    assert adapter._runner.argv[1] == [
        "/usr/bin/spotify_player",
        "playback",
        "start",
        "track",
        "--id",
        "3z8h0TU7ReDPLIbEnYhWZb",
    ]
    assert "Live Aid" in outcome.message


def test_the_blind_first_result_flag_appears_in_no_command_this_adapter_builds(tmp_path):
    adapter = spotify(
        tmp_path, [(0, SEARCH_JSON, ""), (0, "", ""), (0, SEARCH_JSON, ""), (0, "", "")]
    )
    found = adapter.resolve("spotify.play_track", {"query": "bohemian"}, CONTEXT)
    adapter.perform("spotify.play_track", found[0].handle, CONTEXT)
    albums = adapter.resolve("spotify.play_album", {"query": "opera"}, CONTEXT)
    adapter.perform("spotify.play_album", albums[0].handle, CONTEXT)
    assert not any("--name" in argv for argv in adapter._runner.argv)


def test_an_album_is_started_as_a_context_because_that_is_what_an_album_is(tmp_path):
    adapter = spotify(tmp_path, [(0, SEARCH_JSON, ""), (0, "", "")])
    found = adapter.resolve("spotify.play_album", {"query": "opera"}, CONTEXT)
    adapter.perform("spotify.play_album", found[0].handle, CONTEXT)
    assert adapter._runner.argv[1] == [
        "/usr/bin/spotify_player",
        "playback",
        "start",
        "context",
        "--id",
        "1GbtB4zTqAsyfZEsm1RZfx",
        "album",
    ]


def test_a_playlist_is_labelled_by_who_owns_it(tmp_path):
    adapter = spotify(tmp_path, [(0, SEARCH_JSON, "")])
    found = adapter.resolve("spotify.play_playlist", {"query": "rap"}, CONTEXT)
    assert found[0].label == "RapCaviar - Spotify"


def test_a_handle_this_adapter_never_offered_is_refused(tmp_path):
    adapter = spotify(tmp_path, [(0, SEARCH_JSON, "")])
    adapter.resolve("spotify.play_track", {"query": "bohemian"}, CONTEXT)
    with pytest.raises(AdapterError) as caught:
        adapter.perform("spotify.play_track", "4cOdK2wGLETKBW3PvgPWqT", CONTEXT)
    assert "offered" in str(caught.value)
    assert len(adapter._runner.argv) == 1


def test_a_handle_that_would_not_survive_a_command_line_is_refused(tmp_path):
    adapter = spotify(tmp_path)
    adapter._minted.mint("--name Bohemian")
    with pytest.raises(AdapterError):
        adapter.perform("spotify.play_track", "--name Bohemian", CONTEXT)
    assert adapter._runner.argv == []


def test_the_same_words_inside_the_window_are_searched_once(tmp_path):
    clock = Clock()
    adapter = spotify(tmp_path, [(0, SEARCH_JSON, "")], clock=clock)
    adapter.resolve("spotify.play_track", {"query": "bohemian"}, CONTEXT)
    adapter.resolve("spotify.play_track", {"query": "Bohemian"}, CONTEXT)
    adapter.resolve("spotify.play_album", {"query": "bohemian"}, CONTEXT)
    assert len(adapter._runner.argv) == 1


def test_a_search_is_made_again_once_the_answer_is_old(tmp_path):
    clock = Clock()
    adapter = spotify(tmp_path, [(0, SEARCH_JSON, ""), (0, SEARCH_JSON, "")], clock=clock)
    adapter.resolve("spotify.play_track", {"query": "bohemian"}, CONTEXT)
    clock.now += 120.0
    adapter.resolve("spotify.play_track", {"query": "bohemian"}, CONTEXT)
    assert len(adapter._runner.argv) == 2


def test_output_that_is_not_json_becomes_one_plain_sentence(tmp_path):
    adapter = spotify(tmp_path, [(0, "Starting a new client...", "")])
    with pytest.raises(AdapterError) as caught:
        adapter.resolve("spotify.play_track", {"query": "bohemian"}, CONTEXT)
    assert "not the search JSON" in str(caught.value)


def test_a_search_that_found_nothing_says_so(tmp_path):
    adapter = spotify(tmp_path, [(0, json.dumps({"tracks": []}), "")])
    with pytest.raises(AdapterError) as caught:
        adapter.resolve("spotify.play_track", {"query": "zzzz"}, CONTEXT)
    assert "no track" in str(caught.value)


def test_an_answer_in_a_shape_this_cannot_read_is_not_silently_empty(tmp_path):
    payload = json.dumps({"tracks": [{"identifier": "x", "title": "y"}]})
    adapter = spotify(tmp_path, [(0, payload, "")])
    with pytest.raises(AdapterError) as caught:
        adapter.resolve("spotify.play_track", {"query": "y"}, CONTEXT)
    assert "could not read" in str(caught.value)


def test_a_credential_that_expired_since_the_probe_becomes_the_login_sentence(tmp_path):
    adapter = spotify(tmp_path, [(1, "", "error: failed to refresh the access token")])
    with pytest.raises(AdapterError) as caught:
        adapter.resolve("spotify.play_track", {"query": "bohemian"}, CONTEXT)
    assert "spotify_player authenticate" in str(caught.value)


def test_any_other_refusal_keeps_the_tools_own_words(tmp_path):
    adapter = spotify(tmp_path, [(1, "", "no active device found")])
    with pytest.raises(AdapterError) as caught:
        adapter.resolve("spotify.play_track", {"query": "bohemian"}, CONTEXT)
    assert "no active device found" in str(caught.value)


def test_a_capability_spotify_does_not_have_is_refused_rather_than_guessed(tmp_path):
    adapter = spotify(tmp_path)
    with pytest.raises(AdapterError):
        adapter.resolve("spotify.buy_the_album", {"query": "x"}, CONTEXT)
    with pytest.raises(AdapterError):
        adapter.perform("spotify.buy_the_album", "6rqhFgbbKwnb9MLmUQDhG6", CONTEXT)


def test_a_query_longer_than_anything_spoken_is_cut_before_it_is_run(tmp_path):
    adapter = spotify(tmp_path, [(0, SEARCH_JSON, "")])
    adapter.resolve("spotify.play_track", {"query": "la " * 200}, CONTEXT)
    assert len(adapter._runner.argv[0][2]) <= 120


# --------------------------------------------------------------------------- waytify


def hello(protocol=PROTOCOL):
    return {"type": "hello", "protocol": protocol, "version": "0.1.0"}


def result(name, subtitle, uri, kind="track"):
    return {"name": name, "subtitle": subtitle, "uri": uri, "kind": kind}


TRACK = "spotify:track:6rqhFgbbKwnb9MLmUQDhG6"
LIVE = "spotify:track:3z8h0TU7ReDPLIbEnYhWZb"
ALBUM = "spotify:album:1GbtB4zTqAsyfZEsm1RZfx"
LIST = "spotify:playlist:37i9dQZF1DX0XUsuxWHRQd"

RHAPSODY = [
    result("Bohemian Rhapsody", "Queen", TRACK),
    result("Bohemian Rhapsody - Live Aid", "Queen", LIVE),
    result("A Night at the Opera", "Queen", ALBUM, "album"),
    result("RapCaviar", "Spotify", LIST, "playlist"),
]


def state(*, authorized=True, search=()):
    spotify_block = {"authorized": authorized}
    if search:
        spotify_block["search"] = list(search)
    return {"type": "state", "state": {"spotify": spotify_block, "caps": {}}}


class FakeLink:
    """One recorded conversation. Frames are handed over in order and every line the
    adapter writes is kept, so what was asked of the daemon is an assertion."""

    def __init__(self, frames):
        self.pending = [dict(f) if isinstance(f, dict) else f for f in frames]
        self.sent = []
        self.closed = False

    def send(self, line):
        self.sent.append(json.loads(line))

    def readline(self, timeout):
        while self.pending:
            frame = self.pending.pop(0)
            if frame is None:  # the daemon going quiet, which reads as end of stream
                return ""
            return json.dumps(frame) if isinstance(frame, dict) else frame
        return ""

    def close(self):
        self.closed = True

    @property
    def commands(self):
        return [line.get("cmd") for line in self.sent]


class FakeDial:
    """A socket that is always there, answering with one recorded script per connect."""

    def __init__(self, *scripts):
        self.scripts = [list(s) for s in scripts]
        self.links = []
        self.paths = []

    def __call__(self, path, timeout):
        self.paths.append(path)
        script = self.scripts.pop(0) if self.scripts else []
        self.links.append(FakeLink(script))
        return self.links[-1]


def waytify(*scripts, path="/run/waytify/sock", clock=None):
    adapter = Waytify(FakeDial(*scripts), path=path, clock=clock or Clock())
    adapter.listening = lambda: True  # the socket itself is a filesystem question
    return adapter


def searching(rows=RHAPSODY, *, authorized=True, before=()):
    """The frames one search produces: hello, the state on subscribe, then the answer."""
    return [hello(), state(authorized=authorized, search=before), state(search=rows)]


def test_the_socket_is_the_one_waytifys_own_paths_module_resolves(monkeypatch):
    monkeypatch.delenv("WAYTIFY_SOCKET", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    assert str(socket_path()) == "/run/user/1000/waytify/sock"
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    monkeypatch.setenv("USER", "ilyask")
    assert str(socket_path()) == "/tmp/waytify-ilyask/sock"


def test_the_socket_override_is_taken_exactly_as_given(monkeypatch):
    """It is how a second daemon is reached, so nothing may be joined onto it."""
    monkeypatch.setenv("WAYTIFY_SOCKET", "/tmp/somewhere/else.sock")
    assert str(socket_path()) == "/tmp/somewhere/else.sock"


def test_the_probe_names_the_daemon_when_nothing_is_listening(tmp_path):
    usable, why = Waytify(path=tmp_path / "sock").probe()
    assert usable is False
    assert "waytify daemon" in why


def test_deciding_whether_waytify_can_serve_never_opens_a_connection(tmp_path):
    """`can_serve` runs at key down, once per adapter, so it is a stat and nothing more."""
    dial = FakeDial()
    adapter = Waytify(dial, path=tmp_path / "sock")
    assert adapter.can_serve(CONTEXT) is False
    assert dial.links == []


def test_the_probe_names_the_login_when_no_account_is_connected():
    adapter = waytify([hello(), state(authorized=False)])
    usable, why = adapter.probe()
    assert usable is False
    assert "waytify login" in why


def test_a_protocol_this_build_does_not_know_is_refused_rather_than_guessed_at():
    """waytify's own lib.rs says clients exit loudly on a mismatch instead of guessing,
    because a stale client misreading a newer daemon is the case the number exists for."""
    adapter = waytify([hello(protocol=PROTOCOL + 1)])
    usable, why = adapter.probe()
    assert usable is False
    assert f"protocol {PROTOCOL + 1}" in why and f"speaks {PROTOCOL}" in why


def test_the_probe_is_usable_once_the_daemon_answers_with_an_account():
    usable, why = waytify([hello(), state()]).probe()
    assert usable is True
    assert "account connected" in why


def test_the_probe_never_raises_whatever_the_daemon_does():
    class Broken:
        def __call__(self, path, timeout):
            raise RuntimeError("connection reset by peer")

    adapter = Waytify(Broken(), path="/run/waytify/sock")
    adapter.listening = lambda: True
    assert adapter.probe()[0] is False


def test_a_daemon_that_says_nothing_at_all_is_one_sentence_and_not_a_hang():
    assert waytify([]).probe() == (False, "waytify accepted the connection and then said nothing")


def test_the_handshake_subscribes_to_the_full_model_because_the_bar_scope_has_no_search():
    adapter = waytify(searching())
    adapter.resolve("waytify.play_track", {"query": "bohemian rhapsody"}, CONTEXT)
    subscribe = adapter._dial.links[0].sent[0]
    assert subscribe == {"cmd": "subscribe", "scope": "full"}


def test_a_waytify_search_becomes_candidates_in_the_order_spotify_ranked_them():
    adapter = waytify(searching())
    found = adapter.resolve("waytify.play_track", {"query": "bohemian rhapsody"}, CONTEXT)
    assert [a.label for a in found] == [
        "Bohemian Rhapsody - Queen",
        "Bohemian Rhapsody - Live Aid - Queen",
    ]
    assert [a.id for a in found] == ["waytify.play_track#1", "waytify.play_track#2"]
    assert adapter._dial.links[0].sent[1] == {"cmd": "search", "query": "bohemian rhapsody"}


def test_each_family_is_offered_only_the_results_that_are_of_that_kind():
    adapter = waytify(searching(), searching(), searching())
    for capability, label in (
        ("waytify.play_album", "A Night at the Opera - Queen"),
        ("waytify.play_playlist", "RapCaviar - Spotify"),
    ):
        adapter.forget()
        found = adapter.resolve(capability, {"query": "queen"}, CONTEXT)
        assert [a.label for a in found] == [label]


def test_every_waytify_candidate_is_untrusted_because_the_internet_wrote_it():
    found = waytify(searching()).resolve("waytify.play_track", {"query": "queen"}, CONTEXT)
    assert all(a.trust is Trust.UNTRUSTED for a in found)


def test_a_result_with_no_usable_uri_never_becomes_a_candidate():
    """A URI is what goes back over the socket as a command argument, so a row without
    one is dropped rather than carried as an option that cannot be played."""
    rows = [
        result("Nice try", "Queen", "spotify:track:../../etc/passwd"),
        result("Also nice", "Queen", "https://open.spotify.com/track/6rqhFgbbKwnb9MLmUQDhG6"),
        result("Bohemian Rhapsody", "Queen", TRACK),
    ]
    found = waytify(searching(rows)).resolve("waytify.play_track", {"query": "queen"}, CONTEXT)
    assert [a.handle for a in found] == [TRACK]


def test_a_result_of_a_kind_this_build_does_not_know_is_dropped_rather_than_offered():
    rows = [result("A Show", "Somebody", "spotify:track:6rqhFgbbKwnb9MLmUQDhG6", "episode")]
    with pytest.raises(AdapterError):
        waytify(searching(rows)).resolve("waytify.play_track", {"query": "queen"}, CONTEXT)


def test_an_utterance_with_nothing_to_search_for_asks_the_daemon_nothing():
    with pytest.raises(AdapterError, match="say what to play"):
        waytify(searching()).resolve("waytify.play_track", {"query": "  "}, CONTEXT)


def test_a_capability_waytify_does_not_have_is_refused_rather_than_guessed():
    with pytest.raises(AdapterError, match="not something waytify does"):
        waytify(searching()).resolve("waytify.play_artist", {"query": "queen"}, CONTEXT)


def test_the_list_the_daemon_already_held_is_cleared_before_the_real_query_goes_out():
    """waytify publishes a new state only when the search list CHANGED, so a query whose
    answer equals what is already there would publish nothing at all. Clearing first
    costs one local round trip and no Spotify request, and removes that case."""
    stale = [result("Something else", "Somebody", LIVE)]
    adapter = waytify(
        [hello(), state(search=stale), state(search=()), state(search=RHAPSODY)],
    )
    adapter.resolve("waytify.play_track", {"query": "bohemian rhapsody"}, CONTEXT)
    assert adapter._dial.links[0].sent[1:] == [
        {"cmd": "search", "query": ""},
        {"cmd": "search", "query": "bohemian rhapsody"},
    ]


def test_results_left_over_from_somebody_elses_query_are_never_offered_as_this_ones():
    """The baseline is carried for exactly this: with no new state frame, the answer is
    nothing, and not whatever the previous search left in the daemon."""
    stale = [result("Something else", "Somebody", LIVE)]
    adapter = waytify([hello(), state(search=stale), state(search=()), None])
    with pytest.raises(AdapterError, match="came back with nothing"):
        adapter.resolve("waytify.play_track", {"query": "bohemian rhapsody"}, CONTEXT)


def test_the_same_words_inside_the_window_reach_the_daemon_once():
    clock = Clock()
    adapter = waytify(searching(), searching(), clock=clock)
    adapter.resolve("waytify.play_track", {"query": "queen"}, CONTEXT)
    adapter.resolve("waytify.play_track", {"query": "queen"}, CONTEXT)
    assert len(adapter._dial.links) == 1


def test_the_daemon_is_asked_again_once_the_answer_is_old():
    clock = Clock()
    adapter = waytify(searching(), searching(), clock=clock)
    adapter.resolve("waytify.play_track", {"query": "queen"}, CONTEXT)
    clock.now += 60.0
    adapter.resolve("waytify.play_track", {"query": "queen"}, CONTEXT)
    assert len(adapter._dial.links) == 2


def test_nothing_is_cached_when_nothing_came_back():
    """ "Spotify found nothing" and "Spotify has not answered yet" are the same silence on
    this protocol, so caching the silence would turn a slow answer into a wrong one."""
    adapter = waytify([hello(), state(), None], searching())
    with pytest.raises(AdapterError):
        adapter.resolve("waytify.play_track", {"query": "queen"}, CONTEXT)
    found = adapter.resolve("waytify.play_track", {"query": "queen"}, CONTEXT)
    assert len(found) == 2


def test_a_search_with_no_account_says_so_rather_than_answering_with_an_empty_list():
    adapter = waytify([hello(), state(authorized=False)])
    with pytest.raises(AdapterError, match="waytify login"):
        adapter.resolve("waytify.play_track", {"query": "queen"}, CONTEXT)


def test_a_track_is_started_by_its_uri_and_never_by_its_name():
    adapter = waytify(searching(), [hello(), {"type": "ack"}])
    found = adapter.resolve("waytify.play_track", {"query": "queen"}, CONTEXT)
    outcome = adapter.perform("waytify.play_track", found[0].handle, CONTEXT)
    assert adapter._dial.links[1].sent[0] == {"cmd": "play_track", "uri": TRACK}
    assert outcome.ok is True
    assert "Bohemian Rhapsody" in outcome.message
    assert outcome.inverse is None  # there is no "go back to what you interrupted"


def test_an_album_is_started_over_the_socket_as_a_context_too():
    adapter = waytify(searching(), [hello(), {"type": "ack"}])
    found = adapter.resolve("waytify.play_album", {"query": "queen"}, CONTEXT)
    adapter.perform("waytify.play_album", found[0].handle, CONTEXT)
    assert adapter._dial.links[1].sent[0] == {"cmd": "play_context", "uri": ALBUM}


def test_a_uri_this_adapter_never_offered_is_refused():
    adapter = waytify(searching())
    adapter.resolve("waytify.play_track", {"query": "queen"}, CONTEXT)
    with pytest.raises(AdapterError, match="not a result this command offered"):
        adapter.perform("waytify.play_track", "spotify:track:0000000000000000000000", CONTEXT)


def test_a_handle_of_the_wrong_kind_is_refused_before_anything_leaves():
    """An album URI sent as a track would be refused three processes away; refusing it
    here makes the refusal one sentence."""
    adapter = waytify(searching())
    found = adapter.resolve("waytify.play_album", {"query": "queen"}, CONTEXT)
    with pytest.raises(AdapterError, match="not a result this command offered"):
        adapter.perform("waytify.play_track", found[0].handle, CONTEXT)


def test_a_daemon_that_refuses_becomes_one_plain_sentence_in_its_own_words():
    adapter = waytify(searching(), [hello(), {"type": "error", "message": "no Spotify account"}])
    found = adapter.resolve("waytify.play_track", {"query": "queen"}, CONTEXT)
    with pytest.raises(AdapterError, match="no Spotify account"):
        adapter.perform("waytify.play_track", found[0].handle, CONTEXT)


def test_a_daemon_that_never_acknowledges_is_not_reported_as_having_played_anything():
    adapter = waytify(searching(), [hello(), None])
    found = adapter.resolve("waytify.play_track", {"query": "queen"}, CONTEXT)
    with pytest.raises(AdapterError, match="never said whether"):
        adapter.perform("waytify.play_track", found[0].handle, CONTEXT)


def test_every_connection_is_closed_even_when_the_daemon_misbehaves():
    adapter = waytify([hello(), state(authorized=False)])
    with pytest.raises(AdapterError):
        adapter.resolve("waytify.play_track", {"query": "queen"}, CONTEXT)
    assert adapter._dial.links[0].closed is True


def test_a_frame_this_build_does_not_know_is_skipped_and_not_fatal():
    """A newer daemon is allowed to send frames this one has never heard of."""
    adapter = waytify(
        [
            hello(),
            {"type": "popup", "action": "show"},
            state(),
            "not json at all",
            state(search=RHAPSODY),
        ]
    )
    found = adapter.resolve("waytify.play_track", {"query": "queen"}, CONTEXT)
    assert len(found) == 2


def test_a_label_written_by_spotify_cannot_grow_past_the_label_budget():
    rows = [result("Z" * 300, "Queen", TRACK)]
    found = waytify(searching(rows)).resolve("waytify.play_track", {"query": "q"}, CONTEXT)
    assert len(found[0].label) == MAX_LABEL


def test_the_cli_stands_down_whenever_the_owners_own_daemon_is_listening(tmp_path):
    """Demoted to a fallback: two adapters offering the same track under two ids would
    be two sets of guards to keep in step and one option list with duplicates in it."""
    daemon = Waytify(FakeDial(), path=tmp_path / "sock")
    fallback = Spotify(
        FakeRunner(),
        binary="/usr/bin/spotify_player",
        cache=authenticated(tmp_path),
        superseded_by=daemon,
    )
    assert fallback.can_serve(CONTEXT) is True  # no socket, so the CLI answers
    daemon.listening = lambda: True
    assert fallback.can_serve(CONTEXT) is False


# --------------------------------------------------------------------------- together


def test_the_live_adapters_are_the_three_that_exist():
    assert [adapter.name for adapter in live()] == ["mpris", "waytify", "spotify"]


def test_the_live_media_adapter_is_built_with_the_owners_own_privacy_settings():
    """It reads what a player is playing, which is the same class of secret as a window
    title, so it may not quietly hold a default when the owner said never."""
    adapter = live(Config(privacy=Privacy(titles="never")))[0]
    assert adapter._privacy.never is True


def test_a_harvest_of_the_real_adapters_never_raises_whatever_the_machine_has(tmp_path):
    class Dead:
        def call(self, *a, **kw):
            raise OSError("no bus")

    adapters = (
        Mpris(Dead()),
        Waytify(path=tmp_path / "not-a-socket"),
        Spotify(FakeRunner(), binary="nope", cache=tmp_path),
    )
    assert base.offer(adapters, CONTEXT) == ()
