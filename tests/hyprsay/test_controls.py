"""Clicking a control by name. Every test runs against a FAKE tree.

A unit test may never touch the accessibility bus, and on the machine this was written
on the bus is broken anyway, which is the case `probe` exists for: those tests fake the
three ways it breaks rather than waiting for one of them to happen.
"""

import threading
from dataclasses import replace

import pytest

from hyprsay import controls
from hyprsay.model import Window
from hypruse import a11y

WINDOW = Window(
    address="0x1",
    cls="firefox",
    initial_class="firefox",
    title="Inbox",
    workspace_id=1,
    workspace_name="1",
    monitor=0,
    pid=1234,
    at=(100, 200),
    size=(800, 600),
)


def element(name, role="push button", at=(10, 10), size=(80, 24), clickable=True):
    """One node in the shape `hypruse.a11y.find_elements` returns."""
    return {
        "role": role,
        "name": name,
        "extent": (*at, *size),
        "clickable": clickable,
        "svc": ":1.9",
        "path": "/org/a11y/atspi/accessible/" + name.replace(" ", "_"),
    }


def control(name, **kw):
    return controls.Control(
        id=kw.pop("id", f":1.9:/{name}"),
        name=name,
        role=kw.pop("role", "push button"),
        enabled=kw.pop("enabled", True),
        bounds=kw.pop("bounds", (110, 210, 80, 24)),
        window_address=kw.pop("window_address", "0x1"),
        **kw,
    )


class FakeTree:
    """A tree that answers from a list, counts its walks, and can be held open."""

    def __init__(self, elements=(), usable=True, why="", gate=None, error=None):
        self.elements = list(elements)
        self.usable = usable
        self.why = why or ("ready" if usable else "the bus is unreachable; run something")
        self.gate = gate
        self.error = error
        self.reads = 0

    def probe(self):
        return self.usable, self.why

    def read(self, window, limit):
        self.reads += 1
        if self.gate is not None:
            self.gate.wait(5)
        if self.error is not None:
            raise self.error
        return [dict(e) for e in self.elements[:limit]]


def raising(exc):
    def boom(*_args, **_kw):
        raise exc

    return boom


@pytest.fixture(autouse=True)
def _empty_cache():
    controls.invalidate()
    controls._PENDING.clear()
    yield
    controls.invalidate()
    controls._PENDING.clear()


# --------------------------------------------------------------------------- the probe


def test_probe_names_the_repair_command_when_no_accessibility_bus_is_running(monkeypatch):
    monkeypatch.setattr(controls.a11y, "bus_address", raising(a11y.A11yError("no address")))
    usable, why = controls.probe()
    assert usable is False
    assert "no accessibility bus is running" in why
    assert "systemctl --user restart at-spi-dbus-bus" in why


def test_probe_tells_an_orphaned_registry_apart_from_a_missing_bus(monkeypatch):
    monkeypatch.setattr(controls.a11y, "bus_address", lambda: "unix:path=/run/user/1000/bus_1")
    monkeypatch.setattr(
        controls.a11y, "apps", raising(a11y.A11yError("Could not activate remote peer"))
    )
    usable, why = controls.probe()
    assert usable is False
    assert "registry does not answer" in why
    assert "bus_1" in why
    assert "systemctl --user restart at-spi-dbus-bus" in why


def test_probe_tells_an_empty_registry_apart_from_a_broken_one(monkeypatch):
    monkeypatch.setattr(controls.a11y, "bus_address", lambda: "unix:path=/run/user/1000/bus_1")
    monkeypatch.setattr(controls.a11y, "apps", lambda bus: [])
    usable, why = controls.probe()
    assert usable is False
    assert "registry is empty" in why


def test_probe_is_usable_once_the_registry_lists_applications(monkeypatch):
    monkeypatch.setattr(controls.a11y, "bus_address", lambda: "unix:path=/run/user/1000/bus_1")
    monkeypatch.setattr(controls.a11y, "apps", lambda bus: [(":1.2", "/root"), (":1.3", "/root")])
    usable, why = controls.probe()
    assert usable is True
    assert "2 applications" in why


def test_probe_never_raises_whatever_the_bus_does(monkeypatch):
    monkeypatch.setattr(controls.a11y, "bus_address", raising(RuntimeError("busctl exploded")))
    usable, why = controls.probe()
    assert usable is False
    assert why


def test_a_broken_bus_is_reported_before_any_tree_is_walked():
    tree = FakeTree([element("Send")], usable=False, why="the registry is empty; run repair")
    with pytest.raises(controls.ControlsError) as err:
        controls.controls_for(WINDOW, reader=tree)
    assert "run repair" in str(err.value)
    assert tree.reads == 0


def test_a_bus_error_during_the_walk_becomes_one_plain_sentence(monkeypatch):
    monkeypatch.setattr(
        controls.a11y,
        "connect",
        raising(a11y.A11yError("busctl call failed: Connection refused. Use screenshot instead")),
    )
    with pytest.raises(controls.ControlsError) as err:
        controls.Atspi().read(WINDOW, 60)
    said = str(err.value)
    assert said.startswith("the controls of firefox could not be read: busctl call failed")
    assert "screenshot" not in said  # the tail of a hypruse message is advice for an agent


# --------------------------------------------------------------------------- reading


def test_a_control_carries_global_bounds_and_the_window_it_belongs_to():
    tree = FakeTree([element("Send")])
    found = controls.controls_for(WINDOW, reader=tree)
    assert [c.name for c in found] == ["Send"]
    assert found[0].bounds == (110, 210, 80, 24)
    assert found[0].window_address == "0x1"
    assert found[0].role == "push button"


def test_a_control_is_never_trusted_even_when_it_is_constructed_that_way():
    assert control("Send").trusted is False
    assert control("Send", trusted=True).trusted is False
    assert controls.controls_for(WINDOW, reader=FakeTree([element("Send")]))[0].trusted is False


def test_a_widget_the_toolkit_placed_outside_the_window_is_not_offered():
    tree = FakeTree([element("Send"), element("Hidden tab page", at=(9000, 9000))])
    found = controls.controls_for(WINDOW, reader=tree)
    assert [c.name for c in found] == ["Send"]


def test_a_name_written_by_the_application_is_stripped_of_control_characters():
    tree = FakeTree([element("Se​nd\nnow")])
    assert controls.controls_for(WINDOW, reader=tree)[0].name == "Send now"


def test_the_limit_caps_what_one_walk_returns():
    tree = FakeTree([element(f"Button {i}") for i in range(10)])
    assert len(controls.controls_for(WINDOW, limit=3, reader=tree)) == 3


def test_a_window_that_publishes_nothing_is_not_reported_as_having_no_such_button():
    terminal = replace(WINDOW, cls="kitty", initial_class="kitty")
    with pytest.raises(controls.ControlsError) as err:
        controls.controls_for(terminal, reader=FakeTree())
    assert "publishes no controls" in str(err.value)
    assert controls.ACCESSIBILITY_FLAG not in str(err.value)


def test_a_window_that_publishes_nothing_is_only_asked_once():
    tree = FakeTree()
    for _ in range(3):
        with pytest.raises(controls.ControlsError):
            controls.controls_for(WINDOW, reader=tree)
    assert tree.reads == 1


def test_a_walk_that_failed_is_not_remembered_as_a_window_without_buttons():
    tree = FakeTree([element("Send")], error=a11y.A11yError("the bus went away"))
    with pytest.raises(controls.ControlsError) as err:
        controls.controls_for(WINDOW, reader=tree)
    assert "the bus went away" in str(err.value)
    tree.error = None
    assert [c.name for c in controls.controls_for(WINDOW, reader=tree)] == ["Send"]
    assert tree.reads == 2


def test_chrome_with_an_empty_tree_is_named_for_the_flag_it_is_missing():
    chrome = replace(WINDOW, cls="zapzap", initial_class="zapzap")
    with pytest.raises(controls.ControlsError) as err:
        controls.controls_for(chrome, reader=FakeTree())
    said = str(err.value)
    assert controls.ACCESSIBILITY_FLAG in said
    assert "no such button" in said


# --------------------------------------------------------------------------- the cache


def test_a_second_utterance_about_the_same_window_does_not_walk_the_tree_again():
    tree = FakeTree([element("Send")])
    first = controls.controls_for(WINDOW, reader=tree)
    second = controls.controls_for(WINDOW, reader=tree)
    assert first == second
    assert tree.reads == 1


def test_the_cache_expires_so_a_window_that_moved_on_is_read_again(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(controls, "_now", lambda: clock[0])
    tree = FakeTree([element("Send")])
    controls.controls_for(WINDOW, reader=tree)
    clock[0] += controls.CACHE_TTL_S / 2
    controls.controls_for(WINDOW, reader=tree)
    assert tree.reads == 1
    clock[0] += controls.CACHE_TTL_S + 0.1
    controls.controls_for(WINDOW, reader=tree)
    assert tree.reads == 2


def test_a_title_change_invalidates_the_cache_because_every_button_may_have_changed():
    tree = FakeTree([element("Send")])
    controls.controls_for(WINDOW, reader=tree)
    controls.controls_for(replace(WINDOW, title="Compose"), reader=tree)
    assert tree.reads == 2


def test_the_same_address_with_a_new_class_is_a_different_window():
    tree = FakeTree([element("Send")])
    controls.controls_for(WINDOW, reader=tree)
    controls.controls_for(replace(WINDOW, cls="kitty"), reader=tree)
    assert tree.reads == 2


def test_invalidate_drops_one_window_and_leaves_the_others():
    other = replace(WINDOW, address="0x2")
    tree = FakeTree([element("Send")])
    controls.controls_for(WINDOW, reader=tree)
    controls.controls_for(other, reader=tree)
    assert tree.reads == 2
    controls.invalidate("0x1")
    controls.controls_for(other, reader=tree)
    assert tree.reads == 2
    controls.controls_for(WINDOW, reader=tree)
    assert tree.reads == 3


def test_the_cache_does_not_grow_without_bound_in_a_daemon_that_runs_for_weeks():
    tree = FakeTree([element("Send")])
    for i in range(controls.MAX_CACHED_WINDOWS + 4):
        controls.controls_for(replace(WINDOW, address=f"0x{i:x}"), reader=tree)
    assert len(controls._CACHE) == controls.MAX_CACHED_WINDOWS


def test_a_walk_that_runs_past_the_budget_says_so_and_keeps_going():
    gate = threading.Event()
    tree = FakeTree([element("Send")], gate=gate)
    with pytest.raises(controls.ControlsError) as err:
        controls.controls_for(WINDOW, timeout=0.01, reader=tree)
    assert "taking longer" in str(err.value)
    gate.set()
    found = controls.controls_for(WINDOW, timeout=5.0, reader=tree)
    assert [c.name for c in found] == ["Send"]
    assert tree.reads == 1


# --------------------------------------------------------------------------- matching


def test_the_exact_name_outranks_a_longer_name_that_contains_it():
    ranked = controls.best_match("click the send button", [control("Send later"), control("Send")])
    assert [c.name for c, _ in ranked] == ["Send", "Send later"]
    assert ranked[0][1] > ranked[1][1]


def test_more_of_the_phrase_matched_beats_less_of_it():
    pool = [control("Send"), control("Send message")]
    ranked = controls.best_match("click send message", pool)
    assert [c.name for c, _ in ranked] == ["Send message", "Send"]


def test_a_misheard_word_still_finds_the_control():
    ranked = controls.best_match("click kompose", [control("Compose"), control("Archive")])
    assert [c.name for c, _ in ranked] == ["Compose"]


def test_a_control_the_speaker_did_not_name_is_never_a_click_target():
    pool = [control("Delete"), control("Cancel"), control("Send")]
    assert [c.name for c, _ in controls.best_match("click send", pool)] == ["Send"]


def test_a_button_that_names_itself_after_the_click_verb_is_still_not_a_target():
    assert controls.best_match("click the button", [control("Click the button")]) == []


def test_nothing_matches_when_the_speaker_named_nothing():
    assert controls.best_match("click it", [control("Send")]) == []
    assert controls.best_match("", [control("Send")]) == []


def test_a_word_that_only_sounds_a_little_like_the_name_is_not_a_match():
    assert controls.best_match("click send", [control("Spend")]) == []


def test_a_homoglyph_in_a_control_name_never_answers_to_a_spoken_word():
    cyrillic_c = control("сlose")  # looks exactly like "close" on screen
    assert controls.best_match("click close", [cyrillic_c]) == []


def test_the_ranking_is_the_same_whatever_order_the_walk_returned_them_in():
    first, second = control("Save", id=":1.9:/a"), control("Save", id=":1.9:/b")
    one = controls.best_match("click save", [first, second])
    other = controls.best_match("click save", [second, first])
    assert [c.id for c, _ in one] == [c.id for c, _ in other] == [":1.9:/a", ":1.9:/b"]


def test_matching_is_read_only_and_leaves_the_controls_alone():
    pool = [control("Send")]
    controls.best_match("click send", pool)
    assert pool[0].trusted is False
    assert pool[0].name == "Send"


# --------------------------------------------------------------------------- the tier


@pytest.mark.parametrize("word", sorted(controls.DESTRUCTIVE_CONTROL_WORDS))
def test_a_destructive_sounding_name_is_tier_2_whatever_the_control_really_does(word):
    assert controls.click_tier(control(word.title())) == 2


def test_an_ordinary_control_is_tier_1_because_no_click_is_free_to_reverse():
    assert controls.click_tier(control("Reload")) == 1
    assert controls.click_tier(control("Next track")) == 1


def test_close_alone_stays_tier_1_but_closing_an_account_does_not():
    assert controls.click_tier(control("Close")) == 1
    assert controls.click_tier(control("Close account")) == 2


def test_a_destructive_word_anywhere_in_the_name_is_enough():
    assert controls.click_tier(control("Delete everything, without asking")) == 2
    assert controls.click_tier(control("Buy now")) == 2


def test_the_plural_of_a_destructive_word_counts_too():
    assert controls.click_tier(control("Uninstalls")) == 2


def test_an_untrusted_name_may_raise_the_tier_and_never_lower_it():
    hostile = control("Delete")
    assert hostile.trusted is False
    assert controls.click_tier(hostile) == 2


# --------------------------------------------------------------------------- clicking


def test_a_click_goes_through_the_inherited_click_ui_pinned_to_the_window(monkeypatch):
    seen = {}

    def click_ui(**kw):
        seen.update(kw)
        return "clicked push button 'Send' at (150, 222) in firefox"

    monkeypatch.setattr(controls.server, "click_ui", click_ui)
    assert controls.click(control("Send"), WINDOW) == "Clicked 'Send' in firefox."
    assert seen == {"name": "Send", "window": "0x1"}


def test_a_control_from_another_window_is_never_clicked(monkeypatch):
    monkeypatch.setattr(controls.server, "click_ui", raising(AssertionError("must not run")))
    with pytest.raises(controls.ControlsError) as err:
        controls.click(control("Send", window_address="0xdead"), WINDOW)
    assert "different window" in str(err.value)


def test_a_nameless_control_is_never_clicked(monkeypatch):
    monkeypatch.setattr(controls.server, "click_ui", raising(AssertionError("must not run")))
    with pytest.raises(controls.ControlsError) as err:
        controls.click(control("  "), WINDOW)
    assert "nothing you said can have named it" in str(err.value)


def test_a_control_that_is_greyed_out_is_not_clicked(monkeypatch):
    monkeypatch.setattr(controls.server, "click_ui", raising(AssertionError("must not run")))
    with pytest.raises(controls.ControlsError) as err:
        controls.click(control("Send", enabled=False), WINDOW)
    assert "not active" in str(err.value)


def test_an_ambiguous_name_is_refused_rather_than_guessed(monkeypatch):
    monkeypatch.setattr(controls.server, "click_ui", lambda **kw: ["candidate", "candidate"])
    with pytest.raises(controls.ControlsError) as err:
        controls.click(control("Save"), WINDOW)
    assert "more than one control" in str(err.value)


def test_a_dry_run_comes_back_word_for_word(monkeypatch):
    said = "DRY RUN, nothing was delivered: would click push button 'Send' at (150, 222)"
    monkeypatch.setattr(controls.server, "click_ui", lambda **kw: said)
    assert controls.click(control("Send"), WINDOW) == said


def test_a_fall_back_to_vision_note_is_never_reported_as_a_click(monkeypatch):
    note = "firefox exposes no accessibility tree; use screenshot + zoom instead"
    monkeypatch.setattr(controls.server, "click_ui", lambda **kw: note)
    with pytest.raises(controls.ControlsError) as err:
        controls.click(control("Send"), WINDOW)
    assert "could not be clicked" in str(err.value)
