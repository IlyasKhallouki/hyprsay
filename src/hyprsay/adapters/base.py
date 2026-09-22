"""The adapter contract: what an application can do, asked of the application itself.

WHY THIS EXISTS. `recipes.py` is 853 lines of hand-written key sequences keyed by
application kind. Its premise is that an application will never say what it can do, so
hyprsay has to remember a path through it. Where that premise is false the table is the
wrong shape: it cannot serve "the second live version", it goes stale when the app
changes its menus, and it grows by one entry per application forever. An adapter is the
opposite. It asks a running application what it can do RIGHT NOW and turns the answer
into candidates Jev may choose among, so the reachable behaviour grows with the
application rather than with this repository.

The shape is not invented here. GNOME's `org.gnome.Shell.SearchProvider2`, KDE's
`org.kde.krunner1` and Apple's App Intents converge on it from three traditions: a
provider that already owns the domain returns ranked opaque ids, and the caller
activates one. The one place they do NOT converge is typed parameters, which only App
Intents has (SearchProvider2's `GetResultMetas` returns name, id, icon and description
and nothing else), so `Param` below is designed here rather than copied.

THE FOUR MEMBERS, AND WHY THE TIER MODEL SURVIVES THEM UNCHANGED (docs/PLAN.md 5.6):

    can_serve(context)             a pure local predicate. Is the binary installed, does
                                   the player own its bus name. No network, no spawn.
    capabilities(context)          stable ids, each with a tier decided in code and one
                                   line of natural language written in this repository.
    resolve(cap, slots, context)   READ ONLY, and therefore ALWAYS tier 0. This is the
                                   whole safety argument: asking an application what it
                                   has changes nothing, so the expensive half of the
                                   work can run on a partial transcript, and the 0-to-3
                                   tiers only ever have to describe `perform`.
    perform(cap, handle, context)  carries the capability's tier, and accepts ONLY a
                                   handle this adapter itself minted (`Minted`).

What the model emits therefore narrows to one option id. It never writes a selector, a
track id, a bus name or a command string, which is hyprsay's existing rule applied to a
longer list rather than a new rule.

UNTRUSTED TEXT. Everything an adapter returns from `resolve` is written by someone else:
a track name comes off the internet, a player's `Identity` is written by the application.
`candidates()` forces `trust=UNTRUSTED` on all of it, and the rule is the one
`controls.click_tier` already follows: untrusted text may RAISE what an action costs and
may never lower it, and it is never the evidence that authorizes an action. A label's
only job is to let a human and a model tell two options apart. A hostile label cannot
invent an option, because code built every option before the model saw any of them, and
it cannot be dereferenced, because `as_option` does not carry the handle.

TIER 3 IS ABSENT BY CONSTRUCTION. Session-level actions are confirmed by a physical key
and are never offered to the model, exactly as `LOCK_SCREEN` is absent from
`JEV_INTENTS`. An `Affordance` above `MAX_OFFERABLE_TIER` cannot be built at all, and
`offer` drops a capability declared above it, so there is no path from an adapter to a
tier 3 act even if one were declared by mistake.

THREADS. An adapter holds a reading of the machine and the handles it has minted, and
neither is locked. Build each adapter once and call it from one thread, which is what
the daemon already does for anything that writes (`ARCHITECTURE.md`: exactly one
executor worker). Two threads racing cost a duplicate read of the bus or a duplicate
search, never a wrong answer, because nothing here is read-modify-write.
"""

from __future__ import annotations

import logging
import re
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Protocol

from hyprsay.lexicon import mixed_script, sanitize
from hyprsay.model import DesktopState, Outcome

log = logging.getLogger(__name__)

# `resolve` only reads, so it costs nothing to run and nothing to be wrong about.
RESOLVE_TIER = 0
# tier 3 is session level and confirmed by a physical key, so it is never an option
MAX_OFFERABLE_TIER = 2
# 120 candidates at real result length estimate at 3,446 tokens, 1.9x the 1,800 token
# cap, and 120 French accented labels at 2,674, 1.5x it (STRATEGY.md 2.4). So the list
# is capped here and every label is cut, rather than discovered to be too big later.
MAX_LABEL = 60
MAX_CANDIDATES = 50
MAX_HANDLE = 256

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@#/+-]*$")
_WORD = re.compile(r"[^\W_]+")


class AdapterError(Exception):
    """Why an adapter cannot answer. One plain sentence, the same shape as `OpError`.

    The caller turns it into a REFUSE or a SUGGEST with `reason=str(exc)`. It is its own
    class so that `ops` and `controls` stay importable from here without a cycle.
    """


class Trust(StrEnum):
    """Who wrote this text.

    TRUSTED means every character came from hyprsay's own source or from a system
    `.desktop` file. UNTRUSTED means an application, a web page or the internet wrote
    it. There is no third value, and no value that makes a label able to authorize
    anything (docs/PLAN.md 5.6).
    """

    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"


@dataclass(frozen=True)
class Param:
    """One typed slot a capability needs before `resolve` can answer.

    Copied from App Intents rather than from SearchProvider2, which has no typed
    parameters at all. `kind` is a closed set because a slot the grammar cannot fill is
    a slot that will be filled by a model, and that is the thing this design removes.
    """

    name: str
    kind: str = "text"  # "text" | "ordinal" | "number"
    required: bool = True


@dataclass(frozen=True)
class Capability:
    """One thing an adapter can do, with the tier it costs decided in code.

    `needs_query` is what keeps the key-down harvest cheap: a capability that needs
    words from the utterance cannot be resolved before the utterance exists, so it is
    offered as itself and resolved once, after the answer that names it. A capability
    that needs nothing is resolved eagerly, because on this machine that costs 1.6 to
    3.8 ms for every MPRIS player on the bus (measured, `mpris.py`).
    """

    id: str
    summary: str  # one line, written here, so it is TRUSTED text
    tier: int
    kind: str = ""
    params: tuple[Param, ...] = ()
    needs_query: bool = False

    def __post_init__(self) -> None:
        if not _ID.match(self.id):
            raise AdapterError(f"{self.id!r} is not a usable capability id")
        if not 0 <= self.tier <= 3:
            raise AdapterError(f"tier {self.tier} is outside the 0 to 3 model")


@dataclass(frozen=True)
class Affordance:
    """One option, in the shape STRATEGY.md 2.2 specifies: id, label, kind, tier,
    handle, trust. `adapter` and `capability` ride along so that a chosen id can be
    routed home without parsing it back apart, which is the kind of string surgery that
    turns into a bug the first time a track name contains a colon.

    `id` is the only thing Jev ever answers with. `handle` is opaque, is never sent to
    the model, and is dereferenced only by the adapter that minted it.
    """

    id: str
    label: str
    kind: str
    tier: int
    handle: str
    adapter: str
    capability: str
    trust: Trust = Trust.UNTRUSTED

    def __post_init__(self) -> None:
        if not _ID.match(self.id):
            raise AdapterError(f"{self.id!r} is not a usable option id")
        if not 0 <= self.tier <= MAX_OFFERABLE_TIER:
            raise AdapterError(
                f"tier {self.tier} is never offered to the model, so it cannot be an option"
            )
        # the label is application text on its way into a model request and onto the
        # HUD: zero-width and bidi characters vanish, control characters go, and the
        # length is the one the token estimate was sized against
        label = sanitize(self.label)[:MAX_LABEL].strip()
        object.__setattr__(self, "label", label or sanitize(self.kind) or self.id)
        object.__setattr__(self, "handle", sanitize(self.handle)[:MAX_HANDLE])
        object.__setattr__(self, "trust", Trust(self.trust))

    def as_option(self) -> dict[str, str]:
        """What goes into a `jev.types.Choice` option, and nothing else.

        The handle is absent on purpose: the model picks an id, code dereferences it, so
        there is never a dereferenceable thing in a model request. The tier is absent
        too, because the tier is code's decision and describing it to the model invites
        it to reason about a permission it does not hold.
        """
        return {"label": self.label, "kind": self.kind, "trust": self.trust.value}


@dataclass(frozen=True)
class Context:
    """What an adapter is allowed to look at. Frozen, like every other state here, so a
    candidate cannot drift away from the desktop it was resolved against.

    `heard` is the normalized utterance. An adapter may rank against it; it may never
    treat it as authorization, and it must never pass it to a shell.
    """

    state: DesktopState = field(default_factory=DesktopState)
    heard: str = ""
    now: float = 0.0


class Adapter(Protocol):
    """The four members, plus `probe` for the same reason `controls.probe` exists: when
    an adapter cannot work, the owner needs the sentence that names the repair, not an
    empty candidate list that reads as "there is nothing to play"."""

    name: str

    def probe(self) -> tuple[bool, str]: ...

    def can_serve(self, context: Context) -> bool: ...

    def capabilities(self, context: Context) -> tuple[Capability, ...]: ...

    def resolve(
        self, capability: str, slots: Mapping[str, Any], context: Context
    ) -> tuple[Affordance, ...]: ...

    def perform(self, capability: str, handle: str, context: Context) -> Outcome: ...


class Minted:
    """The handles one adapter has issued, so `perform` can accept nothing else.

    This is the half of the contract that makes a handle safe to be opaque: a track id
    parsed out of JSON that came off the internet is only ever acted on if this adapter
    put it in a candidate list first. Bounded, because the daemon runs for weeks; the
    oldest handle is forgotten first, and forgetting one only means the speaker has to
    ask again.
    """

    def __init__(self, limit: int = 256) -> None:
        self._limit = limit
        self._entries: OrderedDict[str, str] = OrderedDict()

    def mint(self, handle: str, label: str = "") -> str:
        self._entries.pop(handle, None)
        self._entries[handle] = label
        while len(self._entries) > self._limit:
            self._entries.popitem(last=False)
        return handle

    def label_for(self, handle: str) -> str:
        return self._entries.get(handle, "")

    def __contains__(self, handle: object) -> bool:
        return handle in self._entries

    def __len__(self) -> int:
        return len(self._entries)


def words(text: str) -> list[str]:
    return _WORD.findall(sanitize(text).casefold())


def tier_for(capability: Capability, label: str) -> int:
    """What acting on a candidate with this label costs.

    Untrusted text may RAISE a tier and may never lower one, so this returns at least
    `capability.tier` whatever the label says. A token that mixes Latin with another
    script is a string built to be read as something it is not, and the cheap honest
    answer to one is a countdown. It is not load bearing here (a label authorizes
    nothing, and the handle is validated by the adapter that minted it) and it costs
    nothing, which is exactly the trade `controls.click_tier` already makes.
    """
    if capability.tier > MAX_OFFERABLE_TIER:
        raise AdapterError(f"{capability.id} is tier {capability.tier} and is never offered")
    tier = capability.tier
    if any(mixed_script(word) for word in words(label)):
        tier += 1
    return min(max(tier, capability.tier), MAX_OFFERABLE_TIER)


def capability_affordance(adapter: Adapter, capability: Capability) -> Affordance:
    """The capability itself as an option, for the ones that cannot be resolved before
    the utterance exists. Its label is written in this repository, so it is TRUSTED: it
    is the one kind of affordance that is."""
    return Affordance(
        id=capability.id,
        label=capability.summary,
        kind=capability.kind or adapter.name,
        tier=capability.tier,
        handle="",
        adapter=adapter.name,
        capability=capability.id,
        trust=Trust.TRUSTED,
    )


def candidates(
    adapter: Adapter,
    capability: Capability,
    slots: Mapping[str, Any],
    context: Context,
    *,
    limit: int = MAX_CANDIDATES,
) -> tuple[Affordance, ...]:
    """`adapter.resolve`, through the guards that make its answer safe to offer.

    Every rule here is enforced rather than documented, because an adapter is the part
    of this system most likely to be written by someone who has not read PLAN 5.6:

    - the tier floor is the capability's, so a candidate can never cost less than the
      capability it came from;
    - `trust` is forced to UNTRUSTED, whatever the adapter set;
    - a candidate above `MAX_OFFERABLE_TIER` is dropped rather than downgraded;
    - ids are made unique, because options are a dict and a repeated key would silently
      swallow an option;
    - the list is capped, for the token reason in STRATEGY.md 2.4.
    """
    if capability.tier > MAX_OFFERABLE_TIER:
        return ()
    out: list[Affordance] = []
    seen: set[str] = set()
    for found in adapter.resolve(capability.id, slots, context):
        if found.id in seen:
            continue
        tier = max(found.tier, capability.tier)
        if tier > MAX_OFFERABLE_TIER:
            continue
        seen.add(found.id)
        out.append(
            replace(
                found,
                tier=tier,
                trust=Trust.UNTRUSTED,
                adapter=adapter.name,
                capability=capability.id,
            )
        )
        if len(out) >= limit:
            break
    return tuple(out)


def offer(
    adapters: Sequence[Adapter],
    context: Context,
    *,
    limit: int = MAX_CANDIDATES,
) -> tuple[Affordance, ...]:
    """Everything the live machine can do through these adapters, as one option list.

    Never raises. An adapter that throws is skipped with a log line, for the reason
    `a11ybus` falls back to busctl rather than failing: a broken adapter must degrade
    the option list, never take the utterance down with it.
    """
    out: list[Affordance] = []
    for adapter in adapters:
        try:
            if not adapter.can_serve(context):
                continue
            for capability in adapter.capabilities(context):
                if capability.tier > MAX_OFFERABLE_TIER:
                    continue
                if capability.needs_query:
                    out.append(capability_affordance(adapter, capability))
                else:
                    out.extend(candidates(adapter, capability, {}, context, limit=limit))
                if len(out) >= limit:
                    return tuple(out[:limit])
        except Exception as exc:  # one adapter must not cost the whole harvest
            log.debug("adapter %s did not answer: %s", getattr(adapter, "name", "?"), exc)
    return tuple(out[:limit])


def find(adapters: Iterable[Adapter], affordance: Affordance) -> Adapter | None:
    """The adapter that minted this option, so a chosen id can be performed."""
    return next((a for a in adapters if a.name == affordance.adapter), None)


def capability_of(adapter: Adapter, capability: str, context: Context) -> Capability | None:
    """The live capability behind a chosen option id.

    Asked of the adapter rather than kept from the harvest, because what an application
    can do now is not what it could do when the key went down: a player that closed
    between the two takes its capability with it, and the second question must be asked
    against the machine as it is.
    """
    return next((c for c in adapter.capabilities(context) if c.id == capability), None)
