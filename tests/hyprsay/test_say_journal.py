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
    assert socket.keywords[0].startswith("keyword bind SUPER, grave, event, hyprsay:down")
    assert socket.keywords[1].startswith("keyword bindr SUPER, grave, event, hyprsay:up")
    assert socket.keywords[-1] == "keyword unbind SUPER, grave"
    assert started and started[0][0] == "/usr/bin/python3"


def test_a_trial_puts_the_session_back_even_when_the_engine_crashes(monkeypatch):
    socket = TrialSocket()
    with pytest.raises(RuntimeError):
        trial(monkeypatch, socket, daemon_raises=RuntimeError("boom"))
    assert any(c.startswith("keyword unbind") for c in socket.keywords)


def test_a_trial_refuses_a_key_that_is_already_bound_and_touches_nothing(monkeypatch):
    taken = [{"modmask": 64, "key": "grave", "description": "terminal"}]
    socket = TrialSocket(binds=taken)
    code, started = trial(monkeypatch, socket)
    assert code == 1
    assert socket.keywords == [] and started == []


def test_a_trial_refuses_the_lua_provider_rather_than_guessing_its_syntax(monkeypatch):
    socket = TrialSocket(provider="lua")
    code, started = trial(monkeypatch, socket)
    assert code == 1
    assert socket.keywords == [] and started == []
