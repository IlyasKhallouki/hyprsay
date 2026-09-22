"""The second turn: the options that could not exist until the first one was answered.

WHY A SECOND TURN AT ALL, AND WHY ONLY ONE. Jev is NON-GENERATIVE. It answers an id out
of a list code built, so it can never produce a Spotify URI, and no amount of prompting
will make it. That is not a limitation to work around, it is the property this whole
design rests on (docs/STRATEGY.md 2.1). It has one consequence nobody can argue with:

    "play the second live version" cannot be one decision, because at the moment the
    question is asked there is no second live version to point at. Nothing on this
    machine knows what Spotify holds until somebody asks Spotify.

So the utterance takes two turns. Turn one chooses, out of everything the machine can do
right now, a capability that says "I can find things by name". Code then FETCHES, which
is `adapters.base.Adapter.resolve` and is READ ONLY and therefore always tier 0, so the
turn between the two turns writes nothing at all. Turn two chooses again, over a harvest
taken fresh plus the real candidates that now exist, and that answer is an ordinary
`Decision` that goes through the tier function and the executor exactly as any other.

ONE, and the ceiling is not arbitrary. docs/STRATEGY.md 3.2 costs a per-step agent loop
against the measured numbers and concludes: "One request per utterance is the target. Two
is the ceiling, and the only legitimate second is fetching candidates that could not exist
before the first answer." This module is that one legitimate second and nothing else. A
third turn would be a per-step agent loop wearing a smaller name.

WHAT STOPS IT, and all of it is code:

- `MAX_TURNS`. A hard count, not a convention. Turn two may not fetch again: an option
  that still needs fetching ends the utterance with a sentence.
- `LOOP_S`. A wall clock deadline over the WHOLE extra turn, fetch and question together,
  because two ceilings that each look reasonable multiply. `waytify.SEARCH_TIMEOUT_S` is
  8 s, which is the right ceiling for a wedged daemon and the wrong one for a person
  standing there, so this cuts it.
- the tier function, on turn two exactly as on turn one. A second turn is not a licence.
  `understand._step` is the same function in both, and `ops._service` re-asks the adapter
  what the capability costs before anything is performed.
- and turn one CANNOT ACT. Its answer names a capability with no handle, and a handle is
  the only thing `perform` accepts (`adapters.base.Minted`), so there is no path on which
  this module acts twice. If turn two answers nothing usable, the utterance ends having
  written nothing, and says which of the reasons below it ended for.

THE MEASURED BUDGET (docs/STRATEGY.md 4.4, and the live numbers in `jev/`): a Jev round
trip from this machine is 315 ms p50, and the Spotify leg was estimated at about the same
with the daemon already warm. So the extra turn is budgeted at about a second of real
work, `LOOP_S` is the ceiling around it, and `FETCH_S` is the ceiling on its first half.

WHAT IT REALLY COST, measured on 2026-09-22 against this machine's live desktop, the real
gateway key, and the real `adapters.waytify` driving a recorded socket, since the daemon
is not running here: "search spotify for the second live version of bohemian rhapsody",
three runs, 758 to 894 ms from transcript to decision, of which

    the second harvest        13.7 to 34.8 ms, p50 21.1, with the live MPRIS adapter on
                              the session bus and `copies=1`
    the fetch                 0.8 to 1.7 ms, which is the recorded socket and not a real
                              Spotify round trip; budget that leg at about 315 ms
    the second question       273.8 to 501.1 ms on the live gateway, one Choice,
                              35 options, about 931 tokens
    the perform               1 to 4 ms through the executor and `ops._service`

So the extra turn is roughly 0.3 to 0.5 s of real waiting plus whatever Spotify takes,
which is what `LOOP_S` at 2.5 s and `FETCH_S` at 1.2 s were sized around.

WHAT DOES NOT CLEAR THE GATE, worth knowing before reading a failure here as a bug. "play
the second live version of bohemian rhapsody", with no service named, puts the right
capability FIRST on the live gateway every time and at a probability code refuses: 6 of 6
identical requests answered it at p1 0.38 to 0.44 with a margin of 0.02 to 0.15, against
`plan.MIN_PROBABILITY` 0.5 and `plan.MIN_MARGIN` 0.3, because the generic
`media_play_pause` holds about a third of the mass and `none` another fifth. The plan
therefore reads UNSURE, there is no step, and no second turn happens at all. That is the
floor doing its job on a near-tie rather than this module failing, and it is a calibration
question those two constants already say out loud they have never had an eval behind.

THE QUERY IS CUT BY CODE, NEVER WRITTEN BY THE MODEL. `Capability.params` says a search
needs a `query`, and the only honest source for one is the speaker's own sentence. The
model is never asked to write it, because the moment it writes a string this stops being
a chooser and starts being a generator, which is the property the safety argument rests
on. `strip_carrier` below is the cut, and it is a closed table for
`understand.DICTATION_OPENERS`' reason: what reaches a search engine is decided by code.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum

from hyprsay.adapters.base import Affordance, Capability
from hyprsay.lexicon import sanitize

# docs/STRATEGY.md 3.2: one request per utterance is the target, two is the ceiling.
MAX_TURNS = 2
# the whole extra turn: the read-only fetch and the question that follows it. A Jev round
# trip is 315 ms p50 from this machine and the Spotify leg was costed at about the same,
# so a second and a half of real work sits well inside this and a wedged daemon does not.
LOOP_S = 2.5
# the fetch alone. `waytify.SEARCH_TIMEOUT_S` is 8 s, which is the ceiling that stops a
# wedged daemon holding a worker thread; it was never a deadline for somebody waiting.
FETCH_S = 1.2


class Stopped(StrEnum):
    """Why the second turn did not happen, or happened and named nothing to do.

    Every one of these ends the utterance with a plain sentence and no write. They are
    named rather than collapsed into "no" because they are five different repairs, and
    `hyprsay inspect --last` is where the owner finds out which one they hit.
    """

    NO_ADAPTER = "no_adapter"  # the adapter that offered it is not in this process
    NO_CAPABILITY = "no_capability"  # it does not offer that capability any more
    NO_QUERY = "no_query"  # nothing in the utterance said what to look for
    FETCH_FAILED = "fetch_failed"  # the adapter could not answer, in its own words
    NOTHING_FOUND = "nothing_found"  # it answered, with nothing that could be offered
    OUT_OF_TIME = "out_of_time"  # `LOOP_S` ran out
    OUT_OF_TURNS = "out_of_turns"  # turn two asked for another fetch
    UNANSWERED = "unanswered"  # the second question failed or came back unreadable
    NO_BETTER = "no_better"  # turn two named nothing turn one had not already named


# One plain sentence per reason, written here so that all of this module's policy text
# sits in one place and the caller only has to look one thing up. Each names the repair
# where there is one, which is `adapters.base.Adapter.probe`'s rule: an empty answer that
# reads as "there is nothing to play" is the failure these sentences exist to prevent.
SENTENCE: dict[Stopped, str] = {
    Stopped.NO_ADAPTER: "whatever offered that is not running any more, so nothing was done",
    Stopped.NO_CAPABILITY: "that cannot be looked up right now, so nothing was done",
    Stopped.NO_QUERY: "say what to look for as well, for example: play bohemian rhapsody",
    Stopped.FETCH_FAILED: "nothing could be looked up just then",
    Stopped.NOTHING_FOUND: "nothing came back for that, so there was nothing to choose from",
    Stopped.OUT_OF_TIME: "looking that up took too long, so nothing was done",
    Stopped.OUT_OF_TURNS: "that would need looking up twice, which is not done in one breath",
    Stopped.UNANSWERED: "the second question went unanswered, so nothing was done",
    Stopped.NO_BETTER: "nothing that came back fitted what you said, so nothing was done",
}

# The reasons where hearing the utterance again could change the answer, which is the one
# thing `Decision.rehearable` governs (`model.py`): a mis-heard track name is worth a
# second recognizer, and a daemon that is not running is not.
REHEARABLE: frozenset[Stopped] = frozenset(
    {Stopped.NO_QUERY, Stopped.NOTHING_FOUND, Stopped.NO_BETTER}
)


@dataclass(frozen=True)
class Budget:
    """How many turns are left and how long they have, as one object rather than two.

    Frozen and spent by replacement, like everything else in this codebase that describes
    a moment: a budget that could be decremented in place is one a retry could refill.
    """

    clock: Callable[[], float]
    turns: int = MAX_TURNS
    deadline: float = 0.0
    spent: int = 1  # turn one has already happened by the time a Budget is built

    def started(self, seconds: float = LOOP_S) -> Budget:
        """The same budget with its wall clock running from now."""
        return replace(self, deadline=self.clock() + seconds)

    @property
    def left(self) -> float:
        return max(self.deadline - self.clock(), 0.0)

    def stop(self) -> Stopped | None:
        """Why another turn may not be taken, or None when one may."""
        if self.spent >= self.turns:
            return Stopped.OUT_OF_TURNS
        if self.left <= 0.0:
            return Stopped.OUT_OF_TIME
        return None

    def taken(self) -> Budget:
        return replace(self, spent=self.spent + 1)

    def fetch_seconds(self) -> float:
        """What the fetch half may have: its own ceiling, or whatever is left of the
        whole turn, whichever is smaller. The question still has to be asked afterwards,
        so a fetch may never eat the deadline it shares."""
        return min(FETCH_S, self.left)


def needs_fetching(affordance: Affordance) -> bool:
    """Is this an option nobody can act on until something is asked for it?

    The handle IS the target (`nlu/plan.bound`). An adapter builds one for every candidate
    that names a live thing and leaves it empty for exactly one kind: a capability offered
    as ITSELF, because it could not be resolved before the utterance existed
    (`adapters.base.Capability.needs_query`). That option is the whole reason this module
    exists, and it is also the one option `perform` can never accept, since `Minted` only
    honours a handle it issued.
    """
    return not affordance.handle


def fetchable(capability: Capability | None) -> bool:
    """Does the adapter itself say this capability is resolved from words?

    Asked of the live capability and not of the option, because `needs_query` is the
    adapter's own declaration and an option is a copy of a moment. A capability with an
    empty handle that does NOT declare `needs_query` is an adapter bug, and the answer to
    an adapter bug is to offer the speaker nothing rather than to guess at its slots.
    """
    return capability is not None and capability.needs_query


# --------------------------------------------------------------------------- the query

# What asks for something to be found, at the head of an utterance. Closed and written
# here, for `understand.DICTATION_OPENERS`' reason: this decides what reaches a search
# engine, and a fuzzy match would send the wrong half of the sentence. Longest first, so
# "search for" is cut before "search" and "look up" before "look".
ASKING: tuple[str, ...] = (
    "start playing",
    "put on some",
    "search for",
    "look for",
    "listen to",
    "queue up",
    "look up",
    "put on",
    "play me",
    "find me",
    "search",
    "play",
    "find",
    "queue",
    "start",
)

# A determiner or an ordinal between the verb and the thing. An ordinal says WHICH result,
# not what to look for: Spotify has no track called "second", and leaving the word in the
# query poisons every result that comes back. It is the one word in the sentence that the
# second turn needs and the fetch must not have.
PICKING: frozenset[str] = frozenset(
    {
        "the",
        "a",
        "an",
        "some",
        "my",
        "that",
        "first",
        "second",
        "third",
        "fourth",
        "fifth",
        "last",
        "next",
        "other",
        "one",
    }
)

# The normalizer rewrites a spoken ordinal as a DIGIT: measured on the live path, "play
# the second live version" reaches here as "play the 2 live version", so a table of
# ordinal words alone leaves the "2" in the query. A bare leading digit is only dropped
# when the recognizer's own tokens held the ordinal WORD, because a leading number is
# often the thing itself: "play 2 unlimited" and "play 99 luftballons" are bands, and the
# digit is all that tells them apart from anything else.
ORDINAL_WORDS: frozenset[str] = frozenset(
    {"first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth"}
)
ORDINAL_DIGITS: frozenset[str] = frozenset("123456789")

# The services this repository has an adapter for, as a person names them mid-sentence.
# "search spotify for X" puts the name between the verb and the thing, where `TRAILING`
# cannot reach it; measured live, that left "spotify for the 2 live version of bohemian
# rhapsody" going to Spotify as a search query.
NAMING: frozenset[str] = frozenset({"spotify", "waytify"})

# What names the service rather than the thing, at the tail. "on spotify" is how a person
# says which application, and it is never part of what they want played.
TRAILING: tuple[str, ...] = (
    "on spotify please",
    "with spotify",
    "from spotify",
    "on spotify",
    "in spotify",
    "on waytify",
    "please",
)

# waytify's own cap, repeated here so a query is cut before it is sent rather than
# discovered to be too long by the adapter (`adapters.waytify.MAX_QUERY`)
MAX_QUERY = 120
_SPACES = re.compile(r"\s+")


def strip_carrier(heard: str, literal: Sequence[str] = ()) -> str:
    """The speaker's own words with the asking taken off the front and the back.

    FROM THE NORMALIZED UTTERANCE and not from the raw one, which is where this differs
    from `grammar._spoken`. A recipe TYPES its span into the person's own machine, so it
    has to be their characters, capitals, punctuation and all. A query is matched by a
    remote search engine that folds case and ignores punctuation anyway, and the
    normalizer's repairs ("firefuck" into "firefox") are exactly the kind of thing that
    makes a search better rather than worse.

    `literal` is what the recognizer actually WROTE, before the normalizer touched it,
    and it is read for ONE thing: whether a leading digit was an ordinal word before the
    normalizer rewrote it as a digit (see `ORDINAL_DIGITS`). It is folded here rather
    than by the caller, so raw transcript words can be passed in as they were heard.

    Four cuts, in this order, because each one uncovers the next: the asking verb, then
    the service the speaker named, then the determiners and the ordinal, then the tail.

    Returns "" when nothing is left, which is a real answer: "play something" says what
    to do and never what to play, and the honest response is to say so.
    """
    text = _SPACES.sub(" ", sanitize(heard).casefold()).strip(" .,!?")
    for phrase in ASKING:
        if text == phrase:
            return ""
        if text.startswith(phrase + " "):
            text = text[len(phrase) + 1 :]
            break
    words = text.split()
    if words and words[0] in NAMING:
        # "search spotify for X". Only after the service name, so "play for whom the bell
        # tolls" keeps the "for" that is the first word of its own title
        words.pop(0)
        if words and words[0] == "for":
            words.pop(0)
    ordinal = bool({w.casefold().strip(" .,!?") for w in literal} & ORDINAL_WORDS)
    while words and (words[0] in PICKING or (ordinal and words[0] in ORDINAL_DIGITS)):
        words.pop(0)
    text = " ".join(words)
    for phrase in TRAILING:
        if text.endswith(" " + phrase):
            text = text[: -(len(phrase) + 1)]
            break
    return text.strip(" .,!?")[:MAX_QUERY].strip()


def slots_for(
    capability: Capability, heard: str, literal: Sequence[str] = ()
) -> dict[str, str] | None:
    """The slots this capability needs, filled from the utterance by code.

    None means a required slot could not be filled, and then nothing is fetched. Only
    `text` slots are filled here: an `ordinal` or a `number` slot is a thing the SECOND
    turn answers by choosing among real candidates, which is the whole point of having a
    second turn, and filling one here would be code guessing at the answer it is about
    to ask for.
    """
    query = strip_carrier(heard, literal)
    out: dict[str, str] = {}
    for param in capability.params:
        if param.kind != "text":
            continue
        if not query:
            if param.required:
                return None
            continue
        out[param.name] = query
    return out


# --------------------------------------------------------------------------- the ids

# The prefix the fetched candidates are renumbered under. Renumbered for
# `affordances.py`'s reason and not for tidiness: an option id is a dict KEY in the
# request, `adapters.base._ID` puts no length limit on one, and a service that minted its
# id from a search result would put unbounded internet text into that key. `x` is what a
# harvested service option already wears, so these get their own letter and the two can
# never collide inside one option map.
FETCHED = "s"


def renumber(found: tuple[Affordance, ...]) -> list[Affordance]:
    """The fetched candidates under ids code minted, IN THE ORDER THE ADAPTER RANKED THEM.

    The order is load bearing and it is the reason this does not go back through
    `affordances.harvest`. A harvest re-ranks everything by how much of it the speaker
    said (`affordances.Ranked.key`), which is right for a mixed list of windows and
    workspaces and wrong for search results: "the second one" is an index into the list
    Spotify returned, and a list this repository reshuffled is one no ordinal can name.
    """
    return [replace(item, id=f"{FETCHED}{n}") for n, item in enumerate(found, 1)]


@dataclass(frozen=True)
class Note:
    """What the extra turn did, for `hyprsay inspect --last`.

    Ids and counts and a reason, never a label: every id here was minted by code, while
    every label came off the internet.
    """

    capability: str = ""
    query_chars: int = 0  # the length, never the words: they are the speaker's own
    fetched: int = 0
    chose: str = ""
    stopped: str = ""
    fetch_ms: float = 0.0
    asked_ms: float = 0.0
    extra: dict[str, object] = field(default_factory=dict)

    def describe(self) -> dict[str, object]:
        return {
            "capability": self.capability,
            "query_chars": self.query_chars,
            "fetched": self.fetched,
            "chose": self.chose,
            "stopped": self.stopped,
            "fetch_ms": round(self.fetch_ms, 1),
            "asked_ms": round(self.asked_ms, 1),
            **self.extra,
        }


__all__ = [
    "ASKING",
    "FETCHED",
    "FETCH_S",
    "LOOP_S",
    "MAX_QUERY",
    "MAX_TURNS",
    "NAMING",
    "ORDINAL_DIGITS",
    "ORDINAL_WORDS",
    "PICKING",
    "REHEARABLE",
    "SENTENCE",
    "TRAILING",
    "Budget",
    "Note",
    "Stopped",
    "fetchable",
    "needs_fetching",
    "renumber",
    "slots_for",
    "strip_carrier",
]
