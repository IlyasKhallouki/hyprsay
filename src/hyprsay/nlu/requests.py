"""Builds the Jev fan-out: several small concurrent requests, each under the token cap.

Why several, and why small (docs/PLAN.md sections 1 and 5.5, all measured):

- Question count is free (1, 5, 15 and 40 questions cost the same 320 ms), so R1 asks
  everything about the utterance at once.
- Payload is not free. Latency is flat to about 2.2k input tokens and then climbs, and
  the HTTP 503 rate climbs with it (13 of 30 at 11.5k). The client refuses oversized
  requests, so this module never builds one: it shrinks instead. A forced choice cannot
  be split without changing its meaning, so R2 drops the least recently used windows.
  Booleans and app shards are independent, so R2b and R3 are split across requests,
  which costs about 45 ms for three on one warm HTTP/2 connection.
- A forced choice always crowns a winner, even when the right window is not open. R2b
  asks the same decision as one absolute Boolean per window; agreement between the two
  formulations is the confidence signal, bought in the same round trip.
- Rp, the ordered plan, is the one request that must NOT be split: see `plan_request`.

What leaves the machine (PLAN section 7): the utterance, `{app, kind, workspace,
recency}` per window, and, in Rp, the label and kind of each affordance code harvested.
Titles are written by whoever owns the window, including a hostile web page, so they
travel only in R2t, only for the same-app windows being told apart, truncated and
redacted, and the caller decides whether R2t exists at all. An affordance label is
untrusted for the same reason and is cut to 60 characters by `adapters.Affordance`
itself; its handle never leaves, because `as_option` does not carry one. Dictated text
never reaches this module: typing is grammar only and the caller never builds a fan-out
for it.

Every request built here carries what `jev.tokens.Drift` knew when it was sized, so the
first real request of a session reports whether the estimator is running low, which is
the dangerous direction (`headroom`).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from hyprsay.adapters.base import Affordance
from hyprsay.config import Privacy
from hyprsay.jev import tokens
from hyprsay.jev.types import Boolean, Choice, Question, Score
from hyprsay.lexicon import sanitize
from hyprsay.model import App, Window
from hyprsay.nlu import bank, plan
from hyprsay.nlu.tiers import is_terminal

# R2b is capped by the plan; R2 uses the same set so the two formulations are comparable
MAX_WINDOWS = 20
# concurrency is cheap, not free: an absurd app list is truncated rather than fanned out
MAX_APP_SHARDS = 6
MAX_VARIANTS = 3
# a push-to-talk command is a sentence. Anything longer is dictation, and would also
# crowd the questions out of the token budget.
MAX_UTTERANCE_CHARS = 400
MAX_NAME_CHARS = 48
REDACTED = "[hidden]"


def scrub(text: str, limit: int = 0) -> str:
    """`lexicon.sanitize` (NFKC, no control or format code points), then an optional cut."""
    text = sanitize(text or "")
    return text[:limit].rstrip() if limit else text


@dataclass(frozen=True)
class Request:
    name: str  # "r1", "r2", "r2b", "r2t", "r3", "rp", with ".2", ".3" for further parts
    state: dict[str, Any]
    questions: dict[str, Question]
    estimated: int
    # what `jev.tokens.Drift` knew when this request was sized. 1.0 over 0 samples is the
    # honest state of the FIRST request of a session: nobody has checked the fit yet.
    drift: float = 1.0
    drift_samples: int = 0

    def as_sent(self) -> dict[str, Any]:
        """The body as the wire carries it, for `inspect --last`."""
        return {
            "name": self.name,
            "state": self.state,
            "questions": wire(self.questions),
            "estimated_tokens": self.estimated,
            "token_drift": {"ratio": round(self.drift, 3), "samples": self.drift_samples},
        }


@dataclass(frozen=True)
class FanOut:
    requests: tuple[Request, ...] = ()
    # option key -> entity. R2 offers "w3"; R2b asks "is_w3"; both mean windows["w3"]
    windows: dict[str, Window] = field(default_factory=dict)
    titled: dict[str, Window] = field(default_factory=dict)  # "t1" -> window, R2t only
    apps: dict[str, App] = field(default_factory=dict)  # "a7" -> app
    dropped_windows: int = 0
    dropped_apps: int = 0

    def named(self, prefix: str) -> tuple[Request, ...]:
        return tuple(r for r in self.requests if r.name.split(".")[0] == prefix)


# --------------------------------------------------------------------------- wire and size


def wire(questions: dict[str, Question]) -> dict[str, Any]:
    """The JSON the client will send. The estimator is fitted on this shape, not ours."""
    return {qid: _wire_one(q) for qid, q in questions.items()}


def _wire_one(question: Question) -> dict[str, Any]:
    if isinstance(question, Choice):
        return {
            "type": "choice",
            "instructions": question.instructions,
            "criteria": dict(question.options),
        }
    if isinstance(question, Score):
        return {
            "type": "score",
            "instructions": question.instructions,
            "criteria": list(question.levels),
        }
    assert isinstance(question, Boolean)
    out: dict[str, Any] = {"type": "boolean", "instructions": question.instructions}
    criteria = {k: v for k, v in (("true", question.true), ("false", question.false)) if v}
    if criteria:
        out["criteria"] = criteria
    return out


def estimate(state: dict[str, Any], questions: dict[str, Question]) -> int:
    return tokens.estimate(state, wire(questions))


def headroom(cap: int, drift: tokens.Drift | None = None) -> int:
    """The cap to build against, given how far the estimator has been running low.

    `tokens.estimate` is a linear fit over eight synthetic English requests, and its own
    docstring says paths, Unicode and long identifiers tokenize worse and that running
    LOW is the dangerous direction: a request believed to be under the cap is not, so
    either the client refuses it or the service starts returning 503 (13 of 30 at 11.5k
    tokens). `JevClient` has recorded every estimate against the count the API reports
    since it was written, and until now nothing read the tally. Reading it here is the
    whole wiring: a session whose requests have been coming back 20 percent larger than
    estimated builds its next request 20 percent smaller, and one whose fit holds builds
    exactly as before.

    Running HIGH is left alone. The cap already keeps headroom under the measured 2.2k
    latency knee, and spending that headroom because a few small requests over-estimated
    is how a fit gets trusted past the point it was measured at.
    """
    ratio = drift.ratio if drift is not None else 1.0
    return int(cap / ratio) if ratio > 1.0 else cap


def _request(
    name: str,
    state: dict[str, Any],
    questions: dict[str, Question],
    drift: tokens.Drift | None = None,
) -> Request:
    return Request(
        name,
        state,
        questions,
        estimate(state, questions),
        drift.ratio if drift is not None else 1.0,
        drift.samples if drift is not None else 0,
    )


def _part(prefix: str, index: int) -> str:
    return prefix if index == 0 else f"{prefix}.{index + 1}"


# --------------------------------------------------------------------------- state


def utterance_state(text: str, variants: Iterable[str] = ()) -> dict[str, Any]:
    """The one state every request carries (PLAN 5.5). Nothing about the desktop is here."""
    text = scrub(text, MAX_UTTERANCE_CHARS)
    seen = {text}
    kept: list[str] = []
    for variant in variants:
        variant = scrub(variant, MAX_UTTERANCE_CHARS)
        if variant and variant not in seen and len(kept) < MAX_VARIANTS:
            seen.add(variant)
            kept.append(variant)
    return {"utterance": text, "variants": kept, "note": bank.NOTE}


# --------------------------------------------------------------------------- descriptions


def recency(window: Window) -> str:
    if window.focus_rank == 0:
        return "focused right now"
    if window.focus_rank == 1:
        return "the one used just before"
    return f"used {window.focus_rank} windows ago"


def describe_window(window: Window, app_name: str, kind: str) -> dict[str, Any]:
    """`{app, kind, workspace, recency}`. NO TITLE: see the module docstring."""
    return {
        "app": scrub(app_name or window.cls or window.initial_class, MAX_NAME_CHARS),
        "kind": scrub(kind, MAX_NAME_CHARS) or "unknown",
        "workspace": scrub(window.workspace_name, MAX_NAME_CHARS) or str(window.workspace_id),
        "recency": recency(window),
    }


def describe_app(app: App) -> dict[str, Any]:
    return {
        "name": scrub(app.name, MAX_NAME_CHARS),
        "kind": scrub(app.kind or app.generic_name, MAX_NAME_CHARS) or "application",
    }


def shown_title(window: Window, kind: str, privacy: Privacy) -> str:
    """The title as R2t may carry it: scrubbed, redacted by class, kind and pattern, cut.

    Class alone does not identify sensitive content (a private browser window has the
    browser's class), so patterns and terminal kinds redact too (PLAN section 7).
    """
    if privacy.redacts_class(window.cls, window.initial_class) or is_terminal(window, kind):
        return REDACTED
    title = scrub(window.title)
    if privacy.redacts_text(title):
        return REDACTED
    return scrub(title, privacy.title_chars)


# --------------------------------------------------------------------------- builders


def _pack(items: dict[str, Any], fits: Callable[[dict[str, Any]], bool]) -> list[dict[str, Any]]:
    """As few parts as fit, then the same number of parts evened out by size.

    Greedy packing alone filled one part to the cap and left a sliver in the next: on
    the development machine 83 apps came out as 77 and 6. A full part sits at the
    latency knee while its sibling idles, and a 77-way choice gives `none` a very
    different prior from a 6-way one. An item that cannot fit alone is left out.
    """
    greedy: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for key, item in items.items():
        if not fits({key: item}):
            continue
        if current and not fits({**current, key: item}):
            greedy.append(current)
            current = {}
        current[key] = item
    if current:
        greedy.append(current)
    even = _even({k: v for part in greedy for k, v in part.items()}, len(greedy))
    return even if all(fits(part) for part in even) else greedy


def _even(items: dict[str, Any], parts: int) -> list[dict[str, Any]]:
    """Contiguous parts of roughly equal serialized size. Order is preserved."""
    if parts <= 1:
        return [items] if items else []
    weights = {key: len(json.dumps(_plain(item), default=str)) for key, item in items.items()}
    total = sum(weights.values())
    out: list[dict[str, Any]] = [{}]
    running = 0
    for key, item in items.items():
        if out[-1] and len(out) < parts and running >= total * len(out) / parts:
            out.append({})
        out[-1][key] = item
        running += weights[key]
    return out


def _plain(item: Any) -> Any:
    return _wire_one(item) if isinstance(item, Choice | Score | Boolean) else item


def split(
    prefix: str,
    state: dict[str, Any],
    questions: dict[str, Question],
    cap: int,
    *,
    drift: tokens.Drift | None = None,
) -> list[Request]:
    """Independent questions (R1, R2b) across as few requests as fit the cap."""
    cap = headroom(cap, drift)
    parts = _pack(questions, lambda part: estimate(state, part) <= cap)
    return [_request(_part(prefix, i), state, part, drift) for i, part in enumerate(parts)]


def window_requests(
    state: dict[str, Any],
    windows: Sequence[Window],
    describe: Callable[[Window], dict[str, Any]],
    cap: int,
    *,
    drift: tokens.Drift | None = None,
) -> tuple[list[Request], dict[str, Window], int]:
    """R2 and R2b over the same windows, most recent first. Returns (requests, keys, dropped)."""
    cap = headroom(cap, drift)
    ordered = sorted(windows, key=lambda w: w.focus_rank)
    kept = ordered[:MAX_WINDOWS]
    while kept:
        options = {f"w{i}": describe(w) for i, w in enumerate(kept, 1)}
        relative = {"window": bank.relative_window(options)}
        if estimate(state, relative) <= cap:
            break
        kept.pop()  # the least recently used window goes first
    if not kept:
        return [], {}, len(ordered)
    keys = {f"w{i}": w for i, w in enumerate(kept, 1)}
    absolute = {f"is_{key}": bank.absolute_window(options[key]) for key in keys}
    built = [
        _request("r2", state, relative, drift),
        *split("r2b", state, absolute, cap, drift=drift),
    ]
    return built, keys, len(ordered) - len(kept)


def title_request(
    state: dict[str, Any],
    windows: Sequence[Window],
    describe: Callable[[Window], dict[str, Any]],
    kind_of: Callable[[Window], str],
    privacy: Privacy,
    cap: int,
    *,
    drift: tokens.Drift | None = None,
) -> tuple[Request | None, dict[str, Window]]:
    """R2t. The CALLER decides whether this may exist; this only builds it safely."""
    cap = headroom(cap, drift)
    kept = sorted(windows, key=lambda w: w.focus_rank)[:MAX_WINDOWS]
    while len(kept) >= 2:
        options = {
            f"t{i}": {**describe(w), "title": shown_title(w, kind_of(w), privacy)}
            for i, w in enumerate(kept, 1)
        }
        questions = {"titled": bank.titled_window(options)}
        if estimate(state, questions) <= cap:
            keys = {f"t{i}": w for i, w in enumerate(kept, 1)}
            return _request("r2t", state, questions, drift), keys
        kept.pop()
    return None, {}


def app_requests(
    state: dict[str, Any], apps: Iterable[App], cap: int, *, drift: tokens.Drift | None = None
) -> tuple[list[Request], dict[str, App], int]:
    """R3, sharded. Sorting by kind keeps similar apps in one shard, where they compete."""
    cap = headroom(cap, drift)
    ordered = sorted((a for a in apps if a.trusted), key=lambda a: (a.kind, a.name.lower()))
    keys = {f"a{i}": app for i, app in enumerate(ordered, 1)}
    described = {key: describe_app(app) for key, app in keys.items()}
    shards = _pack(described, lambda part: estimate(state, {"app": bank.application(part)}) <= cap)
    sent = shards[:MAX_APP_SHARDS]
    offered = {key for shard in sent for key in shard}
    built = [
        _request(_part("r3", i), state, {"app": bank.application(shard)}, drift)
        for i, shard in enumerate(sent)
    ]
    return built, {k: a for k, a in keys.items() if k in offered}, len(keys) - len(offered)


def plan_request(
    state: dict[str, Any],
    affordances: Sequence[Affordance],
    cap: int,
    *,
    steps: int = plan.MAX_STEPS,
    drift: tokens.Drift | None = None,
) -> tuple[Request | None, dict[str, Affordance], int]:
    """Rp: the whole ordered plan, in ONE request, over ONE affordance list.

    The caller passes the affordances already ranked against the transcript, best first;
    the tail goes first when the list has to shrink, exactly as R2 drops the least
    recently used window.

    HOW MANY FIT, measured through this builder at the 1,800 token cap:

        candidates that fit      1 Choice   2 Choices   3 Choices
        60-character labels         30          12           7
        real track labels           32          13           7
        window and app labels       43          18          10

    Three Choices carry the option list three times, because JSON has no references and
    an option description has to be a rich object: bare labels moved a real window pick
    from P=0.04 to P=0.96 the other way (`bank.py`). Putting the list in the shared
    `state` instead and leaving short labels in the options was measured at about 3
    percent cheaper in the range that fits, and no more, because a state character costs
    0.4241 tokens against 0.3386 for a question character (`jev/tokens.py`): the
    cheapest place for the list is inside the questions.

    So STRATEGY.md 2.4's "plan for 50 to 60 candidates" was costed for one Choice and
    does not survive three. A plan request carries 7 to 10. That is enough for what a
    plan is over, which is the things this desktop can do right now after ranking, and
    it is not enough for picking among 50 search results: that pick is a single Choice
    in a second request, the one that offers what `resolve` returned, where 30 to 43
    fit.

    AND IT IS NOT SPLIT, although `split` would happily do it and each step Choice is
    self-contained. A shard that fails would take one step of a compound command with
    it, and the prefix rule would then read the survivors as a shorter plan: doing the
    front of an utterance and dropping the rest, silently, which is the exact v1 bug
    this design removes. One request fails whole, and a whole failure falls back to the
    path that was there before.
    """
    cap = headroom(cap, drift)
    kept = list(affordances)
    while kept:
        options = plan.offered(kept)
        questions = plan.questions(options, steps)
        if questions and estimate(state, questions) <= cap:
            return _request("rp", state, questions, drift), options, len(affordances) - len(options)
        kept.pop()
    return None, {}, len(affordances)
