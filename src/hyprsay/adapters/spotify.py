"""Picking a song on Spotify, through `spotify_player`, by choosing among real tracks.

THE FALLBACK, NOT THE ROUTE. `waytify.py` answers this same question over the owner's own
daemon and is the one that runs when its socket is there: structured candidates rather
than a block of JSON to pick apart, no process spawn per query, a protocol the same shape
as the overlay's, and a Spotify token already in the system keyring. This file is what
answers on a machine where that daemon is not installed or not running, and `can_serve`
below stands down whenever it is, so one track is never offered twice under two ids.

WHY THIS AND NOT MPRIS. Spotify on Linux publishes transport control and nothing else:
the shipped binary contains three `org.mpris.MediaPlayer2*` strings and neither
`TrackList` nor `Playlists` occurs in it, and there is no search anywhere in the MPRIS
specification, in any interface, optional or not. So "pick a song" cannot be answered
over D-Bus on any Linux desktop. It is answered by a service API reached through a local
client, and one is already installed here: `spotify_player 0.24.1` as
`/usr/bin/spotify_player`, from Arch's `extra` repository (verified today with
`pacman -Qi spotify-player`: 0.24.1-1, built 2026-07-20, packaged by an Arch staff
packager, not an AUR drop).

WHY `search` AND NEVER `--name`. `spotify_player playback start track --name "<phrase>"`
looks like the whole feature in one line and it is the exact heuristic this design
exists to remove: upstream `spotify_player/src/cli/client.rs` at v0.24.1 calls
`search_specific_type(&name, SearchType::Track)` and then takes `page.items[0].id`,
bailing with "Cannot find track with name=" otherwise (the literal is in the local
binary too). It is a blind `[0]` wearing a nicer name, it can never serve "the second
one" or "the live version", and it puts the choice in a place no guard can see. So this
adapter runs `search "<query>"`, reads the ranked JSON, offers the results as candidates
and plays the one that was chosen by id. There is no route in any table and no key
sequence anywhere in this file.

THE TWO PREREQUISITES, WHICH ARE NOT THIS TOOL'S AND CANNOT BE CODED AROUND. Spotify's
own Web API says of `PUT /me/player/play`: "This API only works for users who have
Spotify Premium." And the credentials are cached per machine by a one-time interactive
login: measured today, neither `~/.config/spotify-player` nor `~/.cache/spotify-player`
exists here, so `spotify_player` has never been run on this machine. `probe` detects
exactly that from the filesystem, with no spawn and no network, and answers with the
sentence naming the command to run, the same way `controls.probe` names the command that
repairs the accessibility bus.

COST, AND WHAT IS NOT MEASURED. The process floor is 16 to 22 ms (measured, three runs
of `--help`), and after the first call the CLI talks to an already running client over a
localhost socket. The Spotify round trip itself is UNMEASURED on this machine and no
number for it should be quoted: budget it like the measured Jev round trip, about 315 ms,
and note that the first call additionally pays a full client cold start, which a systemd
user unit keeping the client warm removes. Because `resolve` only reads, it is the one
expensive call in hyprsay that is safe to fire on a partial transcript while the key is
still held, which is where the perceived latency goes.

THE HANDLE IS THE SAFETY BOUNDARY. A track id is parsed out of JSON that came off the
internet and then goes onto a command line, so it is checked twice: it must match
`^[0-9A-Za-z]{22}$` to become a candidate at all, and `perform` accepts it only if this
adapter minted it (`base.Minted`). Nothing is ever passed to a shell: every call is an
argument vector. A track NAME is untrusted text that only ever becomes a label.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
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

BINARY = "spotify_player"
# both are written by the one-time interactive login, and named in the shipped README:
# the Web API token under ncspot's client id, and the librespot session credentials
CREDENTIALS = ("credentials.json", "user_client_token.json")

AUTHENTICATE = (
    "run `spotify_player authenticate` once in a terminal and approve both browser "
    "prompts; it needs a Spotify Premium account"
)

# a Spotify id is 22 base62 characters, in every object type. Anything else is not an id
# and never reaches a command line, which is the safe direction: a candidate dropped
# costs one repeated sentence, a candidate trusted costs an argument nobody checked.
SPOTIFY_ID = re.compile(r"^[0-9A-Za-z]{22}$")

# the search is a transatlantic round trip plus, on the first call, a client cold start.
# The caller owns the real deadline; this is only the ceiling that stops a hung client
# holding a worker thread forever.
SEARCH_TIMEOUT_S = 8.0
START_TIMEOUT_S = 8.0

# one utterance may resolve the same words twice (a partial transcript, then the final
# one), and a correction ("no, the second one") asks again within seconds
SEARCH_TTL_S = 20.0
MAX_CACHED_QUERIES = 8
MAX_QUERY = 120


@dataclass(frozen=True)
class Family:
    """One kind of thing a search returns, and how playback is started for it."""

    block: str  # the key in the search JSON
    kind: str  # what the HUD and the model see
    context_type: str = ""  # "" for a track, otherwise album | artist | playlist


FAMILIES: dict[str, Family] = {
    "spotify.play_track": Family("tracks", "spotify track"),
    "spotify.play_album": Family("albums", "spotify album", "album"),
    "spotify.play_artist": Family("artists", "spotify artist", "artist"),
    "spotify.play_playlist": Family("playlists", "spotify playlist", "playlist"),
}

_QUERY = Param("query", "text", required=True)

CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        id="spotify.play_track",
        summary="play a track on Spotify, found by name",
        tier=1,
        kind="spotify track",
        params=(_QUERY,),
        needs_query=True,
    ),
    Capability(
        id="spotify.play_album",
        summary="play an album on Spotify, found by name",
        tier=1,
        kind="spotify album",
        params=(_QUERY,),
        needs_query=True,
    ),
    Capability(
        id="spotify.play_artist",
        summary="play an artist on Spotify, found by name",
        tier=1,
        kind="spotify artist",
        params=(_QUERY,),
        needs_query=True,
    ),
    Capability(
        id="spotify.play_playlist",
        summary="play a playlist on Spotify, found by name",
        tier=1,
        kind="spotify playlist",
        params=(_QUERY,),
        needs_query=True,
    ),
)
BY_ID: dict[str, Capability] = {cap.id: cap for cap in CAPABILITIES}


class Runner(Protocol):
    """Running one command. `Process` is the live one; tests pass a fake driving
    recorded JSON, so the whole adapter is testable with no account and no network."""

    def run(self, argv: Sequence[str], timeout: float) -> tuple[int, str, str]: ...


class Process:
    """An argument vector, never a shell line, for the reason `ops._run_tool` gives."""

    def run(self, argv: Sequence[str], timeout: float) -> tuple[int, str, str]:
        try:
            done = subprocess.run(
                list(argv), capture_output=True, text=True, timeout=timeout, check=False
            )
        except subprocess.TimeoutExpired as exc:
            raise AdapterError(f"{argv[0]} did not answer within {timeout:.0f} seconds") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise AdapterError(f"{argv[0]} could not be run: {_plain(str(exc))}") from exc
        return done.returncode, done.stdout, done.stderr


def _plain(text: str, limit: int = 120) -> str:
    """A tool's own message cut down to one plain clause for the HUD."""
    first = sanitize(text).split(". ")[0]
    return first[:limit].strip() or "no reason given"


def cache_dir() -> Path:
    """Where `spotify_player` caches its two credentials. `--help` prints
    `~/.cache/spotify-player` as the default, and it honours XDG_CACHE_HOME."""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(base) / "spotify-player"


def identifier(value: Any) -> str:
    """The bare Spotify id inside whatever the JSON carried, or "".

    `search` is read with a lenient hand because the exact serialisation of the id field
    could not be measured here: the CLI has never been authenticated on this machine, so
    the shape comes from the shipped README's own example, `.tracks.[0].id`, and from the
    three forms an id is written in anywhere. What is NOT lenient is the pattern below.
    """
    if isinstance(value, dict):
        for key in ("id", "uri", "url"):
            found = identifier(value.get(key))
            if found:
                return found
    if not isinstance(value, str):
        return ""
    text = value.strip().split("?")[0].rstrip("/")
    tail = re.split(r"[:/]", text)[-1] if text else ""
    return tail if SPOTIFY_ID.match(tail) else ""


def _names(value: Any) -> str:
    """The artist names, comma joined, out of a list of artist objects or a string."""
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    out = []
    for item in value:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            out.append(item["name"])
        elif isinstance(item, str):
            out.append(item)
    return ", ".join(out)


def label_for(family: Family, entry: Mapping[str, Any]) -> str:
    """What the speaker and the model see. Untrusted text, cut by `Affordance`."""
    name = entry.get("name") if isinstance(entry.get("name"), str) else ""
    artists = _names(entry.get("artists"))
    if family.block == "playlists":
        owner = entry.get("owner")
        by = owner.get("display_name") if isinstance(owner, dict) else owner
        artists = by if isinstance(by, str) else ""
    if family.block == "tracks" and not artists:
        album = entry.get("album")
        artists = _names(album.get("artists")) if isinstance(album, dict) else ""
    return f"{name} - {artists}" if name and artists else name or artists


class Spotify:
    """The adapter that can actually search. Everything it offers came from Spotify."""

    name = "spotify"

    def __init__(
        self,
        runner: Runner | None = None,
        *,
        binary: str = BINARY,
        cache: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
        superseded_by: Any = None,
    ) -> None:
        self._runner = runner or Process()
        self._binary = binary
        self._cache = cache
        self._clock = clock
        # the adapter this one is the fallback for. Asked per harvest rather than once
        # at startup, so a daemon started after hyprsay takes over the moment it exists
        # and one started later is not missed until the next login.
        self._superseded_by = superseded_by
        self._minted = Minted()
        self._searches: dict[str, tuple[float, dict[str, Any]]] = {}

    # ------------------------------------------------------------------ the machine

    @property
    def cache(self) -> Path:
        return cache_dir() if self._cache is None else self._cache

    def _program(self) -> str | None:
        return shutil.which(self._binary) if os.sep not in self._binary else self._binary

    def probe(self) -> tuple[bool, str]:
        """(usable, one sentence). Never raises, and never spawns: both failures are
        answered from the filesystem, so this is cheap enough to sit in `can_serve`.

        The two repairs are told apart because they are different repairs: a missing
        package is installed, and a missing credential is a one-time interactive login
        that only the owner can do.
        """
        try:
            if self._program() is None:
                return False, (
                    "spotify_player is not installed, so Spotify cannot be searched; "
                    "install the spotify-player package"
                )
            missing = [name for name in CREDENTIALS if not (self.cache / name).exists()]
        except OSError as exc:
            return False, f"the spotify_player cache could not be read: {_plain(str(exc))}"
        if len(missing) == len(CREDENTIALS):
            return False, (
                f"spotify_player has never been authenticated on this machine; {AUTHENTICATE}"
            )
        if missing:
            return False, (
                f"spotify_player is missing {missing[0]}, so one of its two logins has "
                f"expired or was never made; {AUTHENTICATE}"
            )
        return True, "spotify_player is authenticated, so tracks can be found by name"

    def can_serve(self, context: Context) -> bool:
        if self._superseded_by is not None and self._superseded_by.can_serve(context):
            return False
        return self.probe()[0]

    def capabilities(self, context: Context) -> tuple[Capability, ...]:
        """The same four whenever the CLI is usable. There is nothing to read from the
        machine here: what Spotify holds is not a property of this desktop, and asking
        would cost a network call at key down, which is the one thing the harvest may
        not do."""
        return CAPABILITIES

    # ------------------------------------------------------------------ the contract

    def search(self, query: str) -> dict[str, Any]:
        """The ranked JSON for these words, from the CLI, cached for `SEARCH_TTL_S`.

        Cached because one utterance may ask twice: once from a partial transcript while
        the key is still held, once from the final one, and again if the speaker says
        "no, the second one". The same words must not cost three round trips.
        """
        key = query.casefold()
        now = self._clock()
        found = self._searches.get(key)
        if found is not None and now - found[0] < SEARCH_TTL_S:
            return found[1]
        code, out, err = self._runner.run([self._binary, "search", query], timeout=SEARCH_TIMEOUT_S)
        if code != 0:
            raise AdapterError(self._why(err or out))
        try:
            payload = json.loads(out)
        except ValueError as exc:
            raise AdapterError(
                "spotify_player answered something that is not the search JSON, so "
                "nothing could be offered"
            ) from exc
        if not isinstance(payload, dict):
            raise AdapterError("the Spotify search answered in a shape this cannot read")
        while len(self._searches) >= MAX_CACHED_QUERIES:
            self._searches.pop(next(iter(self._searches)))
        self._searches[key] = (now, payload)
        return payload

    def _why(self, text: str) -> str:
        """One plain sentence for a CLI that refused, with the repair when it is known.

        Reading the tool's own words is a heuristic, and it is a safe one because of
        what it can reach: the request has already been refused, so the only thing this
        decides is which sentence the owner reads. It can never turn a refusal into an
        act.
        """
        lowered = text.casefold()
        if any(word in lowered for word in ("auth", "token", "credential", "401", "premium")):
            return f"Spotify refused the request; {AUTHENTICATE}"
        return f"spotify_player refused the request: {_plain(text)}"

    def resolve(
        self, capability: str, slots: Mapping[str, Any], context: Context
    ) -> tuple[Affordance, ...]:
        """Real Spotify results as candidates, ranked as Spotify ranked them.

        Read only, so tier 0, so it may run on a partial transcript. The order is kept
        exactly as it arrived and the ids are 1-based, so "the second one" is an index
        into a real list rather than a guess.
        """
        family = FAMILIES.get(capability)
        if family is None:
            raise AdapterError(f"{capability} is not something Spotify does")
        query = sanitize(str(slots.get("query") or ""))[:MAX_QUERY].strip()
        if not query:
            raise AdapterError("say what to play, for example: play bohemian rhapsody on spotify")
        payload = self.search(query)
        entries = payload.get(family.block)
        if isinstance(entries, dict):  # the Web API's own shape, in case it is passed through
            entries = entries.get("items")
        if not isinstance(entries, list) or not entries:
            raise AdapterError(f"Spotify found no {family.kind.split(' ')[-1]} for {query!r}")
        out: list[Affordance] = []
        for entry in entries:
            if len(out) >= MAX_CANDIDATES:
                break
            if not isinstance(entry, Mapping):
                continue
            handle = identifier(entry.get("id") or entry)
            label = label_for(family, entry)
            if not handle or not sanitize(label):
                continue
            out.append(
                Affordance(
                    id=f"{capability}#{len(out) + 1}",
                    label=label,
                    kind=family.kind,
                    tier=tier_for(BY_ID[capability], label),
                    handle=self._minted.mint(handle, sanitize(label)[:80]),
                    adapter=self.name,
                    capability=capability,
                    trust=Trust.UNTRUSTED,
                )
            )
        if not out:
            raise AdapterError(
                f"Spotify answered about {query!r} in a shape this could not read, so "
                "nothing was offered"
            )
        return tuple(out)

    def perform(self, capability: str, handle: str, context: Context) -> Outcome:
        """Start the chosen thing by its id, and only ever by its id.

        The handle is checked twice: it must be one this adapter minted, and it must
        still look like a Spotify id. `--name` never appears in the vector this builds,
        because `--name` is the blind `[0]` that this whole file exists to replace.
        """
        family = FAMILIES.get(capability)
        if family is None:
            raise AdapterError(f"{capability} is not something Spotify does")
        if handle not in self._minted or not SPOTIFY_ID.match(handle):
            raise AdapterError("that is not a result this command offered, so nothing was played")
        if family.context_type:
            argv = [self._binary, "playback", "start", "context", "--id", handle]
            argv.append(family.context_type)
        else:
            argv = [self._binary, "playback", "start", "track", "--id", handle]
        code, out, err = self._runner.run(argv, timeout=START_TIMEOUT_S)
        if code != 0:
            raise AdapterError(self._why(err or out))
        what = self._minted.label_for(handle) or family.kind
        # no inverse: what was playing before is not recoverable from here, and Spotify
        # has no "go back to what you interrupted". An undo would be a second guess.
        return Outcome(ok=True, message=f"Playing {what}.")
