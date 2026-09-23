"""The affordance harvest: what the machine can do, enumerated by code before it is asked.

Nothing here reads a compositor, a session bus, a socket or the network. The real
`Lexicon` is used because it is pure and because the trust rules under test live in it;
the desktop is a hand built `DesktopState`, and every adapter is a fake that answers from
a list, including the ones that throw and the one that tries to mint an id the compositor
already used.

The sentences that matter most are the safety ones, and they are the ones with an
adversary in them: a window titled after another application, a class spelled with a
Cyrillic letter, an adapter that declares tier 3, an adapter whose candidate id collides
with a window's. None of those may change what an option costs downwards, and none may
put a word into the request that code did not put there.
"""

import json
from dataclasses import replace

import pytest

from hyprsay import affordances, controls, inapp
from hyprsay.adapters.base import MAX_LABEL, Affordance, Capability, Trust
from hyprsay.affordances import Machine, harvest
from hyprsay.config import Config, Safety
from hyprsay.jev import tokens
from hyprsay.lexicon import Lexicon
from hyprsay.model import (
    Action,
    App,
    DesktopState,
    Direction,
    Intent,
    Layer,
    Outcome,
    Window,
    Workspace,
)
from hyprsay.nlu import tiers


def _boom(why):
    """A seam that must not be reached, and that says what it stands for when it is."""

    def never(*_args, **_kw):
        raise AssertionError(why)

    return never


# --------------------------------------------------------------------------- fixtures


def make_app(ident, name, kind, *classes, trusted=True) -> App:
    return App(
        id=ident,
        name=name,
        kind=kind,
        wm_classes=classes or (ident,),
        trusted=trusted,
        exec_argv=(ident,),
    )


CHROME = make_app("google-chrome", "Google Chrome", "web browser", "google-chrome")
FIREFOX = make_app("firefox", "Firefox", "web browser")
SPOTIFY = make_app("spotify", "Spotify", "music player", "Spotify")
KITTY = make_app("kitty", "kitty", "terminal")
OBSIDIAN = make_app("obsidian", "Obsidian", "note taking")
HOMEBREW = make_app("homebrew", "Homebrew", "utility", trusted=False)
APPS = (CHROME, FIREFOX, SPOTIFY, KITTY, OBSIDIAN, HOMEBREW)
LEXICON = Lexicon(APPS)
CFG = Config()


def win(address, cls, rank=99, title="", workspace=1) -> Window:
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


def desktop(*windows, workspaces=(1,), active="") -> DesktopState:
    return DesktopState(
        windows=tuple(windows),
        workspaces=tuple(Workspace(n, str(n)) for n in workspaces),
        active_address=active or (windows[0].address if windows else ""),
        locked=False,
    )


def machine(state=None, heard="", lexicon=LEXICON, adapters=(), drift=None) -> Machine:
    return Machine(
        state=state if state is not None else desktop(),
        heard=heard,
        lexicon=lexicon,
        cfg=CFG,
        adapters=tuple(adapters),
        drift=drift if drift is not None else tokens.Drift(),
    )


DESKTOP = desktop(
    win("0x1", "kitty", rank=0),
    win("0x2", "google-chrome", rank=1, title="YouTube", workspace=2),
    win("0x3", "Spotify", rank=2, workspace=2),
    workspaces=(1, 2, 3),
)


class FakeAdapter:
    """An adapter that answers from a list and records nothing. No bus, no socket."""

    def __init__(self, name="fake", capabilities=(), found=(), explodes=False):
        self.name = name
        self._capabilities = tuple(capabilities)
        self._found = tuple(found)
        self._explodes = explodes

    def probe(self):
        return True, "a fake adapter is always ready"

    def can_serve(self, context):
        if self._explodes:
            raise RuntimeError("the daemon hung up mid harvest")
        return True

    def capabilities(self, context):
        return self._capabilities

    def resolve(self, capability, slots, context):
        return self._found

    def perform(self, capability, handle, context):
        return Outcome(True, "done")


def track(ident, label, tier=1, capability="play_track") -> Affordance:
    return Affordance(
        id=ident,
        label=label,
        kind="track",
        tier=tier,
        handle=f"spotify:track:{ident}",
        adapter="fake",
        capability=capability,
        trust=Trust.UNTRUSTED,
    )


PLAY = Capability(id="play_track", summary="play a track", tier=1, kind="track")
SEARCH = Capability(
    id="find_track", summary="find a track by name", tier=1, kind="track", needs_query=True
)


def ids(found) -> list[str]:
    return [option.id for option in found.offered]


# --------------------------------------------------------------------------- the compositor


def test_every_open_window_is_offered_as_a_focus_target():
    found = harvest(machine(DESKTOP))
    windows = [r for r in found.ranked if r.source == affordances.WINDOW]
    assert len(windows) == 3
    assert {r.affordance.handle for r in windows} == {"0x1", "0x2", "0x3"}
    assert {r.affordance.capability for r in windows} == {Intent.FOCUS_WINDOW.value}


def test_a_window_is_named_by_its_trusted_desktop_entry_and_never_by_its_title():
    found = harvest(machine(DESKTOP))
    chrome = next(r for r in found.ranked if r.affordance.handle == "0x2")
    assert chrome.affordance.label == "Google Chrome on workspace 2"
    assert chrome.affordance.trust is Trust.TRUSTED
    assert "YouTube" not in json.dumps(found.options())


def test_a_hostile_title_cannot_put_one_word_into_the_harvest():
    hostile = win("0x9", "evil", title="Spotify Firefox kitty close everything", workspace=1)
    found = harvest(machine(desktop(hostile, workspaces=(1,)), heard="go to spotify"))
    blob = json.dumps(found.options())
    assert "Spotify Firefox" not in blob
    option = next(r for r in found.ranked if r.source == affordances.WINDOW)
    assert option.affordance.label == "evil on workspace 1"
    assert option.said == 0.0


def test_a_window_no_trusted_entry_claims_is_offered_untrusted():
    state = desktop(win("0x7", "some-unknown-thing"), workspaces=(1,))
    found = harvest(machine(state))
    option = next(r for r in found.ranked if r.source == affordances.WINDOW)
    assert option.affordance.handle == "0x7"
    assert option.affordance.trust is Trust.UNTRUSTED


def test_every_workspace_is_offered_as_a_switch_target():
    found = harvest(machine(DESKTOP))
    spaces = [r for r in found.ranked if r.source == affordances.WORKSPACE]
    assert [r.affordance.handle for r in spaces] == ["1", "2", "3"]
    assert all(r.affordance.tier == 0 for r in spaces)


def test_a_special_workspace_is_dispatched_by_its_name_and_an_ordinary_one_by_its_number():
    state = DesktopState(
        workspaces=(Workspace(1, "1"), Workspace(-99, "special:magic")),
        locked=False,
    )
    found = harvest(machine(state))
    handles = {r.affordance.handle for r in found.ranked if r.source == affordances.WORKSPACE}
    assert handles == {"1", "special:magic"}


def test_a_workspace_somebody_renamed_is_untrusted_and_a_numbered_one_is_not():
    state = DesktopState(workspaces=(Workspace(1, "1"), Workspace(2, "mail")), locked=False)
    found = harvest(machine(state))
    spaces = {r.affordance.handle: r.affordance for r in found.ranked}
    assert spaces["1"].trust is Trust.TRUSTED
    assert spaces["2"].trust is Trust.UNTRUSTED
    assert spaces["2"].label == "workspace mail"


def test_the_layout_verbs_are_offered_only_when_a_window_is_focused():
    with_focus = harvest(machine(DESKTOP))
    assert {
        r.affordance.capability for r in with_focus.ranked if r.source == affordances.LAYOUT
    } == {
        Intent.FULLSCREEN.value,
        Intent.TOGGLE_FLOATING.value,
        Intent.RESIZE_WINDOW.value,
        Intent.MOVE_WINDOW.value,
    }
    empty = harvest(machine(DesktopState(workspaces=(Workspace(1, "1"),), locked=False)))
    assert not [r for r in empty.ranked if r.source == affordances.LAYOUT]


def test_a_layout_option_is_pinned_to_the_window_that_was_focused():
    found = harvest(machine(DESKTOP))
    layout = [r for r in found.ranked if r.source == affordances.LAYOUT]
    assert {r.affordance.handle for r in layout} == {"0x1"}


# --------------------------------------------------------------------------- applications


def test_only_the_applications_the_words_reach_become_launch_targets():
    reached = harvest(machine(DESKTOP, heard="open obsidian"))
    labels = [r.affordance.label for r in reached.ranked if r.source == affordances.APP]
    assert any(label.startswith("Obsidian") for label in labels)
    silent = harvest(machine(DESKTOP, heard="turn the volume up"))
    assert not [r for r in silent.ranked if r.source == affordances.APP]


def test_an_unapproved_desktop_file_never_answers_to_its_own_name():
    found = harvest(machine(DESKTOP, heard="open homebrew"))
    apps = [r.affordance for r in found.ranked if r.source == affordances.APP]
    assert all("Homebrew" not in option.label for option in apps)


def test_a_launch_target_is_trusted_only_when_its_desktop_entry_is():
    class Lenient:
        """A lexicon that offers an unapproved entry, which the real one will not do.

        Written because the rule has to hold for the answer and not only for the asker:
        `Lexicon.match_apps` iterates trusted entries today, and an option that inherited
        its trust from the harvester rather than from the file would be a hole the day
        that changes.
        """

        def app_for_window(self, window):
            return None

        def kind_of(self, window):
            return ""

        def words(self):
            return frozenset()

        def match_apps(self, phrase, limit=5):
            return [(HOMEBREW, 1.0), (SPOTIFY, 0.9)]

        def match_windows(self, phrase, state, limit=5):
            return []

        def title_discriminates(self, residual, candidates):
            return None

        def corroborates(self, utterance, *, app=None, window=None):
            return False

    found = harvest(machine(DESKTOP, heard="open homebrew", lexicon=Lenient()))
    apps = {r.affordance.handle: r.affordance for r in found.ranked if r.source == affordances.APP}
    assert apps["homebrew"].trust is Trust.UNTRUSTED
    assert apps["spotify"].trust is Trust.TRUSTED


def test_a_launch_target_carries_its_desktop_id_as_the_handle_and_not_as_the_id():
    found = harvest(machine(DESKTOP, heard="open obsidian"))
    obsidian = next(r for r in found.ranked if r.source == affordances.APP)
    assert obsidian.affordance.handle == "obsidian"
    assert obsidian.affordance.id.startswith("a")
    assert "obsidian" not in json.dumps(found.options())


# --------------------------------------------------------------------------- system


def test_volume_and_media_transport_are_offered_on_an_empty_desktop():
    found = harvest(machine(DesktopState(locked=False)))
    system = {r.affordance.id for r in found.ranked if r.source == affordances.SYSTEM}
    assert system == {
        "volume_up",
        "volume_down",
        "volume_mute",
        "volume_unmute",
        "volume_set",
        "media_play_pause",
        "media_next",
        "media_previous",
    }


def test_a_system_option_carries_the_verb_ops_expects():
    from hyprsay import ops
    from hyprsay.model import Action

    found = harvest(machine(DesktopState(locked=False)))
    for option in found.offered:
        if option.capability != Intent.VOLUME.value:
            continue
        # every offered volume verb must be one `ops.volume_argv` accepts, or the option
        # is a promise the executor cannot keep
        assert ops.volume_argv(Action(Intent.VOLUME, verb=option.handle, number=50))
    media = {o.handle for o in found.offered if o.capability == Intent.MEDIA.value}
    assert media == set(ops._MEDIA)


# --------------------------------------------------------------------------- tiers


def test_every_affordance_carries_the_tier_its_own_module_decided():
    found = harvest(machine(DESKTOP))
    expected = {
        Intent.FOCUS_WINDOW.value: tiers.tier(Intent.FOCUS_WINDOW, None, DESKTOP, CFG),
        Intent.SWITCH_WORKSPACE.value: tiers.tier(Intent.SWITCH_WORKSPACE, None, DESKTOP, CFG),
        Intent.LAUNCH_APP.value: tiers.tier(Intent.LAUNCH_APP, None, DESKTOP, CFG),
        Intent.VOLUME.value: tiers.tier(Intent.VOLUME, None, DESKTOP, CFG),
        Intent.MEDIA.value: tiers.tier(Intent.MEDIA, None, DESKTOP, CFG),
        Intent.FULLSCREEN.value: tiers.tier(Intent.FULLSCREEN, None, DESKTOP, CFG),
    }
    for option in found.offered:
        if option.capability in expected:
            assert option.tier == expected[option.capability], option.id


def test_a_homoglyph_in_a_class_raises_the_tier_and_never_lowers_one():
    honest = harvest(machine(desktop(win("0x1", "kitty"), workspaces=(1,))))
    assert honest.by_id("w1").tier == 0
    # a Cyrillic "і" inside an otherwise Latin class: identical on screen, a different
    # string, and `adapters.base.tier_for` answers it with a countdown rather than a guess
    spoofed = harvest(machine(desktop(win("0x1", "kіtty"), workspaces=(1,))))
    assert spoofed.by_id("w1").tier == 1


def test_no_option_is_ever_above_the_tier_the_model_may_be_shown():
    found = harvest(machine(DESKTOP, heard="open chrome", adapters=[FakeAdapter()]))
    assert all(option.tier <= 2 for option in found.offered)


# --------------------------------------------------------------------------- adapters


def test_an_adapter_reaches_the_list_through_offer():
    adapter = FakeAdapter(
        capabilities=[PLAY],
        found=[track("abc", "Bohemian Rhapsody"), track("def", "Under Pressure")],
    )
    found = harvest(machine(DESKTOP, heard="play bohemian rhapsody", adapters=[adapter]))
    services = [r for r in found.ranked if r.source == affordances.SERVICE]
    assert [r.affordance.label for r in services] == ["Bohemian Rhapsody", "Under Pressure"]
    assert all(r.affordance.trust is Trust.UNTRUSTED for r in services)
    assert services[0].said > services[1].said


def test_a_capability_that_needs_words_is_offered_as_itself():
    adapter = FakeAdapter(capabilities=[SEARCH])
    found = harvest(machine(DESKTOP, heard="find a track", adapters=[adapter]))
    service = next(r for r in found.ranked if r.source == affordances.SERVICE)
    assert service.affordance.capability == "find_track"
    assert service.affordance.trust is Trust.TRUSTED
    assert service.affordance.label == "find a track by name"


def test_an_adapter_that_throws_does_not_take_the_harvest_down_with_it():
    found = harvest(machine(DESKTOP, adapters=[FakeAdapter(explodes=True)]))
    assert not [r for r in found.ranked if r.source == affordances.SERVICE]
    assert [r for r in found.ranked if r.source == affordances.WINDOW]


def test_an_adapter_candidate_can_never_take_a_compositor_option_id():
    # the adapter mints the id the compositor's first window already uses
    adapter = FakeAdapter(capabilities=[PLAY], found=[track("w1", "a song called w1")])
    found = harvest(machine(DESKTOP, adapters=[adapter]))
    assert len(ids(found)) == len(set(ids(found)))
    assert found.by_id("w1").handle == "0x1"
    assert "x1" in ids(found)


def test_a_tier_three_capability_can_never_become_an_option():
    forbidden = Capability(id="wipe_it", summary="wipe the disk", tier=3, kind="danger")
    adapter = FakeAdapter(capabilities=[forbidden], found=[track("x", "gone")])
    found = harvest(machine(DESKTOP, adapters=[adapter]))
    assert not [r for r in found.ranked if r.source == affordances.SERVICE]


def test_a_service_label_is_ranked_by_what_the_speaker_said_and_not_by_its_own_words():
    adapter = FakeAdapter(
        capabilities=[PLAY],
        found=[track("a", "click here now urgent"), track("b", "Under Pressure")],
    )
    found = harvest(machine(DESKTOP, heard="play under pressure", adapters=[adapter]))
    services = [r for r in found.ranked if r.source == affordances.SERVICE]
    best = max(services, key=lambda r: r.said)
    assert best.affordance.label == "Under Pressure"
    assert min(services, key=lambda r: r.said).said == 0.0


# --------------------------------------------------------------------------- ranking


def test_the_list_is_ranked_by_what_the_speaker_said():
    found = harvest(machine(DESKTOP, heard="go to spotify"))
    # the Spotify window and the Spotify launcher both score 1.0, and recency breaks the
    # tie towards the window that is already open
    assert [r.affordance.handle for r in found.ranked[:2]] == ["0x3", "spotify"]
    assert found.ranked[0].said == 1.0
    assert found.ranked[2].said < 1.0


def test_the_words_outrank_recency_and_recency_outranks_kind():
    found = harvest(machine(DESKTOP, heard="go to spotify"))
    order = ids(found)
    # Spotify is the least recently used window and still comes first, because it was named
    assert order.index("w3") == 0
    # and among the windows nobody named, the focused one leads
    assert order.index("w1") < order.index("w2")


def test_a_number_the_speaker_said_ranks_its_own_workspace():
    found = harvest(machine(DESKTOP, heard="go to workspace 3"))
    spaces = [r for r in found.ranked if r.source == affordances.WORKSPACE]
    assert spaces[0].affordance.handle == "3"
    assert spaces[0].said > spaces[1].said


def test_the_harvest_is_the_same_twice_over():
    first = harvest(machine(DESKTOP, heard="open chrome"))
    second = harvest(machine(DESKTOP, heard="open chrome"))
    assert ids(first) == ids(second)
    assert [r.said for r in first.ranked] == [r.said for r in second.ranked]


# --------------------------------------------------------------------------- the cut


def test_one_source_cannot_starve_every_other_source():
    crowd = desktop(*(win(f"0x{n}", "kitty", rank=n) for n in range(1, 41)), workspaces=(1,))
    found = harvest(machine(crowd))
    windows = [r for r in found.ranked if r.source == affordances.WINDOW]
    assert len(windows) == affordances.QUOTA[affordances.WINDOW]
    assert [r for r in found.ranked if r.source == affordances.SYSTEM]


def test_the_list_is_cut_to_the_option_cap_and_says_what_it_dropped():
    crowd = desktop(*(win(f"0x{n}", "kitty", rank=n) for n in range(1, 41)), workspaces=(1,))
    found = harvest(machine(crowd), limit=10)
    offered = [r for r in found.ranked if r.source == affordances.WINDOW]
    assert len(found.ranked) == 10
    assert dict(found.dropped)[affordances.WINDOW] == 40 - len(offered)
    assert sum(count for _, count in found.dropped) == found.built - 10
    assert "cut" in found.note()


def test_the_list_is_cut_to_the_token_budget_and_says_so():
    found = harvest(machine(DESKTOP), budget=100)
    assert found.estimated <= 100
    assert len(found.ranked) < found.built
    assert "cut" in found.note()


def test_nothing_is_ever_dropped_silently():
    crowd = desktop(*(win(f"0x{n}", "kitty", rank=n) for n in range(1, 41)), workspaces=(1, 2, 3))
    found = harvest(machine(crowd, heard="go to kitty"), limit=12, budget=200)
    assert found.built == len(found.ranked) + sum(count for _, count in found.dropped)
    assert found.note().startswith(f"{len(found.ranked)} of {found.built} options")


def test_a_list_that_fits_still_says_that_nothing_was_cut():
    found = harvest(machine(DESKTOP))
    assert found.dropped == ()
    assert found.note().endswith("nothing cut")


def test_a_label_longer_than_sixty_characters_is_cut():
    long_name = make_app("verbose", "V" * 120, "a kind that is also very long indeed")
    lexicon = Lexicon((*APPS, long_name))
    found = harvest(machine(DESKTOP, heard="open " + "V" * 120, lexicon=lexicon))
    assert all(len(option.label) <= MAX_LABEL for option in found.offered)


# --------------------------------------------------------------------------- the budget


def test_the_cost_of_an_option_is_what_the_wire_will_carry():
    found = harvest(machine(DESKTOP))
    assert found.estimated == affordances.cost(found.offered)
    assert affordances.cost(()) == 0
    assert affordances.cost(found.offered[:1]) < found.estimated


def test_drift_shrinks_the_budget_when_the_estimator_has_been_running_low():
    honest = tokens.Drift()
    assert affordances.budget_for(honest) == affordances.OPTION_BUDGET
    low = tokens.Drift()
    for _ in range(5):
        low.record(estimated=1000, reported=1500)
    assert affordances.budget_for(low) == affordances.OPTION_BUDGET * 2 // 3
    high = tokens.Drift()
    for _ in range(5):
        high.record(estimated=1000, reported=500)
    # the safe direction never buys a bigger list
    assert affordances.budget_for(high) == affordances.OPTION_BUDGET


def test_three_copies_of_the_list_get_a_third_of_the_budget():
    assert affordances.budget_for(tokens.Drift(), copies=3) == affordances.OPTION_BUDGET // 3
    assert affordances.budget_for(tokens.Drift(), copies=0) == affordances.OPTION_BUDGET
    assert affordances.budget_for(tokens.Drift(), copies=500) == affordances.MIN_BUDGET


def test_a_drifting_estimator_actually_shrinks_a_real_harvest():
    low = tokens.Drift()
    for _ in range(5):
        low.record(estimated=100, reported=1000)
    crowd = desktop(*(win(f"0x{n}", "kitty", rank=n) for n in range(1, 41)), workspaces=(1,))
    assert harvest(machine(crowd, drift=low)).budget == affordances.MIN_BUDGET
    assert harvest(machine(crowd)).budget == affordances.OPTION_BUDGET


# --------------------------------------------------------------------------- the request


def test_an_option_carries_no_handle_no_tier_and_no_address():
    found = harvest(machine(DESKTOP, heard="go to chrome"))
    blob = json.dumps(found.options())
    for option in found.offered:
        assert set(option.as_option()) == {"label", "kind", "trust"}
    for window in DESKTOP.windows:
        assert window.address not in blob
    assert "0x" not in blob


def test_a_chosen_id_finds_its_way_home_without_string_surgery():
    adapter = FakeAdapter(capabilities=[PLAY], found=[track("abc", "Bohemian Rhapsody")])
    found = harvest(machine(DESKTOP, adapters=[adapter]))
    window = found.by_id("w1")
    assert (window.adapter, window.capability, window.handle) == (
        affordances.SELF,
        Intent.FOCUS_WINDOW.value,
        "0x1",
    )
    service = found.by_id("x1")
    assert (service.adapter, service.capability) == ("fake", "play_track")
    assert found.by_id("nothing like this") is None


# --------------------------------------------------------------------------- degrading


def test_a_source_that_throws_costs_only_its_own_options(monkeypatch):
    def broken(_context):
        raise RuntimeError("the lexicon went away")

    monkeypatch.setattr(affordances, "_apps", broken)
    found = harvest(machine(DESKTOP, heard="open chrome"))
    assert not [r for r in found.ranked if r.source == affordances.APP]
    assert [r for r in found.ranked if r.source == affordances.WINDOW]
    assert [r for r in found.ranked if r.source == affordances.SYSTEM]


def test_a_harvest_without_a_lexicon_still_offers_the_desktop():
    found = harvest(machine(DESKTOP, lexicon=None))
    assert [r for r in found.ranked if r.source == affordances.WINDOW]
    assert not [r for r in found.ranked if r.source == affordances.APP]
    # with nothing to vouch for the name, a window is labelled by its own class
    assert found.by_id("w1").trust is Trust.UNTRUSTED


def test_a_harvest_of_nothing_at_all_is_still_a_harvest():
    found = harvest(machine(DesktopState(locked=False), lexicon=None))
    assert len(found.ranked) == 8  # volume and media, which need nothing to be open
    assert found.dropped == ()


# --------------------------------------------------------------------------- the scoring


@pytest.mark.parametrize(
    ("heard", "groups", "expected"),
    [
        ("turn the volume up", (("volume",), ("up", "louder")), 1.0),
        ("turn the volume up", (("mute", "silence"),), 0.0),
        ("go to workspace 3", (("workspace",), ("3",)), 1.0),
        ("go to workspace 3", (("workspace",), ("7",)), 0.5),
        ("", (("volume",),), 0.0),
        ("volume", (), 0.0),
    ],
)
def test_said_measures_the_option_vocabulary_the_speaker_used(heard, groups, expected):
    assert affordances.said(affordances.spoken(heard), groups) == expected


def test_a_short_word_only_counts_when_it_is_exactly_right():
    # "ok" must not answer to "up": below MIN_FUZZY_LETTERS only an exact word counts
    assert affordances.said(("ok",), (("up",),)) == 0.0
    assert affordances.said(("up",), (("up",),)) == 1.0


def test_a_misheard_word_still_names_its_option():
    assert affordances.said(("volum",), (("volume",),)) > 0.0
    assert affordances.said(("banana",), (("volume",),)) == 0.0


def test_claims_asks_how_much_of_the_utterance_a_free_text_label_explains():
    spoken = affordances.spoken("play bohemian rhapsody")
    assert affordances.claims(spoken, "Bohemian Rhapsody") == pytest.approx(2 / 3, abs=1e-3)
    assert affordances.claims(spoken, "Something Else Entirely") == 0.0
    assert affordances.claims((), "Bohemian Rhapsody") == 0.0


def test_a_homoglyph_word_in_a_label_claims_nothing():
    # "clоse" with a Cyrillic o is not the word "close" and may not be scored as one
    assert affordances.claims(("close",), "clоse the tab") == 0.0


# ----------------------------------------------------------------- inside a window


def control(name, role="push button", enabled=True, address="0x2"):
    return controls.Control(
        id=f":1.9:/{name}",
        name=name,
        role=role,
        enabled=enabled,
        bounds=(0, 0, 40, 20),
        window_address=address,
    )


def warm(window, *found):
    """Put a reading into the control cache the way a finished walk does, with no bus.

    Through the real cache and not through a stub of `controls.cached`, because the thing
    worth testing is that the harvest goes in by the door that cannot walk.
    """
    controls._CACHE[window.address] = controls._Cached(
        window.title, window.cls, controls._now(), tuple(found)
    )


@pytest.fixture(autouse=True)
def _cold_controls():
    controls.invalidate()
    yield
    controls.invalidate()


CHROME_WINDOW = win("0x2", "google-chrome", rank=0, title="YouTube", workspace=2)
KITTY_WINDOW = win("0x1", "kitty", rank=1)
INSIDE = desktop(CHROME_WINDOW, KITTY_WINDOW, workspaces=(1, 2), active="0x2")
TYPING_ALLOWED = replace(CFG, safety=Safety(type_allow_classes=("google-chrome",)))


def sourced(found, source) -> list:
    return [r for r in found.ranked if r.source == source]


def test_a_control_the_speaker_named_is_offered_as_a_click_target():
    warm(CHROME_WINDOW, control("Reply"), control("Star"))
    found = harvest(machine(INSIDE, heard="click reply"))
    offered = sourced(found, affordances.CONTROL)
    assert [r.affordance.label for r in offered] == ["Reply, push button"]
    assert offered[0].affordance.capability == Intent.CLICK_CONTROL.value
    assert offered[0].affordance.trust is Trust.UNTRUSTED
    assert offered[0].said == 1.0


def test_a_control_the_speaker_did_not_name_is_never_offered():
    warm(CHROME_WINDOW, control("Reply"), control("Star"))
    # the page wrote both names and the speaker used neither: a page may not supply the
    # evidence that selects its own button (controls.best_match)
    assert sourced(harvest(machine(INSIDE, heard="go to chrome")), affordances.CONTROL) == []


def test_a_cold_control_cache_offers_nothing_and_is_not_an_error():
    found = harvest(machine(INSIDE, heard="click reply"))
    assert sourced(found, affordances.CONTROL) == []
    assert found.ranked  # the rest of the desktop is unharmed
    assert "cut" in found.note()


def test_a_window_that_publishes_nothing_is_not_an_error_either():
    warm(CHROME_WINDOW)  # the walk ran and found nothing, which is what Chrome does
    assert sourced(harvest(machine(INSIDE, heard="click reply")), affordances.CONTROL) == []


def test_the_harvest_never_walks_the_accessibility_tree(monkeypatch):
    monkeypatch.setattr(
        controls, "controls_for", _boom("a walk is 3.8 to 16.2 s and the harvest has 250 ms")
    )
    monkeypatch.setattr(controls, "Atspi", _boom("no bus is opened from inside a harvest"))
    warm(CHROME_WINDOW, control("Reply"))
    found = harvest(machine(INSIDE, heard="click reply"))
    assert [r.affordance.label for r in sourced(found, affordances.CONTROL)] == [
        "Reply, push button"
    ]


def test_a_control_name_that_reads_destructive_raises_itself_out_of_the_list():
    warm(CHROME_WINDOW, control("Reply"), control("Delete"))
    found = harvest(machine(INSIDE, heard="click delete and reply"))
    labels = [r.affordance.label for r in sourced(found, affordances.CONTROL)]
    # `controls.click_tier` reads an untrusted name only to RAISE what a click costs, and
    # a tier 2 act cannot be authorized by picking an id out of a list
    assert labels == ["Reply, push button"]
    assert (
        tiers.tier(Intent.CLICK_CONTROL, Action(Intent.CLICK_CONTROL, verb="Delete"), INSIDE, CFG)
        == 2
    )


def test_a_homoglyph_in_a_control_name_raises_it_out_of_the_list():
    # "Rерly" carries two Cyrillic letters: the same pixels, a different string. The word
    # itself can never match anything (`controls.score` drops a mixed-script token), so
    # the case that matters is the one where it rides along beside an honest word.
    warm(CHROME_WINDOW, control("Rерly Star"))
    assert sourced(harvest(machine(INSIDE, heard="click star")), affordances.CONTROL) == []
    controls.invalidate()
    warm(CHROME_WINDOW, control("Reply Star"))
    offered = sourced(harvest(machine(INSIDE, heard="click star")), affordances.CONTROL)
    assert [r.affordance.label for r in offered] == ["Reply Star, push button"]


def test_a_control_name_never_becomes_an_option_id():
    warm(CHROME_WINDOW, control("Reply"))
    found = harvest(machine(INSIDE, heard="click reply"))
    option = sourced(found, affordances.CONTROL)[0].affordance
    assert option.id == "c1"
    # the label is on the wire, which is what it is for; the KEY is never application text
    assert "Reply" in json.dumps(found.options())
    assert "Reply" not in "".join(found.options().keys())


def test_a_control_option_carries_the_window_it_was_read_from():
    warm(CHROME_WINDOW, control("Reply"))
    option = sourced(harvest(machine(INSIDE, heard="click reply")), affordances.CONTROL)[0]
    assert affordances.target(option.affordance.handle) == ("0x2", "Reply")
    assert INSIDE.by_address("0x2") is CHROME_WINDOW


def test_a_control_that_is_greyed_out_is_not_offered():
    warm(CHROME_WINDOW, control("Reply", enabled=False))
    assert sourced(harvest(machine(INSIDE, heard="click reply")), affordances.CONTROL) == []


def test_a_window_cannot_offer_the_controls_of_a_window_it_does_not_own():
    warm(KITTY_WINDOW, control("Reply", address="0x1"))
    # the reading belongs to the window that is not focused, and nothing reads it
    assert sourced(harvest(machine(INSIDE, heard="click reply")), affordances.CONTROL) == []


def test_a_recipe_is_offered_only_when_the_words_reach_it():
    quiet = harvest(machine(INSIDE, heard="go to chrome"))
    assert sourced(quiet, affordances.RECIPE) == []
    asked = harvest(machine(INSIDE, heard="open a new tab"))
    assert [r.affordance.label for r in sourced(asked, affordances.RECIPE)] == ["open a new tab"]


def test_a_recipe_that_types_is_offered_at_the_typing_tier_when_it_was_asked_for():
    heard = "search youtube for dino crisis"
    # typing is tier 2 by default, so the search is offered as a countdown: the speaker
    # reached for it in their own words, which is what a tier 2 option needs
    default = harvest(machine(INSIDE, heard=heard))
    labels = {r.affordance.label: r.affordance.tier for r in sourced(default, affordances.RECIPE)}
    assert labels["search YouTube in a new tab"] == 2
    allowed = harvest(
        Machine(
            state=INSIDE, heard=heard, lexicon=LEXICON, cfg=TYPING_ALLOWED, drift=tokens.Drift()
        )
    )
    # a recipe that types is exactly as dangerous as typing (recipes.tier_for), so this is
    # the typing allowlist deciding, not a second rule here
    tiers_now = {
        r.affordance.label: r.affordance.tier for r in sourced(allowed, affordances.RECIPE)
    }
    assert tiers_now["search YouTube in a new tab"] == 1


def test_a_recipe_that_types_is_not_offered_when_nothing_was_reached_for():
    found = harvest(machine(INSIDE, heard="youtube dino crisis"))
    assert all(r.affordance.tier <= 1 for r in sourced(found, affordances.RECIPE))


def test_a_terminal_is_offered_no_recipe_and_no_gesture():
    terminal = desktop(KITTY_WINDOW, CHROME_WINDOW, workspaces=(1, 2), active="0x1")
    found = harvest(machine(terminal, heard="scroll down and go back"))
    assert sourced(found, affordances.GESTURE) == []
    # the browser beside it still offers its own, pinned to its own address
    handles = {r.affordance.handle.split()[0] for r in sourced(found, affordances.RECIPE)}
    assert handles == {"0x2"}


def test_a_recipe_for_another_window_names_it_and_the_focused_one_is_not_named():
    found = harvest(machine(INSIDE, heard="zoom in"))
    labels = [r.affordance.label for r in sourced(found, affordances.RECIPE)]
    assert labels[0] == "make this page bigger"
    assert all(", in " not in label for label in labels)
    elsewhere = desktop(
        win("0x4", "Spotify", rank=0), CHROME_WINDOW, workspaces=(1, 2), active="0x4"
    )
    found = harvest(machine(elsewhere, heard="zoom in"))
    labels = [r.affordance.label for r in sourced(found, affordances.RECIPE)]
    assert "make this page bigger, in Google Chrome" in labels


def test_a_recipe_the_gesture_source_already_offers_is_not_offered_twice():
    found = harvest(machine(INSIDE, heard="reload this"))
    assert [r.affordance.label for r in sourced(found, affordances.GESTURE)] == ["reload this page"]
    # `recipes.reload` does the same thing to the same window, through one more step
    assert sourced(found, affordances.RECIPE) == []


def test_a_recipe_carries_its_own_name_as_the_handle():
    found = harvest(machine(INSIDE, heard="open a new tab"))
    option = sourced(found, affordances.RECIPE)[0].affordance
    assert affordances.target(option.handle) == ("0x2", "new_tab")
    assert option.capability == Intent.RUN_RECIPE.value
    assert option.trust is Trust.TRUSTED


def test_the_gestures_the_words_reach_are_offered_for_the_focused_window():
    found = harvest(machine(INSIDE, heard="scroll down"))
    offered = sourced(found, affordances.GESTURE)
    # the other direction is offered under it, at half the score, exactly as volume up and
    # volume down both are: the ranking says which one was asked for
    assert offered[0].affordance.label == "scroll down in this window"
    assert offered[0].said > offered[1].said
    assert affordances.target(offered[0].affordance.handle) == ("0x2", "down")
    assert offered[0].affordance.capability == Intent.SCROLL.value
    assert offered[0].affordance.tier == 0


def test_a_chord_handle_is_the_spelling_inapp_expects():
    found = harvest(machine(INSIDE, heard="go to the next tab"))
    offered = sourced(found, affordances.GESTURE)
    _, keys = affordances.target(offered[0].affordance.handle)
    assert keys == "ctrl+Tab"
    assert inapp.canonical(keys) == keys
    assert inapp.press(keys, window="0x2").tier == offered[0].affordance.tier


def test_every_gesture_handle_is_one_inapp_can_build():
    for intent, payload, _summary, _groups in affordances.GESTURES:
        if intent is Intent.SCROLL:
            assert inapp.scroll(Direction(payload), window="0x2").kind == inapp.SCROLL
        else:
            assert inapp.canonical(payload) == payload


def test_a_chord_that_does_more_than_move_a_view_is_refused_where_typing_is_refused():
    # an application no trusted .desktop file claims: `tiers.typing_refusal` turns keys
    # off there, and `inapp.refusal` asks it rather than deciding for itself. Page Down is
    # the keyboard's version of the wheel and survives that, which is `inapp.refusal`'s
    # own distinction and not a second one here
    unknown = win("0x9", "homebrew", rank=0)
    state = desktop(unknown, workspaces=(1,), active="0x9")
    found = harvest(machine(state, heard="scroll down a page and go back"))
    labels = {r.affordance.label for r in sourced(found, affordances.GESTURE)}
    assert "scroll down in this window" in labels
    assert "page down in this window" in labels
    assert "go back a page" not in labels  # alt+Left is tier 1, and keys are off here
    known = harvest(machine(INSIDE, heard="scroll down a page and go back"))
    assert "go back a page" in {r.affordance.label for r in sourced(known, affordances.GESTURE)}


def test_a_launcher_on_screen_takes_every_gesture_out_of_the_list():
    grabbed = replace(INSIDE, layers=(Layer("rofi", "DP-1", 3),))
    found = harvest(machine(grabbed, heard="scroll down and go back a page"))
    assert sourced(found, affordances.GESTURE) == []


def test_an_in_app_option_takes_its_tier_from_the_module_that_owns_the_gesture():
    warm(CHROME_WINDOW, control("Reply"))
    found = harvest(machine(INSIDE, heard="click reply, scroll down, reload and zoom in"))
    for entry in found.ranked:
        option = entry.affordance
        if entry.source not in (affordances.GESTURE, affordances.RECIPE, affordances.CONTROL):
            continue
        _, inside = affordances.target(option.handle)
        intent = Intent(option.capability)
        if intent is Intent.SCROLL:
            action = Action(intent, window=CHROME_WINDOW, direction=Direction(inside))
        else:
            action = Action(intent, window=CHROME_WINDOW, verb=inside)
        assert option.tier == tiers.tier(intent, action, INSIDE, CFG), option.id


def test_no_control_or_gesture_is_ever_offered_above_tier_one():
    warm(CHROME_WINDOW, control("Reply"), control("Delete"), control("Send"))
    heard = "click delete, send, save, close the tab and go to github dot com"
    for cfg in (CFG, TYPING_ALLOWED):
        found = harvest(
            Machine(state=INSIDE, heard=heard, lexicon=LEXICON, cfg=cfg, drift=tokens.Drift())
        )
        inside = [r for r in found.ranked if r.source in (affordances.GESTURE, affordances.CONTROL)]
        # a page can silence its own button by naming it "Delete", and never add one
        assert all(r.affordance.tier <= affordances.MAX_INAPP_TIER for r in inside)


def test_a_tier_two_recipe_is_offered_only_for_the_verb_that_asks_for_it():
    heard = "click delete, send, save, close the tab and go to github dot com"
    found = harvest(machine(INSIDE, heard=heard))
    high = {r.affordance.label for r in sourced(found, affordances.RECIPE) if r.affordance.tier > 1}
    # "close" asked for closing the tab and "go" for typing an address, and nothing else
    assert "close this tab" in high
    assert "leave this page and go to a site in the tab you are on" in high
    quiet = harvest(machine(INSIDE, heard="open a new tab"))
    assert [r.affordance.label for r in sourced(quiet, affordances.RECIPE)] == ["open a new tab"]


def test_nothing_reaches_inside_a_window_unless_the_words_do():
    warm(CHROME_WINDOW, control("Reply"), control("Star"))
    found = harvest(machine(INSIDE, heard="go to chrome"))
    assert all(
        r.source not in (affordances.GESTURE, affordances.RECIPE, affordances.CONTROL)
        for r in found.ranked
    )


def test_an_in_app_option_never_costs_a_harvest_that_asked_for_nothing():
    warm(CHROME_WINDOW, *[control(f"Button {n}") for n in range(60)])
    quiet = harvest(machine(INSIDE, heard="go to workspace 2"))
    assert quiet.built == harvest(machine(INSIDE, heard="go to workspace 2")).built
    assert all(r.source != affordances.CONTROL for r in quiet.ranked)


# --------------------------------------------------------------------------- closing, and again

MUSIC_WINDOW = win("0x3", "Spotify", rank=2, workspace=3)
WITH_MUSIC = desktop(CHROME_WINDOW, KITTY_WINDOW, MUSIC_WINDOW, workspaces=(1, 2, 3), active="0x2")


def test_closing_is_offered_only_when_a_closing_verb_was_said():
    asked = harvest(machine(WITH_MUSIC, heard="could you close the music player"))
    closing = sourced(asked, affordances.CLOSE)
    assert {r.affordance.handle for r in closing} == {"0x1", "0x2", "0x3"}
    assert all(r.affordance.tier == 2 for r in closing)
    assert any("Spotify" in r.affordance.label for r in closing)
    assert (
        sourced(harvest(machine(WITH_MUSIC, heard="focus the music player")), affordances.CLOSE)
        == []
    )


def test_the_last_gesture_is_offered_again_whatever_was_said():
    previous = affordances.Previous(intent="scroll", said="scroll down", address="0x2")
    found = harvest(replace(machine(INSIDE, heard="a bit more"), previous=previous))
    gestures = sourced(found, affordances.GESTURE)
    assert gestures, "a bit more must have something to pick"
    assert gestures[0].affordance.label == "scroll down in this window again"


def test_the_last_gesture_is_not_offered_again_on_another_window():
    previous = affordances.Previous(intent="scroll", said="scroll down", address="0x9")
    found = harvest(replace(machine(INSIDE, heard="a bit more"), previous=previous))
    assert sourced(found, affordances.GESTURE) == []


def test_bringing_a_window_here_is_offered_when_here_was_said():
    found = harvest(machine(WITH_MUSIC, heard="move my browser here"))
    here = [r for r in found.ranked if r.affordance.capability == "move_to_workspace"]
    # every window that is not already on the workspace in front
    assert {r.affordance.handle for r in here} == {"0x1", "0x3"}
    assert all(r.affordance.label.startswith("bring ") for r in here)
    quiet = harvest(machine(WITH_MUSIC, heard="go to my browser"))
    assert not [r for r in quiet.ranked if r.affordance.capability == "move_to_workspace"]


def test_what_the_last_command_touched_comes_first_and_says_so():
    previous = affordances.Previous(intent="launch_app", said="open spotify", app="Spotify")
    found = harvest(replace(machine(WITH_MUSIC, heard="focus on it"), previous=previous))
    windows = sourced(found, affordances.WINDOW)
    assert windows[0].affordance.handle == "0x3"
    assert "just" in windows[0].affordance.label
