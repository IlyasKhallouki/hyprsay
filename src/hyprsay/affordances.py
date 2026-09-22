"""Everything the machine can do right now, as one ranked list of options.

WHY THIS EXISTS. `nlu/requests.py` sends `{utterance, variants, note}` and says in its own
docstring "Nothing about the desktop is here", while `nlu/bank.py` tells the model that
launching is "not_for: going to a window that is already open". The question is asked and
the evidence is withheld. A chooser can only ever reach the behaviour its caller can
enumerate (docs/STRATEGY.md 2.1), so the intelligence lives here, in the function that
builds the candidates, and not in the model that picks one of them.

WHAT IS HARVESTED, AND FROM WHOM THE TIER COMES. Nothing here decides what an action
costs. Every affordance carries the tier the module that owns the gesture already gave it:

    open windows, as focus targets        `nlu/tiers.tier(Intent.FOCUS_WINDOW, ...)`   0
    workspaces, as switch targets         `nlu/tiers.tier(Intent.SWITCH_WORKSPACE, ...)` 0
    the focused window, as the subject of `nlu/tiers.tier(Intent.FULLSCREEN, ...)` and
      fullscreen, float, resize and move  its three siblings                          1
    installed apps, as launch targets     `nlu/tiers.tier(Intent.LAUNCH_APP, ...)`     1
    volume and media transport            `nlu/tiers.tier(Intent.VOLUME | MEDIA, ...)` 1
    adapters, through `adapters.base.offer`  each capability's own declared tier

There is no second tier table in this file and there must never be one. An untrusted
label may RAISE a tier and may never lower one, which is `adapters.base.tier_for`'s rule
and `controls.click_tier`'s rule, applied here by calling them rather than by copying
them.

WHAT IS NOT HARVESTED, AND WHY. Closing a window is absent: it is tier 2, it needs its
verb said out loud (`nlu/tiers.LITERAL_VERBS`), and an option list is not evidence from
the speaker. In-app reach is absent too, and that one is measured rather than argued:
`controls.py` records KCalc's 48 controls taking 16.2 seconds to walk and says the walk
"runs only when the utterance actually asks to click something, never speculatively,
because a speculative one would eat the whole latency budget". A key-down harvest is
exactly a speculative walk. When in-app options do arrive they take their tier from
`controls.click_tier`, `inapp.chord_tier` and `recipes.tier_for`, never from here.

THE HARD CONSTRAINT, AND THE NUMBERS BEHIND THE CUT. Jev takes at most 255 options and
this project caps a request at 1,800 tokens, because latency and HTTP 503s both climb
past about 2.2k (docs/research/live/, `jev/tokens.py`). Measured on this machine's live
desktop, read only, on 2026-09-22:

    9 open windows, 10 workspaces, 8 of them ordinary, 236 installed .desktop entries
    of which 231 are trusted, so a full harvest builds 9 + 10 + 4 + 12 + 8 = 43 options
    before any adapter is reached
    the 43 cost 552 tokens as a Choice, and their labels average 24 characters
    ranking and cutting the 43 takes 1.4 ms, of which 1.2 ms is the lexicon
    60 options at the measured average label cost 762 tokens; 60 at the full 60 character
    label cost 1,246, which is 69 percent of the whole request cap

So the list is capped twice: at `MAX_OPTIONS` candidates, and at `OPTION_BUDGET` tokens,
whichever bites first. `budget_for` shrinks the token budget by however much
`jev.tokens.Drift` has measured the estimator running low, which is the dangerous
direction and the one `tokens.py`'s own docstring warns about. Nothing is cut silently:
`Harvest.dropped` counts every drop by source and `Harvest.note` is one sentence naming
them, because docs/STRATEGY.md 2.4 calls a silent cap a lie.

ADDRESSES DO NOT LEAVE THE MACHINE, so the option id cannot be one. docs/PLAN.md section
7 and `nlu/requests.py` both promise that what travels is `{app, kind, workspace,
recency}` per window and no address. docs/STRATEGY.md 2.2 sketches `focus_window:<addr>`
as the id, and an id is a dict KEY in the request, so that sketch would put every window
address on the wire. It is not followed. A window's id here is `w1`, its address is its
`handle`, and `Affordance.as_option` does not carry a handle. The same key is 26
characters cheaper, which is the second reason and not the first.

A SERVICE'S OWN ID IS NOT USED AS AN OPTION ID EITHER. `Affordance.__post_init__`
sanitizes and caps the label and the handle and does neither to the id: it only checks
the id against a character class that permits `.`, `:`, `@`, `#`, `/` and `+` and no
length at all. So an adapter that mints an id from a search result would put unbounded
service text into the request as a key, and could collide with a compositor id. Every
adapter candidate is renumbered `x1`, `x2` on the way in, and `Affordance.adapter` and
`Affordance.capability` ride along so the choice still routes home with no string surgery.

THREADS AND FRESHNESS. A `Harvest` is a reading of one moment, like every other frozen
thing in `model.py`: it holds the state it was built against, and the executor re-reads
the desktop before it writes, so a stale option cannot be acted on. Call it from the one
loop that owns the conversation.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field, replace

from hyprsay.adapters import base
from hyprsay.adapters.base import Adapter, Affordance, Capability, Context, Trust
from hyprsay.config import Config
from hyprsay.jev import tokens
from hyprsay.lexicon import mixed_script, phonetic_key, sanitize, similarity
from hyprsay.model import Action, App, Intent, Window
from hyprsay.nlu import tiers
from hyprsay.nlu.resolve import LexiconLike

log = logging.getLogger(__name__)

# The adapter name every option code in this repository owns carries. `adapters.base.find`
# looks an adapter up by this name and finds none, which is what tells the caller that the
# option is performed by `ops.py` rather than by an adapter.
SELF = "hyprsay"

WINDOW = "window"
LAYOUT = "layout"
WORKSPACE = "workspace"
APP = "app"
SYSTEM = "system"
SERVICE = "service"

# The last tie break, after what was said and after recency: the cheapest and most
# reversible source first, which is the same order `nlu/tiers.py` puts the intents in.
ORDER: tuple[str, ...] = (WINDOW, LAYOUT, WORKSPACE, APP, SYSTEM, SERVICE)
_RANK: dict[str, int] = {source: index for index, source in enumerate(ORDER)}

# How many options one source may take, so that a long list from one place cannot starve
# every other place. The quotas sum to more than MAX_OPTIONS on purpose: they are a
# ceiling per source, and the global cut below them is what decides the final list.
QUOTA: dict[str, int] = {
    WINDOW: 14,
    LAYOUT: 4,
    WORKSPACE: 10,
    APP: 12,
    SYSTEM: 8,
    SERVICE: 14,
}

# docs/STRATEGY.md 2.4: plan for 50 to 60 candidates, not 120. `MAX_CANDIDATES` is the
# same 50, decided in `adapters/base.py` for the same measurement, so it is imported
# rather than written again.
MAX_OPTIONS = base.MAX_CANDIDATES
# Tokens the option block may cost. Half of `config.Jev.token_cap`, which leaves the other
# half for the utterance, the variants, the note and the questions themselves. Measured
# above: 43 real options cost 552 tokens, and 60 worst case labels cost 1,246, so the
# budget is what stops the worst case rather than the ordinary one.
OPTION_BUDGET = 900
# whatever `Drift` says, a list this small is still worth sending
MIN_BUDGET = 200

# A spoken word must reach this to count as naming a word of an option. This is
# `controls.SAID_FLOOR` and `lexicon._CORROBORATE`, not the lower matching floor, for the
# reason `controls.py` gives: here the spoken word is the only thing that puts an option
# in front of the model, so a near miss must not.
SAID_FLOOR = 0.85
# below this, only an exact word counts: every three letter word sounds like every other
MIN_FUZZY_LETTERS = 4
# a source that has no recency to report sits behind everything that does
NOT_RECENT = 99


# --------------------------------------------------------------------------- what is asked


@dataclass(frozen=True)
class Machine(Context):
    """`adapters.base.Context`, plus the four things only a full harvest needs.

    It extends rather than replaces `Context` so that `adapters.base.offer` can be handed
    this object unchanged: an adapter sees exactly the state, the utterance and the clock
    it saw before, and learns nothing new about the harvest it is part of.

    `adapters` is deliberately empty by default. Building them here would mean a unit
    test that forgot to pass one would open the session bus, and it would also throw away
    the `Minted` handles an adapter issued for the previous utterance. The daemon builds
    them once with `adapters.live(cfg)`, which is where `Mpris` gets the owner's privacy
    settings, and holds them for its lifetime.

    `drift` is the same reason: the estimator's error is a running measurement over the
    whole session, so the caller owns one `Drift` and feeds it every reported token count.
    """

    lexicon: LexiconLike | None = None
    cfg: Config = field(default_factory=Config)
    adapters: tuple[Adapter, ...] = ()
    drift: tokens.Drift = field(default_factory=tokens.Drift)


@dataclass(frozen=True)
class Ranked:
    """One option with the three numbers that ordered it, kept so `hyprsay inspect` can
    show why an option is where it is instead of asserting that it belongs there."""

    affordance: Affordance
    source: str
    said: float = 0.0
    recency: int = NOT_RECENT

    @property
    def key(self) -> tuple[float, int, int, str]:
        """What was said, then recency and focus, then kind (docs/STRATEGY.md 2.4).

        The id is the last term so that two options that agree on everything else always
        come out in the same order: a harvest that reshuffled itself would make the eval
        suite unreproducible for no gain.
        """
        return (-self.said, self.recency, _RANK.get(self.source, len(ORDER)), self.affordance.id)


@dataclass(frozen=True)
class Harvest:
    """The ranked list, and an honest account of what did not fit in it."""

    ranked: tuple[Ranked, ...] = ()
    built: int = 0
    # source -> how many options that source built and did not get to offer
    dropped: tuple[tuple[str, int], ...] = ()
    estimated: int = 0  # tokens the options will add to the request
    budget: int = OPTION_BUDGET

    @property
    def offered(self) -> tuple[Affordance, ...]:
        return tuple(entry.affordance for entry in self.ranked)

    def by_id(self, ident: str) -> Affordance | None:
        """The option a chosen id names, or None. The only way back from an answer."""
        return next((e.affordance for e in self.ranked if e.affordance.id == ident), None)

    def options(self) -> dict[str, dict[str, str]]:
        """The `jev.types.Choice` options, and nothing else. No handle, no tier, no
        address: `Affordance.as_option` decides that, and this only keys it."""
        return {entry.affordance.id: entry.affordance.as_option() for entry in self.ranked}

    def note(self) -> str:
        """One plain sentence for `hyprsay inspect`, naming every drop.

        docs/STRATEGY.md 2.4: a cap that does not say it capped is a lie, so this sentence
        exists whether or not anything was dropped.
        """
        head = f"{len(self.ranked)} of {self.built} options, about {self.estimated} tokens"
        if not self.dropped:
            return f"{head}, nothing cut"
        cut = ", ".join(f"{count} {source}" for source, count in self.dropped)
        return f"{head}, cut {cut}"


# --------------------------------------------------------------------------- what was said


def spoken(heard: str) -> tuple[str, ...]:
    """The utterance as plain words. `adapters.base.words` already sanitizes and folds."""
    return tuple(base.words(heard))


def _matched(token: str, word: str) -> float:
    """How well one spoken token names one word, 0 or SAID_FLOOR to 1.

    Exact first, so that a digit works: `lexicon.similarity` strips everything that is not
    a letter, so "3" against "3" would otherwise score zero and no spoken workspace number
    would ever rank its own workspace.
    """
    if token == word:
        return 1.0
    if min(len(token), len(word)) < MIN_FUZZY_LETTERS:
        return 0.0
    if phonetic_key(token)[:1] != phonetic_key(word)[:1]:
        return 0.0
    heard = similarity(token, word)
    return heard if heard >= SAID_FLOOR else 0.0


def said(words: Sequence[str], groups: Sequence[Sequence[str]]) -> float:
    """How much of an option's own vocabulary the speaker used, 0 to 1.

    A group is a set of spellings of one idea ("up", "louder", "raise"), so a group scores
    by its best member and the option scores by the mean over its groups. That is what
    makes "turn the volume up" name volume-up at 1.0 and mute at 0.0 while "mute" does the
    opposite, with two short tuples instead of a grammar.

    The vocabularies passed here are written in this repository or are a workspace number
    the compositor assigned. A label an application wrote goes to `claims` instead.
    """
    if not words or not groups:
        return 0.0
    hits = [
        max((_matched(word, name) for word in words for name in group), default=0.0)
        for group in groups
    ]
    return round(sum(hits) / len(hits), 4)


def claims(words: Sequence[str], label: str) -> float:
    """How much of what the speaker said an UNTRUSTED label accounts for, 0 to 1.

    The direction is reversed on purpose. `said` asks whether the speaker used this
    option's own words, which only works when this repository wrote them. A track name
    came off the internet, so the honest question is the other one: of the words the
    speaker actually said, how many does this label explain.

    THE RULE, which is `controls.best_match`'s rule. A label may win a SLOT in the list
    and may never win a tier, an authorization or a corroboration: `adapters.base.candidates`
    forces `trust=UNTRUSTED` on every one of these, `adapters.base.tier_for` lets the
    label raise its tier and never lower it, and the handle behind it is only ever
    dereferenced by the adapter that minted it. The worst a hostile label can do here is
    push another option out of the list, and `Harvest.dropped` counts that out loud.
    """
    named = [w for w in base.words(label) if not mixed_script(w)]
    if not words or not named:
        return 0.0
    hits = sum(1 for token in words if any(_matched(token, name) > 0.0 for name in named))
    return round(hits / len(words), 4)


# --------------------------------------------------------------------------- one option


def _capability(
    intent: Intent, kind: str, summary: str, context: Machine, action: Action | None
) -> Capability:
    """The capability a family of options comes from, with its tier asked of `nlu/tiers`.

    Asked rather than stored, because a tier is a function of (intent, action, state, cfg)
    and not a constant on the intent (docs/PLAN.md 5.6). Holding the answer in a constant
    here would be the second tier table this file must not have.
    """
    return Capability(
        id=intent.value,
        summary=summary,
        tier=tiers.tier(intent, action, context.state, context.cfg),
        kind=kind,
    )


def _option(
    capability: Capability, ident: str, label: str, handle: str, trust: Trust
) -> Affordance:
    """One `Affordance`, with its tier taken through `adapters.base.tier_for`.

    Through `tier_for` and not from the capability directly, so that a label carrying a
    homoglyph raises what the option costs. That matters for a window, whose label is
    partly its class, and its class is written by the window's own owner.
    """
    label = sanitize(label)
    return Affordance(
        id=ident,
        label=label,
        kind=capability.kind,
        tier=base.tier_for(capability, label),
        handle=handle,
        adapter=SELF,
        capability=capability.id,
        trust=trust,
    )


# --------------------------------------------------------------------------- the compositor


def _place(window: Window) -> str:
    """Where a window is, as this module's own text when it can be.

    An ordinary workspace is named by its number, which the compositor assigns and this
    file formats, so the label stays text nobody outside the machine wrote. A special
    workspace has no number and is named by whoever created it.
    """
    return str(window.workspace_id) if window.workspace_id > 0 else sanitize(window.workspace_name)


def _window_label(window: Window, lexicon: LexiconLike | None) -> tuple[str, Trust]:
    """What a window is called, and who vouched for the name.

    A window whose class routes back to a trusted `.desktop` entry is labelled by that
    entry's `Name`, which is root owned text (docs/PLAN.md 5.6), so it is TRUSTED. Any
    other window is labelled by the class its own owner set, so it is UNTRUSTED. The title
    is never read, here or anywhere else in the ranking.
    """
    app = lexicon.app_for_window(window) if lexicon is not None else None
    if app is not None and app.trusted:
        return f"{app.name} on workspace {_place(window)}", Trust.TRUSTED
    own = window.cls or window.initial_class or "a window"
    return f"{own} on workspace {_place(window)}", Trust.UNTRUSTED


def _windows(context: Machine) -> list[Ranked]:
    """Every open window as a focus target, ranked by the lexicon and by recency.

    `Lexicon.match_windows` is the ranker because it already enforces the trust order: a
    window is found through its trusted app or through its own class, and never through
    its title. Windows it did not score are still offered, at 0, because "go to the other
    one" names no app and still has to be answerable.
    """
    state = context.state
    if not state.windows:
        return []
    capability = _capability(
        Intent.FOCUS_WINDOW, WINDOW, "go to a window that is already open", context, None
    )
    scores: dict[str, float] = {}
    if context.lexicon is not None:
        found = context.lexicon.match_windows(context.heard, state, limit=len(state.windows))
        scores = {window.address: score for window, score in found}
    out: list[Ranked] = []
    for index, window in enumerate(sorted(state.windows, key=lambda w: w.focus_rank), 1):
        label, trust = _window_label(window, context.lexicon)
        option = _option(capability, f"w{index}", label, window.address, trust)
        out.append(Ranked(option, WINDOW, scores.get(window.address, 0.0), window.focus_rank))
    return out


def _workspaces(context: Machine) -> list[Ranked]:
    """Every workspace as a switch target.

    A workspace that carries a name someone chose is UNTRUSTED for the same reason a
    window class is: `hyprctl` renames a workspace and any local process may call it. The
    number is this file's own text and is TRUSTED.
    """
    state = context.state
    if not state.workspaces:
        return []
    capability = _capability(Intent.SWITCH_WORKSPACE, WORKSPACE, "go to a workspace", context, None)
    words = spoken(context.heard)
    out: list[Ranked] = []
    for workspace in sorted(state.workspaces, key=lambda w: (w.special, w.id)):
        named = bool(workspace.name) and workspace.name != str(workspace.id)
        label = f"workspace {workspace.name}" if named else f"workspace {workspace.id}"
        # a special workspace is dispatched by its name, an ordinary one by its number,
        # which is what `ops.workspace_selector` is handed
        handle = workspace.name if workspace.special else str(workspace.id)
        if not handle:
            continue
        spellings = (str(workspace.id), workspace.name) if named else (str(workspace.id),)
        score = said(words, (("workspace", "desktop"), spellings))
        trust = Trust.UNTRUSTED if named else Trust.TRUSTED
        option = _option(capability, f"ws{workspace.id}", label, handle, trust)
        out.append(Ranked(option, WORKSPACE, score))
    return out


# The layout operations that only mean something when a window is focused, each with the
# words this repository uses for it. `Intent` is the id, so the caller rebuilds the Action
# with `Intent(affordance.capability)` and needs no table of its own.
#
# The summaries are short because they are labels, and a label is the one thing in an
# option that grows without limit: measured below, the scaffolding around one option costs
# about 13 tokens and every 3 characters of label costs 1 more. "fullscreen the focused
# window" says as much to a chooser as a sentence would and costs a third of it.
LAYOUT_OPS: tuple[tuple[Intent, str, tuple[tuple[str, ...], ...]], ...] = (
    (
        Intent.FULLSCREEN,
        "fullscreen the focused window",
        (("fullscreen", "full", "maximize", "maximise"),),
    ),
    (
        Intent.TOGGLE_FLOATING,
        "float or tile the focused window",
        (("float", "floating", "tile", "tiled"),),
    ),
    (
        Intent.RESIZE_WINDOW,
        "resize the focused window",
        (("resize", "bigger", "smaller", "grow", "shrink", "wider", "narrower"),),
    ),
    (
        Intent.MOVE_WINDOW,
        "move the focused window",
        (("move", "push", "shove"),),
    ),
)


def _layout(context: Machine) -> list[Ranked]:
    """The focused window as the subject of the layout verbs.

    Offered only when something is focused, because every one of these takes a window and
    `ops._require_window` refuses without one: an option that can only be refused is not
    an option, it is a trap for the ranker.
    """
    focused = context.state.focused
    if focused is None:
        return []
    words = spoken(context.heard)
    out: list[Ranked] = []
    for intent, summary, groups in LAYOUT_OPS:
        action = Action(intent, window=focused)
        capability = _capability(intent, LAYOUT, summary, context, action)
        option = _option(capability, intent.value, summary, focused.address, Trust.TRUSTED)
        # recency 0: these act on the window the speaker is looking at, which is the most
        # recent thing there is
        out.append(Ranked(option, LAYOUT, said(words, groups), 0))
    return out


# --------------------------------------------------------------------------- applications


def _app_label(app: App) -> str:
    kind = sanitize(app.kind or app.generic_name)
    return f"{app.name}, {kind}" if kind else app.name


def _apps(context: Machine) -> list[Ranked]:
    """The installed applications the words actually reach, as launch targets.

    Not every installed application: 236 entries were counted on this machine and the
    whole request budget is 1,800 tokens. `Lexicon.match_apps` is the filter as well as
    the ranker, and it is the right one because it scores only against trusted fields of
    a `.desktop` file and drops everything below its own matching floor. An utterance that
    names no application therefore offers none, which is correct rather than thrifty:
    "play the next track" has no launch in it.

    An application whose `.desktop` file is not trusted is offered UNTRUSTED rather than
    hidden. `ops._launch_app` refuses it by name and says which file wants approving,
    which is a better answer than an application the owner installed being invisible.
    """
    if context.lexicon is None:
        return []
    capability = _capability(Intent.LAUNCH_APP, APP, "start an application", context, None)
    out: list[Ranked] = []
    for index, (app, score) in enumerate(context.lexicon.match_apps(context.heard, QUOTA[APP]), 1):
        trust = Trust.TRUSTED if app.trusted else Trust.UNTRUSTED
        option = _option(capability, f"a{index}", _app_label(app), app.id, trust)
        out.append(Ranked(option, APP, score))
    return out


# --------------------------------------------------------------------------- system controls

# `ops.volume_argv` and `ops._MEDIA` own the verbs; this is the same set with the words
# the ranker needs beside each one. A verb that is not in `ops` cannot be offered, which
# is why "toggle" is absent: the grammar never produces it and nothing should learn to.
VOLUME_VERBS: tuple[tuple[str, str, tuple[tuple[str, ...], ...]], ...] = (
    ("up", "volume up", (("volume", "sound"), ("up", "louder", "raise", "increase"))),
    ("down", "volume down", (("volume", "sound"), ("down", "quieter", "lower", "decrease"))),
    ("mute", "mute the sound", (("mute", "silence"),)),
    ("unmute", "unmute the sound", (("unmute",),)),
    ("set", "set volume to a percentage", (("volume", "sound"), ("set", "percent"))),
)
MEDIA_VERBS: tuple[tuple[str, str, tuple[tuple[str, ...], ...]], ...] = (
    ("play_pause", "play or pause", (("play", "pause", "resume"),)),
    ("next", "next track", (("next", "skip"), ("track", "song"))),
    ("previous", "previous track", (("previous", "back"), ("track", "song"))),
)


def _system(context: Machine) -> list[Ranked]:
    """Volume and media transport, the two things hyprsay drives that are not a window.

    Always offered, because neither needs anything to be open: the sound card is there
    whether or not a player is. Which PLAYER a transport verb reaches is the MPRIS
    adapter's question, and it answers it with one option per live player, so these two
    families sit beside each other in the list rather than one shadowing the other.
    """
    words = spoken(context.heard)
    out: list[Ranked] = []
    for intent, verbs, prefix in (
        (Intent.VOLUME, VOLUME_VERBS, "volume"),
        (Intent.MEDIA, MEDIA_VERBS, "media"),
    ):
        capability = _capability(intent, SYSTEM, f"{prefix} control", context, None)
        for verb, summary, groups in verbs:
            option = _option(capability, f"{prefix}_{verb}", summary, verb, Trust.TRUSTED)
            out.append(Ranked(option, SYSTEM, said(words, groups)))
    return out


# --------------------------------------------------------------------------- adapters


def _services(context: Machine) -> list[Ranked]:
    """Whatever the running applications say they can do, through `adapters.base.offer`.

    `offer` is the whole integration: it runs `can_serve`, drops a capability above
    `MAX_OFFERABLE_TIER`, forces `trust=UNTRUSTED` on everything `resolve` returned, and
    never raises, so an adapter that breaks costs its own options and not the utterance.
    This function adds exactly two things: the renumbering the module docstring argues
    for, and a rank.
    """
    if not context.adapters:
        return []
    words = spoken(context.heard)
    out: list[Ranked] = []
    for index, found in enumerate(base.offer(context.adapters, context, limit=QUOTA[SERVICE]), 1):
        # a capability offered as itself carries a summary written in this repository, so
        # its own words rank it; a resolved candidate carries text from a service
        score = (
            said(words, tuple((w,) for w in base.words(found.label)))
            if found.trust is Trust.TRUSTED
            else claims(words, found.label)
        )
        out.append(Ranked(replace(found, id=f"x{index}"), SERVICE, score))
    return out


# --------------------------------------------------------------------------- the cut


def _wire(offered: Sequence[Affordance]) -> dict[str, object]:
    """The options as `nlu/requests.wire` will carry them, and nothing else."""
    return {
        "step": {
            "type": "choice",
            "instructions": "",
            "criteria": {option.id: option.as_option() for option in offered},
        }
    }


def cost(offered: Sequence[Affordance]) -> int:
    """What carrying these options adds to a request, in tokens.

    The marginal cost, measured by subtracting an empty Choice from a full one, because
    `jev.tokens.estimate` includes a fixed per-request overhead and a per-question term
    that belong to the request and not to the options. It is the project's own estimator
    on purpose: the same function the client refuses an oversized request with, so
    `Drift` measures one number rather than two.
    """
    return max(tokens.estimate({}, _wire(offered)) - tokens.estimate({}, _wire(())), 0)


def budget_for(drift: tokens.Drift, budget: int = OPTION_BUDGET) -> int:
    """The token budget, shrunk by however much the estimator has been running low.

    `Drift.ratio` above 1 means the API reported more tokens than were estimated, which
    `jev/tokens.py` names as the dangerous direction: requests believed to be under the
    cap are not. Dividing by it turns a measured under-count into a smaller list rather
    than into an HTTP 503, and it does nothing at all until a real request has reported a
    count, which is what "wire Drift in from the first request" has to mean to be worth
    anything.
    """
    ratio = drift.ratio if drift.samples else 1.0
    return max(int(budget / max(ratio, 1.0)), MIN_BUDGET)


def _cut(
    built: Sequence[Ranked], limit: int, budget: int
) -> tuple[tuple[Ranked, ...], tuple[tuple[str, int], ...]]:
    """Quota per source, then one global rank, then the token budget. Counting every drop.

    Three cuts and not one, because they answer three different questions. The quota stops
    one source starving the rest, which is the only defence against a service returning
    fifty tracks. The global cut is the candidate count docs/STRATEGY.md 2.4 asks for. The
    budget is the only one that knows about tokens, and it runs last because it is the
    only one whose answer depends on what survived the other two.
    """
    dropped: Counter[str] = Counter()
    kept: list[Ranked] = []
    for source in ORDER:
        group = sorted((r for r in built if r.source == source), key=lambda r: r.key)
        quota = QUOTA.get(source, limit)
        dropped[source] += max(len(group) - quota, 0)
        kept.extend(group[:quota])
    # a source nobody declared an order for still has to be counted rather than vanish
    for ranked in built:
        if ranked.source not in _RANK:
            kept.append(ranked)
    ranked_all = sorted(kept, key=lambda r: r.key)
    for extra in ranked_all[limit:]:
        dropped[extra.source] += 1
    ranked_all = ranked_all[:limit]
    while ranked_all and cost([r.affordance for r in ranked_all]) > budget:
        dropped[ranked_all[-1].source] += 1
        ranked_all.pop()
    return tuple(ranked_all), tuple(sorted((s, n) for s, n in dropped.items() if n))


# --------------------------------------------------------------------------- the harvest


def harvest(context: Machine, *, limit: int = MAX_OPTIONS, budget: int | None = None) -> Harvest:
    """One ranked list of everything the machine can do right now.

    The single entry point, and the whole of docs/STRATEGY.md 2.2: code enumerates, the
    model selects. Every option came from live state a moment ago, every tier was decided
    by the module that owns the gesture, and nothing an application wrote can add an
    option, raise its own standing above a trusted one at the same score, or lower what it
    costs.

    Never raises. A lexicon that throws or an adapter that hangs up costs its own options
    and no more, for the reason `adapters.base.offer` gives: a broken source must degrade
    the list, never take the utterance down with it.
    """
    built: list[Ranked] = []
    for source, gather in (
        (WINDOW, _windows),
        (WORKSPACE, _workspaces),
        (LAYOUT, _layout),
        (APP, _apps),
        (SYSTEM, _system),
        (SERVICE, _services),
    ):
        try:
            built.extend(gather(context))
        except Exception as exc:  # one source must not cost the whole harvest
            log.debug("the %s affordances could not be built: %s", source, exc)
    allowed = budget_for(context.drift) if budget is None else budget
    ranked, dropped = _cut(built, limit, allowed)
    return Harvest(
        ranked=ranked,
        built=len(built),
        dropped=dropped,
        estimated=cost([r.affordance for r in ranked]),
        budget=allowed,
    )


__all__ = [
    "APP",
    "LAYOUT",
    "MAX_OPTIONS",
    "OPTION_BUDGET",
    "ORDER",
    "QUOTA",
    "SELF",
    "SERVICE",
    "SYSTEM",
    "WINDOW",
    "WORKSPACE",
    "Harvest",
    "Machine",
    "Ranked",
    "budget_for",
    "claims",
    "cost",
    "harvest",
    "said",
    "spoken",
]
