"""The only place hyprsay talks to Jev.

Policy here is set by measurement, not by the vendor's numbers. From the
development machine a warm request takes about 315 ms p50 and 360 to 440 ms
p90, with a tail that reached 1.6 s and once 6.9 s, and about one small request
in two hundred returns HTTP 503. A voice command that hangs is worse than one
that fails, because the speaker has already started repeating. So:

- one persistent connection, opened before the speaker finishes (`warm`)
- a hard deadline on every evaluation; past it the answer is thrown away
- an optional hedge: if nothing is back after `hedge_after`, a duplicate races the
  original. Off by default until tail latency is measured on real request bodies.
- one immediate retry on 503 or a dropped connection, never backoff
- 4xx fails at once; retrying a bad request or a bad key only wastes the deadline
- oversized requests are refused before sending (see tokens.py)

Two wire shapes are supported because the gateway documents both and they
measured the same within noise. "typesafe" is the default: it is TypeSafe's
own shape, so pointing at api.typesafe.ai later is a change of URL, key and
model, not of code.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

import httpx

from . import tokens
from .types import (
    Answer,
    Boolean,
    BooleanAnswer,
    Choice,
    ChoiceAnswer,
    Evaluation,
    Question,
    Score,
    ScoreAnswer,
)

GATEWAY = "https://ai-gateway.vercel.sh"
ROUTES = {
    # route -> (path, the wire name of a boolean question)
    "typesafe": ("/typesafe/v1/systemone", "noul"),
    "evaluate": ("/v1/evaluate", "boolean"),
}
# answers a GET without credentials, so it opens TCP, TLS and HTTP/2 for free
WARM_PATH = "/typesafe/v1/models"

KEY_ENV = "AI_GATEWAY_API_KEY"
KEY_FILE = Path("~/.config/hyprsay/ai-gateway.key")


class JevError(Exception):
    """Base class. Messages never contain the key or request headers."""


class JevAuthError(JevError):
    """401 or 403. The key is missing, wrong, or rotated."""


class JevBadRequest(JevError):
    """400 or 422. The request is malformed; sending it again cannot help."""


class JevBusy(JevError):
    """429. Surface it; do not queue behind it."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class JevUnavailable(JevError):
    """5xx or a transport failure. The one class that earns a retry."""


class JevTimeout(JevError):
    """Nothing usable arrived inside the deadline."""


class JevProtocolError(JevError):
    """A 200 whose body does not answer the questions that were asked."""


class RequestTooLarge(JevError):
    """Estimated tokens exceed the cap. Slice the state; do not raise the cap."""

    def __init__(self, estimated: int, cap: int) -> None:
        super().__init__(f"estimated {estimated} input tokens, cap is {cap}")
        self.estimated = estimated
        self.cap = cap


def load_key() -> str:
    """The gateway key from the environment, else from the private key file."""
    key = os.environ.get(KEY_ENV, "").strip()
    if key:
        return key
    path = KEY_FILE.expanduser()
    try:
        key = path.read_text().strip()
    except OSError as exc:
        raise JevAuthError(f"no gateway key: set {KEY_ENV} or create {path} (mode 600)") from exc
    if not key:
        raise JevAuthError(f"{path} is empty")
    return key


class JevClient:
    def __init__(
        self,
        key: str,
        *,
        route: str = "typesafe",
        base_url: str = GATEWAY,
        model: str = "typesafe-ai/jev",
        deadline: float = 0.9,
        # off by default: a hedge doubles load on a service that already returns 503,
        # and the point to fire it at has to come from measured tails (PLAN.md 5.5)
        hedge_after: float | None = None,
        token_cap: int = tokens.DEFAULT_CAP,
        zero_data_retention: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if route not in ROUTES:
            raise ValueError(f"unknown route {route!r}, expected one of {sorted(ROUTES)}")
        if not key:
            raise JevAuthError("empty gateway key")
        self.route = route
        self.model = model
        self.deadline = deadline
        self.hedge_after = hedge_after
        self.token_cap = token_cap
        self.zero_data_retention = zero_data_retention
        self.drift = tokens.Drift()
        self._path, self._boolean_name = ROUTES[route]
        headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
        options: dict[str, Any] = {
            "base_url": base_url,
            "headers": headers,
            # the deadline is enforced around the whole race; this only stops a
            # single dead socket from outliving it
            "timeout": httpx.Timeout(deadline + 1.0, connect=min(deadline, 1.0)),
            "transport": transport,
        }
        try:
            self._http = httpx.AsyncClient(http2=True, **options)
            self.http2 = True
        except ImportError:
            # h2 is an optional extra. HTTP/1.1 keepalive still reuses the
            # connection; what is lost is multiplexing concurrent requests.
            self._http = httpx.AsyncClient(**options)
            self.http2 = False

    def __repr__(self) -> str:
        return f"JevClient(route={self.route!r}, model={self.model!r}, deadline={self.deadline})"

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> JevClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def warm(self) -> bool:
        """Open the connection ahead of need. A cold first call measured about 1.2 s.

        Called on push-to-talk key down: speech outlasts a handshake, so the
        connection is ready by key up and no keepalive pings are needed.
        """
        try:
            await self._http.get(WARM_PATH, timeout=2.0)
        except httpx.HTTPError:
            return False
        return True

    async def evaluate(self, state: Any, questions: dict[str, Question]) -> Evaluation:
        if not questions:
            raise JevBadRequest("no questions")
        wire_questions = {qid: self._render(q) for qid, q in questions.items()}
        estimated = tokens.estimate(state, wire_questions)
        if estimated > self.token_cap:
            raise RequestTooLarge(estimated, self.token_cap)
        body: dict[str, Any] = {"model": self.model, "state": state, "questions": wire_questions}
        if self.zero_data_retention:
            body["providerOptions"] = {"gateway": {"zeroDataRetention": True}}

        started = time.perf_counter()
        try:
            async with asyncio.timeout(self.deadline):
                raw, attempts = await self._race(body)
        except TimeoutError as exc:
            raise JevTimeout(f"no answer within {self.deadline:.2f} s") from exc
        latency_ms = (time.perf_counter() - started) * 1000

        evaluation = self._parse(raw, questions, latency_ms, attempts, estimated)
        self.drift.record(estimated, evaluation.input_tokens)
        return evaluation

    async def _race(self, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """First good answer wins among the original, one hedge, and one retry."""
        loop = asyncio.get_running_loop()
        pending: set[asyncio.Task[dict[str, Any]]] = set()
        attempts = 0
        retries_left = 1
        hedge_at = loop.time() + self.hedge_after if self.hedge_after else None
        last: Exception | None = None

        def launch() -> None:
            nonlocal attempts
            attempts += 1
            pending.add(loop.create_task(self._once(body)))

        launch()
        try:
            while pending:
                wait = max(0.0, hedge_at - loop.time()) if hedge_at is not None else None
                done, pending = await asyncio.wait(
                    pending, timeout=wait, return_when=asyncio.FIRST_COMPLETED
                )
                if not done:
                    hedge_at = None
                    launch()
                    continue
                # read every finished task before acting: a success must win
                # over a failure that finished in the same wakeup, and an
                # exception nobody reads is logged by asyncio as a leak
                failures = [exc for task in done if (exc := task.exception()) is not None]
                for task in done:
                    if task.exception() is None:
                        return task.result(), attempts
                for exc in failures:
                    if not isinstance(exc, JevUnavailable):
                        raise exc
                    last = exc
                    if retries_left:
                        retries_left -= 1
                        launch()
            assert last is not None
            raise last
        finally:
            # on a win, a fatal error, or the outer deadline: nothing keeps running
            for task in pending:
                task.cancel()

    async def _once(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._http.post(self._path, json=body)
        except httpx.TransportError as exc:
            raise JevUnavailable(f"transport: {type(exc).__name__}") from exc
        status = response.status_code
        if status == 200:
            try:
                return response.json()
            except ValueError as exc:
                raise JevProtocolError("200 with a body that is not JSON") from exc
        message = _error_message(response)
        if status in (401, 403):
            raise JevAuthError(message)
        if status == 429:
            raise JevBusy(message, _retry_after(response))
        if status >= 500:
            raise JevUnavailable(f"HTTP {status}: {message}")
        raise JevBadRequest(f"HTTP {status}: {message}")

    def _render(self, question: Question) -> dict[str, Any]:
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
        if isinstance(question, Boolean):
            wire: dict[str, Any] = {
                "type": self._boolean_name,
                "instructions": question.instructions,
            }
            criteria = {
                name: text
                for name, text in (("true", question.true), ("false", question.false))
                if text is not None
            }
            if criteria:
                wire["criteria"] = criteria
            return wire
        raise TypeError(f"not a question: {type(question).__name__}")

    def _parse(
        self,
        raw: dict[str, Any],
        questions: dict[str, Question],
        latency_ms: float,
        attempts: int,
        estimated: int,
    ) -> Evaluation:
        wire_answers = raw.get("answers")
        if not isinstance(wire_answers, dict):
            raise JevProtocolError("response has no answers object")
        metadata = raw.get("provider_metadata") or raw.get("providerMetadata") or {}
        side_confidence = (metadata.get("typesafe") or {}).get("confidence") or {}

        answers: dict[str, Answer] = {}
        for qid, question in questions.items():
            wire = wire_answers.get(qid)
            if not isinstance(wire, dict):
                raise JevProtocolError(f"question {qid!r} was not answered")
            confidence = wire.get("confidence", side_confidence.get(qid))
            try:
                answers[qid] = _answer(question, wire, confidence)
            except (KeyError, TypeError, ValueError) as exc:
                raise JevProtocolError(f"malformed answer for {qid!r}: {exc}") from exc

        usage = raw.get("usage") or {}
        gateway = metadata.get("gateway") or {}
        return Evaluation(
            answers=answers,
            latency_ms=latency_ms,
            attempts=attempts,
            route=self.route,
            input_tokens=usage.get("input_tokens", usage.get("inputTokens")),
            estimated_tokens=estimated,
            cost=_number(gateway.get("cost")),
            market_cost=_number(gateway.get("marketCost")),
            model=raw.get("model"),
            raw=raw,
        )


def _answer(question: Question, wire: dict[str, Any], confidence: Any) -> Answer:
    confidence = _number(confidence)
    if isinstance(question, Choice):
        choice = wire["choice"]
        if choice not in question.options:
            raise ValueError(f"chose {choice!r}, which was not offered")
        probabilities = {k: float(v) for k, v in (wire.get("probabilities") or {}).items()}
        # the service rounds to two decimals, so sums of 0.99 and 1.01 are
        # normal; an option it left out simply has no mass
        for option in question.options:
            probabilities.setdefault(option, 0.0)
        return ChoiceAnswer(choice, probabilities, confidence)
    if isinstance(question, Score):
        probabilities = {k: float(v) for k, v in (wire.get("probabilities") or {}).items()}
        return ScoreAnswer(float(wire["score"]), probabilities, confidence)
    value = wire["noul"] if "noul" in wire else wire["probability"]
    return BooleanAnswer(float(value))


def _number(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _error_message(response: httpx.Response) -> str:
    """The server's own words, shortened. The two routes shape errors differently."""
    try:
        data = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])[:200]
        if data.get("message"):
            return str(data["message"])[:200]
    return str(data)[:200]


def _retry_after(response: httpx.Response) -> float | None:
    return _number(response.headers.get("retry-after"))
