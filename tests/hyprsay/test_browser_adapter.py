"""The browser adapter against a fake browser: what it offers, and what choosing one does."""

from __future__ import annotations

import pytest

from hyprsay.adapters import base, browser
from hyprsay.adapters.base import AdapterError, Context
from hyprsay.model import DesktopState, Window
from hypruse.browser.client import BrowserError

CHROME = Window("0x2", "google-chrome", "google-chrome", "YouTube", 1, "1", 0, focus_rank=0)
KITTY = Window("0x1", "kitty", "kitty", "zsh", 1, "1", 0, focus_rank=1)
IN_FRONT = DesktopState(windows=(CHROME, KITTY), active_address="0x2", locked=False)
BEHIND = DesktopState(windows=(CHROME, KITTY), active_address="0x1", locked=False)

TABS = [
    {"id": 7, "window_id": 1, "index": 0, "active": True, "audible": False,
     "title": "linus tech tips - YouTube", "url": "https://www.youtube.com/results?q=ltt"},
    {"id": 3, "window_id": 1, "index": 1, "active": False, "audible": True,
     "title": "Spotify - Web Player", "url": "https://open.spotify.com/"},
]  # fmt: skip
SITES = [{"url": "https://www.youtube.com/", "host": "youtube.com", "title": "YouTube"}]


def element(ident, name, kind="link", role="link", group=None, index=0, size=0):
    found = {"id": ident, "role": role, "name": name, "kind": kind, "rect": [0, 0, 10, 10],
             "in_view": True}  # fmt: skip
    if group:
        found["list"] = {"group": group, "index": index, "size": size}
    return found


RESULTS = {
    "snapshot_id": "s1",
    "tab_id": 7,
    "url": "https://www.youtube.com/results?q=ltt",
    "title": "linus tech tips - YouTube",
    "elements": [
        element("e1", "Search", kind="search", role="combobox"),
        element("e2", "I built a PC for 10 years", group="g1", index=1, size=3),
        element("e3", "The new GPU is here", group="g1", index=2, size=3),
        element("e4", "Why we quit", group="g1", index=3, size=3),
        element("e5", "Home", role="link"),
    ],
}


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.snapshots = [RESULTS]
        self.stale_once = False

    def call(self, op, args=None, *, timeout=None):
        """The one generic call the adapter makes when it wants its own timeout."""
        args = args or {}
        if op == "tabs.list":
            return {"tabs": self.tabs()}
        if op == "sites.top":
            return {"sites": self.top_sites()}
        if op == "page.snapshot":
            return self.snapshot(args.get("tab_id"), settle_ms=args.get("settle_ms", 0))
        raise AssertionError(f"unexpected call {op}")

    def tabs(self):
        return TABS

    def top_sites(self):
        return SITES

    def snapshot(self, tab_id=None, *, max=120, settle_ms=0):
        self.calls.append(("snapshot", tab_id, settle_ms))
        return self.snapshots[-1]

    def activate(self, tab_id):
        self.calls.append(("activate", tab_id))

    def open(self, url, where="new"):
        self.calls.append(("open", url, where))
        return 9

    def close_tab(self, tab_id):
        self.calls.append(("close", tab_id))

    def history(self, tab_id, direction):
        self.calls.append(("history", tab_id, direction))

    def scroll(self, direction, tab_id=None):
        self.calls.append(("scroll", direction, tab_id))
        return {}

    def click(self, snapshot_id, element_id):
        if self.stale_once:
            self.stale_once = False
            raise BrowserError("stale_snapshot", "the page moved on")
        self.calls.append(("click", snapshot_id, element_id))

    def type(self, snapshot_id, element_id, text, *, submit=False):
        self.calls.append(("type", snapshot_id, element_id, text, submit))


@pytest.fixture
def adapter(monkeypatch, tmp_path):
    monkeypatch.setattr(browser, "FIND_POLL_S", 0.0)
    (tmp_path / "hypruse").mkdir()
    (tmp_path / "hypruse" / "browser.sock").touch()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    return browser.Browser(client=FakeClient())


def offered(adapter, state, capability):
    return base.candidates(adapter, browser.BY_ID[capability], {}, Context(state=state))


def by_label(options, text):
    return next(o for o in options if text in o.label)


# --------------------------------------------------------------------------- serving


def test_it_serves_when_a_browser_window_exists_and_the_host_is_listening(adapter, tmp_path):
    assert adapter.can_serve(Context(state=BEHIND))
    (tmp_path / "hypruse" / "browser.sock").unlink()
    assert not adapter.can_serve(Context(state=BEHIND))


def test_it_does_not_serve_a_desktop_with_no_browser(adapter):
    only_kitty = DesktopState(windows=(KITTY,), active_address="0x1", locked=False)
    assert not adapter.can_serve(Context(state=only_kitty))


def test_what_is_on_the_page_is_offered_only_with_the_browser_in_front(adapter):
    assert set(adapter.capabilities(Context(state=BEHIND))) == set(browser.ANYWHERE)
    assert browser.CLICK in adapter.capabilities(Context(state=IN_FRONT))


# --------------------------------------------------------------------------- the options


def test_every_tab_is_something_to_switch_to_named_by_its_site(adapter):
    tabs = offered(adapter, BEHIND, "browser.tab")
    assert [o.label for o in tabs] == [
        "switch to the youtube tab (youtube.com)",
        "switch to the spotify tab (open.spotify.com)",
    ]
    assert all(o.trust is base.Trust.UNTRUSTED for o in tabs)


def test_a_top_site_is_offered_when_it_is_named_and_not_otherwise(adapter):
    heard = Context(state=BEHIND, heard="open youtube")
    assert [o.label for o in base.candidates(adapter, browser.OPEN, {}, heard)] == [
        "open youtube.com in a new tab"
    ]
    assert offered(adapter, BEHIND, "browser.open") == ()


def test_the_items_of_the_main_list_are_numbered(adapter):
    clicks = offered(adapter, IN_FRONT, "browser.click")
    labels = [o.label for o in clicks]
    assert "result 2: The new GPU is here" in labels
    assert "Home" in labels
    # the search box is a search, not a click
    assert not any(label == "Search" for label in labels)


def test_the_search_box_is_offered_as_a_search_of_the_site(adapter):
    assert [o.label for o in offered(adapter, IN_FRONT, "browser.search")] == [
        "search youtube.com for words"
    ]


def test_closing_the_tab_names_it_and_needs_the_verb(adapter):
    close = offered(adapter, IN_FRONT, "browser.close_tab")[0]
    assert close.label == "close this tab (youtube.com)"
    assert close.tier == 2
    assert browser.CLOSE.verbs == ("close",)


def test_a_label_is_cut_to_the_length_every_option_has(adapter):
    long = dict(TABS[0], title="x" * 200)
    adapter._client.tabs = lambda: [long]
    assert all(len(o.label) <= browser.MAX_LABEL for o in offered(adapter, BEHIND, "browser.tab"))


# --------------------------------------------------------------------------- acting


def test_choosing_a_tab_switches_to_it(adapter):
    option = by_label(offered(adapter, BEHIND, "browser.tab"), "spotify")
    outcome = adapter.perform(option.capability, option.handle, Context(state=BEHIND))
    assert outcome.ok and ("activate", 3) in adapter._client.calls


def test_choosing_a_site_opens_it_in_a_new_tab(adapter):
    option = base.candidates(
        adapter, browser.OPEN, {}, Context(state=BEHIND, heard="open youtube")
    )[0]
    adapter.perform(option.capability, option.handle, Context(state=BEHIND))
    assert ("open", "https://www.youtube.com/", "new") in adapter._client.calls


def test_choosing_a_result_clicks_it(adapter):
    option = by_label(offered(adapter, IN_FRONT, "browser.click"), "result 2")
    outcome = adapter.perform(option.capability, option.handle, Context(state=IN_FRONT))
    assert ("click", "s1", "e3") in adapter._client.calls
    assert outcome.message == "Clicked result 2: The new GPU is here."


def test_a_click_on_a_page_that_re_rendered_finds_the_same_element_again(adapter):
    option = by_label(offered(adapter, IN_FRONT, "browser.click"), "result 2")
    rerendered = dict(RESULTS, snapshot_id="s2")
    adapter._client.snapshots.append(rerendered)
    adapter._client.stale_once = True
    adapter.perform(option.capability, option.handle, Context(state=IN_FRONT))
    assert ("click", "s2", "e3") in adapter._client.calls


def test_a_search_types_the_words_and_submits(adapter):
    option = offered(adapter, IN_FRONT, "browser.search")[0]
    adapter.perform(option.capability, option.handle, Context(state=IN_FRONT, words="ltt"))
    assert ("type", "s1", "e1", "ltt", True) in adapter._client.calls


def test_a_search_with_no_words_does_nothing(adapter):
    option = offered(adapter, IN_FRONT, "browser.search")[0]
    with pytest.raises(AdapterError):
        adapter.perform(option.capability, option.handle, Context(state=IN_FRONT))


def test_an_address_is_opened_as_the_host_it_spells(adapter):
    option = offered(adapter, BEHIND, "browser.address")[0]
    adapter.perform(option.capability, option.handle, Context(state=BEHIND, words="github dot com"))
    assert ("open", "https://github.com/", "new") in adapter._client.calls


def test_a_handle_the_adapter_never_minted_is_refused(adapter):
    with pytest.raises(AdapterError):
        adapter.perform("browser.tab", "tab 3", Context(state=BEHIND))


# --------------------------------------------------------------------------- find, then choose


def test_finding_types_the_query_into_the_search_box_and_offers_what_came_back(adapter):
    found = base.candidates(adapter, browser.FIND, {"query": "new gpu"}, Context(state=IN_FRONT))
    assert ("type", "s1", "e1", "new gpu", True) in adapter._client.calls
    # what shares the query's words comes first
    assert found[0].label == "The new GPU is here"
    # and nothing that shares no word with it is offered at all
    assert [o.label for o in found] == ["The new GPU is here"]
    chosen = found[0]
    adapter.perform(chosen.capability, chosen.handle, Context(state=IN_FRONT))
    assert ("click", "s1", "e3") in adapter._client.calls


def test_a_page_with_no_search_box_cannot_find_anything(adapter):
    adapter._client.snapshots = [dict(RESULTS, elements=RESULTS["elements"][1:])]
    with pytest.raises(AdapterError):
        base.candidates(adapter, browser.FIND, {"query": "x"}, Context(state=IN_FRONT))


# --------------------------------------------------------------------------- privacy

from hyprsay.config import Privacy  # noqa: E402


def test_a_tab_is_named_by_its_site_alone_unless_two_share_one(adapter):
    adapter._privacy = Privacy()
    labels = [o.label for o in offered(adapter, BEHIND, "browser.tab")]
    assert labels == [
        "switch to the youtube tab (youtube.com)",
        "switch to the spotify tab (open.spotify.com)",
    ]
    twin = dict(TABS[0], id=8, active=False, title="another video - YouTube")
    adapter._client.tabs = lambda: [*TABS, twin]
    adapter.forget_page()  # the tabs are cached for a moment; a new tab is a new reading
    labels = [o.label for o in offered(adapter, BEHIND, "browser.tab")]
    assert "switch to the youtube tab: another video - YouTube" in labels
    assert "switch to the spotify tab (open.spotify.com)" in labels


def test_a_title_that_matches_a_redaction_rule_is_never_sent(adapter):
    adapter._privacy = Privacy()
    secret = dict(TABS[0], id=8, active=False, title="My Password Vault - YouTube")
    adapter._client.tabs = lambda: [*TABS, secret]
    labels = [o.label for o in offered(adapter, BEHIND, "browser.tab")]
    assert not any("Vault" in label for label in labels)


def test_never_withholds_titles_and_what_is_on_the_page(adapter):
    adapter._privacy = Privacy(titles="never")
    twin = dict(TABS[0], id=8, active=False, title="another video - YouTube")
    adapter._client.tabs = lambda: [*TABS, twin]
    assert all("video" not in o.label for o in offered(adapter, BEHIND, "browser.tab"))
    assert offered(adapter, IN_FRONT, "browser.click") == ()
    assert offered(adapter, IN_FRONT, "browser.close_tab")[0].label == "close this tab"
    # the page's own search box is still usable: its label is the site, not page text
    assert offered(adapter, IN_FRONT, "browser.search")[0].label == "search youtube.com for words"


def test_only_the_main_link_of_a_result_is_named_by_its_number(adapter):
    channel = element("e6", "Linus Tech Tips", group="g1", index=2, size=3)
    menu = element("e7", "Action menu", kind="button", role="button", group="g1", index=2, size=3)
    adapter._client.snapshots = [dict(RESULTS, elements=[*RESULTS["elements"], channel, menu])]
    labels = [o.label for o in offered(adapter, IN_FRONT, "browser.click")]
    assert labels.count("result 2: The new GPU is here") == 1
    assert "Linus Tech Tips" in labels and "Action menu" in labels
    assert not any(label.startswith("result 2: Linus") for label in labels)


def test_a_known_site_that_was_said_is_offered_even_when_it_is_not_a_top_site(adapter):
    adapter._client.top_sites = lambda: []
    options = base.candidates(
        adapter, browser.OPEN, {}, Context(state=BEHIND, heard="open github please")
    )
    assert [o.label for o in options] == ["open github.com in a new tab"]
    adapter.perform(options[0].capability, options[0].handle, Context(state=BEHIND))
    assert ("open", "https://github.com/", "new") in adapter._client.calls


def test_a_site_already_offered_as_a_top_site_is_not_offered_twice(adapter):
    options = base.candidates(
        adapter, browser.OPEN, {}, Context(state=BEHIND, heard="open youtube")
    )
    assert [o.label for o in options] == ["open youtube.com in a new tab"]


def test_a_tab_is_named_by_the_site_in_its_address_and_never_by_its_title(adapter):
    tabs = offered(adapter, BEHIND, "browser.tab")
    spotify = by_label(tabs, "spotify")
    assert adapter.corroborates("browser.tab", spotify.handle, "go back to spotify")
    assert not adapter.corroborates("browser.tab", spotify.handle, "go back to the music")
    # the YouTube tab's title says "linus tech tips"; the title names nothing
    youtube = by_label(tabs, "youtube")
    assert not adapter.corroborates("browser.tab", youtube.handle, "go to linus tech tips")
    assert adapter.corroborates("browser.tab", youtube.handle, "go to youtube")
    # and it vouches for nothing but a tab switch
    assert not adapter.corroborates("browser.close_tab", "close 7", "close youtube")


def test_with_the_browser_in_front_the_tab_in_front_is_not_offered_and_named_tabs_lead(adapter):
    tabs = offered(adapter, IN_FRONT, "browser.tab")
    # tab 7 is active in front; only Spotify's is left to switch to
    assert [o.label for o in tabs] == ["switch to the spotify tab (open.spotify.com)"]


def test_the_pages_own_options_come_before_tabs_and_sites(adapter):
    order = [c.id for c in adapter.capabilities(Context(state=IN_FRONT))]
    assert order.index("browser.close_tab") < order.index("browser.tab")
    assert order.index("browser.search") < order.index("browser.open")


def test_the_main_list_is_offered_before_the_rest_of_the_page(adapter):
    sidebar = [element(f"n{n}", f"Sidebar link {n}") for n in range(browser.MAX_CLICKS)]
    adapter._client.snapshots = [dict(RESULTS, elements=[*sidebar, *RESULTS["elements"]])]
    labels = [o.label for o in offered(adapter, IN_FRONT, "browser.click")]
    assert labels[:3] == [
        "result 1: I built a PC for 10 years",
        "result 2: The new GPU is here",
        "result 3: Why we quit",
    ]
    assert len(labels) == browser.MAX_CLICKS


def test_a_page_that_could_not_be_read_is_not_asked_again_by_every_capability(adapter):
    def loading(tab_id=None, *, max=120, settle_ms=0):
        adapter._client.calls.append(("snapshot", tab_id, settle_ms))
        raise BrowserError("timeout", "no answer in time")

    adapter._client.snapshot = loading
    for capability in ("browser.search", "browser.click", "browser.scroll", "browser.close_tab"):
        offered(adapter, IN_FRONT, capability)
    assert sum(1 for call in adapter._client.calls if call[0] == "snapshot") == 1


def test_a_harvest_during_a_warm_serves_what_the_warm_stored_instead_of_asking(
    adapter, monkeypatch
):
    monkeypatch.setattr(browser, "SYNC_TIMEOUT_S", 0.05)
    adapter._idle.clear()  # a warm is reading the page
    assert offered(adapter, IN_FRONT, "browser.search") == ()
    assert adapter._client.calls == []
    adapter._idle.set()
    assert offered(adapter, IN_FRONT, "browser.search")[0].label == "search youtube.com for words"


def test_a_site_whose_search_box_is_not_on_the_page_yet_is_searched_by_its_address(adapter):
    buttons_only = [e for e in RESULTS["elements"] if e.get("kind") != "search"]
    adapter._client.snapshots = [dict(RESULTS, elements=buttons_only)]
    options = offered(adapter, IN_FRONT, "browser.search")
    assert [o.label for o in options] == ["search youtube.com for words"]
    adapter.perform(
        options[0].capability, options[0].handle, Context(state=IN_FRONT, words="linus tech tips")
    )
    url = "https://www.youtube.com/results?search_query=linus%20tech%20tips"
    assert ("open", url, "current") in adapter._client.calls


def test_a_site_with_no_search_address_and_no_search_box_offers_no_search(adapter):
    page = dict(RESULTS, url="https://example.com/", elements=[element("e5", "Home")])
    adapter._client.snapshots = [page]
    adapter._client.tabs = lambda: [dict(TABS[0], url="https://example.com/"), TABS[1]]
    assert offered(adapter, IN_FRONT, "browser.search") == ()


def test_a_page_read_just_after_it_changed_is_read_again_soon(adapter, monkeypatch):
    monkeypatch.setattr(browser, "YOUNG_FRESH_S", 0.0)
    offered(adapter, IN_FRONT, "browser.search")
    offered(adapter, IN_FRONT, "browser.click")
    assert sum(1 for call in adapter._client.calls if call[0] == "snapshot") == 1
    tab = by_label(offered(adapter, IN_FRONT, "browser.tab"), "spotify")
    adapter.perform(tab.capability, tab.handle, Context(state=IN_FRONT))
    offered(adapter, IN_FRONT, "browser.search")
    offered(adapter, IN_FRONT, "browser.click")
    assert sum(1 for call in adapter._client.calls if call[0] == "snapshot") == 3


SPOTIFY_RESULTS = {
    "snapshot_id": "s2",
    "tab_id": 7,
    "url": "https://open.spotify.com/search/hold%20the%20line",
    "title": "Spotify - Search",
    "elements": [
        element("e4", "What do you want to play?", kind="search", role="combobox"),
        element("e24", "Hold the Line TOTO", kind="button", role="button"),
        element("e28", "More options for Hold the Line", kind="button", role="button"),
        element("e31", "Hold the Line", group="g3", index=1, size=4),
        element("e32", "Add to Liked Songs", kind="button", role="button", group="g3", index=1,
                size=4),
        element("e33", "More options for Hold the Line by TOTO", kind="button", role="button",
                group="g3", index=1, size=4),
        element("e34", "TOTO", group="g3", index=1, size=4),
        element("e35", "Hold the Line", group="g3", index=2, size=4),
        element("e38", "TOTO", group="g3", index=2, size=4),
        element("e39", "Africa", group="g3", index=3, size=4),
        element("e60", "Play Hold The Line by Dessina", kind="button", role="button",
                group="g5", index=2, size=7),
        element("e71", "Hold The Line", group="g5", index=2, size=7),
        element("e73", "Dessina", group="g5", index=2, size=7),
    ],
}  # fmt: skip


def _find(adapter, heard):
    adapter._client.snapshots = [SPOTIFY_RESULTS]
    context = Context(state=IN_FRONT, heard=heard)
    return base.candidates(adapter, browser.FIND, {"query": "hold the line"}, context)


def test_what_a_search_found_is_offered_once_per_thing_without_its_menus(adapter):
    labels = [o.label for o in _find(adapter, "search for hold the line")]
    # the top card and the first row are one song; the second row is the same words again
    assert labels == ["Hold the Line TOTO", "Hold The Line, Dessina"]


def test_asked_to_play_a_found_item_is_played_by_its_own_play_button(adapter):
    found = _find(adapter, "play hold the line")
    dessina = by_label(found, "Dessina")
    assert dessina.label == "play Hold The Line, Dessina"
    adapter.perform(dessina.capability, dessina.handle, Context(state=IN_FRONT))
    assert ("click", "s2", "e60") in adapter._client.calls


def test_the_results_are_numbered_and_not_the_filter_tabs_or_the_footer(adapter):
    def item(ident, name, group, index, size, kind="link", in_view=True):
        found = element(ident, name, kind=kind, role=kind, group=group, index=index, size=size)
        found["in_view"] = in_view
        return found

    filters = [item(f"t{n}", f"Filter {n}", "g1", n, 5, kind="tab") for n in range(1, 6)]
    footer = [item(f"f{n}", f"Footer {n}", "g4", n, 6) for n in range(1, 7)]
    videos = []
    for n in range(1, 4):
        videos += [
            item(f"v{n}", f"A long video title number {n}", "g3", n, 3),
            item(f"c{n}", "Linus Tech Tips", "g3", n, 3),
            item(f"m{n}", "More actions", "g3", n, 3, kind="button"),
        ]
    adapter._client.snapshots = [dict(RESULTS, elements=[*filters, *footer, *videos])]
    labels = [o.label for o in offered(adapter, IN_FRONT, "browser.click")]
    assert "result 2: A long video title number 2" in labels
    assert not any(label.startswith("result") and "Filter" in label for label in labels)
    assert not any(label.startswith("result") and "Footer" in label for label in labels)
