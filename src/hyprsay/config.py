"""Configuration: `~/.config/hyprsay/config.toml`, then environment overrides.

Every value has a default, so an absent file is a working configuration. Unknown keys
are an error rather than ignored, because a misspelled safety setting that silently
does nothing is worse than a daemon that refuses to start.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

CONFIG_FILE = Path("~/.config/hyprsay/config.toml")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class PTT:
    # "event": bind/bindr -> `event` dispatcher -> socket2 custom>> (zero dependencies)
    # "global": hyprland_global_shortcuts_v1 (in-process, not forgeable through socket2)
    transport: str = "event"
    max_hold_s: float = 15.0
    # tap once to start, tap again to stop, instead of holding
    latch: bool = False


@dataclass(frozen=True)
class STT:
    # "hybrid": local first, the gateway only when the local transcript leads nowhere
    # "local": never send audio anywhere. "cloud": always use the gateway model.
    backend: str = "hybrid"
    local_model: str = "parakeet-110m"  # or "moonshine-tiny"
    # measured from this machine: fish 452 ms and 8 of 8 on degraded audio;
    # gemini-3.5-transcribe takes about 3.2 s and is not suitable for commands
    cloud_model: str = "fish-audio/transcribe-1"
    device: str = ""  # PortAudio device name or index; empty is the default source
    # keep the input stream open with a pre-roll so the first word is never clipped.
    # Costs a permanently lit mic indicator; off by default.
    preroll: bool = False
    duck_volume: bool = False


@dataclass(frozen=True)
class Jev:
    route: str = "typesafe"
    model: str = "typesafe-ai/jev"
    deadline_s: float = 0.9
    # how long a late answer stays acceptable for tiers 0 and 1 (docs/PLAN.md 5.5)
    late_answer_s: float = 2.5
    token_cap: int = 1800
    zero_data_retention: bool = True
    enabled: bool = True  # False: grammar only, fully offline


@dataclass(frozen=True)
class Privacy:
    # "never": no window title ever leaves the machine.
    # "when_needed": only to tell apart several windows of one app, and only theirs.
    titles: str = "when_needed"
    title_chars: int = 60
    redact_classes: tuple[str, ...] = ("keepassxc", "bitwarden", "1password", "org.gnome.seahorse")
    redact_title_patterns: tuple[str, ...] = ("private", "incognito", "vault", "password")


@dataclass(frozen=True)
class Safety:
    # apps where dictated text may be typed without a countdown. Terminals, launchers
    # and auth dialogs are refused regardless of this list.
    type_allow_classes: tuple[str, ...] = ()
    type_max_chars: int = 200
    countdown_s: float = 1.5
    # user .desktop files approved for launch, as "desktop-id:sha256"
    approved_desktop_files: tuple[str, ...] = ()
    # extra lock screen layer namespaces or window classes (Quickshell lockers)
    locker_namespaces: tuple[str, ...] = ()


@dataclass(frozen=True)
class Gates:
    """Initial thresholds. All are assumptions until the eval harness sets them."""

    addressed: float = 0.6
    margin: float = 0.3
    # the absolute per-window boolean must clear this for "that window exists"
    present: float = 0.5
    fuzzy_exact: float = 0.92
    fuzzy_gap: float = 0.15


@dataclass(frozen=True)
class HUD:
    position: str = "bottom"  # "bottom" | "top"
    margin: int = 48
    hint_timeout_s: float = 4.0
    swap_timeout_s: float = 3.0


@dataclass(frozen=True)
class Config:
    ptt: PTT = field(default_factory=PTT)
    stt: STT = field(default_factory=STT)
    jev: Jev = field(default_factory=Jev)
    privacy: Privacy = field(default_factory=Privacy)
    safety: Safety = field(default_factory=Safety)
    gates: Gates = field(default_factory=Gates)
    hud: HUD = field(default_factory=HUD)
    aliases: dict[str, str] = field(default_factory=dict)  # "browser" -> "firefox"


_CHOICES = {
    ("ptt", "transport"): {"event", "global"},
    ("stt", "backend"): {"hybrid", "local", "cloud"},
    ("stt", "local_model"): {"parakeet-110m", "moonshine-tiny"},
    ("jev", "route"): {"typesafe", "evaluate"},
    ("privacy", "titles"): {"never", "when_needed"},
    ("hud", "position"): {"bottom", "top"},
}


def _build(cls: type, data: dict[str, Any], path: str) -> Any:
    known = {f.name: f for f in fields(cls)}
    unknown = sorted(set(data) - set(known))
    if unknown:
        raise ConfigError(f"unknown key(s) in [{path or 'top level'}]: {', '.join(unknown)}")
    kwargs: dict[str, Any] = {}
    for name, value in data.items():
        default = getattr(cls(), name)
        if is_dataclass(default):
            if not isinstance(value, dict):
                raise ConfigError(f"[{name}] must be a table")
            kwargs[name] = _build(type(default), value, name)
            continue
        if isinstance(default, tuple):
            if not isinstance(value, list):
                raise ConfigError(f"{path}.{name} must be a list")
            value = tuple(str(v) for v in value)
        elif isinstance(default, bool):
            if not isinstance(value, bool):
                raise ConfigError(f"{path}.{name} must be true or false")
        elif isinstance(default, float):
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ConfigError(f"{path}.{name} must be a number")
            value = float(value)
        elif isinstance(default, int):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigError(f"{path}.{name} must be a whole number")
        elif isinstance(default, dict):
            if not isinstance(value, dict):
                raise ConfigError(f"[{name}] must be a table")
            value = {str(k).lower(): str(v) for k, v in value.items()}
        elif not isinstance(value, str):
            raise ConfigError(f"{path}.{name} must be a string")
        allowed = _CHOICES.get((path, name))
        if allowed and value not in allowed:
            raise ConfigError(f"{path}.{name} must be one of {sorted(allowed)}, got {value!r}")
        kwargs[name] = value
    return cls(**kwargs)


def load(path: Path | None = None, env: dict[str, str] | None = None) -> Config:
    """Read the config file if it exists, then apply HYPRSAY_* overrides."""
    env = os.environ if env is None else env
    file = (path or CONFIG_FILE).expanduser()
    data: dict[str, Any] = {}
    if file.exists():
        try:
            data = tomllib.loads(file.read_text())
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{file}: {exc}") from exc
    # HYPRSAY_STT_BACKEND=cloud overrides [stt] backend
    for key, raw in env.items():
        if not key.startswith("HYPRSAY_") or key.count("_") < 2:
            continue
        section, _, name = key[len("HYPRSAY_") :].lower().partition("_")
        if section not in {f.name for f in fields(Config)}:
            continue
        default = getattr(getattr(Config(), section), name, None)
        if default is None or isinstance(default, tuple | dict):
            continue
        value: Any = raw
        try:
            if isinstance(default, bool):
                value = raw.strip().lower() in {"1", "true", "yes", "on"}
            elif isinstance(default, float):
                value = float(raw)
            elif isinstance(default, int):
                value = int(raw)
        except ValueError as exc:
            raise ConfigError(f"{key}={raw!r} is not a number") from exc
        data.setdefault(section, {})[name] = value
    return _build(Config, data, "")
