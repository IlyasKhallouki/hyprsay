"""Stage one against a mock transport. No test here touches the network or the key file.

Each test states one rule of the stage as a sentence: what it refuses locally, what it
sends, what it will accept back, and the one thing it must never let leave the machine.
"""

import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from hyprsay.config import Config, Interpret
from hyprsay.jev.client import JevAuthError
from hyprsay.nlu import interpret as stage
from hyprsay.nlu.context import Turn

KEY = "test-key-not-real"
CFG = Config()


class Server:
    """A scripted gateway: each POST pops the next step and records what it was sent."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.posts = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"models": []})
        self.posts.append(request)
        status, content, delay = self.steps.pop(0) if self.steps else (200, "ok", 0.0)
        if delay:
            await asyncio.sleep(delay)
        if isinstance(content, Exception):
            raise content
        body = {"choices": [{"message": {"role": "assistant", "content": content}}]}
        return httpx.Response(status, json=body)

    def sent(self) -> str:
        """Every byte of every request body, for the tests that assert on absence."""
        return " ".join(post.content.decode() for post in self.posts)

    def user_message(self, index: int = 0) -> str:
        body = json.loads(self.posts[index].content)
        return body["messages"][-1]["content"]


def client(server, **options):
    return stage.Interpreter(KEY, transport=httpx.MockTransport(server), **options)


def run(text, history=(), cfg=CFG, server=None, **options):
    async def go():
        if server is None:
            return await stage.interpret(text, history, cfg)
        async with client(server, **options) as interpreter:
            return await stage.interpret(text, history, cfg, interpreter=interpreter)

    return asyncio.run(go())


# --------------------------------------------------------------------------- the point


def test_babbling_becomes_one_command():
    server = Server((200, "play the music and turn the volume down a bit", 0))
    said = "uh can you put the music on and actually turn it down a bit"
    assert run(said, server=server) == "play the music and turn the volume down a bit"


def test_the_last_command_is_what_turn_it_down_to_50_percent_refers_to():
    server = Server((200, "set the volume to 50", 0))
    out = run("turn it down to 50%", ["play spotify"], server=server)
    assert out == "set the volume to 50"
    assert "play spotify" in server.user_message()


def test_a_turn_the_memory_already_holds_is_the_history_and_no_second_memory_is_built():
    server = Server((200, "set the volume to 50", 0))
    turns = (Turn(said="play spotify", intent="launch_app", app="Spotify", at=1.0),)
    assert run("turn it down to 50%", turns, server=server) == "set the volume to 50"
    assert "play spotify" in server.user_message()


def test_a_turn_whose_words_were_the_speakers_is_carried_as_its_intent_and_never_its_text():
    """`context.remembered` blanks `said` for a turn that typed, ran a recipe or clicked.
    Rendering the turn must not go looking for those words somewhere else."""
    typed = Turn(said="", intent="type_text", app="Obsidian", at=1.0)
    assert stage.recent((typed,)) == ("type text Obsidian",)


def test_at_most_the_three_turns_the_context_keeps_are_sent():
    many = [f"command {n}" for n in range(9)]
    assert len(stage.recent(many)) == stage.MAX_HISTORY == 3


# --------------------------------------------------------------------------- the skips


def test_an_utterance_the_grammar_already_parses_costs_no_network_call():
    server = Server((200, "switch to workspace 3", 0))
    assert run("workspace three", server=server) == "workspace three"
    assert server.posts == []


@pytest.mark.parametrize(
    "said",
    [
        "type my password is hunter2",
        "typed my password is hunter2",
        "i'll say the quiet part out loud",
        "let me write down the recovery phrase",
        "dictate this to the notes app",
    ],
)
def test_dictation_is_never_sent_anywhere_and_comes_back_word_for_word(said):
    server = Server((200, "something the model made up", 0))
    assert run(said, server=server) == said
    assert server.posts == []
    assert "hunter2" not in server.sent()


def test_a_dictated_turn_in_the_history_cannot_re_enter_through_the_history():
    secret = "correct horse battery staple"
    typed = Turn(said="", intent="type_text", app="Obsidian", at=1.0)
    server = Server((200, "focus obsidian", 0))
    run("i want to see my files", (typed,), server=server)
    assert secret not in server.sent()
    assert "type text Obsidian" in server.user_message()


def test_an_utterance_too_long_to_be_a_command_is_dictation_and_stays_home():
    server = Server((200, "tidy", 0))
    rambling = "so " * 200
    assert run(rambling, server=server) == rambling
    assert server.posts == []


def test_a_correction_is_never_rewritten_into_the_command_it_is_rejecting():
    """Live, "no wait put it back" after "switch to workspace 4" came back as "switch to
    workspace 4". The model resolved the pronoun and handed back the command the speaker
    was rejecting, with the "no" that marks it a correction rewritten away."""
    server = Server((200, "switch to workspace 4", 0))
    out = run("no wait put it back", ["switch to workspace 4"], server=server)
    assert out == "no wait put it back"
    assert server.posts == []


def test_a_correction_word_with_nothing_to_correct_is_just_a_sentence():
    """`context.about` says a correction of nothing is not a correction, and this agrees:
    with no history there is no previous command for "no" to be rejecting."""
    server = Server((200, "close the window", 0))
    assert run("no i mean the other window thing", server=server) == "close the window"
    assert len(server.posts) == 1


def test_turning_it_off_in_the_config_makes_it_a_pass_through():
    server = Server((200, "focus firefox", 0))
    cfg = replace(CFG, interpret=Interpret(enabled=False))
    assert run("i want to see my files", cfg=cfg, server=server) == "i want to see my files"
    assert server.posts == []


def test_an_empty_utterance_is_returned_as_it_came():
    assert run("") == ""
    assert run("   ") == "   "


def test_no_gateway_key_means_the_utterance_passes_through_untouched(monkeypatch):
    monkeypatch.setattr(stage, "load_key", lambda: (_ for _ in ()).throw(JevAuthError("no key")))
    asyncio.run(stage.close_shared())
    assert run("i want to see my files") == "i want to see my files"


# --------------------------------------------------------------------------- failures


def test_a_deadline_the_model_misses_costs_the_cleanup_and_never_the_command():
    server = Server((200, "focus obsidian", 0.3))
    assert run("i want to see my files", server=server, deadline=0.05) == "i want to see my files"


@pytest.mark.parametrize("status", [400, 401, 429, 500, 503])
def test_any_http_error_returns_the_utterance_unchanged(status):
    server = Server((status, "focus obsidian", 0))
    assert run("i want to see my files", server=server) == "i want to see my files"


def test_a_dropped_connection_returns_the_utterance_unchanged():
    server = Server((200, httpx.ConnectError("refused"), 0))
    assert run("i want to see my files", server=server) == "i want to see my files"


def test_a_200_that_is_not_the_shape_it_should_be_returns_the_utterance_unchanged():
    async def broken(request):
        if request.method == "GET":
            return httpx.Response(200, json={})
        return httpx.Response(200, text="not json at all")

    assert run("i want to see my files", server=broken) == "i want to see my files"


def test_nothing_is_retried_because_the_retry_would_spend_the_commands_budget():
    server = Server((503, "", 0), (200, "focus obsidian", 0))
    assert run("i want to see my files", server=server) == "i want to see my files"
    assert len(server.posts) == 1


# --------------------------------------------------------------------------- the answer


@pytest.mark.parametrize(
    "answer,why",
    [
        ("", "nothing at all"),
        ("   ", "only whitespace"),
        ("`amixer set Master 5%-`", "a shell line in backticks"),
        ("```\nfocus obsidian\n```", "a markdown fence"),
        ("focus obsidian && close kitty", "shell punctuation"),
        ("echo $HOME", "a variable"),
        ("....", "no word in it"),
        ("focus obsidian because you said bring up my notes and notes means obsidian here", "long"),
        ("set the volume to 30", "a number nobody said"),
    ],
)
def test_an_answer_that_is_not_one_short_command_is_discarded(answer, why):
    server = Server((200, answer, 0))
    assert run("i want to see my files", server=server) == "i want to see my files", why


def test_a_model_that_echoes_the_label_or_quotes_itself_is_still_understood():
    for answer in ("answer: focus obsidian", '"focus obsidian"', "Answer:  focus obsidian  "):
        server = Server((200, answer, 0))
        assert run("i want to see my files", server=server) == "focus obsidian"


def test_a_number_the_speaker_said_in_words_is_not_an_invention():
    """The normalizer is what makes "third" and "3" the same number, so the check runs
    over normalized forms or every good rewrite of a spoken number is thrown away."""
    server = Server((200, "move firefox to workspace 3", 0))
    said = "um move it over to the third workspace i guess"
    assert run(said, ["open firefox"], server=server) == "move firefox to workspace 3"


def test_a_number_that_was_only_in_the_history_is_not_an_invention_either():
    server = Server((200, "move it to workspace 4", 0))
    out = run("send that one over there as well", ["switch to workspace 4"], server=server)
    assert out == "move it to workspace 4"


def test_the_answer_may_grow_a_little_but_not_without_limit():
    assert stage._usable("set the volume to 50", "turn it down to 50", ()) == "set the volume to 50"
    assert stage._usable("x" * 200, "turn it down", ()) == ""


# --------------------------------------------------------------------------- the request


def test_what_is_sent_is_the_normalized_utterance_and_never_the_raw_one():
    """The normalizer has already dropped the fillers and the casing, so sending its text
    is strictly less about the speaker than sending what the recognizer wrote, and it is
    the same string `nlu/requests.py` already sends to Jev."""
    server = Server((200, "play the music and turn the volume down", 0))
    run("Uh, can you PLEASE put the music on?", server=server)
    message = server.user_message()
    assert "put the music on" in message
    assert "Uh" not in message and "PLEASE" not in message


def test_the_request_asks_for_one_short_line_and_nothing_else():
    server = Server((200, "focus obsidian", 0))
    run("i want to see my files", server=server)
    body = json.loads(server.posts[0].content)
    assert body["model"] == CFG.interpret.model
    assert body["max_tokens"] == stage.MAX_TOKENS
    assert body["temperature"] == 0
    assert server.posts[0].url.path == "/v1/chat/completions"


def test_no_stop_sequence_is_asked_for_because_it_measured_at_more_than_twice_the_latency():
    """905 ms p50 with `stop`, 397 ms without, over two interleaved passes of 30. One line
    is enforced in the prompt and in `_usable` instead, which costs nothing."""
    server = Server((200, "focus obsidian", 0))
    run("i want to see my files", server=server)
    assert "stop" not in json.loads(server.posts[0].content)


def test_the_prompt_tells_the_model_to_rewrite_and_never_to_invent():
    assert "Rewrite, never invent" in stage.SYSTEM
    assert "repeat it back word for word" in stage.SYSTEM


def test_an_empty_key_is_refused_before_a_client_exists():
    with pytest.raises(JevAuthError):
        stage.Interpreter("")


# --------------------------------------------------------------------------- the skip rate


def test_the_local_skip_takes_most_of_the_labelled_set_off_the_network():
    """The measured claim in the module docstring, kept honest by the suite. If a grammar
    change moves this, the number in the docstring is wrong and has to be re-measured."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "evals"))
    import cases

    from hyprsay.lexicon import Lexicon
    from hyprsay.nlu.normalize import normalize

    vocabulary = tuple(Lexicon(cases.APPS, {}).words())
    utterances = [row[0] for row in cases.ALL] + [row[0] for row in cases.ADVERSARIAL]
    skipped = sum(stage.already_a_command(normalize(u, vocabulary=vocabulary)) for u in utterances)
    assert skipped / len(utterances) > 0.7
    # every canonical command, which is what people actually say, is free
    canonical = [row[0] for row in cases.CANONICAL + cases.ASR_SURFACE]
    assert all(stage.already_a_command(normalize(u, vocabulary=vocabulary)) for u in canonical)
