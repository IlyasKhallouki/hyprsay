"""Score the understanding pipeline against the labelled set.

    uv run python evals/run.py --offline          grammar and lexicon only, no network
    uv run python evals/run.py --reps 3           live Jev, each case three times
    uv run python evals/run.py --only semantic    one tag

Outcomes are kept apart because they are not the same kind of failure:

    correct       did what the label says
    asked         should have acted, showed hints instead: slower, but safe
    missed        should have acted, did nothing: annoying, but safe
    noisy         should have stayed silent, showed a suggestion: distracting, but safe
    WRONG ACTION  acted with a different intent, target or workspace, or acted at all when
                  it should not have. This is the number that matters.

Every per-case sample is saved, so percentiles and rates can be recomputed later.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import cases  # noqa: E402

from hyprsay import config  # noqa: E402
from hyprsay.model import Decision, Transcript, Verdict  # noqa: E402

ACTING = {Verdict.ACT, Verdict.ACT_SWAP, Verdict.COUNTDOWN, Verdict.CONFIRM_KEY}


def matches(d: Decision, windows, intent, target, workspace, slots=None) -> bool:
    a = d.action
    if a is None or a.intent.value != intent:
        return False
    if workspace is not None and (a.workspace or "") != workspace:
        return False
    # the slot IS the command for these: muting when the speaker said "louder", or moving
    # the window left when they said right, used to score as correct because only the
    # intent was compared
    for field, want in (slots or {}).items():
        got = getattr(a, field, None)
        if hasattr(got, "value"):
            got = got.value
        if got != want:
            return False
    if target is None:
        return True
    if target == "focused":
        focused = windows[cases.FOCUSED].address
        return a.window is None or a.window.address == focused
    if target in windows:
        return a.window is not None and a.window.address == windows[target].address
    return a.app is not None and a.app.id == target


def score(d: Decision, windows, intent, target, workspace, expect, slots=None) -> str:
    acted = d.verdict in ACTING
    if expect == "act":
        if acted:
            ok = matches(d, windows, intent, target, workspace, slots)
            return "correct" if ok else "WRONG ACTION"
        return "asked" if d.verdict is Verdict.HINTS else "missed"
    if acted:
        return "WRONG ACTION"
    wanted = {"hints": Verdict.HINTS, "nothing": Verdict.NOTHING, "refuse": Verdict.REFUSE,
              "suggest": Verdict.SUGGEST}[expect]  # fmt: skip
    if d.verdict is wanted:
        return "correct"
    return "noisy" if expect == "nothing" else "safe-but-different"


OUTCOMES = ("correct", "asked", "missed", "noisy", "safe-but-different", "WRONG ACTION", "CRASH")


def report(samples: list[dict], n_cases: int, args) -> None:
    total = Counter(x["outcome"] for x in samples)
    by_tag: dict[str, Counter] = defaultdict(Counter)
    for x in samples:
        by_tag[x["tag"].split(":")[0]][x["outcome"]] += 1
    n = len(samples)
    mode = "offline" if args.offline else "live Jev"
    print(f"{n} samples ({n_cases} cases x {args.reps}), {mode}\n")
    for name in OUTCOMES:
        if total[name]:
            print(f"  {name:20} {total[name]:4}  {100 * total[name] / n:5.1f}%")
    print("\n  by tag" + " " * 22 + "correct  asked  missed  WRONG")
    for tag, c in sorted(by_tag.items()):
        k = sum(c.values())
        right = f"{c['correct']:3}/{k:<3}"
        print(f"  {tag:26} {right}  {c['asked']:5}  {c['missed']:6}  {c['WRONG ACTION']:5}")
    ms = sorted(x["ms"] for x in samples)
    if ms:
        p90 = ms[min(len(ms) - 1, round(0.9 * (len(ms) - 1)))]
        p50 = statistics.median(ms)
        print(f"\n  per decision: p50 {p50:.0f} ms, p90 {p90:.0f} ms, max {ms[-1]:.0f} ms")
    for x in [x for x in samples if x["outcome"] in ("WRONG ACTION", "CRASH")][:25]:
        print(f"\n  {x['outcome']}: {x['utterance']!r} [{x['tag']}]")
        print(f"     wanted {x['want']} / {x['expect']}")
        print(f"     got    {x.get('verdict')} {x.get('action')} {x.get('error', '')}")
        print(f"     why    {x.get('reason')}")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true", help="no Jev: grammar and lexicon only")
    parser.add_argument("--reps", type=int, default=1)
    parser.add_argument("--only", default="", help="run only cases whose tag contains this")
    parser.add_argument("--out", default=str(Path(__file__).parent / "results" / "latest.json"))
    args = parser.parse_args()

    from hyprsay.lexicon import Lexicon
    from hyprsay.nlu.grammar import Grammar
    from hyprsay.nlu.understand import Understander

    cfg = config.Config()
    client = None
    if not args.offline:
        from hyprsay import jev

        client = jev.JevClient(jev.load_key(), deadline=cfg.jev.deadline_s,
                               token_cap=cfg.jev.token_cap,
                               zero_data_retention=cfg.jev.zero_data_retention)  # fmt: skip
        await client.warm()
    understander = Understander(cfg, Lexicon(cases.APPS, {}), Grammar(), client)

    todo = [(u, cases.WINDOWS, i, t, w, e, tag) for u, i, t, w, e, tag in cases.ALL]
    todo += [
        (u, ws, i, t, w, e, "adversarial:" + tag) for u, ws, i, t, w, e, tag in cases.ADVERSARIAL
    ]
    todo = [c for c in todo if args.only in c[6]]

    samples = []
    for utterance, windows, intent, target, workspace, expect, tag in todo:
        slots = cases.SLOTS.get(utterance)
        state = cases.desktop(windows)
        for rep in range(args.reps):
            started = time.perf_counter()
            try:
                d = await understander.understand(
                    Transcript(utterance, backend="eval"), state,
                    pinned_address=state.active_address)  # fmt: skip
                outcome = score(d, windows, intent, target, workspace, expect, slots)
                got = {"verdict": d.verdict.value, "tier": d.tier, "reason": d.reason,
                       "action": d.action.describe() if d.action else None,
                    # which window, by fixture key: "firefox" alone cannot tell a real
                    # window from a hostile one of the same class
                    "window": next(
                        (k for k, w in windows.items()
                         if d.action and d.action.window and w.address == d.action.window.address),
                        None,
                    ),  # fmt: skip
                       "candidates": [c.label for c in d.candidates]}  # fmt: skip
            except Exception as exc:  # the pipeline promises never to raise: count it if it does
                outcome, got = "CRASH", {"error": f"{type(exc).__name__}: {exc}"}
            elapsed = round((time.perf_counter() - started) * 1000, 1)
            want = {"intent": intent, "target": target, "workspace": workspace, **(slots or {})}
            samples.append(
                {
                    "utterance": utterance,
                    "tag": tag,
                    "expect": expect,
                    "rep": rep,
                    "want": want,
                    "outcome": outcome,
                    "ms": elapsed,
                    **got,
                }  # fmt: skip
            )
    if client is not None:
        await client.aclose()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"offline": args.offline, "reps": args.reps, "samples": samples}, indent=1)
    )

    report(samples, len(todo), args)
    print(f"\nsaved {out}")
    return 1 if any(x["outcome"] in ("WRONG ACTION", "CRASH") for x in samples) else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
