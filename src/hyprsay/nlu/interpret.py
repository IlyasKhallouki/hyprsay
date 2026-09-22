"""Stage one: babbling plus the last few commands, rewritten into ONE crisp command.

This module emits TEXT and nothing else. It never picks an action, never names a window,
never touches the desktop. It sits in front of the rest of the pipeline and hands the
understander a sentence that is easier to parse than the one the recognizer produced.
Everything that decides anything happens after it, from the string it returns.

**Why it is worth a round trip at all.** The grammar is exact and the resolver is local,
so "workspace three" costs microseconds. What neither can do is hear "uh can you put the
music on and actually turn it down a bit" as two commands, or resolve "turn it down to
50%" against "play spotify" said fifteen seconds ago. A small generative model with the
last three commands in front of it does both, and it does them as a rewrite, which is a
much smaller job than choosing an action.

**Latency, measured rather than assumed.** Every candidate below was run from this
machine against the real prompt on a warm HTTP/2 connection, five utterances per
repetition, sequential (which is the shape the daemon uses: one utterance at a time).
Milliseconds, request to parsed answer:

    model                                 n    p50    p90    max   answers
    meta/llama-3.1-8b                    50    395    660   1046   5 of 5, stable
    mistral/ministral-3b                 50    413    649    970   invents numbers
    google/gemini-2.5-flash-lite         50    468    813   2104   5 of 5, stable
    mistral/mistral-small                20    491    748   4951   5 of 5, drifts
    google/gemini-3.5-flash-lite         30    524   1386   2447   5 of 5, drifts
    mistral/mistral-nemo                 30    544    760   1221   4 of 5
    amazon/nova-micro                    30    612   1697   4660   5 of 5, drifts
    amazon/nova-lite                     30    623    808   1083   5 of 5
    spacexai/grok-4.1-fast-non-reasoning 30    640   1538   1760   5 of 5, drifts
    openai/gpt-4o-mini-fast              20   1064   1682   1734   5 of 5
    google/gemma-4-31b-it                20   1172   3283   3540   5 of 5
    meta/llama-4-scout                   20   6518   9723  10060   5 of 5

Measured and rejected on latency or on output: gemini-3.1-flash-lite (826 p50),
gpt-4.1-nano (1130), gpt-4.1-nano-fast (956), zai/glm-4.7-flash (752), tencent/hy-mt2-lite
(698), qwen3.5-flash and qwen3.7-flash (15 to 18 SECONDS, with read timeouts),
deepseek-v4-flash-0731 (2320), minimax-m2.1-lightning (1671), inference-net/schematron-v2-turbo
(1220, answers in JSON), xiaomi/mimo-v2.6-flash (3765), bytedance/seed-2.1-turbo (9647).
Four models answer with nothing usable at all inside a 32 token budget because they spend
it on reasoning tokens: inception/mercury-2.5, inception/mercury-2, zai/glm-4.7-flashx,
alibaba/qwen-3-14b, inclusionai/ling-3.0-flash. nvidia's nemotron models return their
scratchpad as the answer.

So the honest summary: ONE model is under 400 ms p50, by a handful of milliseconds. There
is no comfortable margin here and nothing else on the gateway is close with a usable
answer. Through this module as it actually ships, over 64 calls on twelve utterances, the
default model measured 394 ms p50, 592 ms p90 and 1394 ms worst, which is what
`Interpret.deadline_s` of 0.8 s was set from.

This is not free. A semantic command now costs about 394 + 315 ms p50 instead of 315, and
the local skips below are what keep that off the commands people actually say: over the
119 labelled utterances in `evals/cases.py` the grammar already parses 74.8 percent of
them exactly (100 percent of the canonical and ASR-surface rows), and not one of those
spends a network call here.

Two things had to be measured rather than reasoned about, and both were surprises:

- Asking the gateway for `stop: ["\\n"]`, which is exactly what a one line answer wants,
  measured 905 ms p50 against 397 ms without it, reproduced in two interleaved A/B passes
  of 30 requests. It is not sent. `_usable` enforces the one line for free.
- A cold connection costs the first command its cleanup. Unwarmed, the first call took
  1219 ms and the utterance came back unchanged; `warm()` costs 291 ms on its own, and
  even after it the first inference measured 745 ms before settling near 400. The caller
  must warm this on push-to-talk key down, next to the Jev client, or the first command
  of every session is the one that does not get tidied.

**The five ways this refuses, all but one of them local and free.** Nothing about an
utterance may be lost to this stage, so every one of them returns the input unchanged:

1. `interpret.enabled` is false, or there is no gateway key.
2. The utterance is dictation. `nlu/understand._sounds_like_dictation` decides, not a
   copy of its word list: those words are the speaker's own and never leave the machine
   (docs/PRIVACY.md), so this is the one refusal that is a privacy rule and not a budget.
3. The grammar already parses it. There is nothing to interpret, so there is nothing to
   pay for.
4. It is a correction of the turn before it (`nlu/context.looks_like_correction`). Live,
   "no wait put it back" after "switch to workspace 4" came back as "switch to workspace
   4": the model resolved the pronoun correctly and handed back the very command the
   speaker was rejecting, with the opening "no" that marks it as a correction rewritten
   away. A correction is the one utterance whose first word IS the meaning.
5. The model was late, errored, or answered with something that is not one short command.

**What the answer has to survive.** A rewrite that invents is worse than no rewrite, and
a model that was told not to invent still sometimes does. So the prompt is checked by
code afterwards: one line, no shell or markdown punctuation, not far longer than what
went in, and no number that was not in the utterance or in the history. That last rule is
the one that earns its keep. ministral-3b turned "put the music on and actually turn it
down a bit" into "play the music and lower the volume by 10 percent", and a volume of 10
is a different command from the one that was said.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterable, Sequence
from functools import lru_cache
from typing import Any

import httpx

from hyprsay.config import Config
from hyprsay.jev.client import GATEWAY, JevAuthError, load_key
from hyprsay.lexicon import sanitize
from hyprsay.model import Intent
from hyprsay.nlu import context
from hyprsay.nlu.normalize import normalize
from hyprsay.nlu.requests import MAX_UTTERANCE_CHARS

log = logging.getLogger("hyprsay")

# the OpenAI-compatible route on the same gateway the Jev client and the speech rescue
# already use, so this is one more model on one key and not a second vendor
PATH = "/v1/chat/completions"
# answers a GET without credentials, so it opens TCP, TLS and HTTP/2 for free
WARM_PATH = "/typesafe/v1/models"

# a command is a sentence. 32 tokens is about twenty words, which is well past anything
# worth keeping, and a small budget is also what stops a chatty model from spending the
# deadline explaining itself.
MAX_TOKENS = 32
# the same cut `nlu/context.Turn` puts on a remembered utterance, for the same reason:
# longer than this was never a command
MAX_COMMAND_CHARS = context.MAX_SAID_CHARS
# "far longer than the input" (requirement 5), as a number. A rewrite legitimately grows:
# "turn it down to 50" becomes "set the volume to 50". It does not double and then some.
LENGTH_FACTOR = 1.5
LENGTH_ROOM = 24
# at most the three turns `nlu/context.py` keeps. This module does not have a memory of
# its own and must never grow one (docs/STRATEGY.md 4.5).
MAX_HISTORY = context.MAX_TURNS

# punctuation no one speaks. A model that emits any of it is writing a shell line, a
# markdown fence or an explanation, and none of those is a command. "%", "/" and "-" are
# deliberately absent: "50%", a URL in a recipe and a hyphenated app name are all things
# a speaker really says.
JUNK = frozenset("`|$<>{}&;~*\\\n\r")
# models trained by the few-shot below sometimes echo its label back
LABELS = ("answer:", "command:", "output:")

SYSTEM = """You tidy up one spoken sentence into one short desktop command. Someone is
talking to their Linux desktop while holding a push-to-talk key, so the words arrive with
filler and false starts in them.

Answer with ONE line: the command, in plain imperative English words. Nothing else. No
explanation, no quotes, no backticks, no shell command, no flags, no file path, no
trailing period.

Rewrite, never invent. Every app, number, window and place in your answer must be one the
speaker referred to, in this sentence or in the recent commands. If the sentence is
already a plain command, or you cannot tell what it means, repeat it back word for word.

Resolve it, that, this, there against the recent commands when that is plainly what they
point at.

Examples.

recent commands: none
sentence: uh can you put the music on and actually turn it down a bit
answer: play the music and turn the volume down a bit

recent commands: play spotify
sentence: turn it down to 50%
answer: set the volume to 50

recent commands: none
sentence: workspace three
answer: workspace three

recent commands: none
sentence: so anyway I was telling mark that the whole thing
answer: so anyway I was telling mark that the whole thing"""


def prompt(said: str, history: Sequence[str]) -> str:
    """The user half of the request. The same shape the latency table was measured with;
    changing it invalidates those numbers."""
    recent = ", ".join(history) if history else "none"
    return f"recent commands: {recent}\nsentence: {said}\nanswer:"


# --------------------------------------------------------------------------- the client


class Interpreter:
    """One warm connection to one small model. Holds no state about any utterance.

    Built once by the caller and kept, exactly as `jev.JevClient` is: a cold first call
    measured about 1.2 s against 395 ms warm, and the push-to-talk key going down is the
    free moment to pay for the handshake.
    """

    def __init__(
        self,
        key: str,
        *,
        model: str = "meta/llama-3.1-8b",
        base_url: str = GATEWAY,
        deadline: float = 0.8,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not key:
            raise JevAuthError("empty gateway key")
        self.model = model
        self.deadline = deadline
        # what the last answer cost, for the journal and for the eval harness
        self.last_ms = 0.0
        self._key = key
        options: dict[str, Any] = {
            "base_url": base_url,
            "headers": {"Authorization": f"Bearer {key}", "Accept": "application/json"},
            # the deadline is enforced around the whole call; this only stops one dead
            # socket from outliving it
            "timeout": httpx.Timeout(deadline + 0.5, connect=min(deadline, 1.0)),
            "transport": transport,
        }
        try:
            self._http = httpx.AsyncClient(http2=True, **options)
            self.http2 = True
        except ImportError:
            # h2 is an optional extra; HTTP/1.1 keepalive still reuses the connection
            self._http = httpx.AsyncClient(**options)
            self.http2 = False

    def __repr__(self) -> str:
        return f"Interpreter(model={self.model!r}, deadline={self.deadline})"

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> Interpreter:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def warm(self) -> bool:
        """Open the connection ahead of need, on the same key down that warms Jev."""
        try:
            await self._http.get(WARM_PATH, timeout=2.0)
        except httpx.HTTPError:
            return False
        return True

    async def rewrite(self, said: str, history: Sequence[str] = ()) -> str:
        """The model's line, or "" when there is not one. NEVER raises, never retries.

        No retry on purpose, unlike the Jev client. Jev retries a 503 because its answer
        decides what happens; this answer only decides how tidy the sentence is, and a
        second attempt would spend the rest of a budget that belongs to the command.
        """
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": prompt(said, history)},
            ],
            "max_tokens": MAX_TOKENS,
            "temperature": 0,
            # No `stop` sequence, although one line is exactly what this wants. Asking the
            # gateway to stop at a newline measured 905 ms p50 against 397 ms without it,
            # reproduced in two interleaved A/B passes of 30 requests each: it appears to
            # cost the streamed fast path. One line is enforced in the prompt and again in
            # `_usable`, which is free.
        }
        started = time.perf_counter()
        try:
            async with asyncio.timeout(self.deadline):
                answer = await self._once(body)
        except (TimeoutError, httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            # the message can carry the request body, which can carry the utterance, so
            # only the class name is logged (docs/PRIVACY.md, same rule as understand.py)
            log.debug("interpret: %s", type(exc).__name__)
            return ""
        self.last_ms = (time.perf_counter() - started) * 1000
        return answer

    async def _once(self, body: dict[str, Any]) -> str:
        response = await self._http.post(PATH, json=body)
        if response.status_code != 200:
            # scrubbed, because an error body can echo the request back
            log.debug("interpret: HTTP %s", response.status_code)
            return ""
        choices = response.json().get("choices") or []
        if not choices:
            return ""
        content = (choices[0].get("message") or {}).get("content")
        return content if isinstance(content, str) else ""


# --------------------------------------------------------------------------- the stage


async def interpret(
    text: str,
    history: Sequence[Any] = (),
    cfg: Config | None = None,
    *,
    interpreter: Interpreter | None = None,
    vocabulary: Iterable[str] = (),
) -> str:
    """One utterance, tidied, or the same utterance back. The only entry point.

    `history` is `nlu/context.Context.recent` as it stands, a sequence of `Turn`, or plain
    strings for a caller that has nothing else. It is read, never written: the memory
    lives in `nlu/context.py` and there must not be a second one.

    `vocabulary` is the live lexicon's words, if the caller has them. Without it the local
    grammar check is slightly less able to parse a misheard word, which costs a network
    call that could have been skipped and never costs a command.
    """
    cfg = cfg if cfg is not None else Config()
    said = text or ""
    if not cfg.interpret.enabled or not said.strip():
        return text
    if len(said) > MAX_UTTERANCE_CHARS:
        # the understander already calls anything this long dictation and refuses to send
        # it (`_semantic`); this stage must not be the hole in that rule
        return text
    try:
        norm = normalize(said, vocabulary=tuple(vocabulary), aliases=cfg.aliases)
    except Exception:  # a normalizer fault must never cost the utterance
        return text
    literal = tuple(getattr(token, "text", token) for token in norm.tokens)
    if not norm.text.strip() or is_dictation(literal):
        return text
    if already_a_command(norm):
        return text
    lines = recent(history)
    if lines and context.looks_like_correction(literal):
        # A correction is the one utterance a rewrite destroys. Live, "no wait put it
        # back" after "switch to workspace 4" came back as "switch to workspace 4": the
        # model resolved "it" perfectly and produced the command the speaker was
        # REJECTING. Worse, the opening "no" is what `context.about` reads to set
        # `correction` at all, so the rewrite would have deleted the evidence as well as
        # inverted the meaning. Same closed word list, no second copy of it.
        return text

    client = interpreter if interpreter is not None else await shared(cfg)
    if client is None:
        return text
    try:
        answer = await client.rewrite(norm.text, lines)
    except Exception:  # `rewrite` swallows its own; this is the belt on the braces
        log.debug("interpret: rewrite raised")
        return text
    return _usable(answer, norm.text, lines) or text


def is_dictation(literal: Sequence[str]) -> bool:
    """Does this utterance carry the speaker's own words? The understander's rule, exactly.

    Imported through here rather than copied. A second copy of `DICTATION_OPENERS` would
    be a second thing to update when a recognizer invents a new spelling of "type", and
    the failure mode of missing that update is the speaker's password on the wire.
    """
    return _dictation_test()(list(literal))


def already_a_command(norm: Any) -> bool:
    """Does the exact grammar parse this as it stands? Then there is nothing to interpret.

    The cheap local skip. Measured over the 119 labelled utterances in `evals/cases.py`
    it takes 74.8 percent of them off the network entirely, including every canonical and
    every ASR-surface row, which is what keeps a round trip out of the commands people say
    most. A parse costs tens of microseconds against about 395 ms for the model.
    """
    try:
        parse = _grammar().parse(norm, picking=False)
    except Exception:  # a grammar fault costs the skip, never the utterance
        return False
    return parse is not None and parse.intent is not Intent.NONE


def recent(history: Sequence[Any]) -> tuple[str, ...]:
    """The last few commands as one line each, newest first, trusted fields only.

    A `Turn` is rendered from `said` when it has one, and `said` is deliberately blank on
    any turn that carried the speaker's own text (`nlu/context.remembered`). So dictation
    cannot re-enter the pipeline one utterance later, through the memory, after this
    module refused to send it directly.
    """
    lines: list[str] = []
    for item in list(history)[:MAX_HISTORY]:
        line = item if isinstance(item, str) else _turn_line(item)
        line = sanitize(line)[:MAX_COMMAND_CHARS].rstrip()
        if line:
            lines.append(line)
    return tuple(lines)


def _turn_line(turn: Any) -> str:
    said = getattr(turn, "said", "") or ""
    if said:
        return str(said)
    # no words to show, because they were the speaker's. The intent and the trusted app
    # name are the same fields `context.for_request` already sends.
    intent = str(getattr(turn, "intent", "") or "").replace("_", " ")
    app = str(getattr(turn, "app", "") or "")
    return f"{intent} {app}".strip()


# --------------------------------------------------------------------------- the answer


def _usable(answer: str, said: str, lines: Sequence[str] = ()) -> str:
    """The model's line if it is one short command, else "" and the utterance survives.

    Order matters: the junk test runs before `sanitize`, because sanitize turns a newline
    into a space and would quietly weld a two line explanation into one long "command".
    """
    if not isinstance(answer, str) or not answer.strip():
        return ""
    cleaned = answer.strip()
    lowered = cleaned.lower()
    for label in LABELS:
        if lowered.startswith(label):
            cleaned = cleaned[len(label) :].strip()
            break
    cleaned = cleaned.strip("\"'")
    if JUNK & set(cleaned):
        return ""
    cleaned = sanitize(cleaned)
    if not any(ch.isalnum() for ch in cleaned):
        return ""
    if len(cleaned) > MAX_COMMAND_CHARS or len(cleaned) > LENGTH_FACTOR * len(said) + LENGTH_ROOM:
        return ""
    if _invented_numbers(cleaned, said, lines):
        return ""
    return cleaned


def _invented_numbers(cleaned: str, said: str, lines: Sequence[str]) -> bool:
    """Is there a number in the answer that nobody said? Then it is not a rewrite.

    Compared over normalized forms, because the normalizer is what turns "three" and
    "third" into 3 and "50%" into 50: without that "move it to the third workspace" and
    "move firefox to workspace 3" would look like different numbers and a good rewrite
    would be thrown away.

    This is the one invention code can catch cheaply, and it is the one that hurts most: a
    wrong app is usually a wrong window and visible, a wrong number is a volume of 10 when
    the speaker said nothing about 10.
    """
    spoken = _digits(said) | {d for line in lines for d in _digits(line)}
    return bool(_digits(cleaned) - spoken)


def _digits(text: str) -> set[str]:
    try:
        words = normalize(text).text.split()
    except Exception:
        words = text.split()
    return {word for word in words if word.isdigit()}


# --------------------------------------------------------------------------- plumbing


@lru_cache(maxsize=1)
def _dictation_test() -> Callable[[Sequence[str]], bool]:
    """`nlu/understand._sounds_like_dictation`, fetched at first use and not at import.

    Late on purpose. The pipeline is being inverted so that this module runs BEFORE the
    understander, and the moment `understand` imports this one a module-level import here
    would make the pair a circular import that fails at start-up rather than in a test.
    """
    from hyprsay.nlu.understand import _sounds_like_dictation

    return _sounds_like_dictation


@lru_cache(maxsize=1)
def _grammar() -> Any:
    from hyprsay.nlu.grammar import Grammar

    return Grammar()


# one warm connection per process, for a caller that has not built its own. The daemon
# should build an `Interpreter` and warm it on key down; this exists so that a caller
# which does not still pays the 1.2 s handshake once rather than on every utterance.
_SHARED: dict[str, Any] = {}


async def shared(cfg: Config) -> Interpreter | None:
    """The process-wide interpreter for this config, or None when there is no key.

    Keyed by the event loop as well as by the settings: an `httpx.AsyncClient` belongs to
    the loop it was created on, and handing one to a second loop fails in ways that look
    like a network fault.
    """
    try:
        loop: Any = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    want = (loop, cfg.interpret.model, cfg.interpret.deadline_s)
    if _SHARED.get("key") == want:
        return _SHARED["client"]
    old, old_loop = _SHARED.get("client"), (_SHARED.get("key") or (None,))[0]
    if old is not None and old_loop is loop:
        await old.aclose()
    _SHARED.clear()
    try:
        client = Interpreter(
            load_key(), model=cfg.interpret.model, deadline=cfg.interpret.deadline_s
        )
    except JevAuthError:
        # no key is a configuration, not a fault: every utterance simply passes through.
        # Not cached, so a key written while the daemon runs is picked up.
        return None
    _SHARED.update(key=want, client=client)
    return client


async def close_shared() -> None:
    """Shut the process-wide connection. For shutdown, and for tests."""
    client = _SHARED.pop("client", None)
    _SHARED.pop("key", None)
    if client is not None:
        await client.aclose()
