"""hyprsay on the command line.

hyprsay run                 the engine (normally started by the systemd user unit)
hyprsay say "open firefox"  run a typed sentence through the real pipeline; dry run
                            unless --act. The way to test understanding without a mic.
hyprsay doctor              what works, what is missing, and what to do about it
hyprsay setup               speech model, default config, bind lines, user units
hyprsay binds               the two lines to add to your Hyprland config
hyprsay inspect             what was heard, decided, done, and sent to the cloud
hyprsay ptt down|up|toggle  press the virtual key from a script
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__, config, journal


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hyprsay", description="Voice control for Hyprland.")
    parser.add_argument("--version", action="version", version=f"hyprsay {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="run the engine")
    say = sub.add_parser("say", help="understand a typed sentence against the live desktop")
    say.add_argument("text")
    say.add_argument("--act", action="store_true", help="carry the decision out (default: dry run)")
    say.add_argument("--json", action="store_true")
    sub.add_parser("doctor", help="check the installation")
    setup = sub.add_parser("setup", help="download the speech model, write config, install units")
    setup.add_argument(
        "--units", action="store_true", help="install and enable the systemd user units"
    )
    binds = sub.add_parser("binds", help="print the Hyprland bind lines")
    binds.add_argument("--key", default="SUPER, V")
    inspect = sub.add_parser("inspect", help="show recent utterances and what was sent to Jev")
    inspect.add_argument("-n", type=int, default=1)
    inspect.add_argument("--json", action="store_true")
    ptt = sub.add_parser("ptt", help="emit a push-to-talk event")
    ptt.add_argument("edge", choices=["down", "up", "toggle"])
    args = parser.parse_args(argv)

    try:
        cfg = config.load()
    except config.ConfigError as exc:
        print(f"hyprsay: config error: {exc}", file=sys.stderr)
        return 2
    handler = {"run": _run, "say": _say, "doctor": _doctor, "setup": _setup, "binds": _binds,
               "inspect": _inspect, "ptt": _ptt}[args.command]  # fmt: skip
    return handler(cfg, args)


def _run(cfg, args) -> int:
    from . import daemon

    return daemon.main(cfg)


# ------------------------------------------------------------------------------ say


def _say(cfg, args) -> int:
    from . import jev as jev_pkg
    from .lexicon import Lexicon
    from .model import Transcript, Verdict
    from .nlu.grammar import Grammar
    from .nlu.understand import Understander
    from .world import HyprSocket, snapshot, use_socket_transport

    sock = HyprSocket()
    state = snapshot(sock)
    client = None
    if cfg.jev.enabled:
        try:
            client = jev_pkg.JevClient(jev_pkg.load_key(), route=cfg.jev.route, model=cfg.jev.model,
                                       deadline=cfg.jev.deadline_s, token_cap=cfg.jev.token_cap,
                                       zero_data_retention=cfg.jev.zero_data_retention)  # fmt: skip
        except jev_pkg.JevAuthError as exc:
            print(f"note: {exc}; grammar only", file=sys.stderr)
    lexicon = Lexicon.scan(cfg)
    understander = Understander(cfg, lexicon, Grammar(), client)

    async def go():
        try:
            if client is not None:
                await client.warm()
            return await understander.understand(
                Transcript(args.text, backend="typed"), state, pinned_address=state.active_address
            )
        finally:
            if client is not None:
                await client.aclose()

    decision = asyncio.run(go())
    if args.json:
        print(json.dumps(journal._plain(decision, keep_titles=False), indent=1))
    else:
        print(f"heard     {decision.heard or args.text!r}")
        print(f"verdict   {decision.verdict.value}   (tier {decision.tier})")
        if decision.action:
            print(f"action    {decision.action.describe()}")
        for n, cand in enumerate(decision.candidates, 1):
            mark = "trusted anchor" if cand.corroborated else "not corroborated"
            print(f"  [{n}] {cand.label}   score {cand.score:.2f}   {mark}")
        if decision.suggestions:
            print("try       " + " | ".join(decision.suggestions))
        if decision.reason:
            print(f"why       {decision.reason}")
    if not args.act:
        if decision.action and decision.verdict in (Verdict.ACT, Verdict.ACT_SWAP):
            print("(dry run: pass --act to carry it out)")
        return 0
    if decision.action is None or decision.verdict not in (Verdict.ACT, Verdict.ACT_SWAP):
        print("nothing to carry out: only ACT verdicts run from `say`; countdowns and key "
              "confirmations need the engine", file=sys.stderr)  # fmt: skip
        return 1
    from .executor import Executor, boot
    from .lock import Latch

    boot()
    use_socket_transport(sock)
    latch = Latch(cfg, lambda: snapshot(sock), sock.query)
    outcome = Executor(cfg, lambda: snapshot(sock), latch, lexicon).execute(
        decision.action, pinned_address=state.active_address
    )
    print(("done      " if outcome.ok else "refused   ") + outcome.message)
    return 0 if outcome.ok else 1


# ------------------------------------------------------------------------------ doctor


def _doctor(cfg, args) -> int:
    checks: list[tuple[str, bool | None, str]] = []  # (name, ok / None for advice, detail)

    def check(name: str, fn) -> None:
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001 - a check must never crash the doctor
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        checks.append((name, ok, detail))

    def hyprland():
        from .world import HyprSocket, snapshot

        state = snapshot(HyprSocket())
        if not state.windows and state.locked:
            return False, "cannot read the compositor socket (is HYPRLAND_INSTANCE_SIGNATURE set?)"
        return (
            True,
            f"{len(state.windows)} windows, provider {state.provider}, locked={state.locked}",
        )

    def lua_provider():
        from .world import HyprSocket, snapshot

        provider = snapshot(HyprSocket()).provider
        if provider == "lua":
            return (
                False,
                "Lua config provider: its dispatch strings are unverified in this release",
            )
        return True, "hyprlang"

    def key():
        from . import jev as jev_pkg

        jev_pkg.load_key()
        return True, "present (value never shown)"

    def jev_reachable():
        from . import jev as jev_pkg

        async def go():
            async with jev_pkg.JevClient(jev_pkg.load_key(), deadline=5.0) as c:
                return await c.warm()

        return (
            (True, "gateway reachable")
            if asyncio.run(go())
            else (False, "gateway not reachable: offline?")
        )

    def model():
        from .stt import models

        name = cfg.stt.local_model
        if models.installed(name):
            return True, f"{name} in {models.model_dir(name)}"
        return False, f"{name} is not downloaded: run `hyprsay setup`"

    def microphone():
        import sounddevice as sd

        device = sd.query_devices(cfg.stt.device or None, kind="input")
        return True, f"{device['name']} ({int(device['default_samplerate'])} Hz)"

    def hud():
        probe = "; ".join(
            [
                "from ctypes import CDLL",
                "CDLL('libgtk4-layer-shell.so')",  # must load before gi, or it is a tiled window
                "import gi",
                "gi.require_version('Gtk', '4.0')",
                "gi.require_version('Gtk4LayerShell', '1.0')",
                "from gi.repository import Gtk4LayerShell as L",
                "print(L.is_supported())",
            ]
        )
        out = subprocess.run(
            ["/usr/bin/python3", "-c", probe], capture_output=True, text=True, timeout=10
        )
        if out.returncode != 0:
            return (
                False,
                "system python lacks python-gobject or gtk4-layer-shell: "
                + out.stderr.strip()[-160:],
            )
        return out.stdout.strip() == "True", f"gtk4-layer-shell supported: {out.stdout.strip()}"

    def bind():
        out = subprocess.run(["hyprctl", "binds"], capture_output=True, text=True, timeout=5).stdout
        if "hyprsay" in out:
            return True, "a hyprsay bind is active"
        return (
            False,
            "no hyprsay bind found: run `hyprsay binds` and add the two lines to your config",
        )

    def tools():
        missing = [t for t in ("wpctl", "playerctl") if not shutil.which(t)]
        return (not missing), (
            "volume and media ready" if not missing else f"missing: {', '.join(missing)}"
        )

    for name, fn in [
        ("compositor", hyprland),
        ("config provider", lua_provider),
        ("gateway key", key),
        ("gateway", jev_reachable),
        ("speech model", model),
        ("microphone", microphone),
        ("hud toolkit", hud),
        ("push to talk bind", bind),
        ("volume and media", tools),
    ]:
        check(name, fn)
    width = max(len(name) for name, _, _ in checks)
    for name, ok, detail in checks:
        print(f"  {'ok  ' if ok else 'FAIL'}  {name:<{width}}  {detail}")
    print(
        f"\n  speech: {cfg.stt.backend} (local {cfg.stt.local_model}, cloud {cfg.stt.cloud_model})"
    )
    print(f"  window titles sent to the cloud: {cfg.privacy.titles}")
    return 0 if all(ok for _, ok, _ in checks) else 1


# ------------------------------------------------------------------------------ the rest


def _setup(cfg, args) -> int:
    from .stt import models

    print(f"speech model: {cfg.stt.local_model}")

    def progress(done: int, total: int) -> None:
        print(f"\r  downloading {done // 1_000_000} / {total // 1_000_000} MB", end="", flush=True)

    models.ensure(cfg.stt.local_model, progress=progress)
    print("\r  ready" + " " * 30)
    file = config.CONFIG_FILE.expanduser()
    if not file.exists():
        file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        file.write_text(_DEFAULT_CONFIG)
        print(f"wrote {file}")
    print("\nadd these two lines to your Hyprland config:\n")
    _binds(cfg, argparse.Namespace(key="SUPER, V"))
    if args.units:
        return _install_units()
    print("\nthen: hyprsay setup --units   (installs and starts the systemd user units)")
    return 0


def _install_units() -> int:
    source = Path(__file__).resolve().parents[2] / "packaging" / "systemd"
    target = Path(os.path.expanduser("~/.config/systemd/user"))
    target.mkdir(parents=True, exist_ok=True)
    hud = Path(__file__).resolve().parents[2] / "hud" / "hyprsay_hud.py"
    exe = shutil.which("hyprsay") or f"{sys.executable} -m hyprsay"
    for unit in ("hyprsay-engine.service", "hyprsay-hud.service"):
        text = (source / unit).read_text().replace("@HYPRSAY@", exe).replace("@HUD@", str(hud))
        (target / unit).write_text(text)
        print(f"installed {target / unit}")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    subprocess.run(
        ["systemctl", "--user", "enable", "--now", "hyprsay-engine", "hyprsay-hud"], check=False
    )
    return 0


def _binds(cfg, args) -> int:
    from .activation import bind_lines
    from .world import HyprSocket, snapshot

    provider = snapshot(HyprSocket()).provider
    for line in bind_lines(provider, args.key):
        print(line)
    return 0


def _inspect(cfg, args) -> int:
    entries = journal.tail(args.n, kind="utterance")
    if not entries:
        print("nothing recorded yet")
        return 0
    if args.json:
        print(json.dumps(entries, indent=1))
        return 0
    for e in entries:
        t, d = e.get("transcript") or {}, e.get("decision") or {}
        print(
            f"heard     {t.get('text')!r}   via {t.get('backend')}"
            + ("  (cloud rescue)" if t.get("rescued") else "")
        )
        print(f"decided   {d.get('verdict')} tier {d.get('tier')}: {d.get('reason')}")
        print(f"took      {e.get('ms')} ms for {e.get('seconds')} s of speech")
        exchange = e.get("exchange") or {}
        print("sent to the cloud:")
        print(json.dumps(exchange.get("requests", exchange), indent=1)[:4000])
    return 0


def _ptt(cfg, args) -> int:
    from .world import HyprSocket

    reply = HyprSocket().request(f"dispatch event hyprsay:{args.edge}")
    return 0 if reply.strip() == "ok" else 1


_DEFAULT_CONFIG = """\
# hyprsay configuration. Every key is optional; these are the defaults worth knowing.

[stt]
# hybrid: local first, the cloud only when the local transcript leads nowhere
# local:  audio never leaves this machine      cloud: always use the gateway model
backend = "hybrid"
local_model = "parakeet-110m"
cloud_model = "fish-audio/transcribe-1"

[privacy]
# never: no window title ever leaves the machine
# when_needed: only to tell apart several windows of one app, and only theirs
titles = "when_needed"

[safety]
# apps where dictated text may be typed without a countdown
type_allow_classes = []

[aliases]
# browser = "firefox"
"""


if __name__ == "__main__":
    raise SystemExit(main())
