"""What a CLI verb remembers between calls.

The MCP server is one long-lived process, so the marks numbering, the
`launched` confinement set and the strict-mode seat baseline can live in
module globals. A CLI verb is a fresh process per call, and without this
file three things break: `click_ui --mark N` has no numbering to resolve
against, `HYPRUSE_CONFINE=launched` refuses every action after the launch
(the owned-set is empty again), and HYPRUSE_STRICT never fires (the seat
guard is a no-op until something remembered a seat), which is the one that
fails OPEN and is why this module exists.

The state is keyed by the compositor instance: window addresses are heap
pointers inside one Hyprland process, so anything remembered under another
instance is discarded. Losing the file is safe in the other direction too:
every consumer degrades to "nothing remembered", which is more refusal,
not less.

Verbs overlap (a background `wait_for` beside a `launch`), so a save is a
merge under a lock, not a rewrite: each process writes back only what it
changed since it restored, and the owned-set is a union.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from pathlib import Path
from typing import Any

from hypruse import hyprctl, trust

STATE_VERSION = 1

_flags: dict[str, Any] = {}  # small per-instance facts (was the border rule installed)
_restored: dict[str, Any] = {}  # what restore() loaded, to tell our changes from the file's


def _dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
    d = Path(base) / "hypruse"
    d.mkdir(parents=True, exist_ok=True)
    return d


def path() -> Path:
    return _dir() / "cli-state.json"


def _instance() -> str:
    return os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")


def load() -> dict[str, Any]:
    """The stored state, or {} when there is none, it is unreadable, it was
    written by another version, or it belongs to another compositor."""
    try:
        raw = json.loads(path().read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict) or raw.get("v") != STATE_VERSION:
        return {}
    if raw.get("instance") != _instance():
        return {}
    return raw


def _plain(value: Any) -> Any:
    """Through JSON and back, so a tuple and its list read as equal."""
    return json.loads(json.dumps(value, default=str))


def restore() -> None:
    """Load the remembered state into the modules that consume it."""
    from hypruse import server

    global _flags, _restored
    state = load()
    remembered = state.get("trust")
    remembered = dict(remembered) if isinstance(remembered, dict) else {}
    if os.environ.get("HYPRUSE_CONFINE", "").strip() == "launched":
        remembered["owned"] = _prune_owned(remembered.get("owned"))
    trust.restore_state(remembered)
    server.restore_marks(state.get("marks"))
    flags = state.get("flags")
    _flags = dict(flags) if isinstance(flags, dict) else {}
    _restored = {
        "trust": _plain(trust.export_state()),
        "marks": _plain(server.marks_state()),
    }


def _prune_owned(owned: Any) -> list[str]:
    """An address the compositor no longer lists can be handed to a new,
    unrelated window later, and a persisted owned-set outlives windows far
    more often than a server session does. Drop the dead ones before a
    confinement decision leans on them. An unreadable window list leaves the
    set alone: the action about to run will fail on the same IPC anyway."""
    if not isinstance(owned, list) or not owned:
        return []
    with contextlib.suppress(Exception):
        live = {c.get("address") for c in hyprctl.query("clients")}
        return sorted(a for a in owned if a in live)
    return list(owned)


def flag(name: str) -> Any:
    return _flags.get(name)


def set_flag(name: str, value: Any) -> None:
    _flags[name] = value


def _merge(current: dict[str, Any], mine: dict[str, Any]) -> dict[str, Any]:
    """What goes on disk: the file's view, with this process's changes on
    top. A field this process did not touch keeps whatever a concurrent
    verb wrote meanwhile; the owned-set is a union, since a launch in
    another process is as much ours as one here."""
    cur_trust = current.get("trust") if isinstance(current.get("trust"), dict) else {}
    my_trust, old_trust = mine["trust"], _restored.get("trust", {})
    owned = set(cur_trust.get("owned") or []) | set(my_trust["owned"])
    seat = my_trust["seat"] if my_trust["seat"] != old_trust.get("seat") else (
        cur_trust.get("seat") or my_trust["seat"]
    )
    notify_ts = max(float(cur_trust.get("notify_ts") or 0.0), float(my_trust["notify_ts"]))
    marks = mine["marks"] if mine["marks"] != _restored.get("marks") else (
        current.get("marks") or mine["marks"]
    )
    flags = dict(current.get("flags") or {}) if isinstance(current.get("flags"), dict) else {}
    flags.update(mine["flags"])
    return {
        "trust": {"owned": sorted(owned), "seat": seat, "notify_ts": notify_ts},
        "marks": marks,
        "flags": flags,
    }


def save() -> None:
    """Write the current state atomically, merged with whatever another verb
    saved meanwhile. Best effort: this file is a convenience across calls,
    and a verb that acted must not report failure over it; the next call
    simply remembers less."""
    from hypruse import server

    mine = {
        "trust": _plain(trust.export_state()),
        "marks": _plain(server.marks_state()),
        "flags": _plain(_flags),
    }
    target = path()
    with (
        contextlib.suppress(OSError, TypeError, ValueError),
        open(target.with_suffix(".lock"), "w") as guard,
    ):
        fcntl.flock(guard, fcntl.LOCK_EX)
        merged = _merge(load(), mine)
        payload = {
            "v": STATE_VERSION,
            "instance": _instance(),
            "updated": time.time(),
            **merged,
        }
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(target)
