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
    a wheel notch and the view-moving      `nlu/tiers.tier(Intent.SCROLL | PRESS_CHORD,
      chords, aimed at the focused window   ...)`, which is `inapp.chord_tier`        0 or 1
    the recipes each window offers         `nlu/tiers.tier(Intent.RUN_RECIPE, ...)`, which
                                            is `recipes.tier_for` raised to the typing
                                            tier when the recipe types             0 or 1
    the controls of the focused window,    `nlu/tiers.tier(Intent.CLICK_CONTROL, ...)`,
      read from a cache, never walked       which is `controls.click_tier`               1

There is no second tier table in this file and there must never be one. An untrusted
label may RAISE a tier and may never lower one, which is `adapters.base.tier_for`'s rule
and `controls.click_tier`'s rule, applied here by calling them rather than by copying
them.

WHAT IS NOT HARVESTED, AND WHY. Closing a window is absent: it is tier 2, it needs its
verb said out loud (`nlu/tiers.LITERAL_VERBS`), and an option list is not evidence from
the speaker. Nothing above tier 1 is offered by the three in-app sources either, for the
same sentence: `nlu/tiers.verdict` refuses a tier 2 act that has no literally spoken verb
behind it, and `understand._step` drops a whole PLAN the moment one of its steps is above
tier 1. So a tier 2 option cannot be carried out from a list, it can only cost the
utterance the list was built for, which is `_layout`'s rule in this same file: an option
that can only be refused is not an option, it is a trap for the ranker.

IN-APP REACH, WHICH THIS FILE USED TO REFUSE TO HARVEST (reversed 2026-09-22). The old
argument was measured and it was about the WALK, not about the options: `controls.py`
records KCalc's 48 controls taking 16.2 s, the walk here is 3.8 s on the live ZapZap
window, and the harvest has 250 ms for the whole desktop (`nlu/understand.HARVEST_S`), so
a harvest that walked the accessibility tree would eat the utterance it was built for.
What was wrong was the conclusion. Without these sources the model is asked to drive a
desktop and told nothing about what is inside any window on it, which is the one thing
hypruse exists to provide, so the three sources are here and the walk is still not:

- `controls.cached` READS AND NEVER WALKS. A cold cache means this utterance offers no
  control options, which is a correct answer and never an error. `daemon._warm_controls`
  is what fills it, on a worker, at the focus change and at key down, leaning on the
  behaviour `controls.py` already had: a walk nobody is waiting for still lands in the
  cache. Measured warm, this source costs 0.3 ms.
- a recipe and a gesture need no tree at all. They are a table in this repository and the
  keyboard, which is why `recipes.py` and `inapp.py` exist in the first place.

THE TRUST RULE FOR A CONTROL NAME, which is the whole reason this was excluded second.
`Control.name` is written by the application, and inside a browser by the page, and
`Control.__post_init__` forces `trusted` to False so that no code can say otherwise. So
every control option is UNTRUSTED, and:

- a control name may win a SLOT and never a tier. The tier comes from
  `nlu/tiers.tier(Intent.CLICK_CONTROL, ...)`, which is `controls.click_tier`, which
  reads the name only to RAISE what a click costs. A page that names its button "Delete"
  pushes it to tier 2 and thereby out of the list entirely: a page can silence its own
  button and can never add one, or make one cheaper;
- a control name never selects the window. Controls are read for the focused window
  alone, every option carries that window's own address in its handle, and no source here
  may build an option for a window it did not read;
- a control the speaker did not name is never offered, which is `controls.best_match`'s
  rule, called rather than copied. The gate is STRICTER here than on the click path,
  because the harvest scores against the whole utterance while the click path scores
  against the span the grammar cut out, and strict is the direction an untrusted label
  has to fail in;
- a name is never an option id. Ids are `c1`, `c2`, minted here, for the reason the
  adapter renumbering below gives: an id is a dict KEY in the request, and application
  text does not become a key.

WHAT PUTS AN IN-APP OPTION IN THE LIST AT ALL. The speaker's own words, exactly as with
`_apps`: an utterance that names no application offers no launch, and an utterance that
reaches for nothing inside a window offers no scroll, no recipe and no control. That is
not thrift. "Go to chrome" has no scroll in it, and an in-app option in front of a model
that was not asked for is an invitation to reach into an application instead of doing
what was said. It is also what keeps these three sources free: on an utterance about
windows they build nothing and cost nothing.

THE HARD CONSTRAINT, AND THE NUMBERS BEHIND THE CUT. Jev takes at most 255 options and
this project caps a request at 1,800 tokens, because latency and HTTP 503s both climb
past about 2.2k (docs/research/live/, `jev/tokens.py`). The binding limit is therefore
the payload, never the 255. Measured on this machine's live desktop, read only, on
2026-09-22, with `jev.tokens.estimate` on the wire shape `nlu/requests.wire` builds:

    the desktop: 10 open windows, 4 workspaces, kitty focused; 234 .desktop files in
      /usr/share/applications, of which the lexicon keeps 93 launchable and 83 trusted
    a harvest with nothing said builds 10 + 4 + 4 + 0 + 8 = 26 options at 683 tokens
    "go to chrome" builds 27, at 709 tokens, with 3 windows and 1 app above 0.9
    the whole harvest takes 1.4 to 2.2 ms p50, 5.6 ms on the first cold call
    labels come out at 20.3 characters on average against the 60 character cap

    ONE option costs 17 tokens of scaffolding plus about 1 token per 3 characters of
      label: 24 tokens at that measured average, 37 at the 60 character cap
    so 50 options cost 1,241 tokens at the real average and 1,919 at the cap, and the
      worst case alone is larger than the entire 1,800 token request
    the rest of a request (fixed overhead, utterance, variants, `bank.NOTE`) measured
      379 tokens, which leaves about 1,420 for options

WHAT THE THREE IN-APP SOURCES COST, measured the same way on 2026-09-22 against the same
live desktop, 9 windows, ZapZap focused with its 16 controls in the cache, this machine
under its ordinary load of a dozen agent sessions (so these are pessimistic):

    nothing said, or an utterance about windows: +0.1 ms and +0 options. The three
      sources build nothing at all, which is the point of the paragraph above
    "scroll down", "click reply", "zoom in": the whole harvest goes from 5.5 to 9.1 ms
      p50 to 6.8 to 12.1 ms p50, worst single call of 25 runs 101 ms, against the 250 ms
      deadline `nlu/understand.HARVEST_S` gives the whole thing
    per source, warm: controls 0.6 to 3.5 ms, recipes 0.1 to 3.2 ms, gestures 0.02 to
      1.1 ms. `controls.cached` itself is 6 microseconds
    an in-app option is 22 to 26 tokens, the same as every other option, because the
      labels are the same length: "scroll down in this window", "Reply, push button"

That last pair of numbers is why 50 is a ceiling and not a target. It is also why
`harvest` takes `copies`: docs/STRATEGY.md 2.2 asks `step_1`, `step_2` and `step_3` to be
a Choice over the SAME list, and `requests._wire_one` inlines a Choice's options inside
each question, so three steps in one request carry three copies and cost three times.
A caller that puts all three in one request passes `copies=3`; a caller that shards them
across the fan-out, which costs about 45 ms for three on one warm connection, passes 1.

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

from hyprsay import controls, inapp, recipes
from hyprsay.adapters import base
from hyprsay.adapters.base import Adapter, Affordance, Capability, Context, Trust
from hyprsay.config import Config
from hyprsay.jev import tokens
from hyprsay.lexicon import mixed_script, phonetic_key, sanitize, similarity
from hyprsay.model import Action, App, DesktopState, Direction, Intent, Window
from hyprsay.nlu import tiers
from hyprsay.nlu.resolve import LexiconLike

log = logging.getLogger(__name__)

# The adapter name every option code in this repository owns carries. `adapters.base.find`
# looks an adapter up by this name and finds none, which is what tells the caller that the
# option is performed by `ops.py` rather than by an adapter.
SELF = "hyprsay"

WINDOW = "window"
LAYOUT = "layout"
GESTURE = "gesture"
WORKSPACE = "workspace"
APP = "app"
SYSTEM = "system"
SERVICE = "service"
RECIPE = "recipe"
CONTROL = "control"
CLOSE = "close"
HERE = "here"
BROWSER = "browser"

# The last tie break, after what was said and after recency: the cheapest and most
# reversible source first, which is the same order `nlu/tiers.py` puts the intents in.
# The two in-app sources that press keys or click come last, and `CONTROL` is the very
# last of all, for the reason `controls.click_tier` gives: a click can send a message,
# spend money or delete a file, and there is no inverse for any of it.
ORDER: tuple[str, ...] = (
    WINDOW,
    LAYOUT,
    GESTURE,
    WORKSPACE,
    APP,
    SYSTEM,
    SERVICE,
    BROWSER,
    RECIPE,
    CONTROL,
    CLOSE,
    HERE,
)
_RANK: dict[str, int] = {source: index for index, source in enumerate(ORDER)}

# How many options one source may take, so that a long list from one place cannot starve
# every other place. The quotas sum to more than MAX_OPTIONS on purpose: they are a
# ceiling per source, and the global cut below them is what decides the final list.
#
# The three in-app quotas are the tightest, and the reason is the budget rather than the
# danger: a browser window alone offers 13 recipes and a warm window can hold 60 controls,
# while three copies of the list leave room for about 16 options in all (`budget_for`,
# `copies`). They are also the sources that only appear when they were asked for.
QUOTA: dict[str, int] = {
    WINDOW: 14,
    LAYOUT: 4,
    GESTURE: 6,
    WORKSPACE: 10,
    APP: 12,
    SYSTEM: 8,
    SERVICE: 14,
    RECIPE: 6,
    CONTROL: 6,
    CLOSE: 8,
    HERE: 8,
    # tabs, sites and what is on the page in front: its own quota, so a page full of
    # links cannot starve the media players, and the players cannot starve the page
    BROWSER: 24,
}

# The highest tier an option may carry. Not `base.MAX_OFFERABLE_TIER`, which is 2 and is
# about what may be SHOWN to a model: this is about what a model's answer can actually
# authorize, and a tier 2 act needs a verb the speaker said out loud and an explicit
# target, neither of which picking an id out of a list can be. Applied to the three
# in-app sources, which are the only ones that can produce a tier 2 option from live
# state; see "WHAT IS NOT HARVESTED, AND WHY".
MAX_INAPP_TIER = 1

# docs/STRATEGY.md 2.4: plan for 50 to 60 candidates, not 120. `MAX_CANDIDATES` is the
# same 50, decided in `adapters/base.py` for the same measurement, so it is imported
# rather than written again.
MAX_OPTIONS = base.MAX_CANDIDATES
# Tokens ONE copy of the option block may cost. The measurement above: the rest of a
# request is 379 tokens against a 1,800 cap, so 1,420 are free; 1,200 leaves 220 of
# headroom for a longer utterance, more variants and a bigger note. At the measured
# 24 tokens an option that is about 50 options, and at the 37 token worst case about 32.
OPTION_BUDGET = 1200
# whatever `Drift` and `copies` say, a list this small is still worth sending
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
# where a repeated gesture ranks: above everything the new words did not reach
AGAIN_SCORE = 0.5


# --------------------------------------------------------------------------- what is asked


@dataclass(frozen=True)
class Previous:
    """What the last command did, as much as the harvest needs: its intent, the words it
    was said in, and the window it touched. Built by the understander from its own turn
    memory (`nlu/context.Turn`), so this module does not reach into that one."""

    intent: str = ""
    said: str = ""
    address: str = ""
    # the trusted name of the application it acted on. A launch has no window yet when
    # it is remembered, so this is how "focus on it" finds the window that appeared.
    app: str = ""


# the words that ask for a window to be closed. `nlu/tiers.LITERAL_VERBS` is the table;
# it is read, not copied, so the harvest and the verdict can never disagree about it.
CLOSING_VERBS = tiers.LITERAL_VERBS[Intent.CLOSE_WINDOW]
# the word that asks for a window to come to the workspace in front. "bring" alone is
# not it: "bring up my notes" means show them, and was moving them instead
HERE_WORDS = frozenset({"here"})
# the words that send a window somewhere, the verdict's own table (`nlu/tiers.py`)
SENDING_VERBS = tiers.LITERAL_VERBS[Intent.MOVE_TO_WORKSPACE]
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}  # fmt: skip


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
    previous: Previous = field(default_factory=Previous)


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
    """How much of what the speaker said a free text label accounts for, 0 to 1.

    The direction is reversed on purpose. `said` asks whether the speaker used this
    option's own words, which only works against a short vocabulary written by hand. A
    track name or a one line capability summary is free text, so the honest question is
    the other one: of the words the speaker actually said, how many does this label
    explain.

    THE RULE, when that label is UNTRUSTED, which is `controls.best_match`'s rule. A label
    may win a SLOT in the list and may never win a tier, an authorization or a
    corroboration: `adapters.base.candidates` forces `trust=UNTRUSTED` on everything an
    adapter resolved, `adapters.base.tier_for` lets a label raise its tier and never lower
    it, and the handle behind it is only ever dereferenced by the adapter that minted it.
    The worst a hostile label can do here is push another option out of the list, and
    `Harvest.dropped` counts that out loud.
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


def _named_by(window: Window, lexicon: LexiconLike | None) -> tuple[str, Trust]:
    """What to call a window, and who vouched for the name.

    A window whose class routes back to a trusted `.desktop` entry is named by that
    entry's `Name`, which is root owned text (docs/PLAN.md 5.6), so it is TRUSTED. Any
    other window is named by the class its own owner set, so it is UNTRUSTED. The title
    is never read, here or anywhere else in the ranking.
    """
    app = lexicon.app_for_window(window) if lexicon is not None else None
    if app is not None and app.trusted:
        return app.name, Trust.TRUSTED
    return window.cls or window.initial_class or "a window", Trust.UNTRUSTED


def _window_label(window: Window, lexicon: LexiconLike | None) -> tuple[str, Trust]:
    """A window as a focus target: what it is called, and where it is."""
    name, trust = _named_by(window, lexicon)
    return f"{name} on workspace {_place(window)}", trust


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
    last = context.previous
    for index, window in enumerate(sorted(state.windows, key=lambda w: w.focus_rank), 1):
        label, trust = _window_label(window, context.lexicon)
        recency = window.focus_rank
        if _touched_last(window, last, context.lexicon):
            # "it", "that one": what the last command acted on is the likeliest thing a
            # pronoun means, so it ranks first and the label says why
            label = f"{label}, {'just opened' if last.intent == 'launch_app' else 'just used'}"
            recency = -1
        option = _option(capability, f"w{index}", label, window.address, trust)
        out.append(Ranked(option, WINDOW, scores.get(window.address, 0.0), recency))
    return out


def _touched_last(window: Window, last: Previous, lexicon: LexiconLike | None) -> bool:
    """Did the last command act on this window? By address when it named one, and by the
    trusted application name when it was a launch, whose window came afterwards."""
    if not last.intent:
        return False
    if last.address:
        return window.address == last.address
    if not last.app or lexicon is None:
        return False
    app = lexicon.app_for_window(window)
    return app is not None and app.trusted and app.name == last.app


def here_workspace(state: DesktopState) -> str:
    """The workspace in front: the compositor's own answer, else the focused window's."""
    if state.active_workspace_id:
        return str(state.active_workspace_id)
    focused = state.focused
    return str(focused.workspace_id) if focused is not None else ""


def _spoken_workspace(words: Sequence[str]) -> str:
    """The workspace number said after the word "workspace", or "" when none was."""
    for before, word in zip(words, words[1:], strict=False):
        if before != "workspace":
            continue
        if word.isdigit():
            return word
        if word in _NUMBER_WORDS:
            return str(_NUMBER_WORDS[word])
    return ""


def _sending(context: Machine) -> list[Ranked]:
    """Every window as something to send to the workspace the speaker named.

    Only when a sending verb and a workspace number were both said ("put the music on
    workspace five"). Without this, the only option that mentioned workspace 5 was
    switching to it, and that is what got chosen.
    """
    state = context.state
    words = spoken(context.heard)
    target = _spoken_workspace(words)
    if not state.windows or not target or not SENDING_VERBS & set(words):
        return []
    scores: dict[str, float] = {}
    if context.lexicon is not None:
        found = context.lexicon.match_windows(context.heard, state, limit=len(state.windows))
        scores = {window.address: score for window, score in found}
    out: list[Ranked] = []
    for index, window in enumerate(sorted(state.windows, key=lambda w: w.focus_rank), 1):
        if str(window.workspace_id) == target:
            continue
        action = Action(Intent.MOVE_TO_WORKSPACE, window=window, workspace=target)
        capability = _capability(
            Intent.MOVE_TO_WORKSPACE, WINDOW, "send a window to a workspace", context, action
        )
        name, trust = _named_by(window, context.lexicon)
        label = f"send {name} to workspace {target}"
        option = _option(capability, f"s{index}", label, f"{window.address} {target}", trust)
        out.append(Ranked(option, HERE, scores.get(window.address, 0.0), window.focus_rank))
    return out


def _here(context: Machine) -> list[Ranked]:
    """Every window on another workspace, as something to bring to this one.

    Only when the words ask for it ("move my browser here", "bring spotify"), for the
    reason `_closing` gives: a list of every window twice over would crowd out what was
    asked. The tier comes from `nlu/tiers.tier`, which makes a move that keeps the window
    in view tier 1.
    """
    state = context.state
    if not state.windows or not HERE_WORDS & set(spoken(context.heard)):
        return []
    current = here_workspace(state)
    scores: dict[str, float] = {}
    if context.lexicon is not None:
        found = context.lexicon.match_windows(context.heard, state, limit=len(state.windows))
        scores = {window.address: score for window, score in found}
    out: list[Ranked] = []
    for index, window in enumerate(sorted(state.windows, key=lambda w: w.focus_rank), 1):
        if str(window.workspace_id) == current:
            continue
        action = Action(Intent.MOVE_TO_WORKSPACE, window=window, workspace=current)
        capability = _capability(
            Intent.MOVE_TO_WORKSPACE, WINDOW, "bring a window to this workspace", context, action
        )
        name, trust = _named_by(window, context.lexicon)
        option = _option(capability, f"h{index}", f"bring {name} here", window.address, trust)
        out.append(Ranked(option, HERE, scores.get(window.address, 0.0), window.focus_rank))
    return out


def _closing(context: Machine) -> list[Ranked]:
    """Every open window as something to close, but only when closing was asked for.

    Tier 2, and it stays tier 2: `nlu/tiers.verdict` turns it into a countdown, and only
    when the verb is in the recognizer's own words. The same ranking as `_windows`, so
    "close the music player" puts Spotify first. Without the verb nothing is built, which
    keeps a list of close buttons away from every utterance that did not ask for one.
    """
    state = context.state
    if not state.windows or not CLOSING_VERBS & set(spoken(context.heard)):
        return []
    scores: dict[str, float] = {}
    if context.lexicon is not None:
        found = context.lexicon.match_windows(context.heard, state, limit=len(state.windows))
        scores = {window.address: score for window, score in found}
    out: list[Ranked] = []
    for index, window in enumerate(sorted(state.windows, key=lambda w: w.focus_rank), 1):
        action = Action(Intent.CLOSE_WINDOW, window=window)
        capability = _capability(Intent.CLOSE_WINDOW, WINDOW, "close a window", context, action)
        label, trust = _window_label(window, context.lexicon)
        option = _option(capability, f"q{index}", f"close {label}", window.address, trust)
        out.append(Ranked(option, CLOSE, scores.get(window.address, 0.0), window.focus_rank))
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
# The summaries are short because they are labels, and the label is the only part of an
# option whose size is a choice: measured, the scaffolding around one option costs 17
# tokens and every 3 characters of label costs 1 more. "fullscreen the focused window"
# says as much to a chooser as a sentence would and costs 7 tokens instead of 16.
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

    Not every installed application: 234 `.desktop` files were counted on this machine,
    93 of them launchable, against a whole request budget of 1,800 tokens and a measured
    24 tokens an option. `Lexicon.match_apps` is the filter as well as
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
            if intent is Intent.MEDIA and names_a_thing_to_play(words):
                # "play the second live version" is not a transport verb. Measured live,
                # `media_play_pause` held about a third of the mass beside the adapter
                # option that was actually right, which came back at 0.38 to 0.44 against
                # a floor of 0.5, so the plan read UNSURE and nothing happened. It is the
                # catch-all this bank's own docstring says must not sit beside real
                # options: "play" matches every one of them and distinguishes none. When
                # the speaker named WHAT to play, the transport verbs are not candidates.
                continue
            option = _option(capability, f"{prefix}_{verb}", summary, verb, Trust.TRUSTED)
            out.append(Ranked(option, SYSTEM, said(words, groups)))
    return out


# words that turn "play" from a transport verb into a request for something particular
ORDINAL_WORDS = frozenset(
    {"first", "second", "third", "fourth", "fifth", "last", "next", "previous", "other"}
)
_A_THING = frozenset(
    {
        "version",
        "song",
        "track",
        "album",
        "playlist",
        "artist",
        "by",
        "live",
        "remix",
        "cover",
        "acoustic",
        "episode",
        "podcast",
        "radio",
        "mix",
        "soundtrack",
    }  # fmt: skip
)


def names_a_thing_to_play(words: tuple[str, ...]) -> bool:
    """Did the speaker name something to play, rather than ask for play or pause?

    An ordinal counts on its own ("the second one"): nothing about pausing has a second.
    """
    if not words:
        return False
    if _A_THING & set(words):
        return True
    return any(w in ORDINAL_WORDS for w in words)


# --------------------------------------------------------------------------- adapters


def _services(context: Machine) -> list[Ranked]:
    """Whatever the running applications say they can do, through `adapters.base.offer`.

    `offer` is the whole integration: it runs `can_serve`, drops a capability above
    `MAX_OFFERABLE_TIER`, forces `trust=UNTRUSTED` on everything `resolve` returned, and
    never raises, so an adapter that breaks costs its own options and not the utterance.
    This function adds exactly two things: the renumbering the module docstring argues
    for, and a rank.
    """
    others = tuple(a for a in context.adapters if getattr(a, "name", "") != BROWSER)
    if not others:
        return []
    words = spoken(context.heard)
    out: list[Ranked] = []
    for index, found in enumerate(base.offer(others, context, limit=QUOTA[SERVICE]), 1):
        # `claims` and not `said`, whatever the trust: an adapter's label is free text
        # either way, a one line summary or a track name, and `said` only works against a
        # short hand written vocabulary. The trust of the label changes nothing here
        # because ranking is not authorization; see `claims`.
        out.append(Ranked(replace(found, id=f"x{index}"), SERVICE, claims(words, found.label)))
    return out


def _browser_adapter(context: Machine) -> Adapter | None:
    return next((a for a in context.adapters if getattr(a, "name", "") == BROWSER), None)


def _browser(context: Machine) -> list[Ranked]:
    """Tabs, sites, and what is on the page in front, from the browser extension.

    Renumbered `b1`, `b2` for `_services`' reason: an adapter's own id may carry page text.
    Ranked by what was said, then in the adapter's own order, which puts a list's items in
    the order the page shows them.
    """
    browser = _browser_adapter(context)
    if browser is None:
        return []
    words = spoken(context.heard)
    # "a bit more" after a page scroll names nothing: when the last command went through
    # the browser, what matches the words it was said in ranks first, like `_gestures`
    last = context.previous
    earlier = spoken(last.said) if last.intent == Intent.SERVICE.value else ()
    found = base.offer((browser,), context, limit=base.MAX_CANDIDATES)
    ranked: list[Ranked] = []
    for index, item in enumerate(found, 1):
        score = claims(words, item.label)
        again = claims(earlier, item.label) if earlier and score <= 0.0 else 0.0
        if again > 0.0:
            # graded, so "scroll ... down" after "scroll down" beats "scroll ... up"
            score = AGAIN_SCORE * again
        ranked.append(Ranked(replace(item, id=f"b{index}"), BROWSER, score, index))
    return ranked


def _extension_serves(context: Machine) -> bool:
    browser = _browser_adapter(context)
    try:
        return browser is not None and bool(browser.can_serve(context))
    except Exception:  # a broken adapter serves nothing
        return False


# ------------------------------------------------------------------- inside the window


def _handle(window: Window, payload: str) -> str:
    """An in-app option's handle: the window it was built for, then what it names inside.

    The address goes in the handle and never in the id, which is the rule the module
    docstring states for a window and for the same two reasons: an id is a dict KEY in the
    request, and an address does not leave the machine. A handle is read back here, by
    code, and is never sent anywhere.

    Carrying it is what makes "no source may build an option for a window it did not read"
    checkable by the caller rather than promised by this file: the address in the handle
    is the one the option was authorized against, and `understand._action_for` resolves it
    against fresh state or refuses.
    """
    return f"{window.address} {payload}".strip()


def target(handle: str) -> tuple[str, str]:
    """An in-app handle read back as (window address, what it names inside that window).

    Here rather than in the caller, so that reading a handle is one call and not the
    string surgery `Affordance.adapter` and `.capability` exist to avoid. A Hyprland
    address never contains a space, and `Affordance.__post_init__` has already run
    `sanitize` over the handle, which collapses every run of whitespace to one space and
    strips the ends, so the FIRST space is the seam whatever the application called its
    control.
    """
    address, _, inside = handle.partition(" ")
    return address, inside


def _kind(context: Machine, window: Window) -> str:
    """What `Lexicon.kind_of` calls this window's app, or "" when nothing trusted claims
    it. A harvest with no lexicon gets "", which `recipes.for_window` and
    `nlu/tiers.typing_refusal` both already read as "unknown", so a missing lexicon lands
    on the conservative answer those two already have rather than on a new one.
    """
    return context.lexicon.kind_of(window) if context.lexicon is not None else ""


def _reaches(words: Sequence[str], groups: Sequence[Sequence[str]]) -> float:
    """`said`, with the option's FIRST group of words required.

    `said` is the mean over the groups, so half a vocabulary scores 0.5, and a second
    group holding an ordinary word would let its option in on a sentence that was about
    something else: "put this in workspace 3" reaches zoom-in on the word "in", and
    "play the next track" reaches next-tab on "next". The first group of every table below
    is the distinctive one ("zoom", "tab", "scroll", "wikipedia"), and nothing gets into
    the list without it.
    """
    if not groups or said(words, groups[:1]) <= 0.0:
        return 0.0
    return said(words, groups)


def _reachable(
    intent: Intent,
    source: str,
    action: Action,
    summary: str,
    context: Machine,
    ceiling: int = MAX_INAPP_TIER,
) -> Capability | None:
    """The capability for one in-app option, or None when its tier puts it out of reach.

    The tier is asked of `nlu/tiers.tier`, which asks `inapp.scroll(...).tier`,
    `inapp.chord_tier`, `recipes.tier_for` and `controls.click_tier` in turn: the module
    that owns the gesture decides, here as everywhere else in this file. All this adds is
    the `MAX_INAPP_TIER` cut, which is about what an answer to a Choice can authorize.
    """
    capability = _capability(intent, source, summary, context, action)
    return capability if capability.tier <= ceiling else None


def _inapp_option(
    capability: Capability,
    ident: str,
    label: str,
    handle: str,
    trust: Trust,
    ceiling: int = MAX_INAPP_TIER,
) -> Affordance | None:
    """`_option`, refused when what comes back is still above `MAX_INAPP_TIER`.

    Two tiers have to be read, not one. `_reachable` reads the capability's, which is what
    the module that owns the gesture decided; this reads the option's, which is that one
    raised again by `adapters.base.tier_for` when the LABEL carries a homoglyph. A control
    name spelled with a Cyrillic "c" is a string built to be read as something it is not,
    and the answer to one is the same here as everywhere: it costs a tier more, and a tier
    2 act is not something an answer to a Choice can authorize.
    """
    option = _option(capability, ident, label, handle, trust)
    return option if option.tier <= ceiling else None


# The gestures offered for the focused window, each with the words this repository uses
# for it, exactly like LAYOUT_OPS. `payload` is what `inapp` expects: a `Direction` for a
# scroll, and a chord in `inapp.CHORD_TIERS`' own spelling for a press. Nothing here says
# what any of it costs, and there is no chord in this table that is not in that one.
GESTURES: tuple[tuple[Intent, str, str, tuple[tuple[str, ...], ...]], ...] = (
    (
        Intent.SCROLL,
        Direction.DOWN.value,
        "scroll down in this window",
        (("scroll", "wheel", "scrolling"), ("down", "downwards", "further")),
    ),
    (
        Intent.SCROLL,
        Direction.UP.value,
        "scroll up in this window",
        (("scroll", "wheel", "scrolling"), ("up", "upwards", "back")),
    ),
    (
        Intent.PRESS_CHORD,
        "Page_Down",
        "page down in this window",
        (("page",), ("down", "next", "forward")),
    ),
    (
        Intent.PRESS_CHORD,
        "Page_Up",
        "page up in this window",
        (("page",), ("up", "previous", "back")),
    ),
    (Intent.PRESS_CHORD, "Home", "go to the top", (("top", "beginning", "start"),)),
    (Intent.PRESS_CHORD, "End", "go to the bottom", (("bottom", "end"),)),
    (Intent.PRESS_CHORD, "ctrl+Tab", "the next tab", (("tab", "tabs"), ("next", "following"))),
    (
        Intent.PRESS_CHORD,
        "ctrl+shift+Tab",
        "the previous tab",
        (("tab", "tabs"), ("previous", "last", "back")),
    ),
    (Intent.PRESS_CHORD, "alt+Left", "go back a page", (("back", "backwards"),)),
    (Intent.PRESS_CHORD, "alt+Right", "go forward a page", (("forward", "forwards"),)),
    (Intent.PRESS_CHORD, "ctrl+r", "reload this page", (("reload", "refresh"),)),
)


def _gesture_action(intent: Intent, payload: str, window: Window) -> Action:
    """The Action a gesture option names, in the shape `nlu/tiers` reads it: a scroll
    carries its `Direction`, a chord carries the table's spelling in `verb`."""
    if intent is Intent.SCROLL:
        return Action(intent, window=window, direction=Direction(payload))
    return Action(intent, window=window, verb=payload)


def _gestures(context: Machine) -> list[Ranked]:
    """Scrolling, paging and the named chords, aimed at the window in front of the speaker.

    The focused window and no other, because a wheel notch cannot be aimed anywhere else:
    it goes to whatever is under the cursor, and `inapp.perform` pins it by putting the
    cursor inside the authorized window rather than by naming it.

    Every entry is put through `inapp.refusal`, the function the executor itself calls, so
    a chord is offered exactly where typing is allowed and a scroll wherever no layer has
    grabbed the screen. A terminal is dropped before that, and dropped for BOTH kinds:
    `inapp.perform` refuses a terminal a wheel notch as well as a chord ("a terminal reads
    keys and wheel notches as commands"), so every gesture option on kitty could only ever
    be refused, whatever `inapp.refusal` says about the wheel.
    """
    focused = context.state.focused
    if focused is None:
        return []
    kind = _kind(context, focused)
    if tiers.is_terminal(focused, kind):
        return []
    words = spoken(context.heard)
    # "a bit more" names no gesture. When the last command was a gesture on this same
    # window, the gesture it was is offered again, first, whatever was said this time.
    last = context.previous
    again = (
        spoken(last.said)
        if last.intent in (Intent.SCROLL.value, Intent.PRESS_CHORD.value)
        and last.address == focused.address
        else ()
    )
    out: list[Ranked] = []
    for index, (intent, payload, summary, groups) in enumerate(GESTURES, 1):
        score = _reaches(words, groups)
        repeated = score <= 0.0 and bool(again) and _reaches(again, groups) > 0.0
        if score <= 0.0 and not repeated:
            continue
        if repeated:
            summary, score = f"{summary} again", AGAIN_SCORE
        gesture = inapp.SCROLL if intent is Intent.SCROLL else inapp.CHORD
        keys = "" if intent is Intent.SCROLL else payload
        if inapp.refusal(focused, kind, context.state, context.cfg, gesture, keys):
            continue
        action = _gesture_action(intent, payload, focused)
        capability = _reachable(intent, GESTURE, action, summary, context)
        if capability is None:
            continue
        option = _inapp_option(
            capability, f"g{index}", summary, _handle(focused, payload), Trust.TRUSTED
        )
        if option is None:
            continue
        # recency 0: a gesture reaches into the window the speaker is looking at, which is
        # the most recent thing there is, exactly as a layout verb does
        out.append(Ranked(option, GESTURE, score, 0))
    return out


# The words each recipe answers to, keyed by `Recipe.name`, distinctive group first (see
# `_reaches`). A name with no entry falls back to the words of the name itself, so a
# recipe added to `recipes.py` still ranks by what it is called instead of vanishing.
#
# The site names come from `recipes.KNOWN_SITES` rather than from a second list here: it
# is the table that decides what a bare spoken label may become, so it is also the honest
# answer to "did the speaker name somewhere to go".
# "page" is deliberately not here: a page is where you already are, so "reload the page"
# would reach a navigation recipe on the strength of a word that named nowhere to go
_SITES: tuple[str, ...] = ("url", "site", "website", "address", *recipes.KNOWN_SITES)
_SEARCH: tuple[str, ...] = ("search", "look", "find", "google")
_TAB: tuple[str, ...] = ("tab", "tabs")

RECIPE_WORDS: dict[str, tuple[tuple[str, ...], ...]] = {
    "open_url": (_SITES, ("open", "go", "navigate", "visit", "browse")),
    "go_to_url": (_SITES, ("open", "go", "navigate", "visit")),
    "search_web": (_SEARCH,),
    "search_youtube": (("youtube",), _SEARCH),
    "search_wikipedia": (("wikipedia",), _SEARCH),
    "find": (("find", "search", "look"),),
    "new_tab": (_TAB, ("new", "another", "open")),
    "close_tab": (_TAB, ("close", "quit")),
    "next_tab": (_TAB, ("next", "following")),
    "previous_tab": (_TAB, ("previous", "last", "back")),
    "back": (("back", "backwards"),),
    "forward": (("forward", "forwards"),),
    "reload": (("reload", "refresh"),),
    "zoom_in": (("zoom",), ("in", "bigger", "larger", "closer")),
    "zoom_out": (("zoom",), ("out", "smaller", "further")),
    "zoom_reset": (("zoom",), ("reset", "normal", "default")),
    "save": (("save",),),
    "scroll_down": (("scroll", "wheel", "scrolling"), ("down", "downwards")),
    "scroll_up": (("scroll", "wheel", "scrolling"), ("up", "upwards")),
    "to_top": (("top", "beginning", "start"),),
    "to_bottom": (("bottom", "end"),),
}

# Recipes the gesture source already offers for the focused window. Both would be one act
# with two ids, which costs the tokens twice and splits the model's probability between
# them, and the gesture is the cheaper of the two: it presses one key through `inapp`
# instead of focusing the window first and then pressing it. Listed by recipe NAME,
# because the recipe is the one being dropped, and only for the focused window, since that
# is the only window a gesture is ever offered for.
GESTURE_DOUBLES: frozenset[str] = frozenset(
    {
        "scroll_down",
        "scroll_up",
        "to_top",
        "to_bottom",
        "next_tab",
        "previous_tab",
        "back",
        "forward",
        "reload",
    }
)


def _recipe_label(recipe: recipes.Recipe, window: Window, context: Machine) -> tuple[str, Trust]:
    """What a recipe option says it will do, and to which window.

    The focused window is not named, and the two reasons are the same size. It is the
    window the speaker is looking at, so "reload this page" is already unambiguous; and
    its label is then written entirely in this repository, which makes it TRUSTED text.
    Any other window has to be named, and is named the way a focus target is: by its
    trusted `.desktop` entry if it has one, by the class its owner wrote otherwise, which
    is what makes that label UNTRUSTED.
    """
    focused = context.state.focused
    if focused is not None and window.address == focused.address:
        return recipe.description, Trust.TRUSTED
    name, trust = _named_by(window, context.lexicon)
    return f"{recipe.description}, in {name}", trust


# every recipe name there is, over all the kind tables, read at import from the table
# that owns them so that a recipe added there is scored here without a second edit
RECIPE_NAMES: tuple[str, ...] = tuple(
    dict.fromkeys(name for table in recipes.RECIPES.values() for name in table)
)


def _reached_recipes(words: Sequence[str]) -> dict[str, float]:
    """The recipes the words reach at all, by name, scored once for the whole desktop.

    Once, and before any window is looked at, because what a recipe answers to has
    nothing to do with which window offers it: scoring inside the window loop asked the
    same question nine times over on this machine's desktop and cost 3.2 ms to answer
    "none of them" (measured). This is the same answer for 0.1 ms.
    """
    scored = {
        name: _reaches(words, RECIPE_WORDS.get(name) or (tuple(name.split("_")),))
        for name in RECIPE_NAMES
    }
    return {name: score for name, score in scored.items() if score > 0.0}


def _recipes(context: Machine) -> list[Ranked]:
    """The named key sequences a window offers, for every window the words reach into.

    `recipes.for_window` is the whole offering, asked per window because it is keyed by
    app KIND, and `recipes.refusal_for` is asked again for the checks that need the
    window: a table nobody can run against this window is not an option list.

    Terminals are dropped whole. `recipes.for_window` already offers a terminal nothing
    but the pointer-only recipes, and those deliver their scroll through `inapp.perform`,
    which refuses a terminal outright, so what survives its filter would still be refused
    at the end.
    """
    state = context.state
    words = spoken(context.heard)
    reached = _reached_recipes(words)
    if not reached:
        return []
    # A recipe that types (a search, an address) is tier 2, which a list alone may not
    # authorize. It is offered anyway when the speaker used a reaching verb, and any other
    # tier 2 recipe (closing a tab) only when its own verb was said: the same evidence
    # `understand._reaching` accepts when it is chosen. The tier still makes it a
    # countdown, and "open a new tab" still offers no way to close one.
    reaching = bool(tiers.REACHING_VERBS & set(words))
    # while the extension is connected it is how a browser is driven; the keystroke
    # recipes for a browser window are its fallback, and two ways to do one thing would
    # only split the vote
    extension = _extension_serves(context)
    focused = state.focused
    out: list[Ranked] = []
    index = 0
    for window in sorted(state.windows, key=lambda w: w.focus_rank):
        kind = _kind(context, window)
        if tiers.is_terminal(window, kind):
            continue
        if extension and recipes.recipe_kind(kind) == recipes.BROWSER:
            continue
        at_hand = focused is not None and window.address == focused.address
        # typing goes to the window in front, or to one the speaker named: "search for
        # hold the line" with Spotify in front is not a web search in the browser behind it
        named = at_hand or _names(context, window)
        for recipe in recipes.for_window(window, kind):
            score = reached.get(recipe.name, 0.0)
            if score <= 0.0 or (at_hand and recipe.name in GESTURE_DOUBLES):
                continue
            if recipes.refusal_for(recipe, window, kind):
                continue
            action = Action(Intent.RUN_RECIPE, window=window, verb=recipe.name)
            asked = recipes.verb_said(recipe, words) or (recipe.needs_text and reaching)
            ceiling = 2 if asked and (named or not recipe.needs_text) else MAX_INAPP_TIER
            capability = _reachable(
                Intent.RUN_RECIPE, RECIPE, action, recipe.description, context, ceiling
            )
            if capability is None:
                continue
            label, trust = _recipe_label(recipe, window, context)
            index += 1
            option = _inapp_option(
                capability, f"r{index}", label, _handle(window, recipe.name), trust, ceiling
            )
            if option is None:
                continue
            out.append(Ranked(option, RECIPE, score, window.focus_rank))
    return out


def _names(context: Machine, window: Window) -> bool:
    """Did the speaker name this window's application, by a trusted field?"""
    lexicon = context.lexicon
    corroborates = getattr(lexicon, "corroborates", None)
    if corroborates is None:
        return False
    try:
        return bool(corroborates(context.heard, window=window))
    except Exception:  # a lexicon that throws names nothing
        return False


def _controls(context: Machine) -> list[Ranked]:
    """The controls of the focused window that the speaker actually named.

    THE CACHE ONLY. `controls.cached` reads a dict; `controls.controls_for` walks the
    accessibility tree, and that is 3.8 s on the live ZapZap window and 16.2 s on KCalc's
    48, against 250 ms for this whole function's caller (`nlu/understand.HARVEST_S`). A
    cold cache therefore offers nothing, and offering nothing is a correct answer here,
    not a failure: `daemon._warm_controls` is what makes the next utterance different.

    `controls.best_match` is the filter as well as the ranker, and it is the right one for
    the reason its own docstring gives: a control whose name matches nothing the speaker
    literally said is never returned, so a page cannot supply the evidence that selects
    its own button. Everything it does return is UNTRUSTED, is offered under an id this
    file minted, and carries the tier `controls.click_tier` gave it through
    `nlu/tiers.tier`, which is at least 1 and is 2 for a name that reads destructive, and
    2 again for a name with a homoglyph in it. A tier 2 one is then dropped, which is a
    page raising the price of its own button until nobody may pick it: the harmless
    direction, and the only one an untrusted name is ever allowed to move a tier in.
    """
    focused = context.state.focused
    if focused is None:
        return []
    found = controls.cached(focused)
    if not found:
        # None is a cold cache and () is a window that publishes nothing (a terminal, a
        # canvas, Chrome with no --force-renderer-accessibility). Neither is an error and
        # neither is "there is no such button"; both are simply no options this time.
        return []
    out: list[Ranked] = []
    index = 0
    for control, score in controls.best_match(context.heard, found, limit=QUOTA[CONTROL]):
        if not control.enabled:
            # `controls.click` refuses a control that is not active, so offering one would
            # be offering a refusal
            continue
        action = Action(Intent.CLICK_CONTROL, window=focused, verb=control.name, text=control.name)
        summary = "click a control in the focused window"
        capability = _reachable(Intent.CLICK_CONTROL, CONTROL, action, summary, context)
        if capability is None:
            continue
        index += 1
        label = f"{control.name}, {control.role}" if control.role else control.name
        option = _inapp_option(
            capability, f"c{index}", label, _handle(focused, control.name), Trust.UNTRUSTED
        )
        if option is None:
            continue
        out.append(Ranked(option, CONTROL, score, 0))
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


def budget_for(drift: tokens.Drift, *, copies: int = 1, budget: int = OPTION_BUDGET) -> int:
    """The token budget for the options, after the drift and after the copies.

    `Drift.ratio` above 1 means the API reported more tokens than were estimated, which
    `jev/tokens.py` names as the dangerous direction: requests believed to be under the
    cap are not. Dividing by it turns a measured under-count into a smaller list rather
    than into an HTTP 503, and it does nothing at all until a real request has reported a
    count, which is what "wire Drift in from the first request" has to mean to be worth
    anything.

    `copies` is the other divisor and it is measured, not theoretical: `requests._wire_one`
    inlines a Choice's options inside the question, so `step_1`, `step_2` and `step_3` over
    one list in one request pay for that list three times.
    """
    ratio = drift.ratio if drift.samples else 1.0
    return max(int(budget / max(ratio, 1.0) / max(copies, 1)), MIN_BUDGET)


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
    # every source that exists, not only the ones ORDER knows about: a source added to a
    # builder and forgotten here would otherwise vanish without being counted, which is
    # the exact failure `Harvest.note` exists to make impossible
    for source in dict.fromkeys([*ORDER, *(r.source for r in built)]):
        group = sorted((r for r in built if r.source == source), key=lambda r: r.key)
        quota = QUOTA.get(source, limit)
        dropped[source] += max(len(group) - quota, 0)
        kept.extend(group[:quota])
    ranked_all = sorted(kept, key=lambda r: r.key)
    for extra in ranked_all[limit:]:
        dropped[extra.source] += 1
    ranked_all = ranked_all[:limit]
    while ranked_all and cost([r.affordance for r in ranked_all]) > budget:
        dropped[ranked_all[-1].source] += 1
        ranked_all.pop()
    return tuple(ranked_all), tuple(sorted((s, n) for s, n in dropped.items() if n))


# --------------------------------------------------------------------------- the harvest


def harvest(
    context: Machine,
    *,
    limit: int = MAX_OPTIONS,
    budget: int | None = None,
    copies: int = 1,
) -> Harvest:
    """One ranked list of everything the machine can do right now.

    The single entry point, and the whole of docs/STRATEGY.md 2.2: code enumerates, the
    model selects. Every option came from live state a moment ago, every tier was decided
    by the module that owns the gesture, and nothing an application wrote can add an
    option, raise its own standing above a trusted one at the same score, or lower what it
    costs.

    `copies` is how many times the caller's request will carry this list; see
    `budget_for`. `budget` overrides both it and the drift, for a caller that has already
    priced the rest of its request with `cost`.

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
        (BROWSER, _browser),
        (GESTURE, _gestures),
        (RECIPE, _recipes),
        (CONTROL, _controls),
        (CLOSE, _closing),
        (HERE, _here),
        (HERE, _sending),
    ):
        try:
            built.extend(gather(context))
        except Exception as exc:  # one source must not cost the whole harvest
            log.debug("the %s affordances could not be built: %s", source, exc)
    allowed = budget_for(context.drift, copies=copies) if budget is None else budget
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
    "BROWSER",
    "CLOSE",
    "CONTROL",
    "HERE",
    "GESTURE",
    "LAYOUT",
    "MAX_INAPP_TIER",
    "MAX_OPTIONS",
    "OPTION_BUDGET",
    "ORDER",
    "QUOTA",
    "RECIPE",
    "SELF",
    "SERVICE",
    "SYSTEM",
    "WINDOW",
    "WORKSPACE",
    "Harvest",
    "Machine",
    "Previous",
    "Ranked",
    "budget_for",
    "claims",
    "cost",
    "harvest",
    "here_workspace",
    "said",
    "spoken",
    "target",
]
