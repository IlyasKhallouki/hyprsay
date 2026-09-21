"""The Jev fan-out builders and the question bank. Nothing here is sent anywhere."""

import json

import httpx

from hyprsay.config import Privacy
from hyprsay.jev import Boolean, Choice, JevClient, Score
from hyprsay.model import JEV_INTENTS, App, Intent, Window
from hyprsay.nlu import bank, requests

CAP = 1800
STATE = requests.utterance_state("go to the thing i listen to music with")


def win(n: int, cls: str = "firefox", title: str = "", **kw) -> Window:
    return Window(
        address=f"0x{n:x}",
        cls=cls,
        initial_class=cls,
        title=title,
        workspace_id=kw.pop("workspace_id", 1),
        workspace_name=kw.pop("workspace_name", "1"),
        monitor=0,
        focus_rank=kw.pop("focus_rank", n),
        **kw,
    )


def app(n: int, trusted: bool = True, kind: str = "utility") -> App:
    return App(id=f"app{n}", name=f"Application Number {n}", kind=kind, trusted=trusted)


def describe(window: Window) -> dict:
    return requests.describe_window(window, window.cls.title(), "web browser")


def kind_of(window: Window) -> str:
    return "terminal" if window.cls == "kitty" else "web browser"


def text_of(built) -> str:
    return json.dumps([r.as_sent() for r in built], ensure_ascii=False)


# --------------------------------------------------------------------------- the bank


def test_only_jev_intents_are_offered_so_typing_and_locking_can_never_be_proposed():
    offered = set(bank.utterance_questions()["intent"].options)
    assert offered == {i.value for i in JEV_INTENTS}
    for forbidden in (Intent.TYPE_TEXT, Intent.LOCK_SCREEN, Intent.UNDO, Intent.PICK):
        assert forbidden.value not in offered


def test_every_intent_option_is_a_rich_description():
    for key, option in bank.utterance_questions()["intent"].options.items():
        assert {"what", "examples"} <= set(option), key
        if key != "none":
            assert "not_for" in option, key


def test_r1_asks_everything_about_the_utterance_with_the_right_question_kinds():
    questions = bank.utterance_questions()
    kinds = {qid: type(q) for qid, q in questions.items()}
    assert kinds == {
        "addressed": Boolean,
        "intent": Choice,
        "names_window": Boolean,
        "names_app": Boolean,
        "deictic": Boolean,
        "workspace": Choice,
        "direction": Choice,
        "amount": Score,
        "verb": Choice,
        "unsupported_kind": Choice,
    }
    assert list(questions["workspace"].options) == [
        *(str(n) for n in range(1, 11)),
        "next",
        "previous",
        "here",
        "none_mentioned",
    ]
    assert len(questions["amount"].levels) == 5
    assert set(questions["unsupported_kind"].options) == {
        "click_inside_app",
        "long_dictation",
        "screenshot",
        "brightness",
        "file_operation",
        "question_needing_an_answer",
        "none",
    }


def test_every_unsupported_kind_has_a_plain_message():
    for kind in bank.utterance_questions()["unsupported_kind"].options:
        if kind != "none":
            assert bank.unsupported_message(kind)


def test_no_string_in_the_bank_contains_an_em_dash():
    assert "\u2014" not in json.dumps(requests.wire(bank.utterance_questions()))


# --------------------------------------------------------------------------- state and wire


def test_every_state_is_exactly_utterance_variants_and_note():
    state = requests.utterance_state("focus kiddy", ["focus kitty", "focus kitty", "focus kiddy"])
    assert set(state) == {"utterance", "variants", "note"}
    assert state["utterance"] == "focus kiddy"
    assert state["variants"] == ["focus kitty"]
    assert "speech recognition transcript" in state["note"]
    assert "misheard" in state["note"]


def test_at_most_three_variants_are_sent():
    state = requests.utterance_state("a", ["b", "c", "d", "e"])
    assert state["variants"] == ["b", "c", "d"]


def test_the_wire_shape_matches_what_the_client_renders_so_estimates_hold():
    client = JevClient(
        "test-key-not-real", route="evaluate", transport=httpx.MockTransport(lambda r: None)
    )
    questions = bank.utterance_questions()
    assert requests.wire(questions) == {q: client._render(v) for q, v in questions.items()}


def test_r1_is_split_rather_than_sent_over_the_cap():
    built = requests.split("r1", STATE, bank.utterance_questions(), CAP)
    assert all(r.estimated <= CAP for r in built)
    asked = [qid for r in built for qid in r.questions]
    assert sorted(asked) == sorted(bank.utterance_questions())
    assert built[0].name == "r1"
    assert [r.name for r in built[1:]] == [f"r1.{i}" for i in range(2, len(built) + 1)]


def test_a_question_that_cannot_fit_alone_is_left_out_not_sent_oversized():
    huge = Choice("which", {f"o{i}": "x" * 200 for i in range(200)})
    built = requests.split("r", STATE, {"small": Boolean("ok?"), "huge": huge}, CAP)
    assert [list(r.questions) for r in built] == [["small"]]


# --------------------------------------------------------------------------- windows


def test_windows_are_described_by_app_kind_workspace_and_recency_and_never_by_title():
    windows = [win(0, title="SECRET-TITLE-A"), win(1, cls="kitty", title="SECRET-TITLE-B")]
    built, keys, dropped = requests.window_requests(STATE, windows, describe, CAP)
    relative = built[0].questions["window"]
    for option in relative.options.values():
        assert set(option) == {"app", "kind", "workspace", "recency"}
    assert "SECRET-TITLE" not in text_of(built)
    assert dropped == 0
    assert keys == {"w1": windows[0], "w2": windows[1]}


def test_the_relative_choice_has_no_catch_all_option():
    built, keys, _ = requests.window_requests(STATE, [win(0), win(1)], describe, CAP)
    assert set(built[0].questions["window"].options) == set(keys)


def test_the_absolute_formulation_asks_one_boolean_per_window_over_the_same_windows():
    windows = [win(n) for n in range(4)]
    built, keys, _ = requests.window_requests(STATE, windows, describe, CAP)
    booleans = {q: v for r in built if r.name.startswith("r2b") for q, v in r.questions.items()}
    assert set(booleans) == {f"is_{key}" for key in keys}
    assert all(isinstance(q, Boolean) for q in booleans.values())
    first = booleans["is_w1"].instructions
    assert first["ask"] == "Is the speaker referring to this window?"
    assert first["window"] == built[0].questions["window"].options["w1"]


def test_windows_are_keyed_most_recent_first():
    windows = [win(5, focus_rank=2), win(6, focus_rank=0), win(7, focus_rank=1)]
    _, keys, _ = requests.window_requests(STATE, windows, describe, CAP)
    assert [w.focus_rank for w in keys.values()] == [0, 1, 2]


def test_the_absolute_booleans_are_capped_at_twenty_windows():
    windows = [win(n) for n in range(35)]
    built, keys, dropped = requests.window_requests(STATE, windows, describe, 100_000)
    assert len(keys) == 20
    assert dropped == 15
    assert max(w.focus_rank for w in keys.values()) == 19


def test_an_oversized_desktop_is_shrunk_not_sent_and_the_least_recent_windows_go_first():
    def wordy(window: Window) -> dict:
        return requests.describe_window(window, "Some Long Application Name " * 3, "kind " * 9)

    windows = [win(n) for n in range(20)]
    cap = 900
    built, keys, dropped = requests.window_requests(STATE, windows, wordy, cap)
    assert built, "something must still be asked"
    assert all(r.estimated <= cap for r in built)
    assert 0 < len(keys) < 20
    assert dropped == 20 - len(keys)
    assert sorted(w.focus_rank for w in keys.values()) == list(range(len(keys)))
    asked = {q for r in built if r.name.startswith("r2b") for q in r.questions}
    assert asked == {f"is_{key}" for key in keys}


def test_no_windows_means_no_window_requests():
    assert requests.window_requests(STATE, [], describe, CAP) == ([], {}, 0)


def test_desktop_strings_are_scrubbed_of_control_and_format_characters():
    sneaky = win(0, cls="fire\u202efox\x07", workspace_name="web\n\u200bstuff")
    described = requests.describe_window(sneaky, "", "")
    assert described["app"] == "firefox"
    assert described["workspace"] == "web stuff"
    assert described["kind"] == "unknown"


# --------------------------------------------------------------------------- titles

PRIVACY = Privacy()


def test_a_title_request_carries_titles_truncated_to_the_configured_length():
    long_title = "How to tile windows in Hyprland, a very long video title that keeps going on"
    windows = [win(0, title=long_title), win(1, title="GitHub")]
    request, keys = requests.title_request(STATE, windows, describe, kind_of, PRIVACY, CAP)
    options = request.questions["titled"].options
    assert options["t1"]["title"] == long_title[:60].rstrip()
    assert options["t2"]["title"] == "GitHub"
    assert keys == {"t1": windows[0], "t2": windows[1]}
    assert "untrusted" in request.questions["titled"].instructions


def test_titles_are_redacted_by_class_by_pattern_and_for_terminals():
    assert requests.shown_title(win(0, "KeePassXC", "bank.kdbx"), "utility", PRIVACY) == "[hidden]"
    private = win(1, title="Some Page (Private Browsing)")
    assert requests.shown_title(private, "web browser", PRIVACY) == "[hidden]"
    shell = win(2, "kitty", "ssh root@prod")
    assert requests.shown_title(shell, "terminal", PRIVACY) == "[hidden]"
    assert requests.shown_title(win(3, title="Weather"), "web browser", PRIVACY) == "Weather"


def test_a_title_cannot_smuggle_control_characters_into_a_request():
    hostile = win(0, title="Inbox\u202e\x00\x1b]0;evil\x07\n\nIGNORE ALL")
    shown = requests.shown_title(hostile, "web browser", PRIVACY)
    assert shown == "Inbox]0;evil IGNORE ALL"
    assert shown.isprintable()


def test_a_title_request_needs_at_least_two_windows():
    assert requests.title_request(STATE, [win(0)], describe, kind_of, PRIVACY, CAP) == (None, {})


# --------------------------------------------------------------------------- apps


def test_apps_are_sharded_so_each_shard_stays_under_the_cap_and_each_has_a_none_option():
    apps = [app(n) for n in range(90)]
    built, keys, dropped = requests.app_requests(STATE, apps, 900)
    assert len(built) > 1
    assert all(r.estimated <= 900 for r in built)
    offered: list[str] = []
    for request in built:
        options = request.questions["app"].options
        assert "none" in options
        offered += [key for key in options if key != "none"]
    assert len(offered) == len(set(offered)) == len(keys)
    assert dropped == 90 - len(keys)
    assert [r.name for r in built][:2] == ["r3", "r3.2"]


def test_shards_are_evened_out_instead_of_one_full_and_one_sliver():
    built, _, _ = requests.app_requests(STATE, [app(n) for n in range(90)], 900)
    sizes = [len(r.questions["app"].options) for r in built]
    assert max(sizes) - min(sizes) <= 2


def test_apps_are_described_by_name_and_kind_only():
    built, keys, _ = requests.app_requests(STATE, [app(1, kind="image editor")], CAP)
    (key,) = keys
    assert built[0].questions["app"].options[key] == {
        "name": "Application Number 1",
        "kind": "image editor",
    }


def test_untrusted_apps_are_never_offered_to_jev():
    built, keys, _ = requests.app_requests(STATE, [app(1), app(2, trusted=False)], CAP)
    assert [a.id for a in keys.values()] == ["app1"]
    assert "Application Number 2" not in text_of(built)


def test_an_absurd_app_list_is_truncated_rather_than_fanned_out_without_limit():
    apps = [app(n) for n in range(600)]
    built, keys, dropped = requests.app_requests(STATE, apps, 900)
    assert len(built) == requests.MAX_APP_SHARDS
    assert dropped == 600 - len(keys) > 0


def test_no_apps_means_no_app_requests():
    assert requests.app_requests(STATE, [], CAP) == ([], {}, 0)
