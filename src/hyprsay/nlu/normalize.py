"""Turn what the recognizer wrote into what the grammar can match.

The grammar is exact on purpose, so every spelling the recognizers really produce has
to be folded into one form first. Measured on this machine over eight spoken commands,
five recognizers and four audio conditions (docs/research/live/stt_noise_out.json):

- Every backend decorates: "Switch to Workspace 3.", "Focus, Kitty.". Case and
  punctuation carry no information about the command.
- The same number arrives as "3", "three" and "Three", and under noise the homophone
  wins: moonshine wrote "Move window to workspace too." at 10 dB SNR.
- Compounds split. "Toggle full screen." is the majority spelling on all five
  backends, and parakeet wrote "work space three" at 3 dB SNR.
- A bad microphone produces near misses: "Open firefuck.", "Taggle full screen.".
  Those are not repaired in place. They become `variants`, alternative readings that
  the grammar may try and that Jev is shown, because code proposing "firefox" is a
  guess and the text must keep saying what was heard.
- Some outputs are beyond repair ("Toggle folks, Korean.", "Toggle both screens.").
  They must stay unparseable so the cloud recognizer or Jev is asked instead.

Number repair is slot aware because the homophones are ordinary words: "to" and "for"
become digits only at the end of the utterance right after a word that takes a number
("workspace to", "send it to for"), never in "switch to workspace 3". "one" is a
pronoun at least as often as a number ("the second one", "close this one"), so it
becomes a digit only where a number is expected.

Every token keeps its offsets into the RAW transcript. Dictated text is cut from the
raw string by those offsets, so typing keeps the casing and punctuation the speaker
got from the recognizer and never sees a repair that was meant for commands.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache

MAX_VARIANTS = 3
VARIANT_THRESHOLD = 0.8
# below four letters nearly every word is within 0.8 of some other word
_MIN_VARIANT_LEN = 4


@dataclass(frozen=True)
class Token:
    text: str
    start: int  # offsets into the RAW transcript
    end: int


@dataclass(frozen=True)
class Normalized:
    text: str
    raw: str
    tokens: tuple[Token, ...]
    variants: tuple[str, ...]


# "%" is a token so that "40%" reads as "40 percent"
_WORD = re.compile(r"[^\W_]+(?:['\u2019][^\W_]+)*|%")
_ORDINAL_DIGITS = re.compile(r"(\d+)(?:st|nd|rd|th)?")
_VOCAB_WORD = re.compile(r"[^\W_]+")

_FILLERS_ANYWHERE = frozenset({"uh", "um", "uhm", "umm", "er", "erm", "ah", "hmm", "mm", "please"})
_FILLERS_LEADING = (
    ("i", "would", "like", "you", "to"),
    ("i", "would", "like", "to"),
    ("i'd", "like", "you", "to"),
    ("i'd", "like", "to"),
    ("i", "want", "you", "to"),
    ("i", "need", "you", "to"),
    ("i", "want", "to"),
    ("go", "ahead", "and"),
    ("can", "you"),
    ("could", "you"),
    ("would", "you"),
    ("will", "you"),
    ("hey",),
    ("hi",),
    ("okay",),
    ("ok",),
    ("alright",),
    ("so",),
    ("well",),
    ("yo",),
    ("just",),
    ("kindly",),
)
_FILLERS_TRAILING = (("thank", "you"), ("thanks",), ("for", "me"), ("now",), ("okay",), ("ok",))

_UNITS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
}
_TENS = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
_ORDINALS = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
}
_HOMOPHONES = {"to": 2, "too": 2, "for": 4, "fore": 4, "won": 1, "ate": 8, "tree": 3, "free": 3}
# words that are directly followed by a number
_NUMBER_HEADS = frozenset({"workspace", "desktop", "number"})
# a homophone after one of these is a number only when it ends the utterance
_NUMBER_PREPOSITIONS = frozenset({"to", "on"})
_ONE_FOLLOWS = _NUMBER_HEADS | {"to", "on", "at", "by", "volume"}


def _words(block: str) -> frozenset[str]:
    return frozenset(block.split())


# Every word the grammar matches literally. It feeds split-word repair ("work space")
# and is what a misheard command word ("taggle") may be corrected toward.
COMMAND_WORDS = _words(
    """
    focus switch go jump show select activate raise bring take close quit open launch
    start run move send put throw toggle fullscreen maximize float floating tile tiled
    unfloat workspace desktop window app application next previous left right up down
    above below make bigger larger smaller wider narrower taller shorter grow enlarge
    expand shrink reduce resize volume sound audio mute unmute louder quieter softer
    turn increase decrease raise lower percent play pause resume skip song track music
    type say dictate write lock screen undo revert again repeat cancel abort stop help
    commands number pick choose option nope nevermind this that it here current active
    """
)
# The verbs of operations that destroy or inject something. A near miss is never
# corrected TOWARD one of these: the speaker has to have said the word.
_NEVER_A_VARIANT = frozenset({"close", "quit", "lock", "type", "say", "dictate", "write"})
# ordinary words that must not be "corrected" into an app name
_PLAIN_WORDS = _words(
    """
    this that these those with from what here there have will would could should think
    about your them they then than when where which some more most much very just like
    been were does into onto over also only other another little window please thing
    """
)


def normalize(
    raw: str,
    vocabulary: Iterable[str] = (),
    aliases: Mapping[str, str] | None = None,
) -> Normalized:
    """Fold a raw transcript into the one spelling the grammar matches.

    `vocabulary` is the live lexicon (app names, window classes, workspace names).
    `aliases` maps one spoken word to its replacement ("browser" -> "firefox").
    """
    known = _prepare(tuple(vocabulary))
    tokens = _tokenize(raw)
    tokens = _strip_fillers(tokens)
    tokens = _join_split_words(tokens, known.joinable)
    tokens = _numbers(tokens)
    tokens = _apply_aliases(tokens, aliases or {})
    return Normalized(
        text=" ".join(t.text for t in tokens),
        raw=raw,
        tokens=tuple(tokens),
        variants=_variants(tokens, known),
    )


# --------------------------------------------------------------------------- steps


def _tokenize(raw: str) -> list[Token]:
    tokens = []
    for match in _WORD.finditer(raw):
        text = unicodedata.normalize("NFKC", match.group()).lower().replace("\u2019", "'")
        tokens.append(Token("percent" if text == "%" else text, match.start(), match.end()))
    return tokens


def _starts_with(tokens: list[Token], phrase: tuple[str, ...], at: int = 0) -> bool:
    words = [t.text for t in tokens[at : at + len(phrase)]]
    return words == list(phrase)


def _strip_fillers(tokens: list[Token]) -> list[Token]:
    tokens = [t for t in tokens if t.text not in _FILLERS_ANYWHERE]
    stripped = True
    while stripped:
        stripped = False
        for phrase in _FILLERS_LEADING:
            if _starts_with(tokens, phrase):
                tokens = tokens[len(phrase) :]
                stripped = True
                break
    stripped = True
    while stripped:
        stripped = False
        for phrase in _FILLERS_TRAILING:
            at = len(tokens) - len(phrase)
            if at >= 0 and _starts_with(tokens, phrase, at):
                tokens = tokens[:at]
                stripped = True
                break
    return tokens


def _join_split_words(tokens: list[Token], joinable: frozenset[str]) -> list[Token]:
    out: list[Token] = []
    i = 0
    while i < len(tokens):
        for width in (3, 2):
            parts = tokens[i : i + width]
            joined = "".join(t.text for t in parts)
            if len(parts) == width and joined in joinable and _may_join(parts, joined):
                out.append(Token(joined, parts[0].start, parts[-1].end))
                i += width
                break
        else:
            out.append(tokens[i])
            i += 1
    return out


def _may_join(parts: list[Token], joined: str) -> bool:
    if joined in COMMAND_WORDS:
        return True
    # "open office" is a verb and a name even when "openoffice" is installed
    return not any(t.text in COMMAND_WORDS or t.text in _UNITS or t.text.isdigit() for t in parts)


def _numbers(tokens: list[Token]) -> list[Token]:
    out: list[Token] = []
    i = 0
    while i < len(tokens):
        word = tokens[i].text
        prev = out[-1].text if out else None
        nxt = tokens[i + 1].text if i + 1 < len(tokens) else None
        value, used = _number_at(word, prev, nxt, alone=len(tokens) == 1)
        if value is None:
            out.append(tokens[i])
        else:
            out.append(Token(str(value), tokens[i].start, tokens[i + used - 1].end))
        i += used
    return out


def _number_at(word: str, prev: str | None, nxt: str | None, alone: bool) -> tuple[int | None, int]:
    """The number that starts at `word`, and how many tokens it spans."""
    if word[0].isdigit():
        match = _ORDINAL_DIGITS.fullmatch(word)
        return (int(match.group(1)), 1) if match else (None, 1)
    if word in ("one", "a") and nxt == "hundred":
        return 100, 2
    if word == "hundred":
        return 100, 1
    if word in _TENS:
        if nxt in _UNITS and 1 <= _UNITS[nxt] <= 9:
            return _TENS[word] + _UNITS[nxt], 2
        return _TENS[word], 1
    if word == "one":
        return (1, 1) if prev is None or prev in _ONE_FOLLOWS else (None, 1)
    if word in _UNITS:
        return _UNITS[word], 1
    if word in _ORDINALS:
        return _ORDINALS[word], 1
    if word in _HOMOPHONES and _homophone_is_a_number(prev, nxt, alone):
        return _HOMOPHONES[word], 1
    return None, 1


def _homophone_is_a_number(prev: str | None, nxt: str | None, alone: bool) -> bool:
    if alone or nxt == "percent":
        return True
    # "switch workspace to three" keeps its "to": a number slot is the end of the utterance
    return nxt is None and (prev in _NUMBER_HEADS or prev in _NUMBER_PREPOSITIONS)


def _apply_aliases(tokens: list[Token], aliases: Mapping[str, str]) -> list[Token]:
    if not aliases:
        return tokens
    out: list[Token] = []
    for token in tokens:
        target = aliases.get(token.text)
        if target is None:
            out.append(token)
            continue
        # a replacement of several words shares the span of the word that was spoken
        out.extend(Token(word, token.start, token.end) for word in target.lower().split())
    return out


# --------------------------------------------------------------------------- variants


@dataclass(frozen=True)
class _Vocabulary:
    joinable: frozenset[str]  # a split word may be joined into one of these
    correctable: frozenset[str]  # a near miss may be corrected toward one of these
    # two indexes over `correctable`, so a long utterance does not score the whole lexicon
    by_initial: Mapping[str, tuple[str, ...]]
    by_tail: Mapping[str, tuple[str, ...]]

    def near(self, heard: str) -> set[str]:
        """Words that start with the same sound, or differ only in the first letter."""
        words = {*self.by_initial.get(_initial(heard), ()), *self.by_tail.get(heard[1:], ())}
        return {w for w in words if abs(len(w) - len(heard)) <= 2}


_SOUND_ALIKE_INITIALS = {"c": "k", "q": "k", "p": "f", "v": "f", "z": "s"}


def _initial(word: str) -> str:
    return _SOUND_ALIKE_INITIALS.get(word[0], word[0])


@lru_cache(maxsize=8)
def _prepare(vocabulary: tuple[str, ...]) -> _Vocabulary:
    single: set[str] = set()
    every: set[str] = set()
    for entry in vocabulary:
        words = _VOCAB_WORD.findall(unicodedata.normalize("NFKC", entry).lower())
        every.update(words)
        if len(words) == 1:
            single.update(words)
    correctable = {
        w
        for w in every | COMMAND_WORDS
        if len(w) >= _MIN_VARIANT_LEN and not w.isdigit() and w not in _NEVER_A_VARIANT
    }
    by_initial: dict[str, list[str]] = {}
    by_tail: dict[str, list[str]] = {}
    for word in sorted(correctable):
        by_initial.setdefault(_initial(word), []).append(word)
        by_tail.setdefault(word[1:], []).append(word)
    return _Vocabulary(
        joinable=frozenset(single | COMMAND_WORDS),
        correctable=frozenset(correctable),
        by_initial={k: tuple(v) for k, v in by_initial.items()},
        by_tail={k: tuple(v) for k, v in by_tail.items()},
    )


def _similarity() -> Callable[[str, str], float]:
    try:
        from hyprsay.lexicon import similarity
    except ImportError:
        return lambda a, b: difflib.SequenceMatcher(None, a, b).ratio()
    return similarity


def _variants(tokens: list[Token], vocabulary: _Vocabulary) -> tuple[str, ...]:
    similarity = _similarity()
    repairs: list[tuple[float, int, str]] = []
    for i, token in enumerate(tokens):
        heard = token.text
        if len(heard) < _MIN_VARIANT_LEN or heard.isdigit() or heard in _PLAIN_WORDS:
            continue
        if heard in vocabulary.joinable or heard in vocabulary.correctable:
            continue
        score, word = max(
            ((similarity(heard, w), w) for w in vocabulary.near(heard)), default=(0, "")
        )
        if score >= VARIANT_THRESHOLD:
            repairs.append((score, i, word))
    # best repair first; position breaks ties so the order is stable
    repairs.sort(key=lambda r: (-r[0], r[1]))
    words = [t.text for t in tokens]
    return tuple(
        " ".join([*words[:i], word, *words[i + 1 :]]) for _, i, word in repairs[:MAX_VARIANTS]
    )
