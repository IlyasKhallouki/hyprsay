"""The Jev client against a mock transport. No test here touches the network."""

import asyncio
import json
import time

import httpx
import pytest

from hyprsay.jev import (
    Boolean,
    Choice,
    JevAuthError,
    JevBadRequest,
    JevBusy,
    JevClient,
    JevProtocolError,
    JevTimeout,
    JevUnavailable,
    QuestionError,
    RequestTooLarge,
    Score,
    load_key,
)
from hyprsay.jev import client as client_module

KEY = "test-key-not-real"

QUESTIONS = {
    "intent": Choice("Which action?", {"focus": "Focus a window", "close": "Close a window"}),
    "addressed": Boolean("Is this addressed to the computer?"),
    "amount": Score("How large a change?", ["small", "medium", "large"]),
}

TYPESAFE_BODY = {
    "model": "typesafe-ai/jev",
    "answers": {
        "intent": {
            "type": "choice",
            "choice": "focus",
            "confidence": 0.8,
            "probabilities": {"focus": 0.9, "close": 0.1},
        },
        "addressed": {"type": "noul", "noul": 0.96},
        "amount": {
            "type": "score",
            "score": 1.2,
            "confidence": 0.7,
            "probabilities": {"0": 0.1, "1": 0.6, "2": 0.3},
        },
    },
    "usage": {"input_tokens": 612, "output_tokens": 40},
    "provider_metadata": {"gateway": {"cost": "0", "marketCost": "0.0000257"}},
}

EVALUATE_BODY = {
    "model": "typesafe-ai/jev",
    "answers": {
        "intent": {
            "type": "choice",
            "choice": "focus",
            "probabilities": {"focus": 0.9, "close": 0.1},
        },
        "addressed": {"type": "boolean", "probability": 0.96},
        "amount": {"type": "score", "score": 1.2, "probabilities": {"0": 0.1, "1": 0.6, "2": 0.3}},
    },
    "usage": {"inputTokens": 612, "outputTokens": 40},
    "providerMetadata": {
        "typesafe": {"confidence": {"intent": 0.8, "amount": 0.7}},
        "gateway": {"cost": "0", "marketCost": "0.0000257"},
    },
}


class Server:
    """A scripted gateway: each POST pops the next step."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.posts = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"models": []})
        self.posts.append(request)
        step = self.steps.pop(0) if self.steps else (200, TYPESAFE_BODY, 0.0)
        status, body, delay = step
        if delay:
            await asyncio.sleep(delay)
        return httpx.Response(status, json=body)


def make(server, **options):
    options.setdefault("hedge_after", None)
    return JevClient(KEY, transport=httpx.MockTransport(server), **options)


def evaluate(client, state="close this", questions=None):
    async def go():
        async with client:
            return await client.evaluate(state, questions or QUESTIONS)

    return asyncio.run(go())


def test_typesafe_route_names_booleans_noul_and_reads_inline_confidence():
    server = Server((200, TYPESAFE_BODY, 0))
    result = evaluate(make(server, route="typesafe"))

    sent = json.loads(server.posts[0].content)
    assert server.posts[0].url.path == "/typesafe/v1/systemone"
    assert sent["questions"]["addressed"]["type"] == "noul"
    assert sent["questions"]["amount"]["criteria"] == ["small", "medium", "large"]
    assert result.choice("intent").confidence == 0.8
    assert result.boolean("addressed").probability == 0.96
    assert result.score("amount").score == 1.2
    assert result.input_tokens == 612


def test_evaluate_route_names_booleans_boolean_and_reads_side_channel_confidence():
    server = Server((200, EVALUATE_BODY, 0))
    result = evaluate(make(server, route="evaluate"))

    sent = json.loads(server.posts[0].content)
    assert server.posts[0].url.path == "/v1/evaluate"
    assert sent["questions"]["addressed"]["type"] == "boolean"
    assert result.choice("intent").confidence == 0.8
    assert result.boolean("addressed").probability == 0.96


def test_margin_is_the_distance_to_the_runner_up():
    result = evaluate(make(Server((200, TYPESAFE_BODY, 0))))
    intent = result.choice("intent")
    assert intent.p1 == 0.9
    assert intent.margin == pytest.approx(0.8)
    assert intent.ranked[0] == ("focus", 0.9)


def test_an_option_the_service_omits_has_no_mass():
    body = json.loads(json.dumps(TYPESAFE_BODY))
    body["answers"]["intent"]["probabilities"] = {"focus": 1.0}
    result = evaluate(make(Server((200, body, 0))))
    assert result.choice("intent").probabilities["close"] == 0.0
    assert result.choice("intent").margin == 1.0


def test_cost_is_what_the_gateway_charged_not_the_list_price():
    result = evaluate(make(Server((200, TYPESAFE_BODY, 0))))
    assert result.cost == 0.0
    assert result.market_cost == pytest.approx(0.0000257)


def test_a_choice_that_was_never_offered_is_a_protocol_error():
    body = json.loads(json.dumps(TYPESAFE_BODY))
    body["answers"]["intent"]["choice"] = "reboot"
    with pytest.raises(JevProtocolError, match="not offered"):
        evaluate(make(Server((200, body, 0))))


def test_an_unanswered_question_is_a_protocol_error():
    body = json.loads(json.dumps(TYPESAFE_BODY))
    del body["answers"]["addressed"]
    with pytest.raises(JevProtocolError, match="addressed"):
        evaluate(make(Server((200, body, 0))))


def test_an_oversized_request_is_refused_before_any_network_call():
    server = Server()
    huge = {"windows": [{"title": "x" * 60} for _ in range(400)]}
    with pytest.raises(RequestTooLarge) as info:
        evaluate(make(server), state=huge)
    assert server.posts == []
    assert info.value.estimated > info.value.cap == 1800


def test_the_deadline_fires_and_does_not_wait_for_the_slow_answer():
    server = Server((200, TYPESAFE_BODY, 5.0))
    started = time.perf_counter()
    with pytest.raises(JevTimeout):
        evaluate(make(server, deadline=0.15))
    assert time.perf_counter() - started < 1.0


def test_the_hedge_wins_when_the_original_stalls():
    server = Server((200, TYPESAFE_BODY, 5.0), (200, TYPESAFE_BODY, 0))
    result = evaluate(make(server, deadline=1.0, hedge_after=0.05))
    assert result.attempts == 2
    assert result.latency_ms < 900


def test_no_hedge_is_sent_when_the_answer_is_prompt():
    server = Server((200, TYPESAFE_BODY, 0))
    result = evaluate(make(server, deadline=1.0, hedge_after=0.3))
    assert result.attempts == 1
    assert len(server.posts) == 1


def test_a_503_is_retried_once_at_once():
    server = Server((503, {"error": {"message": "unavailable"}}, 0), (200, TYPESAFE_BODY, 0))
    result = evaluate(make(server))
    assert result.attempts == 2


def test_a_second_503_is_final():
    down = (503, {"error": {"message": "unavailable"}}, 0)
    server = Server(down, down, down)
    with pytest.raises(JevUnavailable, match="503"):
        evaluate(make(server))
    assert len(server.posts) == 2


def test_a_dropped_connection_is_retried_like_a_503():
    calls = []

    async def flaky(request):
        if request.method == "POST":
            calls.append(1)
            if len(calls) == 1:
                raise httpx.ReadError("connection reset")
        return httpx.Response(200, json=TYPESAFE_BODY)

    result = evaluate(make(flaky))
    assert result.attempts == 2


@pytest.mark.parametrize(
    ("status", "error"), [(401, JevAuthError), (403, JevAuthError), (400, JevBadRequest)]
)
def test_client_errors_fail_at_once_without_a_retry(status, error):
    server = Server((status, {"message": "nope", "error_type": "x"}, 0), (200, TYPESAFE_BODY, 0))
    with pytest.raises(error, match="nope"):
        evaluate(make(server))
    assert len(server.posts) == 1


def test_rate_limiting_surfaces_with_its_retry_after():
    async def limited(request):
        body = {"error": {"message": "slow down"}}
        return httpx.Response(429, json=body, headers={"retry-after": "2"})

    with pytest.raises(JevBusy) as info:
        evaluate(make(limited))
    assert info.value.retry_after == 2.0


def test_the_key_never_appears_in_a_repr_or_an_error():
    client = make(Server((401, {"message": "Authentication failed"}, 0)))
    assert KEY not in repr(client)
    with pytest.raises(JevAuthError) as info:
        evaluate(client)
    assert KEY not in str(info.value)
    assert KEY not in repr(info.value)


def test_zero_data_retention_is_requested_only_when_asked():
    plain, private = Server(), Server()
    evaluate(make(plain))
    evaluate(make(private, zero_data_retention=True))
    assert "providerOptions" not in json.loads(plain.posts[0].content)
    sent = json.loads(private.posts[0].content)
    assert sent["providerOptions"] == {"gateway": {"zeroDataRetention": True}}


def test_warming_opens_the_connection_without_credentials_in_the_path():
    seen = []

    async def handler(request):
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={"models": []})

    async def go():
        async with make(handler) as client:
            return await client.warm()

    assert asyncio.run(go()) is True
    assert seen == [("GET", "/typesafe/v1/models")]


def test_warming_reports_failure_instead_of_raising():
    async def dead(request):
        raise httpx.ConnectError("offline")

    async def go():
        async with make(dead) as client:
            return await client.warm()

    assert asyncio.run(go()) is False


def test_question_limits_are_enforced_before_a_round_trip():
    with pytest.raises(QuestionError, match="255"):
        Choice("x", {f"o{i}": "d" for i in range(256)})
    with pytest.raises(QuestionError):
        Choice("x", {})
    with pytest.raises(QuestionError, match="2 to 10"):
        Score("x", ["only one"])
    with pytest.raises(QuestionError, match="2 to 10"):
        Score("x", [str(i) for i in range(11)])
    Choice("x", {f"o{i}": "d" for i in range(255)})


def test_the_key_comes_from_the_environment_first_then_the_file(monkeypatch, tmp_path):
    key_file = tmp_path / "ai-gateway.key"
    key_file.write_text("from-file\n")
    monkeypatch.setattr(client_module, "KEY_FILE", key_file)

    monkeypatch.setenv("AI_GATEWAY_API_KEY", "from-env")
    assert load_key() == "from-env"

    monkeypatch.delenv("AI_GATEWAY_API_KEY")
    assert load_key() == "from-file"

    key_file.unlink()
    with pytest.raises(JevAuthError, match="no gateway key"):
        load_key()
