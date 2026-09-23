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
    out = subprocess.run(
        ["playerctl", "-a", "metadata", "--format", "{{playerName}}.{{playerInstance}} {{status}}"],
        capture_output=True,
        text=True,
    ).stdout
    found: dict[str, str] = {}
    for line in out.splitlines():
        name, _, status = line.strip().rpartition(" ")
        if name:
            found[name] = status
    return found


def _profile() -> str:
    """The Chrome profile the owner used last, which is where the extension is loaded."""
    state = Path("~/.config/google-chrome/Local State").expanduser()
    try:
        return str(json.loads(state.read_text())["profile"].get("last_used") or "Default")
    except (OSError, ValueError, KeyError):
        return "Default"


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").removeprefix("www.")


@dataclass
class Before:
    tabs: list[dict[str, Any]]
    volume: float
    playing: set[str]
    workspace: int


def _record(browser: BrowserClient) -> Before:
    playing = {name for name, status in _players().items() if status == "Playing"}
    workspace = int(_hyprctl("activeworkspace").get("id", 1))
    return Before(browser.tabs(), _volume(), playing, workspace)


def _restore(browser: BrowserClient, before: Before) -> list[str]:
    done: list[str] = []
    for name, status in _players().items():
        if status == "Playing" and name not in before.playing:
            subprocess.run(["playerctl", "-p", name, "pause"], capture_output=True)
            done.append(f"paused {name}")
        elif status != "Playing" and name in before.playing:
            # the owner's own music was playing before the demo and "pause" reached it
            subprocess.run(["playerctl", "-p", name, "play"], capture_output=True)
            done.append(f"resumed {name}")
    now = browser.tabs()
    was = {t["id"] for t in before.tabs}
    for tab in now:
        if tab["id"] not in was:
            browser.close_tab(tab["id"])
            done.append(f"closed {_host(tab['url'])}")
    still = {t["id"] for t in browser.tabs()}
    for tab in before.tabs:
        if tab["id"] not in still and urlparse(tab["url"]).scheme in ("http", "https"):
            browser.open(tab["url"], "new")
            done.append(f"reopened {_host(tab['url'])}")
    if before.volume >= 0:
        subprocess.run(
            ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{before.volume:.2f}"],
            capture_output=True,
        )
        done.append(f"volume back to {before.volume:.2f}")
    subprocess.run(["hyprctl", "dispatch", "workspace", str(before.workspace)], capture_output=True)
    done.append(f"workspace back to {before.workspace}")
    return done


# --------------------------------------------------------------------------- the checks


def _active(browser: BrowserClient) -> dict[str, Any]:
    return next((t for t in browser.tabs() if t.get("active")), {})


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

    def _scroll(self) -> float:
        try:
            return float(self.browser.snapshot(max=1)["viewport"]["scroll_y"])
        except Exception:
            return -1.0

    def check(self, line: Line) -> tuple[bool, str]:
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
        token_cap=cfg.jev.token_cap,
        zero_data_retention=cfg.jev.zero_data_retention,
    )
    await client.warm()
    lexicon = Lexicon.scan(cfg)
    services = adapter_pkg.live(cfg)
    browser_adapter = next(a for a in services if a.name == "browser")
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

    probe_client = BrowserClient(timeout=4.0)
    before = _record(probe_client)
    probe = Probe(probe_client)
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
        subprocess.run(["hyprctl", "dispatch", "workspace", str(free[0])], capture_output=True)
        time.sleep(0.4)
        subprocess.Popen(
            [
                "google-chrome-stable",
                f"--profile-directory={_profile()}",
                "--new-window",
                "https://open.spotify.com/",
                "https://duckduckgo.com/",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(6)
        subprocess.run(["hyprctl", "dispatch", "workspace", str(free[1])], capture_output=True)
        time.sleep(0.6)
        for line in LINES:
            probe.before(line)
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
                outcome = executor.execute(step.action, pinned_address=with_state.active_address)
                understander.acted(outcome.ok)
                did.append(f"{step.action.describe()}: {outcome.message}")
                if not outcome.ok:
                    ok_all = False
                    break
            if not args.dry:
                time.sleep(line.settle)
                passed, evidence = probe.check(line)
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
                }
            )
            mark = "PASS" if passed else "FAIL"
            print(f"{mark}  {line.said!r}  heard {heard!r}  ({decided_ms} ms)")
            print(f"      did: {'; '.join(did)}")
            print(f"      saw: {evidence}")
    finally:
        await client.aclose()
        restored = [] if args.keep else _restore(probe_client, before)
        print("\nrestored: " + ", ".join(restored))
    passed = sum(r["passed"] for r in results)
    print(f"\n{passed}/{len(results)} lines passed")
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps({"voice": args.voice, "results": results}, indent=1))
    print(f"saved {args.out}")
    return 0 if passed == len(results) else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--voice", action="store_true", help="speak each line with a test voice")
    parser.add_argument("--dry", action="store_true", help="decide, but carry nothing out")
    parser.add_argument("--keep", action="store_true", help="leave the machine as the demo left it")
    parser.add_argument("--clips", default=str(Path(__file__).parent / "results" / "demo-clips"))
    parser.add_argument("--out", default=str(Path(__file__).parent / "results" / "demo.json"))
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
