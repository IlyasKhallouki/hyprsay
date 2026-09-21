"""Local entity resolution: which window or app a phrase means, with no network.

This is the fast path's second half (docs/PLAN.md 5.4) and the offline fallback's only
half (5.5). The lexicon scores a phrase against TRUSTED fields only (desktop file names,
kinds, keywords, user aliases), and this module turns scores into one of three outcomes:

- exact: one candidate clears `gates.fuzzy_exact` and leads the next by `gates.fuzzy_gap`.
- ambiguous: several clear the bar together. If words are left over in the utterance
  ("the firefox with youtube") and they single out exactly one candidate's title, that is
  still exact and still local. A title may break a tie among candidates a trusted field
  already selected; it never selects by itself, because the attacker writes titles (5.6).
  With no words left over ("focus kitty", four kitty windows) no model can know: hints.
- none: nothing clears the bar. The semantic path may try; offline, the top three become
  numbered hints so the command degrades to "pick a number" instead of vanishing.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from hyprsay.config import Gates
from hyprsay.model import App, Candidate, DesktopState, Window

MAX_HINTS = 3

# words that carry the command, not the target. Residual words are what is left after
# these, the entity phrase, and the trusted vocabulary are removed.
_COMMAND_WORDS = """
a an the this that these those it its my me i you your please can could would will
to of on in at with for from by and or is are be one ones thing which where
go goto switch focus show open launch start run close quit kill exit move send put bring
take throw make set get give find jump raise toggle float tile resize grow shrink
window windows app apps application applications program workspace workspaces desktop
screen monitor tab left right up down next previous other another new current active
bigger smaller wider narrower taller shorter larger fullscreen full floating volume
louder quieter mute unmute play pause skip back now again
here there over onto into pull push drag bring up down away out want need let us
"""
STOPWORDS = frozenset(_COMMAND_WORDS.split())


class LexiconLike(Protocol):
    """The part of `hyprsay.lexicon.Lexicon` the understanding layer reads."""

    def app_for_window(self, window: Window) -> App | None: ...
    def kind_of(self, window: Window) -> str: ...
    def words(self) -> frozenset[str]: ...
    def match_apps(self, phrase: str, limit: int = 5) -> list[tuple[App, float]]: ...
    def match_windows(
        self, phrase: str, state: DesktopState, limit: int = 5
    ) -> list[tuple[Window, float]]: ...
    def title_discriminates(self, residual: Any, candidates: Any) -> Window | None: ...
    def corroborates(
        self, utterance: str, *, app: App | None = None, window: Window | None = None
    ) -> bool: ...


@dataclass(frozen=True)
class Resolution:
    status: str = "none"  # "exact" | "ambiguous" | "none"
    window: Window | None = None
    app: App | None = None
    # best first. For "ambiguous" these are the tied ones; for "none" the nearest few.
    candidates: tuple[Candidate, ...] = ()
    residual: tuple[str, ...] = ()
    # a title token broke the tie: good enough for tiers 0 and 1, never for tier 2
    by_title: bool = False
    # the tied candidates are windows of one app, the only case a title request may serve
    same_app: bool = False

    @property
    def exact(self) -> bool:
        return self.status == "exact"


def residual_words(tokens: Iterable[str], vocabulary: frozenset[str]) -> tuple[str, ...]:
    """Content words that neither the command nor any trusted anchor accounts for.

    The grammar copies the whole referring phrase ("firefox with youtube") into the
    slot, so the entity phrase cannot simply be subtracted: what names the app is in
    the trusted vocabulary, and what is left ("youtube") is what a title may answer.
    """
    return tuple(
        t for t in tokens if t not in STOPWORDS and t not in vocabulary and not t.isdigit()
    )


def label(lexicon: LexiconLike, window: Window | None = None, app: App | None = None) -> str:
    """What a badge shows. Trusted fields only: a title on a badge is attacker text."""
    if window is not None:
        owner = lexicon.app_for_window(window)
        name = owner.name if owner else (window.cls or window.initial_class or "window")
        return f"{name} (workspace {window.workspace_name or window.workspace_id})"
    return app.name if app else ""


def window_candidate(
    lexicon: LexiconLike, utterance: str, window: Window, score: float = 0.0
) -> Candidate:
    return Candidate(
        label=label(lexicon, window=window),
        window=window,
        app=lexicon.app_for_window(window),
        score=score,
        corroborated=bool(lexicon.corroborates(utterance, window=window)),
    )


def app_candidate(lexicon: LexiconLike, utterance: str, app: App, score: float = 0.0) -> Candidate:
    return Candidate(
        label=app.name,
        app=app,
        score=score,
        corroborated=app.trusted and bool(lexicon.corroborates(utterance, app=app)),
    )


def _leaders(scored: Sequence[tuple[Any, float]], gates: Gates) -> list[tuple[Any, float]]:
    """The entries that clear the bar and sit within the gap of the best one."""
    strong = [(e, s) for e, s in scored if s >= gates.fuzzy_exact]
    if not strong:
        return []
    best = strong[0][1]
    return [(e, s) for e, s in strong if best - s < gates.fuzzy_gap]


def _owner(lexicon: LexiconLike, window: Window) -> str:
    app = lexicon.app_for_window(window)
    return app.id if app else window.cls.lower()


def _ranked_windows(
    phrase: str, state: DesktopState, lexicon: LexiconLike
) -> list[tuple[Window, float]]:
    return sorted(lexicon.match_windows(phrase, state, limit=5), key=lambda ws: -ws[1])


def resolve_window(
    phrase: str,
    utterance: str,
    tokens: Iterable[str],
    state: DesktopState,
    lexicon: LexiconLike,
    gates: Gates,
) -> Resolution:
    vocabulary = lexicon.words()
    scored = _ranked_windows(phrase, state, lexicon)
    leaders = _leaders(scored, gates)
    if not leaders:
        # "firefox with youtube": the leftover word dilutes the phrase's score. Match on
        # the trusted words alone; the leftover is kept as residual for the title step.
        core = " ".join(t for t in phrase.split() if t in vocabulary)
        if core and core != phrase:
            trimmed = _ranked_windows(core, state, lexicon)
            if _leaders(trimmed, gates):
                scored, leaders = trimmed, _leaders(trimmed, gates)
    if not leaders:
        near = tuple(window_candidate(lexicon, utterance, w, s) for w, s in scored[:MAX_HINTS])
        return Resolution("none", candidates=near)
    if len(leaders) == 1:
        window, score = leaders[0]
        chosen = window_candidate(lexicon, utterance, window, score)
        return Resolution("exact", window=window, app=chosen.app, candidates=(chosen,))

    tied = [w for w, _ in leaders]
    candidates = tuple(window_candidate(lexicon, utterance, w, s) for w, s in leaders)
    residual = residual_words(tokens, vocabulary)
    same_app = len({_owner(lexicon, w) for w in tied}) == 1
    if residual:
        winner = lexicon.title_discriminates(residual, tied)
        if winner is not None and any(winner.address == w.address for w in tied):
            first = next(c for c in candidates if c.window.address == winner.address)
            rest = tuple(c for c in candidates if c is not first)
            return Resolution(
                "exact",
                window=winner,
                app=first.app,
                candidates=(first, *rest),
                residual=residual,
                by_title=True,
                same_app=same_app,
            )
    return Resolution("ambiguous", candidates=candidates, residual=residual, same_app=same_app)


def resolve_app(phrase: str, utterance: str, lexicon: LexiconLike, gates: Gates) -> Resolution:
    scored = sorted(lexicon.match_apps(phrase, limit=5), key=lambda a: -a[1])
    leaders = _leaders(scored, gates)
    if not leaders:
        near = tuple(app_candidate(lexicon, utterance, a, s) for a, s in scored[:MAX_HINTS])
        return Resolution("none", candidates=near)
    candidates = tuple(app_candidate(lexicon, utterance, a, s) for a, s in leaders)
    if len(leaders) == 1:
        return Resolution("exact", app=leaders[0][0], candidates=candidates)
    return Resolution("ambiguous", candidates=candidates)


def spoken_app_windows(
    utterance: str, state: DesktopState, lexicon: LexiconLike
) -> tuple[Window, ...]:
    """The live windows of THE app the utterance names, when it names exactly one.

    This is "the locally selected app" of PLAN 5.5: selected by a trusted field, before
    any call, so code knows up front whether a title request could be needed at all.
    """
    by_owner: dict[str, list[Window]] = {}
    for window in state.windows:
        if lexicon.app_for_window(window) is not None:
            by_owner.setdefault(_owner(lexicon, window), []).append(window)
    spoken = [
        windows
        for windows in by_owner.values()
        if lexicon.corroborates(utterance, app=lexicon.app_for_window(windows[0]))
    ]
    return tuple(spoken[0]) if len(spoken) == 1 else ()
