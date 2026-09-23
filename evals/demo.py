"""Run the ten-line demo against the real desktop and the real browser, and check each line.

    uv run python evals/demo.py            typed: the sentences go straight to the pipeline
    uv run python evals/demo.py --voice    each sentence is spoken by a synthetic voice and
                                           heard by the same recognizer the engine uses
    uv run python evals/demo.py --dry      decide everything, carry nothing out

The demo (docs/superpowers/specs/2026-09-23-browser-voice-design.md) is the acceptance test:
from an empty workspace, bring Chrome up, switch to the Spotify tab, play a track, open
YouTube, search it, scroll, click the second video, pause and turn it down, go back to
Spotify, and close the tab. One pipeline runs all ten lines in order, so the conversation's
memory carries from line to line exactly as it does in the engine ("a bit more" means more of
the scroll before it).

Each line is checked by what actually changed, not by what the pipeline said it did: the
active tab and its address, whether a tab is making sound, the page's scroll position, the
system volume.

IT LEAVES THE MACHINE AS IT FOUND IT. Before anything runs it records the open tabs, the
volume, which media players were playing and the workspace in front; afterwards it closes the
tabs it opened, reopens any it closed, pauses anything it started, and puts the volume and the
workspace back. A voice pass needs the Piper voice in ~/.cache/hyprsay/tts.

This is not the owner's run. A synthetic voice is not a human one, and the only acceptance
that counts is the owner saying these lines with the key held.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import subprocess
import sys
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))

from hyprsay import config  # noqa: E402
from hyprsay.model import Transcript, Verdict  # noqa: E402
from hypruse.browser.client import BrowserClient  # noqa: E402

TTS_DIR = Path("~/.cache/hyprsay/tts/vits-piper-en_US-lessac-medium").expanduser()
ACTING = {Verdict.ACT, Verdict.ACT_SWAP, Verdict.COUNTDOWN}


# --------------------------------------------------------------------------- reading the machine


def _hyprctl(*args: str) -> Any:
    out = subprocess.run(["hyprctl", "-j", *args], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def _volume() -> float:
    out = subprocess.run(
        ["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"], capture_output=True, text=True
    ).stdout
    try:
        return float(out.split()[1])
    except (IndexError, ValueError):
        return -1.0


def _players() -> dict[str, str]:
    """Every player by the name `playerctl -p` takes, and what it is doing."""
    names = subprocess.run(["playerctl", "-l"], capture_output=True, text=True).stdout.split()
    found: dict[str, str] = {}
    for name in names:
        status = subprocess.run(
            ["playerctl", "-p", name, "status"], capture_output=True, text=True
        ).stdout.strip()
        found[name] = status
    return found


def _muted() -> bool:
    out = subprocess.run(
        ["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"], capture_output=True, text=True
    ).stdout
    return "[MUTED]" in out


EXTENSION_ID = "pcfjlemgcbbpppclachnobhnnnahpgoj"


def _profile() -> str:
    """The Chrome profile hypruse-browser is loaded in, else the one used last.

    Chrome records an extension per profile, in that profile's preferences, so the profile
    whose preferences name the extension is the one whose windows the extension can see.
    """
    root = Path("~/.config/google-chrome").expanduser()
    for prefs in sorted(root.glob("*/Secure Preferences")) + sorted(root.glob("*/Preferences")):
        try:
            if EXTENSION_ID in prefs.read_text(errors="ignore"):
                return prefs.parent.name
        except OSError:
            continue
    try:
        state = json.loads((root / "Local State").read_text())
        return str(state["profile"].get("last_used") or "Default")
    except (OSError, ValueError, KeyError):
        return "Default"


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").removeprefix("www.")


@dataclass
class Before:
    tabs: list[dict[str, Any]]
    volume: float
    muted: bool
    playing: set[str]
    workspace: int
    windows: set[str]


def _record(browser: BrowserClient) -> Before:
    playing = {name for name, status in _players().items() if status == "Playing"}
    workspace = int(_hyprctl("activeworkspace").get("id", 1))
    windows = {c["address"] for c in _hyprctl("clients")}
    return Before(browser.tabs(), _volume(), _muted(), playing, workspace, windows)


@dataclass
class Own:
    """What the demo itself made or changed, which is all the restore may undo.

    Everything else on the machine is the owner's, including whatever appears while the
    demo runs: a run on 23 September closed a Firefox window and an extension's page that
    the owner opened mid-run, because the restore then closed anything new.
    """

    chrome_window: str = ""  # the Hyprland address of the Chrome window it opened
    chrome_window_id: int | None = None  # the same window, as the extension numbers it
    workspaces: tuple[int, ...] = ()
    launched: list[str] = field(default_factory=list)  # windows a line's launch opened
    paused: set[str] = field(default_factory=set)  # players its "pause" line paused
    closed: list[dict[str, Any]] = field(default_factory=list)  # owner tabs it closed
    seen: list[str] = field(default_factory=list)  # what appeared that it left alone


def _restore(browser: BrowserClient, before: Before, own: Own) -> list[str]:
    done: list[str] = []
    if own.chrome_window:
        # its tabs go with it, and so does whatever they were playing
        subprocess.run(
            ["hyprctl", "dispatch", "closewindow", f"address:{own.chrome_window}"],
            capture_output=True,
        )
        done.append("closed its Chrome window")
    for address in own.launched:
        subprocess.run(
            ["hyprctl", "dispatch", "closewindow", f"address:{address}"], capture_output=True
        )
        done.append("closed a window a line launched")
    for tab in own.closed:
        browser.open(tab["url"], "new")
        done.append(f"reopened {_host(tab['url'])}")
    if own.paused:
        time.sleep(0.5)
        for name, status in _players().items():
            if name in own.paused and name in before.playing and status != "Playing":
                subprocess.run(["playerctl", "-p", name, "play"], capture_output=True)
                done.append(f"resumed {name}")
    if before.volume >= 0:
        subprocess.run(
            ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{before.volume:.2f}"],
            capture_output=True,
        )
        subprocess.run(
            ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "1" if before.muted else "0"],
            capture_output=True,
        )
        done.append(f"volume back to {before.volume:.2f}{', muted' if before.muted else ''}")
    # back to where the owner was, unless they have already gone somewhere themselves
    if int(_hyprctl("activeworkspace").get("id", 0)) in own.workspaces:
        subprocess.run(
            ["hyprctl", "dispatch", "workspace", str(before.workspace)], capture_output=True
        )
        done.append(f"workspace back to {before.workspace}")
    done.extend(f"left alone: {what}" for what in own.seen)
    return done


# --------------------------------------------------------------------------- the checks


def _active(browser: BrowserClient) -> dict[str, Any]:
    return next((t for t in browser.tabs() if t.get("active")), {})


def _moved_away(own: Own, browser: BrowserClient) -> str:
    """What someone else put in front of the demo's Chrome window, or "".

    Two checks, since Hyprland and Chrome each keep their own idea of what is in front: the
    focused window, and the tab the extension will act on (the last focused Chrome window's).
    """
    active = _hyprctl("activewindow")
    if isinstance(active, dict) and active.get("address") != own.chrome_window:
        where = active.get("workspace", {}).get("id", "?")
        return f"a {active.get('class', '?')} window on workspace {where}"
    tab = _active(browser)
    if tab and tab.get("window_id") != own.chrome_window_id:
        return "a tab in another Chrome window"
    return ""


# The laptop's own keyboard and touchpad, as the kernel counts their interrupts. The demo
# drives the machine through Hyprland and the extension, which raise none of these, so a
# count that moves means a person. A USB keyboard shares its interrupt with everything else
# on the bus and is not seen.
_INPUT_DEVICES = ("i8042", "elan_i2c", "touchpad", "keyboard")


def _input_count() -> int:
    total = 0
    try:
        lines = Path("/proc/interrupts").read_text().splitlines()
    except OSError:
        return 0
    for line in lines[1:]:
        fields = line.split()
        if fields and any(d in fields[-1].lower() for d in _INPUT_DEVICES):
            total += sum(int(f) for f in fields[1:] if f.isdigit())
    return total


def _wait_idle(idle_s: float, max_wait_s: float) -> bool:
    """Wait until nobody has touched the keyboard or touchpad for `idle_s`."""
    if idle_s <= 0:
        return True
    started = quiet_since = time.monotonic()
    last = _input_count()
    print(f"waiting for {idle_s:.0f} s with nobody at the keyboard", flush=True)
    while time.monotonic() - started < max_wait_s:
        time.sleep(1.0)
        now = _input_count()
        if now != last:
            last, quiet_since = now, time.monotonic()
        elif time.monotonic() - quiet_since >= idle_s:
            return True
    return False


def _focused_class() -> str:
    return str(_hyprctl("activewindow").get("class", ""))


@dataclass
class Line:
    said: str
    check: str  # a name in CHECKS
    settle: float = 2.0


LINES = (
    Line("open chrome", "chrome_in_front", 1.5),
    Line("open spotify", "on_spotify", 1.5),
    Line("play hold the line", "spotify_playing", 5.0),
    Line("open youtube", "on_youtube", 3.0),
    Line("search for linus tech tips", "youtube_results", 3.0),
    Line("scroll down", "scrolled", 1.5),
    Line("a bit more", "scrolled", 1.5),
    Line("click the second video", "watching", 4.0),
    Line("pause", "youtube_quiet", 1.5),
    Line("turn it down", "quieter", 1.0),
    Line("go back to spotify", "on_spotify", 1.5),
    Line("close this tab", "spotify_closed", 2.5),
)


@dataclass
class Probe:
    browser: BrowserClient
    scroll_y: float = 0.0
    volume: float = 0.0
    spotify_tab: int | None = None
    notes: dict[str, Any] = field(default_factory=dict)

    def before(self, line: Line) -> None:
        if line.check == "scrolled":
            self.scroll_y = self._scroll()
        if line.check == "quieter":
            self.volume = _volume()
        if line.check == "spotify_closed":
            tab = _active(self.browser)
            self.spotify_tab = (
                tab.get("id") if _host(tab.get("url", "")) == "open.spotify.com" else None
            )

    def _logged_out(self) -> bool:
        spotify = [t for t in self.browser.tabs() if _host(t.get("url", "")) == "open.spotify.com"]
        if not spotify:
            return False
        try:
            page = self.browser.snapshot(spotify[0]["id"], max=200)
        except Exception:
            return False
        return any(e.get("name") == "Log in" for e in page.get("elements", []))

    def _scroll(self) -> float:
        try:
            return float(self.browser.snapshot(max=1)["viewport"]["scroll_y"])
        except Exception:
            return -1.0

    def check(self, line: Line) -> tuple[bool | None, str]:
        tab = _active(self.browser)
        url = str(tab.get("url", ""))
        host = _host(url)
        name = line.check
        if name == "chrome_in_front":
            cls = _focused_class()
            return "chrom" in cls.lower(), f"in front: {cls}"
        if name == "on_spotify":
            return host == "open.spotify.com", f"active tab: {host}"
        if name == "spotify_playing":
            audible = any(
                t.get("audible") and _host(t.get("url", "")) == "open.spotify.com"
                for t in self.browser.tabs()
            )
            if not audible and self._logged_out():
                # nothing can play in a player nobody is logged in to: that is a missing
                # prerequisite for the owner, not a fault in the pipeline
                return None, "Spotify is not logged in in this profile, so nothing can play"
            return audible, f"spotify tab audible: {audible}; title: {tab.get('title', '')[:50]}"
        if name == "on_youtube":
            return host == "youtube.com", f"active tab: {host}"
        if name == "youtube_results":
            ok = host == "youtube.com" and "search_query=" in url and "linus" in url.lower()
            return ok, f"url: {url[:80]}"
        if name == "scrolled":
            now = self._scroll()
            return now > self.scroll_y, f"scroll {self.scroll_y:.0f} -> {now:.0f}"
        if name == "watching":
            return host == "youtube.com" and "/watch" in url, f"url: {url[:80]}"
        if name == "youtube_quiet":
            loud = any(
                t.get("audible") and _host(t.get("url", "")) == "youtube.com"
                for t in self.browser.tabs()
            )
            return not loud, f"youtube audible: {loud}"
        if name == "quieter":
            now = _volume()
            return now < self.volume, f"volume {self.volume:.2f} -> {now:.2f}"
        if name == "spotify_closed":
            ids = {t["id"] for t in self.browser.tabs()}
            ok = self.spotify_tab is not None and self.spotify_tab not in ids
            return ok, f"spotify tab {self.spotify_tab} gone: {ok}"
        return False, f"no check called {name}"


def _offered(exchange: dict[str, Any]) -> dict[str, str]:
    """The options the single request put in front of Jev, by id, for the report."""
    for request in exchange.get("requests") or []:
        action = (request.get("questions") or {}).get("action") or {}
        criteria = action.get("criteria") or {}
        if criteria:
            return {
                k: (v.get("label") if isinstance(v, dict) else str(v)) for k, v in criteria.items()
            }
    return {}


def _page(browser_adapter: Any) -> dict[str, Any]:
    """What the adapter had read of the page when the line was decided."""
    from hyprsay.adapters import browser as browser_mod

    snap = dict(browser_adapter._reading.snapshot)
    if not snap:
        return {}
    clickable = browser_mod._clickable(snap)
    groups: dict[str, int] = {}
    for element in clickable:
        listed = element.get("list") or {}
        if listed.get("group"):
            groups[listed["group"]] = int(listed.get("size", 0))
    return {
        "url": str(snap.get("url", ""))[:80],
        "tab": snap.get("tab_id"),
        "viewport": snap.get("viewport"),
        "elements": len(snap.get("elements", [])),
        "groups": groups,
        "main": browser_mod._main_group(clickable),
    }


def _second_turn(exchange: dict[str, Any]) -> dict[str, str]:
    """What the second turn chose among (what a find brought back, then the rest)."""
    sent = exchange.get("requests") or []
    if len(sent) < 2:
        return {}
    out: dict[str, str] = {}
    for question in (sent[1].get("questions") or {}).values():
        for key, value in (question.get("criteria") or {}).items():
            out.setdefault(key, value.get("label") if isinstance(value, dict) else str(value))
    return out


# --------------------------------------------------------------------------- speaking


def _speak(text: str, out: Path) -> Path:
    import sherpa_onnx

    model = next(TTS_DIR.glob("*.onnx"))
    tts = sherpa_onnx.OfflineTts(
        sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                    model=str(model),
                    tokens=str(TTS_DIR / "tokens.txt"),
                    data_dir=str(TTS_DIR / "espeak-ng-data"),
                ),
                num_threads=2,
            )
        )
    )
    audio = tts.generate(text, sid=0, speed=1.0)
    import numpy as np

    samples = np.asarray(audio.samples, dtype=np.float32)
    if audio.sample_rate != 16000:
        # the recognizer takes 16 kHz; a plain linear resample is enough for a test voice
        length = int(len(samples) * 16000 / audio.sample_rate)
        samples = np.interp(
            np.linspace(0, len(samples), length, endpoint=False),
            np.arange(len(samples)),
            samples,
        ).astype(np.float32)
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
    with wave.open(str(out), "wb") as clip:
        clip.setnchannels(1)
        clip.setsampwidth(2)
        clip.setframerate(16000)
        clip.writeframes(pcm)
    return out


# --------------------------------------------------------------------------- the run


async def run(args: argparse.Namespace) -> int:
    from hyprsay import adapters as adapter_pkg
    from hyprsay import jev as jev_pkg
    from hyprsay.executor import Executor, boot
    from hyprsay.executor import use as use_executor
    from hyprsay.lexicon import Lexicon
    from hyprsay.lock import Latch
    from hyprsay.nlu.grammar import Grammar
    from hyprsay.nlu.understand import Understander
    from hyprsay.world import HyprSocket, snapshot, use_socket_transport

    cfg = config.load()
    sock = HyprSocket()
    client = jev_pkg.JevClient(
        jev_pkg.load_key(),
        deadline=cfg.jev.deadline_s,
        hedge_after=cfg.jev.hedge_after_s,
        token_cap=cfg.jev.token_cap,
        zero_data_retention=cfg.jev.zero_data_retention,
    )
    await client.warm()
    lexicon = Lexicon.scan(cfg)
    services = adapter_pkg.live(cfg)
    browser_adapter = next(a for a in services if a.name == "browser")
    # the browser adapter's own account of what it did (where a scroll was heading), kept
    # beside the report
    logging.basicConfig(
        filename=str(Path(args.out).with_suffix(".log")),
        filemode="w",
        level=logging.WARNING,
        format="%(asctime)s %(name)s %(message)s",
    )
    logging.getLogger("hyprsay.adapters.browser").setLevel(logging.DEBUG)
    understander = Understander(cfg, lexicon, Grammar(), client, adapters=services)
    recognizer = None
    if args.voice:
        from hyprsay.stt.hybrid import make_recognizer

        recognizer = make_recognizer(cfg, jev_pkg.load_key())
        warm = getattr(recognizer, "warm", None)
        if warm is not None:
            await warm()
    boot()
    use_socket_transport(sock)
    latch = Latch(cfg, lambda: snapshot(sock), sock.query)
    executor = Executor(cfg, lambda: snapshot(sock), latch, lexicon, adapters=services)
    use_executor(executor)

    if not _wait_idle(args.wait_idle, args.max_wait):
        print("the machine never went quiet, so the demo did not run")
        return 2
    probe_client = BrowserClient(timeout=4.0)
    before = _record(probe_client)
    touched = _input_count()
    probe = Probe(probe_client)
    own = Own()
    results: list[dict[str, Any]] = []
    workdir = Path(args.clips)
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        # A window of its own, in the owner's profile, so the demo never touches a window
        # the owner is using: Spotify's player behind, a neutral page in front, so that
        # "open spotify" is a real switch. It opens on an empty workspace, and the demo
        # then starts from another empty one, so "open chrome" has somewhere to come from.
        used = {w["id"] for w in _hyprctl("workspaces")}
        free = [n for n in range(9, 60) if n not in used]
        own.workspaces = (free[0], free[1])
        subprocess.run(["hyprctl", "dispatch", "workspace", str(free[0])], capture_output=True)
        time.sleep(0.4)
        subprocess.Popen(
            [
                "google-chrome-stable",
                f"--profile-directory={_profile()}",
                "--new-window",
                # the first address is the tab in front, so Spotify waits behind it
                "https://duckduckgo.com/",
                "https://open.spotify.com/",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Spotify's player takes 6 to 10 s to render on this machine (measured)
        time.sleep(10)
        own.chrome_window = next(
            (
                c["address"]
                for c in _hyprctl("clients")
                if c["address"] not in before.windows
                and "chrom" in c["class"].lower()
                and c["workspace"]["id"] == free[0]
            ),
            "",
        )
        was = {t["id"] for t in before.tabs}
        own.chrome_window_id = next(
            (t["window_id"] for t in probe_client.tabs() if t["id"] not in was), None
        )
        if not own.chrome_window or own.chrome_window_id is None:
            raise SystemExit("the demo's Chrome window did not open, so nothing was run")
        subprocess.run(["hyprctl", "dispatch", "workspace", str(free[1])], capture_output=True)
        time.sleep(0.6)
        for n, line in enumerate(LINES):
            if _input_count() != touched:
                print("STOP  someone used the keyboard or touchpad; the demo stopped here")
                results.append({"said": line.said, "passed": None, "skipped": True})
                break
            # every line after the first happens in the demo's own window; anything else in
            # front is someone at the machine, and the engine is not let near their tabs
            moved = _moved_away(own, probe_client) if n else ""
            if moved:
                print(f"NOTE  {moved} was in front, from outside the demo; putting it back")
                subprocess.run(
                    ["hyprctl", "dispatch", "focuswindow", f"address:{own.chrome_window}"],
                    capture_output=True,
                )
                time.sleep(0.8)
                if _moved_away(own, probe_client):
                    print(f"SKIP  {line.said!r}: the demo's window could not be put back in front")
                    results.append(
                        {"said": line.said, "passed": None, "skipped": True, "interrupted": moved}
                    )
                    continue
            probe.before(line)
            windows_before = {c["address"] for c in _hyprctl("clients")}
            playing_before = {name for name, st in _players().items() if st == "Playing"}
            with_state = snapshot(sock)
            # what the engine does at key down, a second or so before the words are done
            with contextlib.suppress(Exception):
                browser_adapter.refresh(
                    page=_focused_class().lower().startswith("google-chrome"), force=True
                )
            heard = line.said
            started = time.perf_counter()
            if recognizer is not None:
                clip = _speak(line.said, workdir / f"{len(results):02d}.wav")
                with wave.open(str(clip)) as w:
                    pcm = w.readframes(w.getnframes())
                transcript = await recognizer.transcribe(pcm, 16000)
                heard = transcript.text
            decision = await understander.understand(
                Transcript(heard, backend="demo"),
                with_state,
                pinned_address=with_state.active_address,
            )
            rescue = getattr(recognizer, "rescue", None)
            if recognizer is not None and rescue is not None and decision.rehearable:
                # what the engine does when the local transcript led nowhere
                second = await rescue(pcm, 16000)
                if second.text.strip() and second.text.strip().lower() != heard.strip().lower():
                    heard = second.text
                    decision = await understander.understand(
                        Transcript(heard, backend="demo"),
                        with_state,
                        pinned_address=with_state.active_address,
                    )
            decided_ms = round((time.perf_counter() - started) * 1000)
            page = _page(browser_adapter)
            did: list[str] = []
            ok_all = True
            for step in (decision, *decision.rest):
                if step.verdict not in ACTING or step.action is None:
                    did.append(f"{step.verdict.value}: {step.reason}")
                    ok_all = False
                    break
                if step.verdict is Verdict.COUNTDOWN:
                    time.sleep(cfg.safety.countdown_s)
                if args.dry:
                    did.append(f"(dry) {step.action.describe()}")
                    continue
                service = step.action.service
                if service is not None and service.capability == "browser.close_tab":
                    closing = int(service.handle.split()[1])
                    theirs = next((t for t in before.tabs if t["id"] == closing), None)
                    if theirs is not None:
                        own.closed.append(theirs)
                outcome = executor.execute(step.action, pinned_address=with_state.active_address)
                understander.acted(outcome.ok)
                did.append(f"{step.action.describe()}: {outcome.message}")
                if not outcome.ok:
                    ok_all = False
                    break
            if not args.dry:
                time.sleep(line.settle)
                passed, evidence = probe.check(line)
                launched = decision.action is not None and decision.action.intent.value in (
                    "launch_app",
                )
                for c in _hyprctl("clients"):
                    if c["address"] in windows_before:
                        continue
                    if launched:
                        own.launched.append(c["address"])
                    else:
                        own.seen.append(f"a {c['class']} window that appeared during the run")
                if line.check == "youtube_quiet":
                    own.paused = {
                        name
                        for name, st in _players().items()
                        if name in playing_before and st != "Playing"
                    }
            else:
                passed, evidence = ok_all, "dry run"
            results.append(
                {
                    "said": line.said,
                    "heard": heard,
                    "verdict": decision.verdict.value,
                    "did": did,
                    "passed": passed,
                    "evidence": evidence,
                    "decided_ms": decided_ms,
                    "path": understander.last_exchange.get("path"),
                    "decided": understander.last_exchange.get("decided"),
                    "options": _offered(understander.last_exchange),
                    "turn": understander.last_exchange.get("turn"),
                    "second": _second_turn(understander.last_exchange),
                    "errors": dict(understander.last_exchange.get("errors") or {}),
                    "interrupted": moved,
                    "page": page,
                }
            )
            mark = {True: "PASS", False: "FAIL", None: "BLOCKED"}[passed]
            print(f"{mark}  {line.said!r}  heard {heard!r}  ({decided_ms} ms)")
            print(f"      did: {'; '.join(did)}")
            print(f"      saw: {evidence}")
    finally:
        await client.aclose()
        restored = [] if args.keep else _restore(probe_client, before, own)
        print("\nrestored: " + ", ".join(restored))
    passed = sum(r["passed"] is True for r in results)
    skipped = sum(bool(r.get("skipped")) for r in results)
    blocked = sum(r["passed"] is None for r in results) - skipped
    print(f"\n{passed}/{len(results)} lines passed, {blocked} blocked on a prerequisite")
    if skipped:
        print(f"{skipped} skipped because someone else had the machine in front of them")
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps({"voice": args.voice, "results": results}, indent=1))
    print(f"saved {args.out}")
    return 0 if passed + blocked == len(results) and not skipped else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--voice", action="store_true", help="speak each line with a test voice")
    parser.add_argument("--dry", action="store_true", help="decide, but carry nothing out")
    parser.add_argument("--keep", action="store_true", help="leave the machine as the demo left it")
    parser.add_argument(
        "--wait-idle",
        type=float,
        default=60.0,
        help="seconds of nobody at the keyboard before starting (0: start at once)",
    )
    parser.add_argument("--max-wait", type=float, default=3600.0, help="give up waiting after")
    parser.add_argument("--clips", default=str(Path(__file__).parent / "results" / "demo-clips"))
    parser.add_argument("--out", default=str(Path(__file__).parent / "results" / "demo.json"))
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
