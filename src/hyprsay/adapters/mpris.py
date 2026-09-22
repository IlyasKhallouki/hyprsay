"""Media transport over MPRIS, one candidate per player that is actually on the bus.

WHY, AND WHERE THE CEILING IS. MPRIS is the cheapest semantic thing on a Linux desktop
and it is also tiny. Measured on this machine: Spotify's shipped binary contains exactly
three `org.mpris.MediaPlayer2*` strings and neither `TrackList` nor `Playlists` occurs
anywhere in it, and its own MPRIS dispatcher has error strings for exactly two
interfaces. So MPRIS can play, pause, skip and seek, and it can NEVER search. "Pick a
song" does not live here and no amount of engineering moves it here; it lives in
`spotify.py`, through a service API. What lives here is the other half: knowing WHICH
player, out of the several a browser alone can publish, and acting on that one.

WHY IT IS WORTH ITS OWN ADAPTER WHEN `ops._media` ALREADY EXISTS. That op runs
`playerctl <verb>`, which spawns a process (12 to 31 ms measured) and aims at whatever
playerctld calls the last active player. It cannot name a player, so it cannot answer
"pause the video" when music is also playing. This adapter enumerates the players and
offers one option each, at 0.19 ms for the enumeration and 1.6 to 1.9 ms for a player's
properties (p50, 20 runs, over the warm connection). A whole cold harvest of the two
players live on this machine, options built and all, is 6.3 ms p50; a second harvest
inside `CACHE_TTL_S` is 0.41 ms.

WHY `Properties.GetAll` AND NOT `Introspect`. Measured here on the live bus:
`Introspect` on `org.mpris.MediaPlayer2.chromium.instance1325064` returns 158 characters
and ZERO interface elements, while the same object answers every property read. The
plasma bridge returns 5 interfaces and playerctld 8. So introspection is not a capability
list on Chromium-family players, and the `Can*` properties are.

WHY THE TRANSPORT IS `a11ybus` AND WHAT IT CANNOT DO YET. `a11ybus.connection_for("")`
is the session bus on one held-open authenticated connection, which is what makes a
property read 0.2 ms instead of the 9.8 ms a `busctl` spawn costs. It has one defect
this module has to live around: `a11ybus._Reader._read_array` writes
`entries[self.read(key)] = self.read(value)`, and Python evaluates the right side of an
assignment first, so every dict-entry array is read value-before-key and comes apart.
Reproduced with no bus at all, in process, by marshalling a body of `a{sv}` with the
public `marshal_method_call` and handing it straight back to the public `parse_message`:
it raises `TransportError: cannot parse the D-Bus type '\\x00\\x00'`. Until that one line
is swapped, `GetAll` cannot be unmarshalled, so `_properties` reads the properties one
`Get` at a time (a nine-property sweep measured 1.6 ms, 1.9 ms and, through playerctld,
3.8 ms p50) and `Metadata`, which is `a{sv}` inside a variant, cannot be read at all.
`_dicts_readable()` runs that in-process round trip once and this module switches itself
back to the single `GetAll` the day the line is fixed, with no edit here.

WHAT IS DELIBERATELY NOT OFFERED. `Quit` is on every player and is not here: closing an
application has its own path with its own tier 2 guards, and a second route around them
is precisely the thing that must not exist. `Volume` is not here either, because
`ops._volume` already owns volume and one knob must never serve two questions
(ARCHITECTURE.md). `playerctld` is dropped whenever it is on the bus, because it is a
proxy for "the last active player" and would sit beside the real players as a catch-all
option: `jev.types.Choice` records that a `focused` entry beside real windows absorbed
about half the probability mass regardless of the utterance.

TIER. Every capability here is tier 1, the tier `nlu/tiers.py` already gives
`Intent.MEDIA`, so this adapter adds reach without adding a new cost class. `Raise` is
tier 1 rather than tier 0 for `controls.click_tier`'s reason: tier 0 is for what is free
to reverse, and a request INTO an application is not, because this adapter has no
inverse to offer for it.

WHAT IS PLAYING IS A WINDOW TITLE BY ANOTHER NAME. `xesam:title`, `xesam:artist` and
`xesam:album` say what the owner is watching or listening to, which is the same sentence
about the same person that docs/PRIVACY.md promises no window title will carry off the
machine by default. Until this was gated, a resolved option read "Pause Chromium: Dr
Smith explains my HIV test results" and that string went into the Jev request body and
into the journal in the clear. So `cfg.privacy` governs it exactly as it governs a title:
`titles = "never"` means no metadata is kept at all, `when_needed` keeps it only for
players that would otherwise carry the SAME label and so could not be told apart, the
redaction lists run over it, and `journal.py` drops the fields that hold it. The gate is
applied in `players()`, at the moment the bus is read, so nothing downstream can leak
what was never stored: "Pause Chromium" is the label, and the track is only ever added
when it is the thing doing the telling apart.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Any, Protocol

from hyprsay import a11ybus
from hyprsay.adapters.base import (
    MAX_LABEL,
    AdapterError,
    Affordance,
    Capability,
    Context,
    Minted,
    Trust,
    tier_for,
)
from hyprsay.config import Privacy
from hyprsay.lexicon import sanitize
from hyprsay.model import Outcome

PREFIX = "org.mpris.MediaPlayer2."
PATH = "/org/mpris/MediaPlayer2"
ROOT = "org.mpris.MediaPlayer2"
PLAYER = "org.mpris.MediaPlayer2.Player"
PROPERTIES = "org.freedesktop.DBus.Properties"
DBUS = "org.freedesktop.DBus"
DBUS_PATH = "/org/freedesktop/DBus"

# a proxy for "the last active player", so it always duplicates a real one
PROXY = PREFIX + "playerctld"

# the only shape a bus name may have before anything is dispatched to it
NAME = re.compile(r"^org\.mpris\.MediaPlayer2\.[A-Za-z0-9_][A-Za-z0-9_.\-]*$")

# how long a reading of the bus stays good. Short, because a player appears and vanishes
# with a tab, and the reading is cheap enough that the only thing a cache buys is not
# sweeping twice inside one utterance.
CACHE_TTL_S = 1.0

ROOT_KEYS = ("Identity", "DesktopEntry", "CanRaise")
PLAYER_KEYS = (
    "PlaybackStatus",
    "CanControl",
    "CanPlay",
    "CanPause",
    "CanGoNext",
    "CanGoPrevious",
    "CanSeek",
)


@dataclass(frozen=True)
class Verb:
    """One MPRIS method, with the flag that says whether this player will honour it."""

    member: str
    interface: str
    flag: str
    tier: int
    summary: str
    word: str  # what the option label starts with
    done: str  # the sentence afterwards, with the player's name in it


VERBS: dict[str, Verb] = {
    "mpris.play_pause": Verb(
        "PlayPause", PLAYER, "CanPlay", 1, "play or pause a player", "Play", "Play or pause {name}."
    ),
    "mpris.next": Verb(
        "Next", PLAYER, "CanGoNext", 1, "skip to the next track", "Next", "Next track on {name}."
    ),
    "mpris.previous": Verb(
        "Previous",
        PLAYER,
        "CanGoPrevious",
        1,
        "go back to the previous track",
        "Previous",
        "Previous track on {name}.",
    ),
    "mpris.stop": Verb("Stop", PLAYER, "CanControl", 1, "stop a player", "Stop", "Stopped {name}."),
    "mpris.raise": Verb(
        "Raise", ROOT, "CanRaise", 1, "bring a player to the front", "Show", "Brought up {name}."
    ),
}

CAPABILITIES: tuple[Capability, ...] = tuple(
    Capability(id=key, summary=verb.summary, tier=verb.tier, kind="media player")
    for key, verb in VERBS.items()
)
BY_ID: dict[str, Capability] = {cap.id: cap for cap in CAPABILITIES}


class Bus(Protocol):
    """What this adapter needs from D-Bus. `a11ybus.Connection.call` satisfies it as it
    stands; tests pass a fake, because a unit test may never touch the session bus."""

    def call(
        self,
        destination: str,
        path: str,
        interface: str,
        member: str,
        signature: str = "",
        args: Sequence[Any] = (),
    ) -> tuple[str, list[Any]]: ...


@dataclass(frozen=True)
class Player:
    """One live player. `bus_name` is trustworthy as an ADDRESS, because the bus daemon
    decides who owns it; `identity` is the application's own word for itself and is as
    untrusted as a window title."""

    bus_name: str
    identity: str = ""
    desktop_entry: str = ""
    status: str = ""
    can: frozenset[str] = frozenset()
    playing: str = ""  # "" when this transport cannot read `Metadata`

    @property
    def name(self) -> str:
        """The best short name for a label. Never authorizes anything."""
        return self.identity or self.desktop_entry or self.bus_name[len(PREFIX) :]


@lru_cache(maxsize=1)
def _dicts_readable() -> bool:
    """Can this transport unmarshal `a{sv}`? Answered in process, with no bus involved.

    A method call carrying one dict entry is marshalled and parsed straight back with
    the two public functions. If the answer does not survive the round trip, `GetAll`
    and `Metadata` are unreadable here and the per-property path is the only one.
    """
    probe = {"k": a11ybus.Variant("b", True)}
    try:
        parsed = a11ybus.parse_message(
            a11ybus.marshal_method_call(1, "a.b", "/a", "a.b", "C", "a{sv}", [probe])
        )
    except Exception:
        return False
    body = parsed.body[0] if parsed.body else None
    return isinstance(body, dict) and _plain(body.get("k")) is True


def _plain(value: Any) -> Any:
    """A property value, whether it arrived wrapped in a variant or bare."""
    return value.value if hasattr(value, "value") and hasattr(value, "signature") else value


def _short(text: str, limit: int = 120) -> str:
    """A bus error, or an application's own word for itself, as one clause for the HUD."""
    return sanitize(str(text))[:limit].strip() or "no reason given"


def _text(value: Any) -> str:
    plain = _plain(value)
    return plain if isinstance(plain, str) else ""


class Mpris:
    """The generic media adapter. Everything it offers is built from the live bus."""

    name = "mpris"

    def __init__(
        self,
        bus: Bus | None = None,
        *,
        get_all: bool | None = None,
        clock: Callable[[], float] = time.monotonic,
        privacy: Privacy | None = None,
    ) -> None:
        self._bus = bus
        self._get_all = _dicts_readable() if get_all is None else get_all
        self._clock = clock
        # the default is the configured default, so an adapter built with no privacy
        # argument withholds as much as one built from the owner's own configuration
        self._privacy = privacy or Privacy()
        self._minted = Minted()
        self._cached: tuple[float, tuple[Player, ...]] | None = None

    # ------------------------------------------------------------------ the transport

    @property
    def bus(self) -> Bus:
        if self._bus is None:
            self._bus = a11ybus.connection_for("")
        return self._bus

    def _names(self) -> list[str]:
        """The MPRIS names owned right now. `ListNames` and not `ListActivatableNames`:
        a player that would have to be started is not a player that is playing."""
        try:
            _, body = self.bus.call(DBUS, DBUS_PATH, DBUS, "ListNames")
        except Exception as exc:
            raise AdapterError(
                f"the session bus could not be asked who is playing: {_short(str(exc))}"
            ) from exc
        found = [
            n for n in (body[0] if body else []) if isinstance(n, str) and n.startswith(PREFIX)
        ]
        return sorted(n for n in found if n != PROXY)

    def _properties(self, bus_name: str, interface: str, keys: Sequence[str]) -> dict[str, Any]:
        """The properties of one interface, by whichever route this transport can read.

        A property a player does not implement answers with an error, which is an answer
        (Chrome's own player has no `DesktopEntry` here, measured), so one missing key
        never costs the others.
        """
        if self._get_all:
            try:
                _, body = self.bus.call(bus_name, PATH, PROPERTIES, "GetAll", "s", [interface])
            except Exception:
                return {}
            found = body[0] if body else {}
            return dict(found) if isinstance(found, dict) else {}
        out: dict[str, Any] = {}
        for key in keys:
            try:
                _, body = self.bus.call(bus_name, PATH, PROPERTIES, "Get", "ss", [interface, key])
            except Exception:
                continue
            if body:
                out[key] = body[0]
        return out

    def _metadata(self, properties: Mapping[str, Any]) -> str:
        """The artist and title for the label, empty when a dict cannot be read here.

        Both fields are written by whoever is playing, a web page included, so they only
        ever help a human tell two options apart. Nothing is read at all when the owner
        said no title ever leaves the machine, because the cheapest way not to leak a
        string is not to hold one.
        """
        if self._privacy.never:
            return ""
        raw = _plain(properties.get("Metadata"))
        if not isinstance(raw, dict):
            return ""
        title = _text(raw.get("xesam:title"))
        artists = _plain(raw.get("xesam:artist"))
        if isinstance(artists, list):
            artist = ", ".join(a for a in (_text(item) for item in artists) if a)
        else:
            artist = _text(artists)
        return f"{artist} - {title}" if artist and title else title or artist

    def _describable(self, players: Sequence[Player]) -> tuple[Player, ...]:
        """The same players, with what is playing kept only where it is needed.

        The rule is `requests.shown_title`'s, applied to the other field that says what
        the owner is doing: a track is carried only to tell apart players that would
        otherwise wear the same label, only for those players, and never for a player or
        a track the redaction lists name. One Chromium playing one thing is described as
        "Chromium", which is all the model needs to aim a Pause at it.
        """
        seen: dict[str, int] = {}
        for player in players:
            seen[player.name] = seen.get(player.name, 0) + 1
        out: list[Player] = []
        for player in players:
            withheld = (
                seen.get(player.name, 0) < 2  # nothing to tell apart, so nothing to say
                or self._privacy.redacts_class(
                    player.name, player.desktop_entry, player.bus_name[len(PREFIX) :]
                )
                or self._privacy.redacts_text(player.playing)
            )
            out.append(replace(player, playing="") if withheld else player)
        return tuple(out)

    def players(self, context: Context | None = None) -> tuple[Player, ...]:
        """Every live player, read once per `CACHE_TTL_S` so `can_serve` and
        `capabilities` in the same harvest do not sweep the bus twice."""
        now = self._clock()
        if self._cached is not None and now - self._cached[0] < CACHE_TTL_S:
            return self._cached[1]
        # `Metadata` is asked for only on the route that can parse it. Asking for it on
        # the other one is not merely useless, it is expensive: `Connection.call` reads a
        # TransportError as a lost connection, so it closes the socket and reopens it,
        # SASL and Hello included, once per player. Measured: a cold harvest of the two
        # players on this machine went from 11.9 ms to 6.3 ms (p50 of 20 runs, 4.9 to
        # 8.3) by leaving out a key this route could never have read.
        keys = (*PLAYER_KEYS, "Metadata") if self._get_all else PLAYER_KEYS
        found: list[Player] = []
        for bus_name in self._names():
            root = self._properties(bus_name, ROOT, ROOT_KEYS)
            player = self._properties(bus_name, PLAYER, keys)
            flags = {
                key
                for key, value in (*root.items(), *player.items())
                if key.startswith("Can") and _plain(value) is True
            }
            found.append(
                Player(
                    bus_name=bus_name,
                    identity=_text(root.get("Identity")),
                    desktop_entry=_text(root.get("DesktopEntry")),
                    status=_text(player.get("PlaybackStatus")),
                    can=frozenset(flags),
                    playing=self._metadata(player),
                )
            )
        self._cached = (now, self._describable(found))
        return self._cached[1]

    def forget(self) -> None:
        """Drop the reading. The next call sweeps the bus again."""
        self._cached = None

    # ------------------------------------------------------------------ the contract

    def probe(self) -> tuple[bool, str]:
        """(usable, one sentence). Never raises: probe is how a caller finds out that
        something is wrong, so it may not be another thing that goes wrong."""
        try:
            live = self.players()
        except AdapterError as exc:
            return False, str(exc)
        except Exception as exc:
            return False, f"no media player could be read from the session bus: {_short(str(exc))}"
        if not live:
            return False, (
                "no media player is on the session bus, so there is nothing to play or "
                "skip; start one and it will appear by itself"
            )
        where = ", ".join(sorted({player.name for player in live}))
        if not self._get_all:
            return True, (
                f"{len(live)} media players are on the bus ({where}), but this build "
                "cannot read their track names, so options are named by their player"
            )
        return True, f"{len(live)} media players are on the bus ({where})"

    def can_serve(self, context: Context) -> bool:
        try:
            return bool(self.players(context))
        except AdapterError:
            return False

    def capabilities(self, context: Context) -> tuple[Capability, ...]:
        """Only the verbs some live player will honour, so a dead option is never built.

        `CanControl` false means the player says it takes no orders at all, and nothing
        it declares below that is worth offering.
        """
        flags: set[str] = set()
        for player in self.players(context):
            if "CanControl" in player.can:
                flags |= player.can
        return tuple(cap for cap in CAPABILITIES if VERBS[cap.id].flag in flags)

    def resolve(
        self, capability: str, slots: Mapping[str, Any], context: Context
    ) -> tuple[Affordance, ...]:
        """One candidate per player that will honour this verb. Read only, tier 0."""
        verb = VERBS.get(capability)
        if verb is None:
            raise AdapterError(f"{capability} is not something a media player does")
        out: list[Affordance] = []
        for index, player in enumerate(self.players(context), start=1):
            if "CanControl" not in player.can or verb.flag not in player.can:
                continue
            # the one label that is worth deriving from live state: "Play Chrome" on a
            # player that is already playing reads as a bug to whoever is looking at it
            playing = verb.member == "PlayPause" and player.status == "Playing"
            label = f"{'Pause' if playing else verb.word} {player.name}"
            if player.playing:
                label = f"{label}: {player.playing}"
            out.append(
                Affordance(
                    id=f"{capability}#{index}",
                    label=label,
                    kind="media player",
                    tier=tier_for(BY_ID[capability], label),
                    handle=self._minted.mint(player.bus_name, _short(player.name, MAX_LABEL)),
                    adapter=self.name,
                    capability=capability,
                    trust=Trust.UNTRUSTED,
                )
            )
        return tuple(out)

    def perform(self, capability: str, handle: str, context: Context) -> Outcome:
        """Send the one method this capability names, to a player this adapter offered.

        Three checks before anything leaves: the capability is one of ours, the handle
        is one we minted, and the name is still owned right now. The last one is the
        adapter's own fresh-state re-check: a player that closed between the decision
        and the act must fail as a refusal, not reach whoever took the name next.
        """
        verb = VERBS.get(capability)
        if verb is None:
            raise AdapterError(f"{capability} is not something a media player does")
        if handle not in self._minted or not NAME.match(handle):
            raise AdapterError("that is not a player this command offered, so nothing was sent")
        if handle not in self._names():
            raise AdapterError(f"{self._minted.label_for(handle) or 'that player'} is gone")
        try:
            self.bus.call(handle, PATH, verb.interface, verb.member)
        except Exception as exc:
            raise AdapterError(
                f"{self._minted.label_for(handle) or 'that player'} refused: {_short(str(exc))}"
            ) from exc
        self.forget()
        # no inverse: the undo of a transport change is the same change against the SAME
        # player, and `model.Action` has no field that names one, so an UNDO replayed
        # through `Intent.MEDIA` would land on whatever playerctld calls active instead.
        name = self._minted.label_for(handle) or "that player"
        return Outcome(ok=True, message=verb.done.format(name=name))
