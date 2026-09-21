"""The live lexicon: which words may name an app or a window, and who vouches for them.

The speech recognizer hands us "focus firefuck" or "open the kiddy terminal". This
module turns such words into apps and windows, and it is also where the most
important safety rule of hyprsay is enforced: only a TRUSTED field may select or
corroborate a target (docs/PLAN.md 5.6).

What is trusted, and why:

- `Name`, `GenericName`, `Categories` (as a kind), `Keywords`, the desktop id and
  `StartupWMClass` of a .desktop file that a user process cannot rewrite: the file,
  its directory and every ancestor are owned by root and writable by root alone.
- The same fields of a user-writable .desktop file, but only after the owner approved
  that exact file by hash (`safety.approved_desktop_files`, "id:sha256").
- User aliases from the config file.
- A window's class. It is set by the client, so a local process can lie, but a local
  process already runs as the user. The threat here is remote: a web page.

What is never trusted: a window title. A hostile page can set its title to every
command word and every app name at once. So `match_apps`, `match_windows` and
`corroborates` never read a title. `title_discriminates` is the single reader, and it
can only break a tie among windows that a trusted field already selected.

Measured on the development machine: 236 entries in /usr/share/applications, all
root-owned, two of them (VS Code's) group-writable for group root, which is why
`root_owned_and_closed` tolerates exactly that. ~/.local/share/applications and the
per-user Flatpak export directory are owned by the user, so Chrome web apps and user
Flatpaks start untrusted. Flatpak exports are symlinks, so trust is judged on the
resolved file. A scan takes about 0.2 s, a query 1 to 10 ms. google-chrome.desktop
carries `StartupWMClass` twice and `Name=` again in every `[Desktop Action]` group,
so only the `[Desktop Entry]` group is read and every class value is kept.

Fuzzy matching is deliberately narrow. Recognizer errors are phonetic ("firefuck",
"kiddy"), so similarity compares how words sound more than how they are spelled, and
it runs only against trusted NAME tokens of four letters or more. Kinds, keywords,
ids and classes match by exact token.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import stat
import unicodedata
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .config import Config
from .model import App, DesktopState, Window

# --------------------------------------------------------------------------- text


def sanitize(text: str) -> str:
    """NFKC, no control or format code points, single spaces.

    Whitespace is turned into a space before the strip, because tab and newline are
    themselves control characters and would otherwise glue two words together.
    Zero-width and bidi characters are category Cf and simply vanish.
    """
    text = unicodedata.normalize("NFKC", text)
    spaced = (" " if ch.isspace() else ch for ch in text)
    kept = (ch for ch in spaced if unicodedata.category(ch) not in {"Cc", "Cf"})
    return " ".join("".join(kept).split())


def _script(ch: str) -> str:
    # the first word of a letter's Unicode name is its script: LATIN, CYRILLIC, GREEK
    return unicodedata.name(ch, "UNKNOWN").split(" ", 1)[0]


def mixed_script(token: str) -> bool:
    """True when Latin letters share a token with letters of another script.

    "kitty" spelled with a Cyrillic "к" looks identical on screen and is a different
    string. No honest app name or spoken word mixes scripts inside one token.
    """
    scripts = {_script(ch) for ch in token if ch.isalpha()}
    return "LATIN" in scripts and len(scripts) > 1


_TOKEN = re.compile(r"[^\W_]+")


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(sanitize(text).casefold())


def _singular(token: str) -> str:
    return token[:-1] if len(token) > 3 and token.endswith("s") else token


# --------------------------------------------------------------------------- sound

_VOWELS = frozenset("aeiouy")
_SOFTENERS = frozenset("eiy")
_SAME = frozenset("klmnr")
_VOICED = {"b": "P", "p": "P", "d": "T", "t": "T", "v": "F", "f": "F", "z": "S", "s": "S"}
# (pattern, code) tried in order at each position; longest and most specific first
_CLUSTERS = (
    ("tion", "XN"),
    ("sion", "XN"),
    ("tch", "X"),
    ("sch", "SK"),
    ("ph", "F"),
    ("sh", "X"),
    ("th", "T"),
    ("ck", "K"),
    ("qu", "K"),
    ("wh", "W"),
)
_SILENT_STARTS = {"kn": "N", "gn": "N", "wr": "R", "ps": "S"}


def _letters(word: str) -> str:
    folded = unicodedata.normalize("NFKD", word).lower()
    return "".join(ch for ch in folded if "a" <= ch <= "z")


@lru_cache(maxsize=4096)
def _sound(word: str) -> str:
    """Spell a word the way it sounds: consonant classes in capitals, "a" per vowel run.

    Voiced and unvoiced pairs share a class (d/t, b/p, v/f, z/s, g/k) because that is
    exactly the confusion a recognizer makes on a short command. Spaces are dropped,
    so "fire fox" and "firefox" are the same sound.
    """
    s = re.sub(r"(.)\1+", r"\1", _letters(word))
    if len(s) > 3 and s.endswith("e") and s[-2] not in _VOWELS:
        s = s[:-1]  # silent final e: "chrome", "code"
    out: list[str] = []

    def emit(code: str) -> None:
        # a repeated symbol is one sound: "ck" after "c", a run of vowels, "sc" in "science"
        for symbol in code:
            if not out or out[-1] != symbol:
                out.append(symbol)

    i, n = 0, len(s)
    if s[:2] in _SILENT_STARTS:
        emit(_SILENT_STARTS[s[:2]])
        i = 2
    while i < n:
        ch = s[i]
        nxt = s[i + 1] if i + 1 < n else ""
        cluster = next(((p, c) for p, c in _CLUSTERS if s.startswith(p, i)), None)
        if ch in _VOWELS:
            emit("a")
            i += 1
        elif cluster:
            emit(cluster[1])
            i += len(cluster[0])
        elif ch == "c":
            if nxt == "h":
                # "chrome", "christmas": ch before a consonant is a K
                after = s[i + 2] if i + 2 < n else ""
                emit("K" if after and after not in _VOWELS else "X")
                i += 2
            else:
                emit("S" if nxt in _SOFTENERS else "K")
                i += 1
        elif ch == "g":
            if nxt == "h":
                after = s[i + 2] if i + 2 < n else ""
                if i == 0 or after in _VOWELS:
                    emit("K")  # "ghost", "spaghetti"; silent in "night", "light"
                i += 2
            else:
                emit("J" if i > 0 and nxt in _SOFTENERS else "K")
                i += 1
        elif ch == "d" and nxt == "g":
            emit("J")  # "widget", "edge"
            i += 2
        elif ch == "x":
            emit("KS")
            i += 1
        elif ch == "q":
            emit("K")
            i += 1
        elif ch == "j":
            emit("J")
            i += 1
        elif ch == "m" and nxt == "b" and i + 2 == n:
            emit("M")  # "thumb", "climb"
            i += 2
        elif ch in ("w", "h"):
            if nxt in _VOWELS:
                emit(ch.upper())
            i += 1
        elif ch in _VOICED:
            emit(_VOICED[ch])
            i += 1
        elif ch in _SAME:
            emit(ch.upper())
            i += 1
        else:
            i += 1
    return "".join(out)


def phonetic_key(word: str) -> str:
    """A compact Metaphone-like key: the consonant skeleton, plus "A" for a vowel onset.

    "kiddy" and "kitty" are both "KT"; "fire fox" and "firefox" are both "FRFKS".
    """
    sound = _sound(word)
    head = "A" if sound.startswith("a") else ""
    return head + sound.replace("a", "")


def _edit_distance(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    row = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, cb in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (ca != cb))
    return row[-1]


def _ratio(a: str, b: str) -> float:
    longest = max(len(a), len(b))
    return 1.0 - _edit_distance(a, b) / longest if longest else 0.0


_SOUND_WEIGHT = 0.6
_PREFIX_BONUS = 0.1
_PREFIX_FLOOR = 0.7


@lru_cache(maxsize=8192)
def similarity(a: str, b: str) -> float:
    """How likely two words are the same word, misheard. 0 to 1.

    Forty percent spelling, sixty percent sound, then the Winkler idea: two words that
    already agree well and also start with the same sounds get a bonus, because a
    recognizer garbles the tail of a word far more often than its onset. The bonus is
    withheld below 0.7 so that "firefox" and "files" do not profit from sharing "fi".
    """
    a, b = _letters(a), _letters(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    sa, sb = _sound(a), _sound(b)
    score = (1 - _SOUND_WEIGHT) * _ratio(a, b) + _SOUND_WEIGHT * _ratio(sa, sb)
    if score >= _PREFIX_FLOOR:
        shared = len(os.path.commonprefix([sa, sb])[:4])
        score += shared * _PREFIX_BONUS * (1.0 - score)
    return min(score, 1.0)


# --------------------------------------------------------------------------- words


def _wordset(block: str) -> frozenset[str]:
    return frozenset(block.split())


# Words that carry a command, not a target. They are removed from an utterance before
# corroboration, so an app or a title that calls itself "Focus" or "Close" gains nothing.
STOPWORDS: frozenset[str] = _wordset(
    """
    a an the this that these those it its here there my me i you your we us
    to on in at of with for from into onto and or by as is are be do does can could
    would will please just now then ok okay hey let lets s
    focus switch go goto jump show open launch start run spawn close quit kill exit
    move send put bring take throw resize grow shrink make toggle float floating tile
    tiled fullscreen full maximize minimize pin unpin type write say dictate enter press
    lock unlock undo redo again repeat cancel stop nevermind help select pick choose
    find give get use want need set turn raise lower increase decrease change
    volume mute unmute louder quieter sound play pause resume skip track song
    window windows app apps application applications program programs workspace
    workspaces desktop desktops monitor monitors display displays screen screens
    left right up down next previous prev last back forward first other another
    current active focused same all every some any new
    big bigger biggest small smaller smallest large larger wide wider narrow narrower
    tall taller short shorter tiny huge little bit lot much more less half percent
    zero one two three four five six seven eight nine ten eleven twelve
    second third fourth fifth sixth seventh eighth ninth tenth
    """
)

# Categories -> kind, most specific first. The first category present wins.
_KIND_BY_CATEGORY: tuple[tuple[str, str], ...] = (
    ("WebBrowser", "web browser"),
    ("TerminalEmulator", "terminal"),
    ("FileManager", "file manager"),
    ("Email", "email client"),
    ("InstantMessaging", "chat"),
    ("Chat", "chat"),
    ("IRCClient", "chat"),
    ("VideoConference", "video call"),
    ("IDE", "code editor"),
    ("TextEditor", "code editor"),
    ("Music", "music player"),
    ("Player", "media player"),  # refined by _kind: audio only is a music player
    ("PackageManager", "package manager"),
    ("Calculator", "calculator"),
    ("Calendar", "calendar"),
    ("WordProcessor", "word processor"),
    ("Spreadsheet", "spreadsheet"),
    ("Presentation", "presentation"),
    ("Photography", "photo manager"),
    ("RasterGraphics", "image editor"),
    ("VectorGraphics", "image editor"),
    ("2DGraphics", "image editor"),
    ("Mixer", "volume mixer"),
    ("Monitor", "system monitor"),
    ("Archiving", "archive manager"),
    ("Compression", "archive manager"),
    ("RemoteAccess", "remote desktop"),
    ("Maps", "maps"),
    ("Game", "game"),
    ("Settings", "settings"),
)
_KIND_WORDS: frozenset[str] = frozenset(
    word for _, kind in _KIND_BY_CATEGORY for word in kind.split()
) | {"viewer", "emulator", "messenger", "mail", "tab"}
# a title word that is a command or a kind says nothing about WHICH window
_TITLE_STOPWORDS = STOPWORDS | _KIND_WORDS
# reverse-DNS furniture in desktop ids and classes: "org.gnome.Nautilus"
_ID_NOISE = _wordset(
    "org com net io de app apps application desktop github gitlab freedesktop www kde gnome xfce"
)


def _is_kind_word(token: str) -> bool:
    return token in _KIND_WORDS or _singular(token) in _KIND_WORDS


def _kind(categories: Sequence[str], generic_name: str) -> str:
    present = set(categories)
    for category, kind in _KIND_BY_CATEGORY:
        if category not in present:
            continue
        if category == "Player" and "Audio" in present and "Video" not in present:
            return "music player"
        return kind
    if "Viewer" in present and "Graphics" in present:
        return "image viewer"
    if "Viewer" in present and "Office" in present:
        return "document viewer"
    generic = sanitize(generic_name).lower()
    return generic if len(generic) <= 40 else ""


# --------------------------------------------------------------------------- .desktop files

_MAX_DESKTOP_BYTES = 256 * 1024
_STRING_ESCAPES = {"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\"}
# every field code of the spec, plus the deprecated ones launchers still meet
_FIELD_CODE = re.compile(r"%[fFuUdDnNickvm]")


def _unescape(value: str) -> str:
    return re.sub(r"\\(.)", lambda m: _STRING_ESCAPES.get(m.group(1), m.group(0)), value)


def parse_exec(value: str) -> tuple[str, ...]:
    """`Exec` as an argument vector with field codes removed, or () when it cannot be read.

    The spec quotes with double quotes and escapes `"`, `` ` ``, `$` and `\\` inside
    them; shlex handles the first and last, the other two are unescaped here first.
    Nothing is ever expanded: `$HOME` stays the five characters it is. An argument
    that embeds a field code ("--url=%u") is dropped whole, since we launch without
    files and a dangling "--url=" is worse than no flag.
    """
    line = re.sub(r"\\(.)", lambda m: m.group(1) if m.group(1) in "$`" else m.group(0), value)
    try:
        words = shlex.split(line, posix=True)
    except ValueError:
        return ()
    argv: list[str] = []
    for word in words:
        if _FIELD_CODE.search(word.replace("%%", "")):
            continue
        argv.append(word.replace("%%", "%"))
    # flatpak's file forwarding brackets, left empty once "%u" is gone: "@@u @@"
    cleaned: list[str] = []
    for word in argv:
        if word == "@@" and cleaned and cleaned[-1] in ("@@", "@@u"):
            cleaned.pop()
        else:
            cleaned.append(word)
    return tuple(cleaned)


_KEYS = frozenset(
    {
        "Type",
        "Name",
        "GenericName",
        "Exec",
        "Categories",
        "Keywords",
        "StartupWMClass",
        "NoDisplay",
        "Hidden",
    }
)


def _main_group(text: str) -> dict[str, list[str]]:
    """The keys we use, unlocalized, from `[Desktop Entry]` only.

    Actions repeat Name= and Exec=, and a KDE file carries a hundred Name[xx]= lines,
    which is where a naive parser spends its time.
    """
    keys: dict[str, list[str]] = {}
    inside = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            inside = line == "[Desktop Entry]"
            continue
        if not inside or line.startswith("#"):
            continue
        key, found, value = line.partition("=")
        key = key.strip()
        if found and key in _KEYS:
            keys.setdefault(key, []).append(_unescape(value.strip()))
    return keys


def _list_value(value: str) -> tuple[str, ...]:
    return tuple(item for item in (sanitize(part) for part in value.split(";")) if item)


def parse_desktop_entry(
    data: bytes, desktop_id: str, *, trusted: bool = False, source: str = ""
) -> App | None:
    """One launchable application, or None for anything a launcher would not list."""
    keys = _main_group(data.decode("utf-8", errors="replace"))

    def last(key: str) -> str:
        return keys.get(key, [""])[-1]

    if last("Type") != "Application" or "true" in (last("NoDisplay"), last("Hidden")):
        return None
    name = sanitize(last("Name"))
    exec_argv = parse_exec(last("Exec"))
    if not name or not exec_argv:
        return None
    generic = sanitize(last("GenericName"))
    categories = _list_value(last("Categories"))
    classes = [sanitize(value) for value in keys.get("StartupWMClass", [])] + [desktop_id]
    return App(
        id=desktop_id,
        name=name,
        generic_name=generic,
        kind=_kind(categories, generic),
        keywords=_list_value(last("Keywords")),
        categories=categories,
        exec_argv=exec_argv,
        wm_classes=tuple(dict.fromkeys(c for c in classes if c)),
        trusted=trusted,
        source=source,
    )


# --------------------------------------------------------------------------- trust


def root_owned_and_closed(st: os.stat_result, groups: Collection[int] | None = None) -> bool:
    """Owned by root, and nobody but root may write.

    World-writable never passes. Group-writable passes only when the group is root's
    (gid 0) and this process is not in it: /usr/share/applications/code.desktop ships as
    0775 root:root on the development machine, and that bit lets no user process in.
    `groups` is the caller's group ids, read from the process when not given.
    """
    if st.st_uid != 0 or st.st_mode & stat.S_IWOTH:
        return False
    if not st.st_mode & stat.S_IWGRP:
        return True
    mine = {os.getegid(), *os.getgroups()} if groups is None else set(groups)
    return st.st_gid == 0 and 0 not in mine


def trusted_location(path: Path, stat_fn: Callable[[Path], os.stat_result] = os.stat) -> bool:
    """Can a process running as the user change what this .desktop file says?

    False means yes, or that we could not tell. Symlinks are resolved first (Flatpak
    exports are symlinks), then the real file, the directory that lists it, and every
    ancestor of both must be root's and closed: a root-owned file inside a directory
    the user owns can be swapped by renaming the directory.
    """
    try:
        real = path.resolve(strict=True)
        if not stat.S_ISREG(stat_fn(real).st_mode):
            return False
        chain = {real, *real.parents, *path.parent.resolve(strict=True).parents, path.parent}
        return all(root_owned_and_closed(stat_fn(p)) for p in chain)
    except OSError:
        return False


def approval_token(desktop_id: str, data: bytes) -> str:
    """The "id:sha256" string that `safety.approved_desktop_files` holds."""
    return f"{desktop_id}:{hashlib.sha256(data).hexdigest()}"


def default_dirs(env: Mapping[str, str] | None = None) -> tuple[Path, ...]:
    """Application directories in XDG precedence order: the user's first.

    `XDG_DATA_DIRS` is honoured, but being listed grants nothing: every file still
    has to pass `trusted_location`.
    """
    env = os.environ if env is None else env
    home = Path(env.get("HOME") or Path.home())
    data_home = Path(env.get("XDG_DATA_HOME") or home / ".local/share")
    roots = [data_home, home / ".local/share/flatpak/exports/share"]
    roots += [Path(p) for p in env.get("XDG_DATA_DIRS", "").split(":") if p.startswith("/")]
    roots += [Path("/usr/local/share"), Path("/usr/share"), Path("/var/lib/flatpak/exports/share")]
    return tuple(dict.fromkeys(root / "applications" for root in roots))


def _desktop_files(directory: Path) -> Iterator[tuple[str, Path]]:
    try:
        found = sorted(directory.rglob("*.desktop"))
    except OSError:
        return
    for path in found:
        # the spec's id for applications/kde/kate.desktop is "kde-kate"
        relative = path.relative_to(directory).with_suffix("")
        yield "-".join(relative.parts), path


def _read(path: Path) -> bytes | None:
    try:
        if path.stat().st_size > _MAX_DESKTOP_BYTES:
            return None
        return path.read_bytes()
    except OSError:
        return None


# --------------------------------------------------------------------------- the lexicon

_W_NAME = 1.0
_W_CLASS = 1.0  # desktop id and wm class tokens
_W_KIND = 0.8  # GenericName and kind: many apps share one, so it must rank below a name
# a kind word inside a name or an id. "Code" in "Visual Studio Code" is also its kind
# and should beat other editors; "Browser" in "Avahi SSH Server Browser" is incidental
# and must not compete with a real web browser for "the browser".
_W_NAME_AND_KIND = 0.9
_W_INCIDENTAL = 0.55
_W_KEYWORD = 0.6
_W_WINDOW_CLASS = 0.9  # a window's own class, when no trusted app claims it
# a misheard name token must reach this to count at all in a match
_FUZZY_FLOOR = 0.75
# and this to corroborate (docs/PLAN.md 5.6)
_CORROBORATE = 0.85
_FUZZY_MIN_LETTERS = 4
# results below this are not candidates, they are noise
_MATCH_FLOOR = 0.5
# how much of a full score depends on the phrase covering the whole name:
# "chrome" finds "Google Chrome", but "google chrome" finds it better
_NAME_COVERAGE = 0.15


def _name_similarity(token: str, name_token: str) -> float:
    """Exact, plural, or misheard. The only fuzzy comparison in this module.

    Short words are excluded because everything sounds like "vlc" a little, and the
    onsets must agree because that is what a recognizer gets right.
    """
    if token == name_token or _singular(token) == _singular(name_token):
        return 1.0
    if min(len(token), len(name_token)) < _FUZZY_MIN_LETTERS:
        return 0.0
    if phonetic_key(token)[:1] != phonetic_key(name_token)[:1]:
        return 0.0
    return similarity(token, name_token)


def _class_tokens(*classes: str) -> frozenset[str]:
    tokens = {t for cls in classes for t in _tokens(cls)}
    return frozenset(t for t in tokens if t not in _ID_NOISE and len(t) > 1 and not t.isdigit())


@dataclass(frozen=True)
class _Anchors:
    """The trusted words of one app, ready to be matched."""

    name: tuple[str, ...]
    exact: Mapping[str, float]  # every other trusted token -> its weight

    @classmethod
    def of(cls, app: App) -> _Anchors:
        exact: dict[str, float] = {}
        own_kind = set(_tokens(f"{app.generic_name} {app.kind}"))

        def add(tokens: Iterable[str], weight: float) -> None:
            for token in tokens:
                if len(token) > 1 and not mixed_script(token):
                    exact[token] = max(exact.get(token, 0.0), weight)

        def as_kind_word(token: str) -> float:
            own = token in own_kind or _singular(token) in own_kind
            return _W_NAME_AND_KIND if own else _W_INCIDENTAL

        add(_tokens(" ".join(app.keywords)), _W_KEYWORD)
        add(own_kind, _W_KIND)
        for ident in (app.id, *app.wm_classes):
            tokens = _class_tokens(ident)
            for token in tokens:
                # a whole id is a name in its own right: "code", "kitty"
                plain = len(tokens) == 1 or not _is_kind_word(token)
                add([token], _W_CLASS if plain else as_kind_word(token))
        spelled = [t for t in _tokens(app.name) if not mixed_script(t)]
        proper = [t for t in spelled if not _is_kind_word(t)]
        if proper:
            for token in set(spelled) - set(proper):
                add([token], as_kind_word(token))
        # an app called "Files" or "Terminal" has nothing but kind words for a name
        return cls(name=tuple(proper or spelled), exact=exact)

    def score(self, tokens: Sequence[str]) -> float:
        """How well a spoken phrase names this app, 0 to 1."""
        if not tokens:
            return 0.0
        total, named = 0.0, set()
        for token in tokens:
            near, which = max(((_name_similarity(token, n), n) for n in self.name), default=(0, ""))
            weight = max(self.exact.get(token, 0.0), self.exact.get(_singular(token), 0.0))
            if near >= _FUZZY_FLOOR and near * _W_NAME >= weight:
                named.add(which)
                weight = near * _W_NAME
            total += weight
        covered = len(named) / len(self.name) if self.name else 0.0
        score = total / len(tokens) * (1 - _NAME_COVERAGE + _NAME_COVERAGE * covered)
        if len(tokens) > 1 or len(self.name) > 1:
            # "fire fox" against "Firefox", "libre office" against "LibreOffice"
            joined = _name_similarity("".join(tokens), "".join(self.name))
            score = max(score, joined if joined >= _FUZZY_FLOOR else 0.0)
        return score

    def anchors(self, token: str) -> bool:
        """Does this one spoken token vouch for the app?"""
        if token in self.exact or _singular(token) in self.exact:
            return True
        return any(_name_similarity(token, n) >= _CORROBORATE for n in self.name)


def _content(text: str) -> list[str]:
    """Tokens that could name a target: no command words, numbers or homoglyph tokens."""
    return [
        t
        for t in _tokens(text)
        if t not in STOPWORDS and not t.isdigit() and len(t) > 1 and not mixed_script(t)
    ]


def _contains(haystack: Sequence[str], needle: Sequence[str]) -> bool:
    size = len(needle)
    return bool(size) and any(
        list(haystack[i : i + size]) == list(needle) for i in range(len(haystack) - size + 1)
    )


class Lexicon:
    """Installed apps plus user aliases, queried by spoken phrase.

    `apps` holds everything that was found, untrusted entries included, so `doctor` can
    list what awaits approval. Only trusted entries ever answer a query.
    """

    def __init__(self, apps: Sequence[App], aliases: Mapping[str, str] | None = None) -> None:
        self.apps: tuple[App, ...] = tuple(apps)
        self._trusted = tuple(app for app in self.apps if app.trusted)
        self._anchors = {app: _Anchors.of(app) for app in self._trusted}
        self._by_class: dict[str, list[App]] = {}
        for app in sorted(self.apps, key=lambda a: not a.trusted):
            for cls in app.wm_classes:
                self._by_class.setdefault(cls.casefold(), []).append(app)
        # spoken alias (as tokens) -> what it points at, casefolded: an app id, a name,
        # or failing both a window class
        self._aliases = {
            tuple(_tokens(spoken)): sanitize(target).casefold()
            for spoken, target in (aliases or {}).items()
            if _tokens(spoken) and not any(mixed_script(t) for t in _tokens(spoken))
        }

    # ----------------------------------------------------------------- building

    @classmethod
    def scan(cls, cfg: Config, dirs: Sequence[Path] | None = None) -> Lexicon:
        """Read the application directories, highest XDG precedence first.

        For each desktop id the first TRUSTED file wins. An untrusted file that
        shadows it is kept in `apps` as untrusted, so it can be listed and approved,
        but it does not replace the system entry and inherits nothing from it. This
        way dropping a firefox.desktop into ~/.local/share/applications neither
        hijacks "open firefox" nor breaks it.
        """
        approved = frozenset(cfg.safety.approved_desktop_files)
        winners: dict[str, App] = {}
        shadows: list[App] = []
        for directory in default_dirs() if dirs is None else dirs:
            for desktop_id, path in _desktop_files(Path(directory)):
                settled = winners.get(desktop_id)
                if settled is not None and settled.trusted:
                    continue
                data = _read(path)
                if data is None:
                    continue
                trusted = trusted_location(path) or approval_token(desktop_id, data) in approved
                app = parse_desktop_entry(data, desktop_id, trusted=trusted, source=str(path))
                if app is None:
                    continue
                if settled is None:
                    winners[desktop_id] = app
                elif trusted:
                    shadows.append(settled)
                    winners[desktop_id] = app
        return cls([*winners.values(), *shadows], cfg.aliases)

    # ----------------------------------------------------------------- windows to apps

    def app_for_window(self, window: Window) -> App | None:
        """The app that owns a window, by class. A trusted claim beats an untrusted one.

        Order: a declared class (`StartupWMClass` or the file stem) against `class`, then
        against `initialClass`, then the last segment of a reverse-DNS id, which is how
        "org.mozilla.firefox" meets a window whose class is "firefox".
        """
        seen = [c.casefold() for c in (window.cls, window.initial_class) if c]
        for cls in seen:
            if cls in self._by_class:
                return self._by_class[cls][0]
        for cls in seen:
            for app in self._trusted:
                if "." in app.id and app.id.rsplit(".", 1)[-1].casefold() == cls:
                    return app
        return None

    def _trusted_app_for(self, window: Window) -> App | None:
        app = self.app_for_window(window)
        return app if app is not None and app.trusted else None

    def kind_of(self, window: Window) -> str:
        """The kind of a window's app, "" when unknown.

        An untrusted entry gives no kind: typing policy keys on it, and a user-writable
        file must not be able to relabel a terminal as a text editor.
        """
        app = self._trusted_app_for(window)
        return app.kind if app else ""

    # ----------------------------------------------------------------- vocabulary

    def words(self) -> frozenset[str]:
        """Every trusted token, for the normalizer's split-word and homophone repair."""
        vocabulary: set[str] = set()
        for anchors in self._anchors.values():
            vocabulary.update(anchors.name, anchors.exact)
        for spoken in self._aliases:
            vocabulary.update(spoken)
        return frozenset(w for w in vocabulary if len(w) > 1 and not w.isdigit())

    # ----------------------------------------------------------------- matching

    def _alias_targets(self, tokens: Sequence[str]) -> set[str]:
        return {target for spoken, target in self._aliases.items() if _contains(tokens, spoken)}

    @staticmethod
    def _is_alias_target(app: App, targets: set[str]) -> bool:
        return app.id.casefold() in targets or app.name.casefold() in targets

    def _anchors_of(self, app: App) -> _Anchors:
        return self._anchors.get(app) or _Anchors.of(app)

    def match_apps(self, phrase: str, limit: int = 5) -> list[tuple[App, float]]:
        """Trusted apps a phrase could name, best first. Pass the target words only.

        Every content word of the phrase is expected to name the app, so "firefox with
        youtube" scores lower than "firefox". Split the residual off first (anything
        not in `words()`) and hand it to `title_discriminates`.
        """
        targets = self._alias_targets(_tokens(phrase))
        tokens = _content(phrase)
        scored = []
        for app in self._trusted:
            aliased = self._is_alias_target(app, targets)
            score = 1.0 if aliased else self._anchors[app].score(tokens)
            if score >= _MATCH_FLOOR:
                scored.append((app, round(score, 4)))
        scored.sort(key=lambda pair: (-pair[1], pair[0].name.casefold(), pair[0].id))
        return scored[:limit]

    def match_windows(
        self, phrase: str, state: DesktopState, limit: int = 5
    ) -> list[tuple[Window, float]]:
        """Open windows a phrase could name, best first, then most recently focused.

        A window is found through its trusted app, or through its own class when no
        trusted app claims it. Never through its title.
        """
        targets = self._alias_targets(_tokens(phrase))
        tokens = _content(phrase)
        scored = []
        for window in state.windows:
            app = self._trusted_app_for(window)
            if app is not None:
                score = 1.0 if self._is_alias_target(app, targets) else 0.0
                score = max(score, self._anchors[app].score(tokens))
            else:
                own = _class_tokens(window.cls, window.initial_class)
                hits = sum(1 for t in tokens if t in own)
                score = _W_WINDOW_CLASS * hits / len(tokens) if tokens else 0.0
            if {window.cls.casefold(), window.initial_class.casefold()} & targets:
                score = 1.0
            if score >= _MATCH_FLOOR:
                scored.append((window, round(score, 4)))
        scored.sort(key=lambda pair: (-pair[1], pair[0].focus_rank, pair[0].address))
        return scored[:limit]

    def title_discriminates(
        self, residual: Sequence[str], candidates: Sequence[Window]
    ) -> Window | None:
        """The one candidate whose title holds a residual word no other title holds.

        THE ONLY PLACE A TITLE IS READ. The caller passes windows that a trusted field
        already selected ("the firefox with youtube": all Firefox windows), so a title
        can choose among them and nothing more. Command words and kind words never
        discriminate, and two words pointing at two different windows is a tie.
        """
        titles = [frozenset(t for t in _tokens(w.title) if not mixed_script(t)) for w in candidates]
        chosen: set[int] = set()
        for word in _content(" ".join(residual)):
            if word in _TITLE_STOPWORDS:
                continue
            holders = [i for i, title in enumerate(titles) if word in title]
            if len(holders) == 1:
                chosen.add(holders[0])
        return candidates[chosen.pop()] if len(chosen) == 1 else None

    def corroborates(
        self, utterance: str, *, app: App | None = None, window: Window | None = None
    ) -> bool:
        """Did the speaker actually say something that names this target?

        True only when a content word of the utterance matches a TRUSTED field of the
        app (or of the window's app, or the window's class): exactly, through a user
        alias, or as a mishearing of a name token. Kind words count: "the browser"
        corroborates Firefox. The window's title is never consulted, so a page titled
        "focus close firefox kitty terminal" corroborates nothing.
        """
        if app is not None and not app.trusted:
            return False
        spoken = _tokens(utterance)
        content = _content(utterance)
        targets = self._alias_targets(spoken)
        owner = app if app is not None else self._trusted_app_for(window) if window else None
        if owner is not None:
            anchors = self._anchors_of(owner)
            if self._is_alias_target(owner, targets) or any(anchors.anchors(t) for t in content):
                return True
            # a name the recognizer split in two: "fire fox". Both halves must be content
            # words, or "focus the" would pass for an app called Focus.
            pairs = (
                a + b
                for a, b in zip(spoken, spoken[1:], strict=False)
                if a in content and b in content
            )
            if any(_name_similarity(p, n) >= _CORROBORATE for p in pairs for n in anchors.name):
                return True
        if window is not None:
            own = _class_tokens(window.cls, window.initial_class)
            classes = {window.cls.casefold(), window.initial_class.casefold()}
            return bool(classes & targets) or any(t in own for t in content)
        return False
