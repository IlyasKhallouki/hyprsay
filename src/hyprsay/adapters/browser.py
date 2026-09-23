"""The browser, through the hypruse-browser extension and hypruse's browser host.

WHAT IT OFFERS. Tabs to switch to and sites to open, from anywhere. When a Chromium window is
in front, also what is on the page: its links and buttons to click (numbered when they are
the items of a list, so "click the second video" has something to name), its search box to
search with, a way to find something by searching and then play or open it, scrolling, going
back and forward, and closing the tab.

WHERE THE WORDS COME FROM. Jev cannot write a query or an address, so the two capabilities
that need one declare `wants_words` (the words the single request pointed at arrive as
`Context.words`), and "find it and play it" is a `needs_query` capability that is fetched:
`resolve` types the query into the page's own search box, waits for the page to settle, and
returns what came back as the candidates of the second turn.

THE ONE EXCEPTION TO "resolve only reads". Every other adapter's `resolve` is read only. This
one types a search into the page when the find capability is fetched, because the results
ARE the candidates and a page has no read-only search. It is a search, which a reader can
undo by going back, and it only ever happens after the speaker asked to find something by
name; nothing else in `resolve` writes.

NOTHING HERE WAITS ON THE BROWSER DURING A HARVEST if it can help it. Tabs, top sites and the
page snapshot are cached; `warm` refreshes them in the background (the daemon calls it at key
down and when focus lands on a browser), and a harvest only refreshes synchronously when the
cache is older than it should be, with a short timeout.

EVERY LABEL IS UNTRUSTED: page titles and element names are written by web pages.
`base.candidates` forces that, and a label authorizes nothing (docs/PLAN.md 5.6).

WHAT LEAVES THE MACHINE follows `privacy.titles`, the rule window titles already follow
(docs/PRIVACY.md): a tab is named by its site, and its title goes only to tell apart two tabs
of one site, cut and redacted. The names of the links and buttons in view go only while a
browser is in front, which is when the speaker is reaching into it. `"never"` withholds every
title and every name on the page; tabs, sites, scrolling, history and the page's own search
box still work, because none of them needs page text.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from hyprsay import recipes
from hyprsay.adapters.base import (
    AdapterError,
    Affordance,
    Capability,
    Context,
    Minted,
    Param,
    Trust,
    tier_for,
)
from hyprsay.config import Privacy
from hyprsay.model import Outcome, Window
from hypruse.browser.client import BrowserClient, BrowserError, BrowserUnavailable, socket_path

log = logging.getLogger(__name__)

NAME = "browser"
MAX_LABEL = 60

TAB = Capability("browser.tab", "switch to a browser tab", tier=0, kind="browser tab")
OPEN = Capability("browser.open", "open a website in a new browser tab", tier=1, kind="website")
ADDRESS = Capability(
    "browser.address",
    "open a website by its address in a new browser tab",
    tier=1,
    kind="website",
    wants_words=True,
    words_kind="address",
)
CLICK = Capability("browser.click", "click something on this page", tier=1, kind="page")
SEARCH = Capability(
    "browser.search", "search this site for words", tier=1, kind="page search", wants_words=True
)
FIND = Capability(
    "browser.find",
    "find something on this site by searching for it, then play or open it",
    tier=1,
    kind="page search",
    params=(Param("query", "text"),),
    needs_query=True,
    fetch_s=2.4,
)
SCROLL = Capability("browser.scroll", "scroll this page", tier=0, kind="page")
HISTORY = Capability("browser.history", "go back or forward a page", tier=1, kind="page")
CLOSE = Capability(
    "browser.close_tab", "close this browser tab", tier=2, kind="browser tab", verbs=("close",)
)
ANYWHERE = (TAB, OPEN, ADDRESS)
IN_FRONT = (CLICK, SEARCH, FIND, SCROLL, HISTORY, CLOSE)
BY_ID = {cap.id: cap for cap in (*ANYWHERE, *IN_FRONT)}

# how old a reading may be before a harvest refreshes it itself
TABS_FRESH_S = 1.5
SNAPSHOT_FRESH_S = 2.0
SITES_FRESH_S = 60.0
# how long a harvest may wait on the browser when it has to
SYNC_TIMEOUT_S = 0.6
# how long the find capability lets a search settle before reading the results
FIND_SETTLE_MS = 1500
MAX_CLICKS = 40
MAX_FOUND = 12

_CHROMIUM = ("chrom", "brave")
_WORDS_TO_SKIP = frozenset({"the", "a", "an", "of", "by", "on", "in", "to", "and", "for", "play"})


def is_browser(window: Window | None) -> bool:
    if window is None:
        return False
    cls = (window.cls or window.initial_class or "").lower()
    return any(marker in cls for marker in _CHROMIUM)


def _host(url: str) -> str:
    host = urlparse(url).hostname or ""
    return host.removeprefix("www.")


def _site_name(host: str) -> str:
    """What a person calls a site: open.spotify.com is spotify, mail.google.com is google."""
    parts = host.split(".")
    return parts[-2] if len(parts) >= 2 else host


def _cut(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= MAX_LABEL else text[: MAX_LABEL - 1].rstrip() + "…"


@dataclass
class _Reading:
    tabs: list[dict[str, Any]] = field(default_factory=list)
    tabs_at: float = 0.0
    sites: list[dict[str, Any]] = field(default_factory=list)
    sites_at: float = 0.0
    snapshot: dict[str, Any] = field(default_factory=dict)
    snapshot_at: float = 0.0


class Browser:
    """Implements `adapters.base.Adapter`."""

    name = NAME

    def __init__(self, client: BrowserClient | None = None, privacy: Privacy | None = None) -> None:
        self._client = client
        self._privacy = privacy or Privacy()
        self._minted = Minted(limit=512)
        # what a click handle pointed at, so a click on a page that re-rendered can find
        # the same element again instead of refusing
        self._targets: dict[str, tuple[str, str, int]] = {}
        self._reading = _Reading()
        self._lock = threading.Lock()
        self._warming = threading.Event()

    def __repr__(self) -> str:
        return "Browser()"

    # ------------------------------------------------------------------ the connection

    def _browser(self, timeout: float = 2.0) -> BrowserClient:
        if self._client is None:
            self._client = BrowserClient(timeout=timeout)
        return self._client

    def _call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except BrowserUnavailable as exc:
            raise AdapterError(
                "the browser extension is not connected: is Chrome running with "
                "hypruse-browser loaded? (hypruse browser status)"
            ) from exc
        except BrowserError as exc:
            raise AdapterError(_sentence(exc)) from exc

    # ------------------------------------------------------------------ the reading

    def refresh(self, *, page: bool, force: bool = False, timeout: float = 2.0) -> None:
        """Read tabs, top sites and, with `page`, the page in front. Raises AdapterError."""
        client = self._browser(timeout)
        now = time.monotonic()
        reading = self._reading
        if force or now - reading.tabs_at > TABS_FRESH_S:
            tabs = self._call(client.tabs)
            with self._lock:
                reading.tabs, reading.tabs_at = tabs, time.monotonic()
        if force or now - reading.sites_at > SITES_FRESH_S:
            try:
                sites = self._call(client.top_sites)
            except AdapterError:
                sites = []
            with self._lock:
                reading.sites, reading.sites_at = sites, time.monotonic()
        if page:
            active = self._active_tab()
            stale = now - reading.snapshot_at > SNAPSHOT_FRESH_S
            other_tab = active is not None and reading.snapshot.get("tab_id") != active.get("id")
            if force or stale or other_tab:
                try:
                    snap = self._call(client.snapshot)
                except AdapterError:
                    # a page Chrome will not let an extension into (a new tab page, the Web
                    # Store) has nothing to offer; the tabs still do
                    snap = {}
                with self._lock:
                    reading.snapshot, reading.snapshot_at = snap, time.monotonic()

    def warm(self, *, page: bool = True) -> None:
        """Refresh in the background, once at a time. Never raises, never blocks."""
        if self._warming.is_set() or not socket_path().exists():
            return
        self._warming.set()

        def run() -> None:
            try:
                self.refresh(page=page)
            except Exception:
                log.debug("the browser could not be read", exc_info=True)
            finally:
                self._warming.clear()

        threading.Thread(target=run, name="browser-warm", daemon=True).start()

    def forget_page(self) -> None:
        with self._lock:
            self._reading.snapshot_at = 0.0
            self._reading.tabs_at = 0.0

    def _active_tab(self) -> dict[str, Any] | None:
        # the extension lists the focused window first, so its active tab comes first
        return next((t for t in self._reading.tabs if t.get("active")), None)

    def _ensure(self, page: bool) -> None:
        try:
            self.refresh(page=page, timeout=SYNC_TIMEOUT_S)
        except AdapterError:
            log.debug("the browser could not be read for this harvest", exc_info=True)

    # ------------------------------------------------------------------ the contract

    def probe(self) -> tuple[bool, str]:
        try:
            hello = self._browser().connect()
        except BrowserUnavailable:
            return False, (
                "the browser extension is not connected: load hypruse-browser in Chrome and "
                "run hypruse browser install"
            )
        return True, f"hypruse-browser {hello.get('extension', '?')} in {hello.get('browser', '?')}"

    def can_serve(self, context: Context) -> bool:
        return any(is_browser(w) for w in context.state.windows) and socket_path().exists()

    def capabilities(self, context: Context) -> tuple[Capability, ...]:
        if not is_browser(context.state.focused):
            return ANYWHERE
        if self._privacy.never:
            # clicking by name and finding by name both need the page's own text
            return (*ANYWHERE, *(c for c in IN_FRONT if c not in (CLICK, FIND)))
        return (*ANYWHERE, *IN_FRONT)

    def resolve(
        self, capability: str, slots: Mapping[str, Any], context: Context
    ) -> tuple[Affordance, ...]:
        declared = BY_ID.get(capability)
        if declared is None:
            raise AdapterError(f"{capability} is not something the browser does")
        in_front = is_browser(context.state.focused)
        if capability == FIND.id:
            return self._find(str(slots.get("query", "")))
        self._ensure(page=in_front)
        with self._lock:
            tabs = list(self._reading.tabs)
            sites = list(self._reading.sites)
            snap = dict(self._reading.snapshot)
        active = next((t for t in tabs if t.get("active")), None)
        if capability == TAB.id:
            return tuple(self._tab(t, tabs) for t in tabs if t.get("id") is not None)
        if capability == OPEN.id:
            return self._sites(sites, tabs, context.heard)
        if capability == ADDRESS.id:
            return (self._option(ADDRESS, "a1", "open a website by its address", "address"),)
        if not in_front or active is None:
            return ()
        tab = int(active["id"])
        if capability == SCROLL.id:
            return tuple(
                self._option(SCROLL, f"g{n}", label, f"scroll {tab} {way}")
                for n, (way, label) in enumerate(
                    (
                        ("down", "scroll this page down"),
                        ("up", "scroll this page up"),
                        ("top", "go to the top of this page"),
                        ("bottom", "go to the bottom of this page"),
                    ),
                    1,
                )
            )
        if capability == HISTORY.id:
            return (
                self._option(HISTORY, "p1", "go back to the previous page", f"history {tab} back"),
                self._option(HISTORY, "p2", "go forward a page", f"history {tab} forward"),
            )
        if capability == CLOSE.id:
            host = _host(str(active.get("url", "")))
            label = (
                "close this tab" if self._privacy.never or not host else f"close this tab ({host})"
            )
            return (self._option(CLOSE, "k1", label, f"close {tab}"),)
        if not snap or snap.get("tab_id") != tab:
            return ()
        if capability == SEARCH.id:
            return self._searches(snap)
        if capability == CLICK.id and not self._privacy.never:
            return self._clicks(snap)
        return ()

    def corroborates(self, capability: str, handle: str, heard: str) -> bool:
        """Did the speaker name this tab? By its site, read from the URL's host.

        Not from the title, which the page writes: a host is the site's own name, and
        "go back to spotify" names open.spotify.com. Only for switching tabs, which is tier
        0 and undone by switching back; nothing that costs more rests on it.
        """
        if capability != TAB.id or not handle.startswith("tab "):
            return False
        try:
            wanted = int(handle.split()[1])
        except (IndexError, ValueError):
            return False
        with self._lock:
            tab = next((t for t in self._reading.tabs if t.get("id") == wanted), None)
        if tab is None:
            return False
        site = _site_name(_host(str(tab.get("url", ""))))
        return bool(site) and site in heard.lower().split()

    def perform(self, capability: str, handle: str, context: Context) -> Outcome:
        if handle not in self._minted:
            raise AdapterError("that is not an option the browser offered, so nothing was done")
        client = self._browser()
        kind, _, rest = handle.partition(" ")
        label = self._minted.label_for(handle)
        if kind == "tab":
            self._call(client.activate, int(rest))
            self.forget_page()
            return Outcome(True, "Switched tabs.")
        if kind == "open":
            self._call(client.open, rest, "new")
            self.forget_page()
            return Outcome(True, f"Opened {_host(rest)} in a new tab.")
        if kind == "address":
            try:
                host = recipes.normalize_host(context.words)
            except recipes.RecipeError as exc:
                raise AdapterError(str(exc)) from exc
            self._call(client.open, f"https://{host}/", "new")
            self.forget_page()
            return Outcome(True, f"Opened {host} in a new tab.")
        if kind == "scroll":
            tab, way = rest.split(" ", 1)
            self._call(client.scroll, way, int(tab))
            return Outcome(True, "Scrolled.")
        if kind == "history":
            tab, way = rest.split(" ", 1)
            self._call(client.history, int(tab), way)
            self.forget_page()
            return Outcome(True, "Went back." if way == "back" else "Went forward.")
        if kind == "close":
            self._call(client.close_tab, int(rest))
            self.forget_page()
            return Outcome(True, "Closed the tab.")
        if kind == "click":
            self._on_element(handle, rest, lambda s, e: client.click(s, e))
            self.forget_page()
            return Outcome(True, f"Clicked {label}." if label else "Clicked.")
        if kind == "search":
            if not context.words.strip():
                raise AdapterError("say what to search for as well")
            words = context.words
            self._on_element(handle, rest, lambda s, e: client.type(s, e, words, submit=True))
            self.forget_page()
            return Outcome(True, "Searched.")
        raise AdapterError("that is not something the browser does")

    # ------------------------------------------------------------------ building options

    def _option(self, capability: Capability, ident: str, label: str, handle: str) -> Affordance:
        label = _cut(label)
        return Affordance(
            id=ident,
            label=label,
            kind=capability.kind,
            tier=tier_for(capability, label),
            handle=self._minted.mint(handle, label),
            adapter=NAME,
            capability=capability.id,
            trust=Trust.UNTRUSTED,
        )

    def _tab(self, tab: dict[str, Any], tabs: list[dict[str, Any]]) -> Affordance:
        host = _host(str(tab.get("url", "")))
        # named by the site as a person says it ("the spotify tab"), with the host beside it:
        # measured live, "open.spotify.com tab" lost to "start Spotify" for "open spotify"
        label = f"switch to the {_site_name(host)} tab ({host})" if host else "switch to a tab"
        title = self._shown_title(tab, tabs)
        if title:
            # two tabs of one site: the title tells them apart, and the host would only
            # push it past the label's 60 characters
            label = f"switch to the {_site_name(host)} tab: {title}"
        return self._option(TAB, f"t{tab['id']}", label, f"tab {int(tab['id'])}")

    def _shown_title(self, tab: dict[str, Any], tabs: list[dict[str, Any]]) -> str:
        """A tab's title, only when it is needed to tell it from another tab of its site."""
        privacy = self._privacy
        if privacy.never:
            return ""
        host = _host(str(tab.get("url", "")))
        twins = [t for t in tabs if _host(str(t.get("url", ""))) == host]
        if len(twins) < 2:
            return ""
        title = " ".join(str(tab.get("title", "")).split())
        lowered = title.lower()
        if any(pattern.lower() in lowered for pattern in privacy.redact_title_patterns):
            return ""
        return title[: privacy.title_chars]

    def _sites(
        self, sites: list[dict[str, Any]], tabs: list[dict[str, Any]], heard: str = ""
    ) -> tuple[Affordance, ...]:
        out: list[Affordance] = []
        seen: set[str] = set()
        # a site the speaker named that the table of known sites spells ("youtube"), first:
        # the top sites are the profile's habits, and a site not among them was still asked for
        for word in heard.lower().split():
            host = recipes.KNOWN_SITES.get(word, "")
            if host and host not in seen:
                seen.add(host)
                label = f"open {host} in a new tab"
                out.append(self._option(OPEN, f"o{len(out) + 1}", label, f"open https://{host}/"))
        for site in sites:
            url = str(site.get("url", ""))
            host = _host(url)
            if not host or host in seen or urlparse(url).scheme not in ("http", "https"):
                continue
            seen.add(host)
            out.append(
                self._option(OPEN, f"o{len(out) + 1}", f"open {host} in a new tab", f"open {url}")
            )
            if len(out) >= 10:
                break
        return tuple(out)

    def _searches(self, snap: dict[str, Any]) -> tuple[Affordance, ...]:
        host = _host(str(snap.get("url", "")))
        found = [e for e in snap.get("elements", []) if e.get("kind") == "search"]
        out: list[Affordance] = []
        for n, element in enumerate(found[:2], 1):
            where = host or "this site"
            label = f"search {where} for words" if n == 1 else f"search {where}: {element['name']}"
            handle = f"search {snap['tab_id']} {snap['snapshot_id']} {element['id']}"
            self._remember(handle, element)
            out.append(self._option(SEARCH, f"q{n}", label, handle))
        return tuple(out)

    def _clicks(
        self, snap: dict[str, Any], elements: list[dict[str, Any]] | None = None
    ) -> tuple[Affordance, ...]:
        elements = elements if elements is not None else _clickable(snap)
        main = _main_group(elements)
        leads = _leads(elements, main)
        out: list[Affordance] = []
        for element in elements[:MAX_CLICKS]:
            listed = element.get("list") or {}
            name = str(element.get("name", ""))
            # only the item's main link is "result 2": its channel link and its menu keep
            # their own names, or "the second video" could land on a channel page
            numbered = listed.get("group") == main and leads.get(listed.get("index")) is element
            label = f"result {listed['index']}: {name}" if numbered else name
            handle = f"click {snap['tab_id']} {snap['snapshot_id']} {element['id']}"
            self._remember(handle, element)
            out.append(self._option(CLICK, f"c{element['id']}", label, handle))
        return tuple(out)

    def _remember(self, handle: str, element: dict[str, Any]) -> None:
        listed = element.get("list") or {}
        self._targets[handle] = (
            str(element.get("role", "")),
            str(element.get("name", "")),
            int(listed.get("index", 0)),
        )
        if len(self._targets) > 1024:
            for key in list(self._targets)[:256]:
                del self._targets[key]

    # ------------------------------------------------------------------ acting on elements

    def _on_element(self, handle: str, rest: str, act: Any) -> None:
        """Act on the element a handle names, finding it again if the page re-rendered."""
        tab, snapshot_id, element_id = rest.split(" ", 2)
        try:
            self._call(act, snapshot_id, element_id)
            return
        except AdapterError as exc:
            if "stale" not in str(exc):
                raise
        role, name, index = self._targets.get(handle, ("", "", 0))
        fresh = self._call(self._browser().snapshot, int(tab))
        again = next(
            (
                e
                for e in fresh.get("elements", [])
                if e.get("role") == role
                and e.get("name") == name
                and int((e.get("list") or {}).get("index", 0)) == index
            ),
            None,
        )
        if again is None:
            raise AdapterError("the page changed and that is not on it any more; say it again")
        self._call(act, fresh["snapshot_id"], again["id"])

    # ------------------------------------------------------------------ find, then choose

    def _find(self, query: str) -> tuple[Affordance, ...]:
        """Type the query into the page's search box and offer what came back."""
        query = query.strip()
        if not query:
            return ()
        client = self._browser(timeout=3.0)
        snap = self._call(client.snapshot)
        box = next((e for e in snap.get("elements", []) if e.get("kind") == "search"), None)
        if box is None:
            raise AdapterError("this page has no search box to find that with")
        self._call(client.type, snap["snapshot_id"], box["id"], query, submit=True)
        results = self._call(client.snapshot, settle_ms=FIND_SETTLE_MS)
        with self._lock:
            self._reading.snapshot, self._reading.snapshot_at = results, time.monotonic()
        clickable = _clickable(results)
        ranked = _ranked_for(query, clickable)
        return self._clicks(results, ranked[:MAX_FOUND])


def _clickable(snap: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        e
        for e in snap.get("elements", [])
        if e.get("kind") in ("link", "button", "tab", "option", "toggle", "other") and e.get("name")
    ]


def _main_group(elements: list[dict[str, Any]]) -> str:
    """The list most of the page is about: the group with the most items."""
    sizes: dict[str, int] = {}
    for element in elements:
        listed = element.get("list") or {}
        if listed.get("group"):
            sizes[listed["group"]] = max(sizes.get(listed["group"], 0), int(listed.get("size", 0)))
    return max(sizes, key=lambda g: sizes[g]) if sizes else ""


def _leads(elements: list[dict[str, Any]], group: str) -> dict[Any, dict[str, Any]]:
    """Each item of the main list, and its main link: the link with the longest name,
    which on a results page is the title rather than the channel or a menu button."""
    leads: dict[Any, dict[str, Any]] = {}
    for element in elements:
        listed = element.get("list") or {}
        if not group or listed.get("group") != group:
            continue
        index = listed.get("index")
        best = leads.get(index)
        rank = (element.get("kind") == "link", len(str(element.get("name", ""))))
        if best is None or rank > (best.get("kind") == "link", len(str(best.get("name", "")))):
            leads[index] = element
    return leads


def _ranked_for(query: str, elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What a search returned, the ones that share the most words with the query first,
    keeping the page's own order among equals ("the second one" is still the second)."""
    wanted = {w for w in query.lower().split() if w not in _WORDS_TO_SKIP}

    def overlap(element: dict[str, Any]) -> int:
        return len(wanted & set(str(element.get("name", "")).lower().split()))

    scored = [(overlap(e), n, e) for n, e in enumerate(elements)]
    hits = [e for score, n, e in sorted(scored, key=lambda x: (-x[0], x[1])) if score > 0]
    return hits or elements


def _sentence(exc: BrowserError) -> str:
    return {
        "stale_snapshot": "the page changed before that could be done (stale)",
        "restricted_page": "Chrome does not let extensions into this page",
        "refused": "that field is not one words may be typed into",
        "not_found": "that tab or element is gone",
        "timeout": "the browser took too long to answer",
        "no_extension": "the browser extension is not connected",
    }.get(exc.code, f"the browser said: {exc.message or exc.code}")


__all__ = ["BY_ID", "NAME", "Browser", "is_browser"]
