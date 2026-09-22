"""Adapters: ask a running application what it can do, instead of remembering a path.

`base` is the contract and its guards, `mpris` is the generic media adapter over D-Bus,
`waytify` is the one that can search, and `spotify` is its fallback for a machine where
the waytify daemon is not there. Adding an adapter means writing four members and adding
it to `live()`; it means touching nothing else, which is the whole point of the contract
existing.
"""

from hyprsay.adapters.base import (
    MAX_CANDIDATES,
    MAX_LABEL,
    MAX_OFFERABLE_TIER,
    RESOLVE_TIER,
    Adapter,
    AdapterError,
    Affordance,
    Capability,
    Context,
    Minted,
    Param,
    Trust,
    candidates,
    capability_affordance,
    capability_of,
    find,
    offer,
    tier_for,
)
from hyprsay.adapters.mpris import Mpris
from hyprsay.adapters.spotify import Spotify
from hyprsay.adapters.waytify import Waytify
from hyprsay.config import Config


def live(cfg: Config | None = None) -> tuple[Adapter, ...]:
    """Every adapter this build has. Which of them can serve is decided per utterance by
    `can_serve`, which is local and cheap on purpose, so this list never has to be
    conditioned on what happens to be installed.

    `cfg` is here for one reason: `Mpris` reads what a player is playing, which is the
    same class of secret as a window title, so it needs the owner's `privacy` settings
    rather than a default (docs/PRIVACY.md). Everything else is decided from the machine.
    `Spotify` is built holding `Waytify`, which is how the CLI path stands down whenever
    the owner's own daemon is listening, rather than both offering the same track twice.
    """
    privacy = (cfg or Config()).privacy
    waytify = Waytify()
    return (Mpris(privacy=privacy), waytify, Spotify(superseded_by=waytify))


__all__ = [
    "MAX_CANDIDATES",
    "MAX_LABEL",
    "MAX_OFFERABLE_TIER",
    "RESOLVE_TIER",
    "Adapter",
    "AdapterError",
    "Affordance",
    "Capability",
    "Context",
    "Minted",
    "Mpris",
    "Param",
    "Spotify",
    "Trust",
    "Waytify",
    "candidates",
    "capability_affordance",
    "capability_of",
    "find",
    "live",
    "offer",
    "tier_for",
]
