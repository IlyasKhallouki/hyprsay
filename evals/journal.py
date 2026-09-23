"""The owner's real voice commands, replayed through the decision path.

    uv run python evals/journal.py --path single --reps 3
    uv run python evals/journal.py --path fanout --reps 3
    uv run python evals/journal.py --offline

Every transcript below is what the recognizer produced from the owner's own voice on 21 and
22 September 2026 (`~/.local/state/hyprsay/journal.jsonl`, backends
`local:parakeet-110m` and `gateway:fish-audio/transcribe-1`). The desktop each one was
spoken on is rebuilt from the journal's own context block where it has one, and from the
order of events where it does not. The label is what the owner meant, which the journal
shows through what they said next.

Four utterances are left out, each for a reason written in `EXCLUDED`, and the three
identical "Open GitHub. com" in a row count once.

The capabilities are the ones that existed in the live session: the waytify adapter's
"play a track, album or playlist on Spotify, found by name", MPRIS play or pause per live
player (the "Play Spotify" option that pulled Jev away from the right answer live), and
the browser keystroke recipes. The adapters here are stand-ins with no socket behind them;
the options they put in front of Jev are worded exactly as the real ones.

Outcomes, as in `run.py`, plus one:

    partial   the first command of a compound was right and a later one was missing.
              Safe, and not counted as correct.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from hyprsay import config  # noqa: E402
from hyprsay.adapters import mpris, waytify  # noqa: E402
from hyprsay.adapters.base import Adapter, Affordance, Minted, Trust, tier_for  # noqa: E402
from hyprsay.model import (  # noqa: E402
    Action,
    App,
    Decision,
    DesktopState,
    Monitor,
    Outcome,
    Transcript,
    Verdict,
    Window,
    Workspace,
)

ACTING = {Verdict.ACT, Verdict.ACT_SWAP, Verdict.COUNTDOWN, Verdict.CONFIRM_KEY}

# --------------------------------------------------------------------------- the machine


def _app(id_: str, name: str, generic: str, kind: str, *keywords: str, cls: str = "") -> App:
    return App(id_, name, generic, kind, tuple(keywords), (), (id_,), (cls or id_,), True,
               f"/usr/share/applications/{id_}.desktop")  # fmt: skip


APPS = (
    _app("google-chrome", "Google Chrome", "Web Browser", "web browser", "browser", "internet"),
    _app("spotify", "Spotify", "Music Player", "music player", "music", "songs", cls="Spotify"),
    _app("kitty", "kitty", "Terminal emulator", "terminal", "shell", "console"),
    _app("com.rtosta.zapzap", "ZapZap", "WhatsApp client", "chat", "whatsapp", cls="zapzap"),
    _app("org.kde.dolphin", "Dolphin", "File Manager", "file manager", "files", "folders"),
    _app("code", "Visual Studio Code", "Text Editor", "code editor", "vscode", "editor"),
)

# class, title, the workspace it lived on in the session
WINDOW = {
    "kitty": ("kitty", "~/projects/hyprsay", 6),
    "chrome": ("google-chrome", "YouTube - Google Chrome", 8),
    "spotify": ("Spotify", "Spotify Premium", 9),
    "zapzap": ("zapzap", "ZapZap", 2),
}
ADDRESS = {"kitty": "0x0a01", "chrome": "0x0a02", "spotify": "0x0a03", "zapzap": "0x0a04"}


def desk(focused: str, *others: str, ws: dict[str, int] | None = None) -> DesktopState:
    """The desktop: `focused` in front, `others` open behind it in the order given."""
    order = (focused, *others)
    placed = {**{k: v[2] for k, v in WINDOW.items()}, **(ws or {})}
    windows = tuple(
        Window(ADDRESS[key], WINDOW[key][0], WINDOW[key][0], WINDOW[key][1], placed[key],
               str(placed[key]), 0, focus_rank=rank, at=(40, 40), size=(1800, 1000))
        for rank, key in enumerate(order)
    )  # fmt: skip
    current = placed[focused]
    return DesktopState(
        windows=windows,
        workspaces=tuple(Workspace(n, str(n), "eDP-1") for n in sorted({*placed.values()})),
        monitors=(Monitor(0, "eDP-1", 0, 0, 1920, 1080, 1.0, True, current),),
        active_address=ADDRESS[focused],
        active_workspace_id=current,
        locked=False,
    )


class Waytify(Adapter):
    """The waytify adapter's three capabilities, answering any query with three results,
    the named one first, as Spotify's own ranking would."""

    name = "waytify"

    def __init__(self) -> None:
        self._minted = Minted()

    def probe(self):
        return True, "stand-in"

    def can_serve(self, context):
        return True

    def capabilities(self, context):
        return waytify.CAPABILITIES

    def resolve(self, capability, slots, context):
        declared = waytify.BY_ID[capability]
        query = str(slots.get("query", "")).strip().title() or "Something"
        # labelled the way Spotify names each kind: a track and an album carry an artist,
        # a playlist is just its name
        found = {
            "waytify.play_track": (f"{query} - Toto", f"{query} (Live) - Toto", "Africa - Toto"),
            "waytify.play_album": (f"{query} - Toto", f"{query} (Deluxe) - Toto", "IV - Toto"),
            "waytify.play_playlist": (query, f"{query} Mix", "Daily Mix 1"),
        }[capability]
        return tuple(
            Affordance(id=f"{capability}#{n}", label=label, kind=declared.kind,
                       tier=tier_for(declared, label),
                       handle=self._minted.mint(f"spotify:{capability}:{n}", label),
                       adapter=self.name, capability=capability, trust=Trust.UNTRUSTED)
            for n, label in enumerate(found, 1)
        )  # fmt: skip

    def perform(self, capability, handle, context):
        return Outcome(True, "stand-in")


class Players(Adapter):
    """MPRIS: one player per open Spotify or Chrome window, play or pause and skip."""

    name = "mpris"
    NAMES = {"Spotify": "Spotify", "google-chrome": "Google Chrome"}

    def __init__(self) -> None:
        self._minted = Minted()

    def probe(self):
        return True, "stand-in"

    def _players(self, context):
        return [self.NAMES[w.cls] for w in context.state.windows if w.cls in self.NAMES]

    def can_serve(self, context):
        return bool(self._players(context))

    def capabilities(self, context):
        return tuple(mpris.BY_ID[k] for k in ("mpris.play_pause", "mpris.next", "mpris.previous"))

    def resolve(self, capability, slots, context):
        declared = mpris.BY_ID[capability]
        word = mpris.VERBS[capability].word
        return tuple(
            Affordance(id=f"{capability}#{n}", label=f"{word} {name}", kind=declared.kind,
                       tier=tier_for(declared, name),
                       handle=self._minted.mint(f"org.mpris.MediaPlayer2.{n}", name),
                       adapter=self.name, capability=capability, trust=Trust.UNTRUSTED)
            for n, name in enumerate(self._players(context), 1)
        )  # fmt: skip

    def perform(self, capability, handle, context):
        return Outcome(True, "stand-in")


# --------------------------------------------------------------------------- the labels


@dataclass(frozen=True)
class Want:
    """One acceptable action. Every field left at its default is not checked."""

    intents: frozenset[str]
    app: str = ""  # a WINDOW key for window targets, an app id for launches
    workspace: str | None = None
    verbs: frozenset[str] = frozenset()
    text: str = ""  # a regex the action's text must contain
    number: int | None = None
    capabilities: frozenset[str] = frozenset()
    rest: tuple[Want, ...] = ()


def want(intents: str, app: str = "", **kw: Any) -> Want:
    rest = tuple(kw.pop("rest", ()))
    for key in ("verbs", "capabilities"):
        if key in kw:
            kw[key] = frozenset(kw[key].split())
    return Want(frozenset(intents.split()), app, rest=rest, **kw)


# play or pause, however it is reached: the transport verb or the player's own option
PLAY_PAUSE = (want("media", verbs="play_pause"),
              want("service", capabilities="mpris.play_pause"))  # fmt: skip
SPOTIFY_THING = "waytify.play_track waytify.play_album waytify.play_playlist"


@dataclass(frozen=True)
class Case:
    said: str
    state: DesktopState
    wants: tuple[Want, ...]  # any one of them is correct
    tag: str
    expect: str = "act"
    # (words, intent, window key, seconds ago) of the command before, when it matters
    previous: tuple[str, str, str, int] | None = None
    note: str = ""


def case(said, state, *wants, tag, previous=None, expect="act", note="") -> Case:
    flat: list[Want] = []
    for w in wants:
        flat.extend(w if isinstance(w, tuple) else (w,))
    return Case(said, state, tuple(flat), tag, expect, previous, note)


BASE = ("kitty", "chrome", "spotify", "zapzap")

CASES: tuple[Case, ...] = (
    # 21 September, early afternoon: a terminal in front, no browser yet
    case("Workspace ten.", desk("kitty"), want("switch_workspace", workspace="10"),
         tag="canonical"),
    case("Open Zap.", desk("kitty"), want("launch_app", "com.rtosta.zapzap"), tag="asr",
         note="ZapZap, cut short by the recognizer"),
    case("Open Zep Zep here.", desk("kitty"), want("launch_app", "com.rtosta.zapzap"),
         tag="asr"),
    case("Open Chrome.", desk("kitty", "zapzap"), want("launch_app", "google-chrome"),
         tag="canonical", note="Chrome was not running yet"),
    # 21 September, evening
    case("Open Spotify and search for Deft Bunk.", desk("kitty", "chrome", "zapzap"),
         want("launch_app", "spotify", rest=(want("service", capabilities=SPOTIFY_THING),)),
         tag="compound"),
    case("Search for Death Punk.", desk("spotify", "kitty", "chrome", "zapzap"),
         want("service", capabilities=SPOTIFY_THING), tag="spotify",
         note="Spotify was in front; the owner wanted Spotify, not the web"),
    case("Search for a depth punk on Spotify.", desk("chrome", "spotify", "kitty", "zapzap"),
         want("service", capabilities=SPOTIFY_THING), tag="spotify"),
    case("Open Spotify.", desk("chrome", "kitty", "zapzap"), want("launch_app", "spotify"),
         tag="canonical"),
    case("Close Spotify.", desk("chrome", "spotify", "kitty", "zapzap"),
         want("close_window", "spotify"), tag="canonical"),
    case("Look in YouTube for linus detectives", desk("chrome", "kitty", "zapzap"),
         want("run_recipe", verbs="search_youtube", text="linus"), tag="browser"),
    # 22 September, midday: Chrome on 8, Spotify on 9, the owner on 6
    case("Open Spotify in WorkSpace nine.", desk("kitty", "chrome", "zapzap"),
         want("launch_app", "spotify"),
         want("switch_workspace", workspace="9", rest=(want("launch_app", "spotify"),)),
         tag="compound"),
    case("Focus on Spotify.", desk("kitty", *BASE[1:]), want("focus_window", "spotify"),
         tag="canonical"),
    case("Play on repeat playlist.", desk("spotify", "kitty", "chrome", "zapzap"),
         want("service", capabilities="waytify.play_playlist"), tag="spotify"),
    case("Pause the music.", desk("spotify", "kitty", "chrome", "zapzap"), PLAY_PAUSE,
         tag="canonical"),
    case("Turn it on again and turn down the volume.",
         desk("spotify", "kitty", "chrome", "zapzap"),
         *(replace(w, rest=(want("volume", verbs="down"),)) for w in PLAY_PAUSE),
         tag="compound", previous=("pause the music", "media", "spotify", 18)),
    case("Open Chrome and open YouTube. com", desk("spotify", "kitty", "chrome", "zapzap"),
         want("focus_window", "chrome",
              rest=(want("run_recipe", verbs="go_to_url open_url", text=r"youtube\.com"),)),
         tag="compound"),
    case("Open YouTube.com.", desk("chrome", "spotify", "kitty", "zapzap"),
         want("run_recipe", verbs="go_to_url open_url", text=r"youtube\.com"), tag="browser"),
    case("Find the YouTube channel LT", desk("chrome", "spotify", "kitty", "zapzap"),
         want("run_recipe", verbs="search_youtube find", text=r"\blt"), tag="browser"),
    case("Open the YouTube channel LTT.", desk("chrome", "spotify", "kitty", "zapzap"),
         want("run_recipe", verbs="search_youtube", text="ltt"), tag="browser"),
    case("Open Spotify on Workspace Nine.", desk("kitty", "chrome", "zapzap"),
         want("launch_app", "spotify"),
         want("switch_workspace", workspace="9", rest=(want("launch_app", "spotify"),)),
         tag="compound"),
    case("Focus on it.", desk("kitty", "spotify", "chrome", "zapzap"),
         want("focus_window", "spotify"), tag="context",
         previous=("open spotify on workspace 9", "launch_app", "spotify", 15)),
    case("Focus on spotterfoy.", desk("kitty", "spotify", "chrome", "zapzap"),
         want("focus_window", "spotify"), tag="asr"),
    case("Move my browser here.", desk("spotify", "chrome", "kitty", "zapzap"),
         want("move_to_workspace", "chrome", workspace="9"), tag="semantic"),
    case("Play the song, but turn down the volume to 50%.",
         desk("spotify", "kitty", "chrome", "zapzap"),
         *(replace(w, rest=(want("volume", verbs="set", number=50),)) for w in PLAY_PAUSE),
         tag="compound"),
    case("Open my browser and go to github dot com", desk("spotify", "chrome", "kitty", "zapzap"),
         want("focus_window", "chrome",
              rest=(want("run_recipe", verbs="go_to_url open_url", text=r"github\.com"),)),
         tag="compound"),
    case("Move Chrome to workspace number nine.", desk("spotify", "chrome", "kitty", "zapzap"),
         want("move_to_workspace", "chrome", workspace="9"), tag="canonical"),
    case("Open GitHub. com", desk("chrome", "spotify", "kitty", "zapzap", ws={"chrome": 9}),
         want("run_recipe", verbs="go_to_url open_url", text=r"github\.com"), tag="browser",
         note="said three times in a row, counted once"),
    case("GitHub.com.", desk("chrome", "spotify", "kitty", "zapzap", ws={"chrome": 9}),
         want("run_recipe", verbs="go_to_url open_url", text=r"github\.com"), tag="browser",
         note="no verb at all, so a countdown cannot be authorized; missing it is safe"),
    case("Open Spotify here, play and turn the volume down to fifty percent.",
         desk("chrome", "kitty", "zapzap", ws={"chrome": 9}),
         want("launch_app", "spotify", rest=(*PLAY_PAUSE[:1], want("volume", verbs="set",
                                                                     number=50))),
         tag="compound"),
    case("Turn it back to seventy five percent, the value.",
         desk("spotify", "chrome", "kitty", "zapzap"),
         want("volume", verbs="set", number=75), tag="context",
         previous=("open spotify here play and turn the volume down to 50 percent",
                   "launch_app", "spotify", 10)),
    # 22 September, evening: Chrome on 13 with the owner
    case("Open Chrome", desk("kitty", "chrome", "zapzap", ws={"chrome": 13, "kitty": 13}),
         want("focus_window", "chrome"), tag="canonical"),
    case("Go to YouTube.", desk("chrome", "kitty", "zapzap", ws={"chrome": 13}),
         want("run_recipe", verbs="go_to_url open_url", text="youtube"), tag="browser"),
    case("Look up LT.", desk("chrome", "kitty", "zapzap", ws={"chrome": 13}),
         want("run_recipe", verbs="search_web search_youtube", text=r"\blt\b"), tag="browser",
         note="LTT, cut short by the recognizer"),
    case("Look up LTT on YouTube.", desk("chrome", "kitty", "zapzap", ws={"chrome": 13}),
         want("run_recipe", verbs="search_youtube", text="ltt"), tag="browser"),
    case("Spotify.", desk("chrome", "kitty", "zapzap", ws={"chrome": 13}),
         want("launch_app", "spotify"), tag="semantic"),
    case("Play. Hold the line.", desk("spotify", "chrome", "kitty", "zapzap",
                                      ws={"chrome": 13, "spotify": 13}),
         want("service", capabilities="waytify.play_track"), tag="spotify",
         previous=("play spotify", "launch_app", "spotify", 8)),
    case("Play hold the line.", desk("spotify", "chrome", "kitty", "zapzap",
                                     ws={"chrome": 13, "spotify": 13}),
         want("service", capabilities="waytify.play_track"), tag="spotify"),
    case("Search for hold the line.", desk("spotify", "chrome", "kitty", "zapzap",
                                           ws={"chrome": 13, "spotify": 13}),
         want("service", capabilities=SPOTIFY_THING), tag="spotify",
         previous=("play hold the line", "media", "spotify", 12)),
)  # fmt: skip

EXCLUDED = {
    "Select the second one.": "points into a page the journal did not record",
    "Two.": "an answer to a numbered prompt, not a command",
    "Open Chrome and Workspace number nine.": "no single intent; the owner restated it as "
    "the next line, which is in the set",
    "Open my browser Open a new channel.": "no recoverable intent",
}


# --------------------------------------------------------------------------- scoring


def _fits(action: Action | None, w: Want) -> bool:
    if action is None or action.intent.value not in w.intents:
        return False
    if w.app:
        if action.intent.value == "launch_app":
            if action.app is None or action.app.id != w.app:
                return False
        elif action.window is None or action.window.address != ADDRESS.get(w.app):
            return False
    if w.workspace is not None and (action.workspace or "") != w.workspace:
        return False
    if w.verbs and (action.verb or "") not in w.verbs:
        return False
    if w.text and not re.search(w.text, _typed(action)):
        return False
    if w.number is not None and action.number != w.number:
        return False
    return not w.capabilities or (
        action.service is not None and action.service.capability in w.capabilities
    )


def _typed(action: Action) -> str:
    """What the action would actually type: an address recipe types the host its text
    becomes, and a text that cannot become one types nothing."""
    text = (action.text or "").lower()
    if action.verb in ("go_to_url", "open_url"):
        from hyprsay import recipes

        try:
            return recipes.normalize_host(text)
        except recipes.RecipeError:
            return ""
    return text


def score(d: Decision, c: Case) -> str:
    acted = d.verdict in ACTING
    if c.expect != "act":
        return "WRONG ACTION" if acted else "correct"
    if not acted:
        return "asked" if d.verdict is Verdict.HINTS else "missed"
    matched = [w for w in c.wants if _fits(d.action, w)]
    if not matched:
        return "WRONG ACTION"
    best = "partial"
    for w in matched:
        rest = list(d.rest)
        if any(r.verdict in ACTING and not any(_fits(r.action, x) for x in w.rest) for r in rest):
            return "WRONG ACTION"
        done = [r for r in rest if r.verdict in ACTING]
        if len(done) >= len(w.rest) and all(
            _fits(r.action, x) for r, x in zip(done, w.rest, strict=False)
        ):
            best = "correct"
    return best


OUTCOMES = ("correct", "partial", "asked", "missed", "WRONG ACTION", "CRASH")


# --------------------------------------------------------------------------- running


def _remember(understander: Any, c: Case, lexicon: Any) -> None:
    """Put the command before this one into the understander's memory, as it was live."""
    if c.previous is None:
        return
    from hyprsay.nlu import context

    said, intent, key, ago = c.previous
    window = c.state.by_address(ADDRESS[key])
    app = lexicon.app_for_window(window) if window is not None else None
    now = understander._clock()
    turn = context.Turn(
        said=said,
        intent=intent,
        app=app.name if app else "",
        kind=app.kind if app else "",
        address=window.address if window else "",
        at=now - ago,
        ok=True,
    )
    understander.context = understander.context.remember(turn)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true", help="no Jev: grammar and lexicon only")
    parser.add_argument("--path", choices=("single", "fanout"), default="single")
    parser.add_argument("--reps", type=int, default=1)
    parser.add_argument("--only", default="", help="run only cases whose tag contains this")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    from hyprsay.lexicon import Lexicon
    from hyprsay.nlu.grammar import Grammar
    from hyprsay.nlu.understand import Understander

    base = config.Config()
    cfg = replace(base, jev=replace(base.jev, path=args.path))
    client = None
    if not args.offline:
        from hyprsay import jev

        client = jev.JevClient(jev.load_key(), deadline=cfg.jev.deadline_s,
                               token_cap=cfg.jev.token_cap,
                               zero_data_retention=cfg.jev.zero_data_retention)  # fmt: skip
        await client.warm()
    lexicon = Lexicon(APPS, {})
    grammar = Grammar()
    todo = [c for c in CASES if args.only in c.tag]
    samples: list[dict[str, Any]] = []
    for rep in range(args.reps):
        for c in todo:
            understander = Understander(
                cfg, lexicon, grammar, client, adapters=(Waytify(), Players())
            )
            _remember(understander, c, lexicon)
            started = time.perf_counter()
            try:
                d = await understander.understand(
                    Transcript(c.said, backend="journal"), c.state,
                    pinned_address=c.state.active_address)  # fmt: skip
                outcome = score(d, c)
                got = {
                    "verdict": d.verdict.value,
                    "action": d.action.describe() if d.action else None,
                    "text": d.action.text if d.action else None,
                    "rest": [
                        f"{r.action.describe()} {r.action.text or ''}".strip()
                        if r.action
                        else r.verdict.value
                        for r in d.rest
                    ],
                    "reason": d.reason,
                    "path": understander.last_exchange.get("path"),
                    "decided": understander.last_exchange.get("decided"),
                    "errors": understander.last_exchange.get("errors"),
                }
            except Exception as exc:  # the pipeline promises never to raise: count it if it does
                outcome, got = "CRASH", {"error": f"{type(exc).__name__}: {exc}"}
            ms = round((time.perf_counter() - started) * 1000, 1)
            samples.append({"said": c.said, "tag": c.tag, "rep": rep, "outcome": outcome,
                            "ms": ms, **got})  # fmt: skip
    if client is not None:
        await client.aclose()

    out = Path(args.out or Path(__file__).parent / "results" / f"journal-{args.path}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"path": args.path, "offline": args.offline, "reps": args.reps,
                               "excluded": EXCLUDED, "samples": samples}, indent=1))  # fmt: skip
    report(samples, len(todo), args)
    print(f"\nsaved {out}")
    return 1 if any(x["outcome"] in ("WRONG ACTION", "CRASH") for x in samples) else 0


def report(samples: list[dict[str, Any]], n_cases: int, args: Any) -> None:
    total = Counter(x["outcome"] for x in samples)
    n = len(samples)
    mode = "offline" if args.offline else "live Jev"
    print(f"{n} samples ({n_cases} cases x {args.reps}), path {args.path}, {mode}\n")
    for name in OUTCOMES:
        if total[name]:
            print(f"  {name:14} {total[name]:4}  {100 * total[name] / n:5.1f}%")
    by_tag: dict[str, Counter[str]] = {}
    for x in samples:
        by_tag.setdefault(x["tag"], Counter())[x["outcome"]] += 1
    print("\n  by tag            correct  partial  asked  missed  WRONG")
    for tag, c in sorted(by_tag.items()):
        k = sum(c.values())
        print(f"  {tag:16} {c['correct']:3}/{k:<4} {c['partial']:7}  {c['asked']:5}  "
              f"{c['missed']:6}  {c['WRONG ACTION']:5}")  # fmt: skip
    ms = sorted(x["ms"] for x in samples)
    if ms:
        p90 = ms[min(len(ms) - 1, round(0.9 * (len(ms) - 1)))]
        print(f"\n  per decision: p50 {statistics.median(ms):.0f} ms, p90 {p90:.0f} ms, "
              f"max {ms[-1]:.0f} ms")  # fmt: skip
    for x in samples:
        if x["outcome"] != "correct":
            print(f"\n  {x['outcome']}: {x['said']!r} [{x['tag']}] rep {x['rep']}")
            print(f"     got {x.get('verdict')} {x.get('action')} text={x.get('text')!r} "
                  f"rest={x.get('rest')}")  # fmt: skip
            print(f"     why {x.get('reason')!r} path={x.get('path')} {x.get('decided') or ''}")


__all__ = ["CASES", "EXCLUDED", "Case", "Want", "score"]


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
