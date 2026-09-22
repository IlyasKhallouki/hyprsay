"""Phase 0: the measurements that decide how much of Phase 3 gets built.

docs/STRATEGY.md section 0 and risk R1 say one unmeasured number decides a third of the
plan: how long the push-to-talk key stays down after the last word. Everything else on
the post-release path is already measured on this machine and none of it helps without
that one. Capture flush is about 40 ms, a Parakeet TDT 110M decode of a 0.85 to 1.56 s
clip is 93 ms p50, and the Jev fan-out is 315 ms p50 at 725 to 2,246 input tokens, so
about 450 ms of work sits after key up today. Speculation can only hide the part of that
which fits inside the hold, and nobody knows how long the hold outlasts the speech.

Three modes, each writing JSON under evals/results/ so a later run can recompute:

    uv run python evals/measure.py release --n 12
        Hold a dozen utterances from the terminal. Reports the distance from the last
        voiced frame to the release, and the verdict R1 asks for.

    uv run python evals/measure.py corpus --count 40
        Walk a prompt list, record one clip each, write a manifest. Half the default
        prompts are French, because R5 is that no number anywhere, local or vendor, has
        been measured on French, and Parakeet 110M is English only.

    uv run python evals/measure.py ladder --manifest evals/results/corpus/manifest.json
        Decode growing prefixes of each clip with the real recognizer and report, per
        rung, whether the decode is a true prefix of what was said and whether the
        DECISION taken on it matches the decision taken on the whole clip. That last
        column is the one that matters: R2 is that prefixes are confidently wrong at
        exactly the horizon a speculative act fires on, and at the 1.4 s rung Parakeet
        decoded "wish to say" where the truth was "wish to see", on both runs.

Release lag is measured against the terminal's Enter key rather than the compositor
bind, on purpose. A bind would fold hyprctl and socket2 latency into a number that is
about the speaker, and STRATEGY Phase 0 asks for something runnable in two minutes.

Two things bias that lag DOWNWARD, and both are worth knowing before the verdict is
read, because a short lag is the reading that cuts Phase 3. The prompt is on the screen,
so the speaker knows exactly where the sentence ends and lets go sooner than they would
mid-thought. And the buffer ends where PortAudio last delivered a block rather than at
the release itself, which is why the capture flush is added back and reported on its own
line. If the number comes out just under 150 ms, it is under 150 ms for reasons that
include the harness, and the honest move is to measure again from the real bind.

One thing biased it UPWARD and has been taken out. The hold clock used to start when
`Recorder.start()` returned, which is when the stream has been told to start rather than
when it begins delivering audio, and with pre-roll forced off the device is opened once
per hold, so every hold carried a full PortAudio start-up. That term landed in the
capture flush and therefore in the lag, and an inflated lag argues FOR building the part
of Phase 3 that costs the most. The clock now starts at the first block PortAudio
actually delivered (`first_block`), and both numbers go into the JSON: `lag_ms` is
corrected, `lag_raw_ms` is what the same hold would have reported before, and
`median_startup_ms` is the difference.

Nothing here decides anything on its own. It produces numbers, and section 8 of the
strategy already says what each number means before it exists.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import sys
import time
import unicodedata
import wave
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))

import cases  # noqa: E402

from hyprsay import config  # noqa: E402
from hyprsay.audio import FRAME_MS, RATE  # noqa: E402
from hyprsay.model import Decision, Transcript, Verdict  # noqa: E402

RESULTS = Path(__file__).resolve().parent / "results"

# a verdict that acted. Two agreeing prefix rungs only matter when they would have fired
# something, so the rungs that agree on "say nothing" are not counted as a hit.
ACTING = frozenset({Verdict.ACT, Verdict.ACT_SWAP, Verdict.COUNTDOWN, Verdict.CONFIRM_KEY})

# STRATEGY 8 R1: above the first, speculation hides most of the post-release cost and
# Phase 3 is worth building in full; under the second, cut Phase 3 to decode-only.
LAG_FULL_MS = 300.0
LAG_CUT_MS = 150.0
# STRATEGY 3.2 S3 and R2: two consecutive rungs must agree this often before anything
# may act before the key is released.
TWO_RUNG_TARGET = 0.90

# audio.py's own docstring: "the key press itself clicks for a frame or two". In this
# harness the release IS a key press, into the same microphone, so a trailing burst this
# short with real silence in front of it is the Enter key and not the last word. Without
# the discount one click lands a 0 ms lag and argues Phase 3 away on an artifact.
CLICK_FRAMES = 2
CLICK_GAP_FRAMES = 5  # 100 ms between the last word and the click


# ------------------------------------------------------------------- release lag: analysis


def last_voiced_frame(
    voiced: Sequence[bool],
    *,
    click_frames: int = CLICK_FRAMES,
    gap_frames: int = CLICK_GAP_FRAMES,
) -> int | None:
    """Index of the last frame of speech, with a trailing key click discounted.

    `click_frames=0` disables the discount and gives the strict last voiced frame.
    A short burst counts as speech, not a click, when nothing precedes it or when it sits
    within `gap_frames` of earlier speech: the end of a word is allowed to be brief.
    """
    end = len(voiced)
    while True:
        last = _last_true(voiced, end)
        if last is None:
            return None
        start = last
        while start > 0 and voiced[start - 1]:
            start -= 1
        if last - start + 1 > click_frames:
            return last
        before = _last_true(voiced, start)
        if before is None or start - before - 1 < gap_frames:
            return last
        end = start


def _last_true(voiced: Sequence[bool], end: int) -> int | None:
    for i in range(min(end, len(voiced)) - 1, -1, -1):
        if voiced[i]:
            return i
    return None


@dataclass(frozen=True)
class Hold:
    """One utterance held from the terminal. Times in milliseconds."""

    index: int
    prompt: str
    held_ms: float  # Enter to Enter, from the moment `start()` returned
    audio_ms: float  # what the recorder had buffered when the release was seen
    flush_ms: float  # held minus start-up minus buffered: audio not delivered yet
    trailing_ms: float  # end of the last voiced frame to the end of the buffer
    strict_ms: float  # the same with no key-click discount, for comparison
    voiced_frames: int
    clicked: bool  # a trailing burst was discounted as the Enter key
    # the whole point: last word to release. None when nothing was said.
    lag_ms: float | None
    # the PortAudio stream start-up deficit that was removed, and the number it was
    # removed from, kept so the correction can be audited rather than believed
    startup_ms: float = 0.0
    lag_raw_ms: float | None = None


def measure_hold(
    index: int,
    prompt: str,
    voiced: Sequence[bool],
    audio_ms: float,
    held_ms: float,
    *,
    startup_ms: float = 0.0,
    click_frames: int = CLICK_FRAMES,
    gap_frames: int = CLICK_GAP_FRAMES,
) -> Hold:
    """Turn one hold's voiced flags and two clocks into the lag STRATEGY R1 asks for.

    The lag is trailing silence inside the buffer plus the capture flush, because the
    buffer ends where PortAudio last delivered a block and the release happened after
    that. Both terms are reported so a suspicious total can be blamed on the right one.

    THE TERM THAT WAS REMOVED, AND WHY. `held_ms` starts when `Recorder.start()` returns,
    which is when the stream has been TOLD to start, not when it begins handing over
    audio. With pre-roll forced off the device is opened and closed once per hold, so
    every hold paid that start-up in full, and it landed in `flush_ms`, since flush was
    computed as "held minus buffered" and the undelivered tail is not the only thing that
    difference contains. It therefore landed in `lag_ms` too. That number is the one
    measurement Phase 0 exists to produce and the only input to whether Phase 3 gets
    built, and inflating it argues FOR building speculation, so the error pointed exactly
    the wrong way: it would have bought a week of work with a PortAudio artifact.
    `startup_ms` is that deficit, measured from the first block PortAudio actually
    delivered, and it is subtracted here. `lag_raw_ms` keeps the uncorrected number, and
    with `startup_ms=0.0` the two are identical, which is what every offline caller and
    every recomputation from an old JSON file gets.
    """
    strict = last_voiced_frame(voiced, click_frames=0)
    last = last_voiced_frame(voiced, click_frames=click_frames, gap_frames=gap_frames)
    flush_ms = held_ms - startup_ms - audio_ms
    if last is None:
        return Hold(
            index, prompt, held_ms, audio_ms, flush_ms, 0.0, 0.0, 0, False, None, startup_ms, None
        )
    trailing_ms = audio_ms - (last + 1) * FRAME_MS
    strict_ms = audio_ms - ((strict or 0) + 1) * FRAME_MS
    return Hold(
        index=index,
        prompt=prompt,
        held_ms=round(held_ms, 1),
        audio_ms=round(audio_ms, 1),
        flush_ms=round(flush_ms, 1),
        trailing_ms=round(trailing_ms, 1),
        strict_ms=round(strict_ms, 1),
        voiced_frames=sum(1 for v in voiced if v),
        clicked=strict != last,
        lag_ms=round(max(0.0, trailing_ms + flush_ms), 1),
        startup_ms=round(startup_ms, 1),
        lag_raw_ms=round(max(0.0, trailing_ms + flush_ms + startup_ms), 1),
    )


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest rank, the same rule evals/run.py uses for its p90. Small n, no smoothing."""
    ordered = sorted(values)
    if not ordered:
        raise ValueError("no values")
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def summarize_holds(holds: Sequence[Hold]) -> dict[str, Any]:
    """Both numbers, always. `median_ms` is the corrected one the verdict reads;
    `median_raw_ms` is what the same holds would have said with the PortAudio stream
    start-up still inside them, so the size of the correction is in the file rather
    than in somebody's memory."""
    said = [h for h in holds if h.lag_ms is not None]
    lags = [h.lag_ms for h in said if h.lag_ms is not None]
    if not lags:
        return {"n": 0, "silent": len(holds)}
    raw = [h.lag_raw_ms if h.lag_raw_ms is not None else h.lag_ms for h in said]
    return {
        "n": len(lags),
        "silent": len(holds) - len(lags),
        "median_ms": round(statistics.median(lags), 1),
        "p10_ms": round(percentile(lags, 0.10), 1),
        "p90_ms": round(percentile(lags, 0.90), 1),
        "median_trailing_ms": round(statistics.median([h.trailing_ms for h in said]), 1),
        "median_flush_ms": round(statistics.median([h.flush_ms for h in said]), 1),
        "median_startup_ms": round(statistics.median([h.startup_ms for h in said]), 1),
        "median_raw_ms": round(statistics.median([v for v in raw if v is not None]), 1),
        "clicked": sum(1 for h in holds if h.clicked),
    }


def lag_verdict(summary: dict[str, Any]) -> tuple[str, str]:
    """(key, the sentence to print). The thresholds are STRATEGY 8 R1, not new policy."""
    n = summary.get("n", 0)
    if not n:
        return "no-data", "nothing was said in any hold, so there is no lag to report."
    median = summary["median_ms"]
    thin = "" if n >= 8 else f" Only {n} holds: run it again with more before deciding."
    if median > LAG_FULL_MS:
        return "full", (
            f"median {median:.0f} ms is above {LAG_FULL_MS:.0f} ms, so speculation hides most "
            f"of the post-release cost. Phase 3 is worth building in full: S1, then S2, then "
            f"S3 behind the ladder numbers.{thin}"
        )
    if median < LAG_CUT_MS:
        return "decode-only", (
            f"median {median:.0f} ms is under {LAG_CUT_MS:.0f} ms, so the Jev round trip does "
            f"not fit inside the hold. STRATEGY 8 R1 says to cut Phase 3 to decode-only (S1 "
            f"alone) and to say so publicly.{thin}"
        )
    return "inconclusive", (
        f"median {median:.0f} ms sits between {LAG_CUT_MS:.0f} and {LAG_FULL_MS:.0f} ms. Ship "
        f"S1, which is free either way, and measure again before writing a line of S2.{thin}"
    )


# ---------------------------------------------------------------- prefix ladder: analysis

_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def words(text: str) -> tuple[str, ...]:
    """Runs of letters and digits, casefolded, with diacritics folded away.

    Voice-Light's invalidation rule, which S2 copies verbatim: a revision limited to
    case, punctuation, whitespace or apostrophes is not a lexical change. So "l'ecran"
    and "l ecran" are the same two words here, and only a different word counts.

    Diacritics join that list for one measured reason. Parakeet 110M is English only, so
    it can never emit an e with a circumflex, and a French prompt written the way it is
    read would score every rung wrong for a reason that has nothing to do with whether
    the prefix drifted onto a different word. R5 asks whether French decodes usably; the
    orthography of the answer is a separate question and the raw text is kept in the JSON.
    """
    folded = unicodedata.normalize("NFD", text.casefold())
    return tuple(_WORD.findall("".join(c for c in folded if not unicodedata.combining(c))))


def is_prefix(decoded: str, truth: str) -> bool:
    """Is the decode a word-for-word prefix of what was actually said?

    An empty decode passes trivially, which is why every summary counts the non-empty
    rungs apart: STRATEGY 3.2 found 3 of 30 apparent successes were empty rungs.
    """
    got, want = words(decoded), words(truth)
    return len(got) <= len(want) and want[: len(got)] == got


def rungs(total_s: float, *, start: float = 0.6, step: float = 0.2) -> tuple[float, ...]:
    """Prefix durations from `start` in steps of `step`, with the whole clip always last.

    The whole clip is the truth rung: every other rung is scored against the decision it
    produced. In production S2 hard-stops past 2.5 s of buffer (at 7.43 s of audio a
    naive 200 ms ladder cost 2.41 times real time), but a measurement wants every rung.
    """
    out: list[float] = []
    t = start
    while t < total_s - 1e-9:
        out.append(round(t, 3))
        t += step
    out.append(round(total_s, 3))
    return tuple(out)


def prefix_pcm(pcm: bytes, seconds: float, rate: int = RATE) -> bytes:
    """The first `seconds` of a 16 bit mono clip, cut on a sample boundary."""
    return pcm[: max(0, int(seconds * rate)) * 2]


def decision_key(decision: Decision) -> tuple[str, ...]:
    """What two rungs are compared on: the act, never the transcript (STRATEGY 3.2 S2).

    Two spellings of one command are the same decision, so a reworded prefix that means
    the same thing must not cancel speculated work. Dictated text is deliberately absent:
    it never leaves the machine and it is not what a speculative act would fire on.
    """
    action = decision.action
    if action is None:
        return (decision.verdict.value, "", "", "", str(decision.tier))
    target = action.window.address if action.window else action.app.id if action.app else ""
    slots = "|".join(
        str(x) for x in (action.workspace, action.direction, action.verb, action.amount)
    )
    return (decision.verdict.value, action.intent.value, target, slots, str(decision.tier))


Decider = Callable[[Transcript], Awaitable[Decision]]


async def run_ladder(
    pcm: bytes,
    truth: str,
    *,
    recognizer: Any,
    decide: Decider,
    start: float = 0.6,
    step: float = 0.2,
    rate: int = RATE,
) -> list[dict[str, Any]]:
    """Decode every rung of one clip and score it against the decision on the whole clip."""
    rows: list[dict[str, Any]] = []
    for seconds in rungs(len(pcm) / 2 / rate, start=start, step=step):
        started = time.perf_counter()
        transcript = await recognizer.transcribe(prefix_pcm(pcm, seconds, rate), rate)
        decode_ms = (time.perf_counter() - started) * 1000
        decision = await decide(transcript)
        rows.append(
            {
                "seconds": seconds,
                "text": transcript.text,
                "decode_ms": round(decode_ms, 1),
                "empty": not words(transcript.text),
                "prefix_ok": is_prefix(transcript.text, truth),
                # the whole sentence, not just a prefix of it: true early means the
                # speaker had finished the command before the clip ended
                "exact": words(transcript.text) == words(truth),
                "verdict": decision.verdict.value,
                "acts": decision.verdict in ACTING,
                "key": list(decision_key(decision)),
            }
        )
    final = rows[-1]["key"]
    for row in rows:
        row["matches_final"] = row["key"] == final
    return rows


def summarize_ladder(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """One clip's rungs, reduced to the four numbers Phase 0 was run for.

    Everything scored here excludes the whole-clip rung, because that rung is the answer
    rather than a guess at it: it always matches itself, and counting it would pad every
    rate in this file by one. `early_match` is therefore out of `early_rungs`, and the
    per-rung `matches_final` flags stay in the JSON for whoever wants the full table.

    `settles_at` is the earliest rung from which no later rung changes the decision, as a
    fraction of the clip. `two_rung_*` is the S3 firing rule scored honestly: a pair of
    agreeing consecutive rungs, both strictly before the whole clip, whose shared
    decision would have acted.
    """
    if not rows:
        return {"rungs": 0}
    final = rows[-1]["key"]
    non_empty = [r for r in rows[:-1] if not r["empty"]]
    settles_at: float | None = None
    for i, row in enumerate(rows):
        if all(later["key"] == final for later in rows[i:]):
            settles_at = row["seconds"] / rows[-1]["seconds"] if rows[-1]["seconds"] else 1.0
            break
    fires = correct = 0
    for a, b in zip(rows[:-2], rows[1:-1], strict=False):
        if a["key"] == b["key"] and a["acts"]:
            fires += 1
            correct += a["key"] == final
    return {
        "rungs": len(rows),
        "non_empty": len(non_empty),
        "prefix_ok": sum(1 for r in non_empty if r["prefix_ok"]),
        "early_rungs": len(rows) - 1,
        "early_match": sum(1 for r in rows[:-1] if r["matches_final"]),
        "settles_at": None if settles_at is None else round(settles_at, 3),
        "two_rung_fires": fires,
        "two_rung_correct": correct,
        # the whole clip against what was actually said: R5's question, not R2's
        "final_correct": bool(rows[-1].get("exact")),
        "final_verdict": rows[-1]["verdict"],
    }


def aggregate_ladder(clips: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Roll the per-clip summaries up, overall and per language."""

    def roll(chosen: Sequence[dict[str, Any]]) -> dict[str, Any]:
        s = [c["summary"] for c in chosen]
        settled = [x["settles_at"] for x in s if x.get("settles_at") is not None]
        fires = sum(x["two_rung_fires"] for x in s)
        return {
            "clips": len(s),
            "rungs": sum(x["rungs"] for x in s),
            "non_empty": sum(x["non_empty"] for x in s),
            "prefix_ok": sum(x["prefix_ok"] for x in s),
            "early_rungs": sum(x["early_rungs"] for x in s),
            "early_match": sum(x["early_match"] for x in s),
            "final_correct": sum(1 for x in s if x["final_correct"]),
            "median_settles_at": round(statistics.median(settled), 3) if settled else None,
            "two_rung_fires": fires,
            "two_rung_correct": sum(x["two_rung_correct"] for x in s),
            "two_rung_precision": (
                round(sum(x["two_rung_correct"] for x in s) / fires, 3) if fires else None
            ),
        }

    langs = sorted({c.get("lang", "") for c in clips if c.get("lang")})
    return {
        "overall": roll(clips),
        "by_language": {lang: roll([c for c in clips if c.get("lang") == lang]) for lang in langs},
    }


def ladder_verdict(overall: dict[str, Any]) -> tuple[str, str]:
    """(key, the sentence to print). The 90% bar is STRATEGY 8 R2, not new policy."""
    precision = overall.get("two_rung_precision")
    fires = overall.get("two_rung_fires", 0)
    if precision is None:
        return "no-data", (
            "no two consecutive rungs ever agreed on an action, so S3 has nothing to fire on "
            "and speculation can only pre-warm."
        )
    thin = "" if fires >= 30 else f" Only {fires} firings: this is a reading, not a rate."
    if precision >= TWO_RUNG_TARGET:
        return "act-early-allowed", (
            f"two agreeing rungs were right {100 * precision:.0f}% of the time, at or above the "
            f"{100 * TWO_RUNG_TARGET:.0f}% R2 asks for. S3 may act early, still tier 0 only and "
            f"still with an inverse recorded first.{thin}"
        )
    return "pre-warm-only", (
        f"two agreeing rungs were right {100 * precision:.0f}% of the time, below the "
        f"{100 * TWO_RUNG_TARGET:.0f}% R2 asks for. Never act early: use speculation only to "
        f"pre-warm the decode and the connection.{thin}"
    )


# ------------------------------------------------------------------------ prompts and clips


# Drawn from evals/cases.py so the corpus exercises the vocabulary the pipeline is
# already scored on, rather than a second list that could drift away from it.
def english_prompts() -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for utterance, *_ in cases.CANONICAL + cases.PARAPHRASE:
        seen.setdefault(utterance, None)
    return tuple(seen)


# STRATEGY 8 R5: every local number is English and Parakeet 110M is English only, so
# half the corpus is French to start finding out what that costs. These mirror the
# English list rather than translating it word for word, because a French speaker asks
# for a workspace differently than an English one does.
FRENCH_PROMPTS: tuple[str, ...] = (
    "va au bureau trois",
    "passe à l'espace de travail deux",
    "ouvre firefox",
    "ferme cette fenêtre",
    "montre moi mes notes",
    "mets spotify sur le bureau cinq",
    "déplace cette fenêtre vers le bureau quatre",
    "plein écran",
    "lance un terminal",
    "monte le son",
    "baisse le volume",
    "coupe le son",
    "mets le volume à quarante pour cent",
    "chanson suivante",
    "morceau précédent",
    "mets en pause",
    "va sur le navigateur",
    "ouvre la calculatrice",
    "agrandis cette fenêtre",
    "déplace cette fenêtre vers la gauche",
    "ferme spotify",
    "bascule en fenêtre flottante",
    "verrouille l'écran",
    "ouvre mes fichiers",
    "annule ça",
    "j'aimerais voir mes fichiers",
    "emmène moi sur le lecteur de musique",
    "donne tout l'écran à cette fenêtre",
    "il y a beaucoup trop de bruit",
    "passe à la fenêtre de droite",
)


def default_prompts(count: int) -> tuple[tuple[str, str], ...]:
    """(language, prompt) pairs, alternating so any prefix of the plan is half French."""
    english, french = english_prompts(), FRENCH_PROMPTS
    plan: list[tuple[str, str]] = []
    for i in range(count):
        pool, lang = (english, "en") if i % 2 == 0 else (french, "fr")
        plan.append((lang, pool[(i // 2) % len(pool)]))
    return tuple(plan)


def read_prompt_file(path: Path) -> tuple[tuple[str, str], ...]:
    """One prompt per line. `fr: ...` or `en: ...` tags the language; `#` is a comment."""
    plan: list[tuple[str, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lang, sep, rest = line.partition(":")
        if sep and lang.strip().lower() in {"en", "fr"}:
            plan.append((lang.strip().lower(), rest.strip()))
        else:
            plan.append(("own", line))
    return tuple(plan)


def read_wav(path: Path) -> bytes:
    """Any PCM WAV as the 16 kHz mono signed 16 bit the recognizers take.

    Deliberately a copy of what `cli._read_wav` does rather than an import of a private
    name: the harness has to keep reading the corpus even while cli.py is being edited.
    """
    with wave.open(str(path), "rb") as handle:
        rate, channels, width = handle.getframerate(), handle.getnchannels(), handle.getsampwidth()
        raw = handle.readframes(handle.getnframes())
    if width != 2:
        raise ValueError(f"{path}: only 16 bit PCM is supported, this file is {8 * width} bit")
    audio = np.frombuffer(raw, dtype="<i2").astype(np.float32)
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    if rate != RATE and len(audio):
        n = int(len(audio) * RATE / rate)
        audio = np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio)
    return np.clip(audio, -32768, 32767).astype("<i2").tobytes()


def write_wav(path: Path, pcm: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(pcm)


def write_manifest(path: Path, clips: Sequence[dict[str, Any]]) -> None:
    """Clip paths are stored relative to the manifest, so the corpus can be moved."""
    write_json(
        path,
        {
            "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "sample_rate": RATE,
            "clips": list(clips),
        },
    )


def load_manifest(path: Path) -> list[dict[str, Any]]:
    """Manifest rows with `path` resolved against the manifest's own directory."""
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data["clips"] if isinstance(data, dict) else data
    out = []
    for row in rows:
        clip = dict(row)
        clip["path"] = str((path.parent / row["path"]).resolve())
        out.append(clip)
    return out


def write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
    return path


# ------------------------------------------------------------------------------ the modes


class Stopped(Exception):
    """The owner pressed Ctrl-C or Ctrl-D. Whatever was measured so far still counts."""


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt) as exc:
        print()
        raise Stopped from exc


def voiced_flags(recorder: Any) -> tuple[bool, ...]:
    """The energy gate's per-frame speech flags for the hold in progress.

    `Recorder` publishes `trailing_silence` and `voiced_after`, and neither can express
    "where was the last voiced frame" once the Enter key clicks onto the final frame.
    Reading the gate is the coupling this harness accepts rather than running a second
    VAD over the same audio and reporting a lag the daemon would never agree with.
    Must be called before `stop()`, which wipes the gate.
    """
    gate = getattr(recorder, "_gate", None)
    return tuple(bool(v) for v in getattr(gate, "voiced", ()) or ())


def open_recorder(cfg: config.Config) -> Any:
    """A Recorder with pre-roll and ducking forced off.

    Pre-roll prepends audio from before the key went down, which breaks the arithmetic
    that turns two clocks into a lag. Ducking would move the owner's output volume during
    a measurement, and a measurement changes nothing.

    The price of pre-roll being off is that the device is opened and closed once per
    hold, so every hold pays a PortAudio stream start-up. `first_block` measures it and
    `measure_hold` subtracts it; keeping the stream open instead would mean keeping
    pre-roll on, which is the thing that breaks the arithmetic in the first place.
    """
    from hyprsay.audio import Recorder

    measured = replace(cfg, stt=replace(cfg.stt, preroll=False, duck_volume=False))
    return Recorder(measured)


# how long to wait for PortAudio to hand over the first block before giving up on
# correcting this hold. Generous: a device that takes longer than this has a problem
# worth seeing in the numbers rather than papering over.
FIRST_BLOCK_TIMEOUT_S = 1.0


def first_block(recorder: Any, now: Callable[[], float] = time.monotonic) -> float | None:
    """Wait until the recorder has any audio at all, and answer with the clock then.

    `Recorder.start()` returns once the stream has been asked to start; PortAudio then
    takes a while to deliver anything, and with pre-roll off that happens once per hold.
    Everything before the first block is device start-up rather than a speaker holding a
    key, so the hold clock starts here and not at `start()`.

    Polled rather than hooked because the recorder publishes no first-block callback and
    the one seam that would reach it, `stream_factory`, is the daemon's own. A 1 ms poll
    against a start-up measured in tens of milliseconds is inside the rounding.

    None means nothing ever arrived, and then no correction is applied at all: a hold
    with no audio has no lag to report either.
    """
    deadline = now() + FIRST_BLOCK_TIMEOUT_S
    while True:
        if recorder.seconds() > 0.0:
            return now()
        if now() >= deadline:
            return None
        time.sleep(0.001)


def startup_deficit(returned: float, delivered: float | None) -> float:
    """How much of `held_ms` was the device starting rather than the speaker holding.

    The first block covers the first `FRAME_MS` of capture and arrives at the end of it,
    so capture began one frame before it was delivered. Never negative: a clock that
    disagrees with itself must not lengthen the lag.
    """
    if delivered is None:
        return 0.0
    return max(0.0, (delivered - returned) * 1000 - FRAME_MS)


def mode_release(cfg: config.Config, args: argparse.Namespace) -> int:
    from hyprsay.audio import AudioError

    recorder = open_recorder(cfg)
    plan = default_prompts(args.n)
    holds: list[Hold] = []
    print(
        f"release lag, {plural(args.n, 'hold')}. Press Enter, say the line, then press Enter at\n"
        f"the exact moment you would let go of the push-to-talk key. Say it the way you would\n"
        f"say it to the machine, not the way you would read it aloud. Type q or press Ctrl-C to\n"
        f"stop; whatever is measured by then still counts.\n"
    )
    try:
        for index, (lang, prompt) in enumerate(plan, 1):
            if ask(f"[{index}/{args.n}] {lang}  {prompt!r}\n    Enter to start ").lower() == "q":
                break
            try:
                recorder.start()
            except AudioError as exc:
                print(f"measure: {exc}", file=sys.stderr)
                return 1
            started = time.monotonic()
            # the prompt waits for the microphone to be live, so the owner is never
            # told to speak into a device that is not delivering anything yet
            startup_ms = startup_deficit(started, first_block(recorder))
            try:
                ask("    speak, then Enter as you would release ")
                released = time.monotonic()
                audio_ms = recorder.seconds() * 1000
                flags = voiced_flags(recorder)
            finally:
                recorder.stop()
            hold = measure_hold(
                index,
                prompt,
                flags,
                audio_ms,
                (released - started) * 1000,
                startup_ms=startup_ms,
            )
            holds.append(hold)
            if hold.lag_ms is None:
                print("    nothing was heard in that hold; it will not count\n")
            else:
                clicked = "  (trailing click discounted)" if hold.clicked else ""
                print(
                    f"    lag {hold.lag_ms:.0f} ms   audio {hold.audio_ms / 1000:.2f} s{clicked}\n"
                )
    except Stopped:
        print("stopped early.\n")

    summary = summarize_holds(holds)
    key, sentence = lag_verdict(summary)
    out = write_json(
        Path(args.out or RESULTS / "release_lag.json"),
        {
            "mode": "release",
            "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "frame_ms": FRAME_MS,
            "click_frames": CLICK_FRAMES,
            # what was taken out of every hold, named in the file so a reader does not
            # have to read this source to know the numbers were corrected
            "corrected_for": "portaudio stream start-up, per hold, with preroll off",
            "summary": summary,
            "verdict": key,
            "holds": [asdict(h) for h in holds],
        },
    )
    print(f"release lag: {plural(summary.get('n', 0), 'usable hold')} of {len(holds)}")
    if summary.get("n"):
        print(
            f"  median {summary['median_ms']:.0f} ms"
            f"   p10 {summary['p10_ms']:.0f} ms   p90 {summary['p90_ms']:.0f} ms"
        )
        print(
            f"  of which capture flush: median {summary['median_flush_ms']:.0f} ms"
            f"   (trailing silence in the buffer: {summary['median_trailing_ms']:.0f} ms)"
        )
        print(
            f"  device start-up removed: median {summary['median_startup_ms']:.0f} ms"
            f"   (uncorrected median would read {summary['median_raw_ms']:.0f} ms)"
        )
        if summary["clicked"]:
            print(f"  a trailing key click was discounted on {summary['clicked']} of {len(holds)}")
    print(f"verdict: {sentence}")
    print(f"saved {out}")
    return 0


def mode_corpus(cfg: config.Config, args: argparse.Namespace) -> int:
    from hyprsay.audio import AudioError

    plan = read_prompt_file(Path(args.prompts)) if args.prompts else default_prompts(args.count)
    manifest = Path(args.out or RESULTS / "corpus" / "manifest.json")
    recorder = open_recorder(cfg)
    clips: list[dict[str, Any]] = []
    print(
        f"corpus, {len(plan)} prompts. Enter records, Enter stops. Type s to skip a prompt,\n"
        f"q to finish early. Everything is written after every clip.\n"
    )
    try:
        for index, (lang, prompt) in enumerate(plan, 1):
            answer = ask(f"[{index}/{len(plan)}] {lang}  {prompt!r}\n    Enter to record ").lower()
            if answer == "q":
                break
            if answer == "s":
                continue
            try:
                recorder.start()
            except AudioError as exc:
                print(f"measure: {exc}", file=sys.stderr)
                return 1
            try:
                ask("    recording, Enter when done ")
            finally:
                pcm = recorder.stop()
            name = f"{index:03d}-{lang}.wav"
            write_wav(manifest.parent / name, pcm)
            clips.append(
                {
                    "path": name,
                    "prompt": prompt,
                    "seconds": round(len(pcm) / 2 / RATE, 3),
                    "lang": lang,
                    "index": index,
                }
            )
            write_manifest(manifest, clips)
            print(f"    {name}   {clips[-1]['seconds']:.2f} s\n")
    except Stopped:
        print("stopped early.\n")
    write_manifest(manifest, clips)

    seconds = sum(c["seconds"] for c in clips)
    by_lang = {
        lang: sum(1 for c in clips if c["lang"] == lang) for lang in {c["lang"] for c in clips}
    }
    spread = ", ".join(f"{n} {lang}" for lang, n in sorted(by_lang.items()))
    print(f"corpus: {plural(len(clips), 'clip')}, {seconds:.1f} s of audio ({spread or 'nothing'})")
    print(f"saved {manifest}")
    print(f"next: uv run python evals/measure.py ladder --manifest {manifest}")
    return 0


async def build_decider(cfg: config.Config, *, live: bool) -> tuple[Decider, Any]:
    """The real understanding pipeline over the eval fixture desktop.

    The fixture rather than the live desktop on purpose: a rung is scored against the
    decision the WHOLE clip produced, and that comparison is only meaningful if the
    desktop did not move between the first rung and the last.
    """
    from hyprsay.lexicon import Lexicon
    from hyprsay.nlu.grammar import Grammar
    from hyprsay.nlu.understand import Understander

    client = None
    if live:
        from hyprsay import jev

        client = jev.JevClient(
            jev.load_key(),
            route=cfg.jev.route,
            model=cfg.jev.model,
            deadline=cfg.jev.deadline_s,
            token_cap=cfg.jev.token_cap,
            zero_data_retention=cfg.jev.zero_data_retention,
        )
        await client.warm()
    understander = Understander(cfg, Lexicon(cases.APPS, {}), Grammar(), client)
    state = cases.desktop()

    async def decide(transcript: Transcript) -> Decision:
        return await understander.understand(transcript, state, pinned_address=state.active_address)

    return decide, client


async def _ladder(cfg: config.Config, args: argparse.Namespace) -> int:
    from hyprsay.stt.local import LocalRecognizer

    if args.manifest:
        clips = load_manifest(Path(args.manifest))
    elif args.wav:
        clips = [{"path": str(Path(args.wav).resolve()), "prompt": args.truth, "lang": "own"}]
    else:
        print("measure: ladder needs --manifest or --wav with --truth", file=sys.stderr)
        return 2
    missing = [c for c in clips if not c.get("prompt")]
    if missing:
        print("measure: every clip needs its true transcript", file=sys.stderr)
        return 2

    from hyprsay.jev import JevError
    from hyprsay.stt.models import SttError

    # a missing model or a missing key is an ordinary setup problem, not a crash: this
    # runs on a laptop in the two minutes before a measurement, not in CI
    try:
        recognizer = LocalRecognizer(args.model or cfg.stt.local_model)
        decide, client = await build_decider(cfg, live=args.jev)
        await recognizer.transcribe(b"\0\0" * 1600, RATE)  # load the model off the clock
    except (SttError, JevError, OSError) as exc:
        print(f"measure: {exc}", file=sys.stderr)
        return 1

    done: list[dict[str, Any]] = []
    try:
        for n, clip in enumerate(clips, 1):
            pcm = read_wav(Path(clip["path"]))
            rows = await run_ladder(
                pcm,
                clip["prompt"],
                recognizer=recognizer,
                decide=decide,
                start=args.start,
                step=args.step,
            )
            done.append(
                {
                    "path": clip["path"],
                    "prompt": clip["prompt"],
                    "lang": clip.get("lang", ""),
                    "seconds": round(len(pcm) / 2 / RATE, 3),
                    "rungs": rows,
                    "summary": summarize_ladder(rows),
                }
            )
            print(f"  [{n}/{len(clips)}] {clip['prompt']!r}: {done[-1]['summary']['rungs']} rungs")
    finally:
        if client is not None:
            await client.aclose()

    totals = aggregate_ladder(done)
    overall = totals["overall"]
    key, sentence = ladder_verdict(overall)
    out = write_json(
        Path(args.out or RESULTS / "prefix_ladder.json"),
        {
            "mode": "ladder",
            "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "recognizer": recognizer.name,
            "decider": "jev" if args.jev else "grammar-only",
            "start_s": args.start,
            "step_s": args.step,
            "totals": totals,
            "verdict": key,
            "clips": done,
        },
    )
    path = "live Jev" if args.jev else "grammar only"
    print(
        f"\nprefix ladder: {plural(overall['clips'], 'clip')}, {plural(overall['rungs'], 'rung')}, "
        f"{recognizer.name}, {path}"
    )
    # every rate below leaves out the whole-clip rung, which is the answer and not a guess
    print(
        f"  an early rung was a true prefix of what was said: "
        f"{_ratio(overall['prefix_ok'], overall['non_empty'])} of the non-empty ones"
    )
    print(
        f"  an early rung decided what the whole clip decided: "
        f"{_ratio(overall['early_match'], overall['early_rungs'])}"
    )
    if overall["median_settles_at"] is not None:
        print(f"  the decision settles at {100 * overall['median_settles_at']:.0f}% of the clip")
    print(
        f"  two agreeing rungs would have fired {plural(overall['two_rung_fires'], 'time')}, "
        f"right {overall['two_rung_correct']}"
    )
    whole = _ratio(overall["final_correct"], overall["clips"])
    print(f"  the whole clip transcribed word for word: {whole}")
    for lang, roll in totals["by_language"].items():
        print(
            f"    {lang}: prefix {_ratio(roll['prefix_ok'], roll['non_empty'])}, "
            f"two-rung {_ratio(roll['two_rung_correct'], roll['two_rung_fires'])}, "
            f"whole clip {_ratio(roll['final_correct'], roll['clips'])}"
        )
    print(f"verdict: {sentence}")
    print(f"saved {out}")
    return 0


def _ratio(part: int, whole: int) -> str:
    return f"{part}/{whole} ({100 * part / whole:.0f}%)" if whole else f"{part}/0"


def plural(n: int, one: str) -> str:
    return f"{n} {one}" if n == 1 else f"{n} {one}s"


def mode_ladder(cfg: config.Config, args: argparse.Namespace) -> int:
    return asyncio.run(_ladder(cfg, args))


# ------------------------------------------------------------------------------ entry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="measure.py", description="Phase 0 measurements (docs/STRATEGY.md section 6)."
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    release = sub.add_parser("release", help="how long the key stays down after the last word")
    release.add_argument("--n", type=int, default=12, help="how many holds to measure")
    release.add_argument("--out", default="")

    corpus = sub.add_parser("corpus", help="record a clip per prompt and write a manifest")
    corpus.add_argument("--count", type=int, default=40, help="how many default prompts")
    corpus.add_argument("--prompts", default="", help="a file of prompts, one per line")
    corpus.add_argument("--out", default="", help="the manifest path; clips land beside it")

    ladder = sub.add_parser("ladder", help="decode growing prefixes and compare the decisions")
    ladder.add_argument("--manifest", default="", help="a corpus manifest to run over")
    ladder.add_argument("--wav", default="", help="one clip instead of a manifest")
    ladder.add_argument("--truth", default="", help="what was said in --wav")
    ladder.add_argument("--start", type=float, default=0.6, help="first rung, seconds")
    ladder.add_argument("--step", type=float, default=0.2, help="rung spacing, seconds")
    ladder.add_argument("--model", default="", help="local model name; default is the config's")
    ladder.add_argument("--jev", action="store_true", help="decide with live Jev, not grammar only")
    ladder.add_argument("--out", default="")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = config.load()
    except config.ConfigError as exc:
        print(f"measure: config error: {exc}", file=sys.stderr)
        return 2
    handler = {"release": mode_release, "corpus": mode_corpus, "ladder": mode_ladder}[args.mode]
    return handler(cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
