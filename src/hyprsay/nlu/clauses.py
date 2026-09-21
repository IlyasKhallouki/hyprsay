"""Where one utterance stops being one command. Code proposes the seams, Jev judges them.

v1 did one action per utterance, and the two commands that started this work were heard
perfectly and obeyed a third of the way: "open zapzap in a new workspace" launched zapzap
and dropped the rest, "open chrome, navigate to youtube and look up ltt" launched chrome
and dropped the rest. Nothing was mis-recognized in either. Nothing ever cut the utterance
in two.

Cutting is not the model's job, and it is not a regex's job either:

- " and " in "open bits and bytes" joins one name. " and " in "open firefox and move it to
  workspace 3" joins two commands. The characters are identical, so no rule over the words
  alone can tell those apart, and a rule that guessed would close a window the speaker
  named half of.
- The same word inside a dictated tail is the speaker's own text: "type hello and goodbye"
  types four words, it does not run two commands. Text from a carrier verb onward is opaque
  here for the same reason it never reaches Jev at all (`understand.DICTATION_OPENERS`,
  docs/PLAN.md 5.6).

So this module only ever proposes. `split_candidates` returns the offsets where a seam
might belong, built by code from a closed list of connectives; the caller asks one Boolean
per offset (`bank.separates`) in the fan-out it was already sending, because question count
is free and payload is not (PLAN 5.5); it hands back the subset that answered yes.
`clauses_for` re-derives the seams from the text itself and uses only the offsets it
proposed, so no answer, and no bug in a caller, can carve the utterance where code never
offered a seam. Every clause is a verbatim slice: `text[c.start : c.end] == c.text`.

At most three seams, so at most four clauses. Four is already a long sentence to hold a
push-to-talk key through; past that the words are far more likely to be talk that happens
to contain "and" than a chain of orders, and the safe reading of talk is to do nothing
extra, so the whole utterance stays one clause.

Binding. "it", "that" and "there" in clause n mean what clause n-1 acted on: "open firefox
and move IT to workspace 3" moves firefox, not whatever happened to be focused when the key
went down. Each `Clause` carries its `index` and the `back_reference` word it used, which
is what the understander needs to aim clause n at clause n-1's result: a window for "it"
and "that", a place for "there". The binding itself belongs to the understander; this
module only makes it expressible. Two rules are already applied here:

- A pronoun in clause 0 is never a back reference. Nothing came before it, so it stays
  deixis and the window pinned at key down answers it, exactly as in v1.
- "this" and "here" are deliberately absent from the list. They point at what the speaker
  is looking at, which is the pinned window, and re-pointing them at the previous clause
  would move a target the speaker never looked away from.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from hyprsay.nlu.normalize import Normalized

MAX_SPLITS = 3

# The words a seam may be proposed at, longest first so that "and then" is one seam and
# not "and" followed by "then". Matched as whole words between spaces; a comma is its own
# case below, because it may carry one of these words right behind it ("chrome, then go").
CONNECTIVES: tuple[str, ...] = ("after that", "and then", "then", "also", "and")
_CONNECTIVE_WORDS = frozenset(word for phrase in CONNECTIVES for word in phrase.split())

# What a later clause may use to mean what an earlier one acted on. See the module
# docstring for why "this" and "here" are not here.
BACK_REFERENCES: frozenset[str] = frozenset({"it", "that", "there"})

_WORD = re.compile(r"[^\W_]+(?:['\u2019][^\W_]+)*")


@dataclass(frozen=True)
class Clause:
    """One command's worth of an utterance, and where it sat in it."""

    text: str
    start: int  # offsets into the normalized utterance the clause was cut from
    end: int
    index: int = 0  # 0 for the first clause; what "it" in clause n reaches back past
    # the word this clause used to point at the previous one, or "" when it names its
    # own target. Always "" at index 0: nothing came before it.
    back_reference: str = ""

    @property
    def refers_back(self) -> bool:
        return bool(self.back_reference)


def split_candidates(normalized: str | Normalized) -> list[int]:
    """The offsets where a seam MIGHT belong, in the order they occur.

    An offset is the first character of the connective run, which is what
    `bank.separates` is asked about and what `clauses_for` takes back. More than
    `MAX_SPLITS` of them is not a compound command, so none are offered.
    """
    text = _text(normalized)
    opaque = _dictated_from(text)
    found = [start for start, _ in _seams(text) if start < opaque]
    return found if len(found) <= MAX_SPLITS else []


def clauses_for(normalized: str | Normalized, splits: Iterable[int]) -> list[Clause]:
    """The clauses that taking `splits` produces, in the order they were spoken.

    `splits` is a subset of what `split_candidates` offered for this same text. Anything
    else in it is ignored rather than honoured: the seams are recomputed here from the
    text, so an offset nobody proposed cannot become a cut.
    """
    text = _text(normalized)
    wanted = set(splits)
    ends = dict(_seams(text))
    taken = [start for start in split_candidates(text) if start in wanted]
    pieces: list[tuple[int, int]] = []
    at = 0
    for start in taken:
        pieces.append((at, start))
        at = ends[start]
    pieces.append((at, len(text)))
    out: list[Clause] = []
    for piece in pieces:
        start, end = _trim(text, *piece)
        if start == end:
            continue
        body = text[start:end]
        index = len(out)
        out.append(Clause(body, start, end, index, _back_reference(body) if index else ""))
    return out


# --------------------------------------------------------------------------- the seams


def _seams(text: str) -> list[tuple[int, int]]:
    """Every connective run as (start, end), non-overlapping, left to right.

    A run needs a command's worth of words on each side. "open firefox and" is one
    command that trailed off, and the "then" in "and then close kitty" has nothing but
    the word "and" in front of it, which is a sentence that starts mid-thought, not two
    commands.
    """
    out: list[tuple[int, int]] = []
    at = previous = 0
    while at < len(text):
        span = _seam_at(text, at)
        if span is None:
            at += 1
            continue
        if _has_words(text[previous : span[0]]) and text[span[1] :].strip():
            out.append(span)
        at = previous = span[1]
    return out


def _seam_at(text: str, at: int) -> tuple[int, int] | None:
    """The connective run starting at `at`, or None. The run includes its own spaces, so
    the clause on the left ends at `at` and the one on the right starts at the end.

    A run swallows every connective it touches, so "and also" and "then also" leave the
    right-hand clause starting at its verb. A clause handed on as "also close kitty"
    would parse as nothing at all.
    """
    if text[at] == ",":
        end, found = _past_spaces(text, at + 1), True
    elif text[at].isspace():
        end, found = at, False
    else:
        return None
    while (past := _connective_at(text, end)) is not None:
        end, found = past, True
    return (at, end) if found else None


def _connective_at(text: str, at: int) -> int | None:
    """The offset past the connective word beginning at or just after `at`, its trailing
    spaces included, or None when there is no whole connective word there."""
    begins = _past_spaces(text, at)
    for word in CONNECTIVES:
        after = begins + len(word)
        if not text.startswith(word, begins):
            continue
        if after == len(text):
            return after
        if text[after].isspace():
            return _past_spaces(text, after)
    return None


def _dictated_from(text: str) -> int:
    """Where the speaker's own words begin, or the end of the text when they never do.

    From the first carrier verb onward the words belong to the speaker, not to the
    grammar, so no seam is offered there however many "and"s the sentence has.
    """
    carriers = _carriers()
    for match in _WORD.finditer(text):
        if match.group().lower() in carriers:
            return match.start()
    return len(text)


def _carriers() -> frozenset[str]:
    """The dictation openers, fetched late on purpose.

    The understander imports this module to cut an utterance up, so importing it back at
    module level would be a cycle that fails on whichever side Python loads first. The
    closed list still lives in exactly one place, `understand.DICTATION_OPENERS`.
    """
    from hyprsay.nlu.understand import DICTATION_OPENERS

    return DICTATION_OPENERS


# --------------------------------------------------------------------------- small parts


def _text(normalized: str | Normalized) -> str:
    """The string the offsets are counted in: a `Normalized`, or its text on its own.

    Read through `getattr` rather than `isinstance`, the way the understander already
    reads tokens: a test's stand-in normalizer is not the real dataclass.
    """
    return getattr(normalized, "text", normalized)


def _past_spaces(text: str, at: int) -> int:
    while at < len(text) and text[at].isspace():
        at += 1
    return at


def _trim(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _back_reference(text: str) -> str:
    words = (m.group().lower() for m in _WORD.finditer(text))
    return next((w for w in words if w in BACK_REFERENCES), "")


def _has_words(fragment: str) -> bool:
    """Is there anything here but joining words? A clause made only of them says nothing."""
    words = {m.group().lower() for m in _WORD.finditer(fragment)}
    return bool(words - _CONNECTIVE_WORDS)
