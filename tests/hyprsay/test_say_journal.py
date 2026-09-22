"""The journal is private by construction. These tests are the privacy contract."""

import json
import stat

import pytest

from hyprsay import journal
from hyprsay.model import Action, Decision, Intent, Transcript, Verdict, Window


@pytest.fixture(autouse=True)
def private_state_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("HYPRSAY_JOURNAL", raising=False)
    monkeypatch.delenv("HYPRSAY_JOURNAL_TITLES", raising=False)


WINDOW = Window("0x1", "firefox", "firefox", "My bank: accounts", 1, "1", 0)


def test_dictated_text_is_never_written():
    secret = "my password is hunter2"
    action = Action(Intent.TYPE_TEXT, window=WINDOW, text=secret)
    journal.record("action", action=action, ok=True)
    written = journal.path().read_text()
    assert secret not in written and "hunter2" not in written
    entry = json.loads(written)
    assert entry["action"]["text"] == journal.redact_text(secret)
    assert entry["action"]["text"]["chars"] == len(secret)


def test_window_titles_are_dropped_unless_asked_for(monkeypatch):
    decision = Decision(
        Verdict.ACT, action=Action(Intent.FOCUS_WINDOW, window=WINDOW), heard="focus firefox"
    )
    journal.record("utterance", decision=decision)
    assert "My bank" not in journal.path().read_text()

    monkeypatch.setenv("HYPRSAY_JOURNAL_TITLES", "1")
    journal.record("utterance", decision=decision)
    assert "My bank" in journal.path().read_text().splitlines()[-1]


def test_the_transcript_of_a_command_is_kept_because_inspect_needs_it():
    journal.record(
        "utterance", transcript=Transcript("focus firefox", backend="local:parakeet-110m", ms=93.0)
    )
    assert journal.tail(1)[0]["transcript"]["text"] == "focus firefox"


def test_the_file_and_its_directory_are_private():
    journal.record("utterance", x=1)
    assert stat.S_IMODE(journal.path().stat().st_mode) == 0o600
    assert stat.S_IMODE(journal.path().parent.stat().st_mode) == 0o700


def test_it_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("HYPRSAY_JOURNAL", "off")
    journal.record("utterance", x=1)
    assert not journal.path().exists()


def test_it_rotates_once_when_it_grows_too_large(monkeypatch):
    monkeypatch.setattr(journal, "MAX_BYTES", 200)
    for i in range(12):
        journal.record("utterance", filler="x" * 40, i=i)
    assert journal.path().with_suffix(".jsonl.1").exists()
    assert journal.path().stat().st_size <= 400


def test_a_write_failure_never_reaches_the_caller(monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", "/proc/definitely/not/writable")
    journal.record("utterance", x=1)  # must not raise


def test_tail_filters_by_kind_and_survives_a_corrupt_line():
    journal.record("utterance", n=1)
    journal.record("action", n=2)
    with journal.path().open("a") as handle:
        handle.write("{not json\n")
    journal.record("utterance", n=3)
    assert [e["n"] for e in journal.tail(5, kind="utterance")] == [1, 3]
    assert journal.tail(1)[0]["n"] == 3


def test_setup_offers_the_default_key_not_a_hardcoded_one(monkeypatch, capsys):
    """`setup` built its own arguments and kept offering SUPER+V after the default moved."""
    import argparse
    import pathlib

    from hyprsay import cli
    from hyprsay.activation import DEFAULT_KEY

    shown: list[str] = []
    monkeypatch.setattr(cli, "_binds", lambda cfg, args: shown.append(args.key) or 0)
    monkeypatch.setattr(
        "hyprsay.stt.models.ensure", lambda name, progress=None, **kw: None, raising=False
    )
    monkeypatch.setattr(cli.config, "CONFIG_FILE", pathlib.Path("/dev/null"))
    cli._setup(cli.config.Config(), argparse.Namespace(units=False))
    assert shown == [""]  # empty asks for the default, which _binds then checks for conflicts
    assert "V" not in DEFAULT_KEY  # and the default is no longer the clipboard key


# --------------------------------------------------------------------------- hyprsay try


class TrialSocket:
    """A compositor that records the keyword commands it is sent."""

    def __init__(self, binds=(), provider="hyprlang"):
        self.sent: list[str] = []
        self._binds = list(binds)
        self.provider = provider

    def request(self, command):
        self.sent.append(command)
        return "ok"

    def query(self, command):
        return self._binds if command == "binds" else []

    @property
    def keywords(self):
        return [c for c in self.sent if c.startswith("keyword")]


def trial(monkeypatch, socket, *, daemon_result=0, daemon_raises=None, key=""):
    """Run cli._try with the compositor, the HUD and the daemon replaced."""
    import argparse

    from hyprsay import cli, daemon, world
    from hyprsay.model import DesktopState

    monkeypatch.setattr(world, "HyprSocket", lambda *a, **k: socket)
    monkeypatch.setattr(world, "snapshot", lambda s: DesktopState(provider=socket.provider))
    started: list[list[str]] = []

    class Hud:
        def __init__(self, argv, **kwargs):
            started.append(argv)
            self.terminated = False

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr("subprocess.Popen", Hud)

    def run(cfg):
        if daemon_raises is not None:
            raise daemon_raises
        return daemon_result

    monkeypatch.setattr(daemon, "main", run)
    code = cli._try(cli.config.Config(), argparse.Namespace(key=key))
    return code, started


def test_a_trial_installs_both_bindings_and_removes_them_on_the_way_out(monkeypatch):
    socket = TrialSocket()
    code, started = trial(monkeypatch, socket)
    assert code == 0
    assert socket.keywords[0].startswith("keyword bind SUPER, code:49, event, hyprsay:down")
    assert socket.keywords[1].startswith("keyword bindr SUPER, code:49, event, hyprsay:up")
    assert socket.keywords[-1] == "keyword unbind SUPER, code:49"
    assert started and started[0][0] == "/usr/bin/python3"


def test_a_trial_puts_the_session_back_even_when_the_engine_crashes(monkeypatch):
    socket = TrialSocket()
    with pytest.raises(RuntimeError):
        trial(monkeypatch, socket, daemon_raises=RuntimeError("boom"))
    assert any(c.startswith("keyword unbind") for c in socket.keywords)


def test_a_trial_refuses_a_key_that_is_already_bound_and_touches_nothing(monkeypatch):
    # the default binds by position, so a clash is reported as a keycode
    taken = [{"modmask": 64, "key": "", "keycode": 49, "description": "terminal"}]
    socket = TrialSocket(binds=taken)
    code, started = trial(monkeypatch, socket)
    assert code == 1
    assert socket.keywords == [] and started == []


def test_a_trial_refuses_the_lua_provider_rather_than_guessing_its_syntax(monkeypatch):
    socket = TrialSocket(provider="lua")
    code, started = trial(monkeypatch, socket)
    assert code == 1
    assert socket.keywords == [] and started == []


def test_a_window_title_inside_a_request_body_is_not_journalled(monkeypatch, tmp_path):
    """The titles request is a plain nested dict, and plain dicts were copied verbatim."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("HYPRSAY_JOURNAL", raising=False)
    monkeypatch.delenv("HYPRSAY_JOURNAL_TITLES", raising=False)
    exchange = {
        "requests": [
            {
                "name": "r2t",
                "questions": {
                    "titled": {
                        "criteria": {
                            "t1": {"app": "Firefox", "title": "Q3 salaries - Google Sheets"}
                        }
                    }
                },
            }
        ]
    }
    journal.record("utterance", exchange=exchange)
    written = journal.path().read_text()
    assert "Q3 salaries" not in written
    assert "Firefox" in written  # the app name is trusted and stays


def test_what_a_media_player_is_playing_is_not_journalled_either(monkeypatch, tmp_path):
    """docs/PRIVACY.md. `xesam:title` says what the owner is watching, which is the same
    sentence a window title carries, so it is dropped under the same flag and by the same
    rule, wherever it travels: as a `Player` field or as raw MPRIS metadata."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("HYPRSAY_JOURNAL", raising=False)
    monkeypatch.delenv("HYPRSAY_JOURNAL_TITLES", raising=False)
    secret = "Dr Smith explains my HIV test results"
    journal.record(
        "utterance",
        players=[{"bus_name": "org.mpris.MediaPlayer2.chromium", "playing": secret}],
        properties={"Metadata": {"xesam:title": secret, "xesam:album": "A Podcast"}},
    )
    written = journal.path().read_text()
    assert secret not in written and "A Podcast" not in written
    assert "chromium" in written  # the bus name is the bus daemon's word and stays


def test_what_an_adapter_says_it_played_is_not_journalled_either(tmp_path, monkeypatch):
    """`Service.label` was redacted on the way in and the same words came back out in
    an adapter's own success sentence, so the promise held for the request and not for
    the file on disk."""
    from hyprsay import journal
    from hyprsay.model import Action, Intent, Service

    monkeypatch.setenv("HYPRSAY_JOURNAL", "on")
    monkeypatch.setattr(journal, "path", lambda: tmp_path / "j.jsonl")
    secret = "Bohemian Rhapsody"
    action = Action(
        Intent.SERVICE,
        service=Service(adapter="waytify", capability="play_track", handle="h", label=secret),
    )
    journal.record("action", action=action, ok=True, message=f"Playing {secret}.")
    written = (tmp_path / "j.jsonl").read_text()
    assert secret not in written
    assert "waytify" in written  # the adapter's own name is hyprsay's word and stays


def test_hyprsays_own_message_is_not_redacted_by_that(tmp_path, monkeypatch):
    from hyprsay import journal
    from hyprsay.model import Action, Intent

    monkeypatch.setenv("HYPRSAY_JOURNAL", "on")
    monkeypatch.setattr(journal, "path", lambda: tmp_path / "j.jsonl")
    journal.record("action", action=Action(Intent.FOCUS_WINDOW), ok=True, message="Focused kitty.")
    assert "Focused kitty." in (tmp_path / "j.jsonl").read_text()


# ------------------------------------------------------------ carrying out from the terminal


def carry(verdict, *, tier=0, rest=(), yes=False, countdown=0.01, ok=True):
    """Run cli._carry_out with a fake executor and see what ceremony it demanded."""
    import argparse
    from dataclasses import replace as _replace

    from hyprsay import cli
    from hyprsay.config import Config, Safety
    from hyprsay.model import Action, Decision, Intent, Outcome

    calls = []

    class Fake:
        def execute(self, action, *, pinned_address=""):
            calls.append(action)
            return Outcome(ok, "Done." if ok else "the window changed")

    step = Decision(verdict, Action(Intent.CLOSE_WINDOW), tier=tier, rest=rest)
    cfg = _replace(Config(), safety=Safety(countdown_s=countdown))
    code = cli._carry_out(step, Fake(), "0x1", cfg, argparse.Namespace(yes=yes), 1, 1)
    return code, calls


def test_a_countdown_runs_in_the_terminal_instead_of_needing_the_engine():
    from hyprsay.model import Verdict

    code, calls = carry(Verdict.COUNTDOWN, tier=2)
    assert code == 0 and len(calls) == 1


def test_a_session_action_needs_an_explicit_yes():
    from hyprsay.model import Verdict

    code, calls = carry(Verdict.CONFIRM_KEY, tier=3)
    assert code == 1 and calls == []  # refused, and nothing ran
    code, calls = carry(Verdict.CONFIRM_KEY, tier=3, yes=True)
    assert code == 0 and len(calls) == 1


def test_a_verdict_that_is_not_an_action_carries_nothing_out():
    from hyprsay.model import Verdict

    for verdict in (Verdict.HINTS, Verdict.REFUSE, Verdict.SUGGEST, Verdict.NOTHING):
        code, calls = carry(verdict)
        assert code == 1 and calls == []


# ------------------------------------------------------------------ answering the hints


def answer_hints(*, pick=None, typed=None, isatty=True, json=False, monkeypatch=None):
    """Run cli._answer_hints against a stand-in understander and report what it asked."""
    import argparse
    import sys as _sys

    from hyprsay import cli
    from hyprsay.model import Action, Candidate, Decision, DesktopState, Intent, Verdict

    template = Action(Intent.RUN_RECIPE, verb="find")
    resolved = Decision(Verdict.ACT, template, tier=1)
    asked = []

    class Understander:
        def __init__(self):
            self.pending = None

        async def understand(self, transcript, state, *, picking=()):
            asked.append((transcript.text, self.pending, picking))
            return resolved

    monkeypatch.setattr(_sys.stdin, "isatty", lambda: isatty, raising=False)
    if typed is not None:
        monkeypatch.setattr("builtins.input", lambda _prompt: typed)
    candidates = (Candidate("Google Chrome (workspace 11)"), Candidate("Google Chrome (ws 8)"))
    step = Decision(Verdict.HINTS, template, candidates, tier=2)
    got = cli._answer_hints(
        step, Understander(), DesktopState, pick, argparse.Namespace(json=json), 1, 2
    )
    return got, asked


def test_a_hinted_step_can_be_answered_with_a_number(monkeypatch):
    """`say` printed numbered candidates and had nothing that could hear an answer, so
    a chain whose first clause was hinted carried nothing out and stopped there."""
    from hyprsay.model import Verdict

    got, asked = answer_hints(pick=2, monkeypatch=monkeypatch)
    assert got.verdict is Verdict.ACT
    said, pending, picking = asked[0]
    assert said == "2"
    # the number is a target, not a permission: it goes back through the understander
    # with the template it belongs to, and is judged by the tier function like any other
    assert pending is not None and len(picking) == 2


def test_a_typed_answer_reaches_the_understander(monkeypatch):
    _got, asked = answer_hints(typed=" two ", monkeypatch=monkeypatch)
    assert asked[0][0] == "two"


def test_an_empty_answer_leaves_the_hints_standing(monkeypatch):
    got, asked = answer_hints(typed="", monkeypatch=monkeypatch)
    assert got is None and asked == []


def test_nothing_is_asked_when_there_is_nobody_to_ask(monkeypatch):
    """A pipe and --json are read by something that cannot answer a prompt."""
    got, asked = answer_hints(isatty=False, monkeypatch=monkeypatch)
    assert got is None and asked == []
    got, asked = answer_hints(json=True, monkeypatch=monkeypatch)
    assert got is None and asked == []
