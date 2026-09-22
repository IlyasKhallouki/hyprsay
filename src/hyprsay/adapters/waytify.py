"""Picking a song, through the owner's own media daemon, by choosing among real results.

WHY WAYTIFY AND NOT THE `spotify_player` CLI. `spotify.py` answers the same question and
this replaces it, for four reasons that are worth stating rather than assuming:

1. Search results come back as live STRUCTURED candidates. waytify's `SearchResult` is
   `{name, subtitle, uri, kind}` and its daemon already asked Spotify for tracks, albums
   and playlists in one request. The CLI path had to be told which block of JSON to read
   and, in its own `--name` form, took `page.items[0]` blind. Structured candidates are
   what `base.candidates` exists to carry, so nothing has to be guessed at any point.
2. There is no process spawn per query. A `busctl` spawn was measured at 17 ms on this
   machine and a CLI spawn is worse (`spotify.py` measured 16 to 22 ms for the process
   floor alone, before the client cold start). A Unix socket connect costs neither.
3. The protocol is the same shape hyprsay already speaks to its own overlay: newline
   delimited JSON over a Unix socket at a path under `XDG_RUNTIME_DIR`, one object per
   line, an override environment variable for a second instance. `hudproto` is the same
   idea, so there is one wire vocabulary in this repository rather than two.
4. It is the owner's own code, its Spotify refresh token already lives in the system
   keyring, and a defect found here is one the owner can fix. The CLI's two-browser-prompt
   `authenticate` had never been run on this machine at all.

WHAT THIS DELIBERATELY DOES NOT OFFER. waytify also exposes the transport commands
(`PlayPause`, `Next`, `Previous`, `Seek`), `SetVolume`, `TransferTo` and the popup. None
of those are here. `mpris.py` already owns transport and names WHICH player it is aimed
at, `ops._volume` already owns volume, and ARCHITECTURE.md's rule is that one knob must
never serve two questions: a second route to the same act is a second set of guards to
keep in step. What is here is the half nothing else on a Linux desktop can do, which is
finding something by name and starting it.

WHAT IT DOES NOT READ. `State` also carries the CURRENT track, the play context, the
queue, the recent list and synced lyrics. None of it is read, because all of it says what
the owner is listening to, which is the class of data docs/PRIVACY.md keeps on the
machine (`mpris.py` gates the same fields for the same reason). A search result is
different in kind: the speaker said the query out loud a moment ago.

TRUST. Everything that comes back over this socket is text from the internet by way of
Spotify: a track name, an artist, a playlist owner's display name, and the daemon's own
error strings. Every label is sanitized and marked UNTRUSTED, which `base.candidates`
enforces again whatever this file sets. A label's only job is to let a human and a model
tell two options apart; it authorizes nothing, and it is never dereferenced, because
`as_option` does not carry the handle.

THE HANDLE IS THE SAFETY BOUNDARY. A Spotify URI is parsed out of JSON that came off the
internet and then goes back over the socket as a command argument, so it is checked
twice: it must match `URI` to become a candidate at all, and `perform` accepts it only if
this adapter minted it (`base.Minted`). Nothing is ever passed to a shell, because
nothing here runs a process.

TESTING WITHOUT A DAEMON. The socket is behind `Dial`, one function that returns a
`Link`. The live one opens `AF_UNIX`; the tests pass a fake driving recorded frames, so
every path below is exercised with no daemon, no account and no network, exactly the way
`spotify.py`'s `Runner` seam works.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import socket
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from hyprsay.adapters.base import (
    MAX_CANDIDATES,
    AdapterError,
    Affordance,
    Capability,
    Context,
    Minted,
    Param,
    Trust,
    tier_for,
)
from hyprsay.lexicon import sanitize
from hyprsay.model import Outcome

# crates/waytify-ipc/src/lib.rs: bumped whenever a change would make an older client
# misread a newer daemon, and its own docstring says clients exit loudly on a mismatch
# rather than guess. So this is a hard equality and not a floor.
PROTOCOL = 1

# crates/waytify-ipc/src/paths.rs, reproduced exactly, override included
SOCKET_ENV = "WAYTIFY_SOCKET"
APP = "waytify"

# spotify:track:6rqhFgbbKwnb9MLmUQDhG6. Every Spotify object id is 22 base62 characters,
# and the three types below are the only ones `Search` can return.
URI = re.compile(r"^spotify:(track|album|playlist):[0-9A-Za-z]{22}$")

# The socket is local, so connecting and handshaking are microseconds. The search leg is
# a transatlantic round trip made by the daemon, so it gets its own, longer ceiling. Both
# are the ceiling that stops a wedged daemon holding a worker thread, not a deadline: the
# caller owns the real deadline.
CONNECT_TIMEOUT_S = 1.0
SEARCH_TIMEOUT_S = 8.0
PLAY_TIMEOUT_S = 4.0

# one utterance may resolve the same words twice (a partial transcript, then the final
# one), and a correction ("no, the second one") asks again within seconds
SEARCH_TTL_S = 20.0
MAX_CACHED_QUERIES = 8
MAX_QUERY = 120

NOT_RUNNING = (
    "the waytify daemon is not running, so Spotify cannot be searched; start it with "
    "`waytify daemon`, or run any waytify command and it starts itself"
)
LOGIN = (
    "waytify has no Spotify account connected, so there is nothing to search; run "
    "`waytify login` once and approve the browser prompt"
)


def socket_path() -> Path:
    """Where waytify listens. `WAYTIFY_SOCKET` wins, taken exactly as given.

    The fallback chain is `paths.rs`'s: `$XDG_RUNTIME_DIR/waytify/sock`, and failing that
    `/tmp/waytify-$USER/sock`, namespaced by user so two accounts cannot collide. Read on
    every call rather than cached, because the override is how a second daemon is run and
    a cached answer would send this at the wrong one.
    """
    override = os.environ.get(SOCKET_ENV)
    if override:
        return Path(override)
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / APP / "sock"
    user = os.environ.get("USER") or "unknown"
    return Path(f"/tmp/{APP}-{user}") / "sock"


@dataclass(frozen=True)
class Found:
    """One search result, as waytify's `SearchResult` puts it on the wire."""

    name: str
    subtitle: str
    uri: str
    kind: str  # "track" | "album" | "playlist"

    @property
    def label(self) -> str:
        """What the speaker and the model see. Untrusted, and cut by `Affordance`."""
        return f"{self.name} - {self.subtitle}" if self.name and self.subtitle else self.name


@dataclass(frozen=True)
class Family:
    """One kind of thing a search returns, and how playback is started for it.

    A track is played on its own; an album or a playlist is a CONTEXT, which Spotify
    starts rather than plays, and waytify keeps those as two commands for that reason
    (`state.rs`: "those are different request bodies").
    """

    kind: str  # waytify's own SearchKind, on the wire
    noun: str  # what the HUD and the model see
    command: str  # the waytify command that starts it


FAMILIES: dict[str, Family] = {
    "waytify.play_track": Family("track", "spotify track", "play_track"),
    "waytify.play_album": Family("album", "spotify album", "play_context"),
    "waytify.play_playlist": Family("playlist", "spotify playlist", "play_context"),
}

_QUERY = Param("query", "text", required=True)

# Every summary is written here, so it is the one piece of text in this file that is
# TRUSTED. Tier 1 is `Intent.MEDIA`'s tier, so this adapter adds reach without adding a
# new cost class, and a tier 1 act is what starting somebody's music has always cost.
CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        id="waytify.play_track",
        summary="play a track on Spotify, found by name",
        tier=1,
        kind="spotify track",
        params=(_QUERY,),
        needs_query=True,
    ),
    Capability(
        id="waytify.play_album",
        summary="play an album on Spotify, found by name",
        tier=1,
        kind="spotify album",
        params=(_QUERY,),
        needs_query=True,
    ),
    Capability(
        id="waytify.play_playlist",
        summary="play a playlist on Spotify, found by name",
        tier=1,
        kind="spotify playlist",
        params=(_QUERY,),
        needs_query=True,
    ),
)
BY_ID: dict[str, Capability] = {cap.id: cap for cap in CAPABILITIES}


class Link(Protocol):
    """One open connection, as this adapter uses it.

    `readline` answers with "" when nothing more is coming, whether that is the daemon
    closing the socket or the daemon going quiet past `timeout`. The two are the same
    thing to every caller here: there is no answer, and the sentence for that is the
    same either way.
    """

    def send(self, line: str) -> None: ...

    def readline(self, timeout: float) -> str: ...

    def close(self) -> None: ...


class Dial(Protocol):
    def __call__(self, path: Path, timeout: float) -> Link: ...


class _Socket:
    """The live connection. Newline delimited, so a length prefix is never needed."""

    def __init__(self, sock: Any) -> None:
        self._sock = sock
        self._buffer = b""

    def send(self, line: str) -> None:
        try:
            self._sock.sendall(line.encode("utf-8"))
        except OSError as exc:
            raise AdapterError(f"waytify stopped listening: {_short(str(exc))}") from exc

    def readline(self, timeout: float) -> str:
        while b"\n" not in self._buffer:
            try:
                self._sock.settimeout(max(0.001, timeout))
                block = self._sock.recv(65536)
            except (TimeoutError, OSError):
                return ""
            if not block:
                return ""
            self._buffer += block
        line, _, self._buffer = self._buffer.partition(b"\n")
        return line.decode("utf-8", "replace")

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._sock.close()


def connect(path: Path, timeout: float = CONNECT_TIMEOUT_S) -> Link:
    """Open the daemon's socket, or say the daemon is not there."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(str(path))
    except OSError as exc:
        sock.close()
        raise AdapterError(f"{NOT_RUNNING} ({_short(str(exc))})") from exc
    return _Socket(sock)


def _short(text: str, limit: int = 120) -> str:
    """Someone else's words as one plain clause for the HUD."""
    return sanitize(str(text)).split(". ")[0][:limit].strip() or "no reason given"


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def results_in(state: Mapping[str, Any]) -> tuple[Found, ...]:
    """The search results inside a `State` frame, dropping anything unreadable.

    Lenient about shape and strict about the one field that is acted on: an entry with
    no usable URI is dropped rather than carried as a candidate that cannot be played.
    """
    spotify = state.get("spotify")
    rows = spotify.get("search") if isinstance(spotify, Mapping) else None
    if not isinstance(rows, list):
        return ()
    out: list[Found] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        uri = _text(row.get("uri")).strip()
        kind = _text(row.get("kind"))
        if not URI.match(uri) or kind not in {f.kind for f in FAMILIES.values()}:
            continue
        out.append(Found(_text(row.get("name")), _text(row.get("subtitle")), uri, kind))
    return tuple(out)


def authorized(state: Mapping[str, Any]) -> bool:
    """Has a Spotify account been connected? A `State` with no `spotify` object at all
    is one waytify skipped as empty, which means no account and nothing cached."""
    spotify = state.get("spotify")
    return bool(isinstance(spotify, Mapping) and spotify.get("authorized"))


class Waytify:
    """The adapter that can search, over the owner's own daemon."""

    name = "waytify"

    def __init__(
        self,
        dial: Dial | None = None,
        *,
        path: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._dial = dial or connect
        self._path = path
        self._clock = clock
        self._minted = Minted()
        self._searches: dict[str, tuple[float, tuple[Found, ...]]] = {}

    # ------------------------------------------------------------------ the machine

    @property
    def path(self) -> Path:
        return socket_path() if self._path is None else self._path

    def listening(self) -> bool:
        """Is there a socket at that path at all? A stat, with no connect behind it.

        This is what `can_serve` is allowed to cost (`base.Adapter`: a pure local
        predicate, no network and no spawn), and it is also what decides whether the
        `spotify_player` fallback runs, so it may not depend on a daemon answering.
        """
        try:
            return self.path.is_socket()
        except OSError:
            return False

    def probe(self) -> tuple[bool, str]:
        """(usable, one sentence). Never raises: probe is how a caller finds out that
        something is wrong, so it may not be another thing that goes wrong.

        Three failures, three repairs, because they are three different repairs: a daemon
        that is not running is started, a protocol this build does not know is a version
        mismatch that only an upgrade fixes, and an account that was never connected is a
        one-time interactive login only the owner can do.
        """
        if not self.listening():
            return False, NOT_RUNNING
        try:
            link = self._dial(self.path, CONNECT_TIMEOUT_S)
        except AdapterError as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001 - probe may not be a second failure
            return False, f"{NOT_RUNNING} ({_short(str(exc))})"
        try:
            state = self._open(link)
        except AdapterError as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001
            return False, f"waytify answered something this could not read: {_short(str(exc))}"
        finally:
            link.close()
        if not authorized(state):
            return False, LOGIN
        return True, "waytify is running with a Spotify account connected, so tracks can be found"

    def can_serve(self, context: Context) -> bool:
        return self.listening()

    def capabilities(self, context: Context) -> tuple[Capability, ...]:
        """The same three whenever the daemon is there. What Spotify holds is not a
        property of this desktop, and asking would cost a network call at key down,
        which is the one thing the harvest may not do."""
        return CAPABILITIES

    # ------------------------------------------------------------------ the protocol

    def _frames(self, link: Link, deadline: float) -> Iterator[dict[str, Any]]:
        """Every frame that arrives before `deadline`. A line that is not a JSON object
        is skipped rather than fatal: a frame this build does not know is not an error,
        and a newer daemon is allowed to send ones it does not."""
        while True:
            left = deadline - self._clock()
            if left <= 0:
                return
            line = link.readline(left)
            if not line:
                return
            try:
                frame = json.loads(line)
            except ValueError:
                continue
            if isinstance(frame, dict):
                yield frame

    def _check(self, frame: Mapping[str, Any]) -> None:
        """An `Error` frame is the daemon refusing, in its own words. Non-fatal on the
        wire, and fatal here: the thing that was asked for did not happen."""
        if frame.get("type") == "error":
            raise AdapterError(f"waytify refused: {_short(_text(frame.get('message')))}")

    def _greet(self, link: Link) -> Iterator[dict[str, Any]]:
        """Read `Hello`, check the protocol, and hand back the rest of the conversation.

        `Hello` is first on every connection and carries the protocol number, so the
        version check happens before anything is asked for rather than after something
        has been misread.
        """
        frames = self._frames(link, self._clock() + CONNECT_TIMEOUT_S)
        hello = next(frames, None)
        if hello is None:
            raise AdapterError("waytify accepted the connection and then said nothing")
        self._check(hello)
        if hello.get("type") != "hello":
            raise AdapterError("waytify did not introduce itself, so this is not its socket")
        spoken = hello.get("protocol")
        if spoken != PROTOCOL:
            raise AdapterError(
                f"waytify speaks protocol {spoken} and this build speaks {PROTOCOL}, so "
                "nothing was asked of it; upgrade whichever of the two is older"
            )
        return frames

    def _open(self, link: Link) -> dict[str, Any]:
        """Handshake and subscribe, answering with the state the daemon sent back.

        `Subscribe` is answered with the current state immediately (waytify sends it
        rather than waiting for a change, so a bar starting during a paused track still
        renders), which is what makes one connection enough to learn whether an account
        is connected and what the search list already holds.

        Only the paths that need to READ state subscribe. Starting a track does not, and
        a subscriber is something the daemon then has to keep publishing to, so `perform`
        greets and asks and nothing else.
        """
        frames = self._greet(link)
        link.send(json.dumps({"cmd": "subscribe", "scope": "full"}) + "\n")
        for frame in frames:
            self._check(frame)
            if frame.get("type") == "state":
                state = frame.get("state")
                return dict(state) if isinstance(state, Mapping) else {}
        raise AdapterError("waytify never sent its state, so there was nothing to read")

    def _await_search(
        self, link: Link, was: tuple[Found, ...], seconds: float
    ) -> tuple[Found, ...]:
        """The next search list the daemon publishes that is not the one it already had.

        waytify answers `Search` with an `Ack` straight away and does the round trip in
        the background, then publishes a new `State` only when the list actually CHANGED
        (`engine.rs`: `if self.state.spotify.search != results`). So the ack proves
        nothing and the baseline has to be carried: waiting for "a state frame" would
        return whatever the previous search left behind, and waiting for a non-empty list
        would hand back results for somebody else's query.
        """
        deadline = self._clock() + seconds
        for frame in self._frames(link, deadline):
            self._check(frame)
            if frame.get("type") != "state":
                continue
            state = frame.get("state")
            found = results_in(state) if isinstance(state, Mapping) else ()
            if found != was:
                return found
        return ()

    def search(self, query: str) -> tuple[Found, ...]:
        """Everything Spotify returned for these words, in the order it ranked them.

        Cached for `SEARCH_TTL_S` because one utterance may ask twice: once from a
        partial transcript while the key is still held, once from the final one, and
        again if the speaker says "no, the second one".
        """
        key = query.casefold()
        now = self._clock()
        cached = self._searches.get(key)
        if cached is not None and now - cached[0] < SEARCH_TTL_S:
            return cached[1]
        link = self._dial(self.path, CONNECT_TIMEOUT_S)
        try:
            state = self._open(link)
            if not authorized(state):
                raise AdapterError(LOGIN)
            was = results_in(state)
            if was:
                # an empty query clears the list without asking Spotify anything
                # (`api.rs` returns early), so this costs one local round trip and
                # removes the case where the new results equal the old ones and the
                # daemon therefore publishes nothing at all
                link.send(json.dumps({"cmd": "search", "query": ""}) + "\n")
                was = self._await_search(link, was, CONNECT_TIMEOUT_S)
            link.send(json.dumps({"cmd": "search", "query": query}) + "\n")
            found = self._await_search(link, was, SEARCH_TIMEOUT_S)
        finally:
            link.close()
        if not found:
            # nothing is cached, because nothing was learned: on this protocol "Spotify
            # found nothing" and "Spotify has not answered yet" are the same silence
            return ()
        while len(self._searches) >= MAX_CACHED_QUERIES:
            self._searches.pop(next(iter(self._searches)))
        self._searches[key] = (now, found)
        return found

    def forget(self) -> None:
        """Drop the cached searches. The next resolve asks the daemon again."""
        self._searches.clear()

    # ------------------------------------------------------------------ the contract

    def resolve(
        self, capability: str, slots: Mapping[str, Any], context: Context
    ) -> tuple[Affordance, ...]:
        """Real Spotify results as candidates, ranked as Spotify ranked them.

        Read only, so tier 0, so it may run on a partial transcript while the key is
        still held, which is where the perceived latency goes. The ids are 1-based inside
        their own family, so "the second one" is an index into a real list.
        """
        family = FAMILIES.get(capability)
        if family is None:
            raise AdapterError(f"{capability} is not something waytify does")
        query = sanitize(str(slots.get("query") or ""))[:MAX_QUERY].strip()
        if not query:
            raise AdapterError("say what to play, for example: play bohemian rhapsody on spotify")
        every = self.search(query)
        found = [row for row in every if row.kind == family.kind]
        if not found:
            if not every:
                raise AdapterError(
                    f"waytify came back with nothing for {query!r}, which on this "
                    "protocol means either Spotify found nothing or it has not answered"
                )
            raise AdapterError(f"Spotify found no {family.kind} for {query!r}")
        out: list[Affordance] = []
        for row in found[:MAX_CANDIDATES]:
            label = row.label
            if not sanitize(label):
                continue
            out.append(
                Affordance(
                    id=f"{capability}#{len(out) + 1}",
                    label=label,
                    kind=family.noun,
                    tier=tier_for(BY_ID[capability], label),
                    handle=self._minted.mint(row.uri, sanitize(label)[:80]),
                    adapter=self.name,
                    capability=capability,
                    trust=Trust.UNTRUSTED,
                )
            )
        if not out:
            raise AdapterError(
                f"Spotify answered about {query!r} with nothing that could be named, so "
                "nothing was offered"
            )
        return tuple(out)

    def perform(self, capability: str, handle: str, context: Context) -> Outcome:
        """Start the chosen thing by its URI, and only ever by its URI.

        The handle is checked twice: it must be one this adapter minted, and it must
        still be a Spotify URI of the type this capability starts. An album URI sent as a
        track would be refused by Spotify anyway; refusing it here means the refusal is
        one sentence instead of a shrug from three processes away.
        """
        family = FAMILIES.get(capability)
        if family is None:
            raise AdapterError(f"{capability} is not something waytify does")
        match = URI.match(handle)
        if handle not in self._minted or match is None or match.group(1) != family.kind:
            raise AdapterError("that is not a result this command offered, so nothing was played")
        link = self._dial(self.path, CONNECT_TIMEOUT_S)
        try:
            self._greet(link)
            link.send(json.dumps({"cmd": family.command, "uri": handle}) + "\n")
            deadline = self._clock() + PLAY_TIMEOUT_S
            for frame in self._frames(link, deadline):
                self._check(frame)
                if frame.get("type") == "ack":
                    break
            else:
                raise AdapterError("waytify never said whether it started that, so it may not have")
        finally:
            link.close()
        what = self._minted.label_for(handle) or family.noun
        # no inverse: what was playing before is not recoverable from here, and Spotify
        # has no "go back to what you interrupted". An undo would be a second guess.
        return Outcome(ok=True, message=f"Playing {what}.")
