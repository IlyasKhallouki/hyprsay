"""The action journal: what gets recorded, what deliberately does not,
and the ways a recorder must not break the thing it records."""

import inspect
import json

import pytest

from hypruse import journal
from hypruse import server as srv
from hypruse.trust import TrustError


@pytest.fixture
def journal_file(tmp_path, monkeypatch):
    path = tmp_path / "journal.ndjson"
    monkeypatch.setenv("HYPRUSE_JOURNAL", str(path))
    monkeypatch.setattr(journal, "_seq", 0)
    return path


def entries(path):
    return list(journal.read(path))


@journal.journaled("act")
def acted(action: str, text: str = "", then: str = "none") -> str:
    return f"did {action}"


@journal.journaled("observe")
def looked(window: str = "") -> list:
    return ["a very large payload the journal must not copy"]


# --- what recording is, and is not ------------------------------------------


def test_off_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("HYPRUSE_JOURNAL", raising=False)
    assert journal.path() is None
    assert journal.enabled() is False
    acted("click")  # no file, no crash


def test_explicitly_off_is_off(monkeypatch):
    monkeypatch.setenv("HYPRUSE_JOURNAL", "0")
    assert journal.path() is None


def test_1_means_the_default_path(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("HYPRUSE_JOURNAL", "1")
    assert journal.path() == tmp_path / "hypruse" / "journal.ndjson"


def test_default_path_is_state_not_runtime(monkeypatch, tmp_path):
    # an audit trail in XDG_RUNTIME_DIR (tmpfs) would not survive a reboot
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert journal.default_path().parent == tmp_path / "state" / "hypruse"


def test_records_a_call(journal_file):
    acted("click")
    (entry,) = entries(journal_file)
    assert entry["tool"] == "acted"
    assert entry["kind"] == "act"
    assert entry["args"] == {"action": "click"}
    assert entry["outcome"] == "ok"
    assert entry["result"] == "did click"
    assert entry["v"] == journal.RECORD_VERSION
    assert "ms" in entry and "ts" in entry


def test_only_the_arguments_actually_passed(journal_file):
    # defaults are not recorded: the record should show what the agent
    # asked for, which is also what replay re-issues
    acted("click")
    assert entries(journal_file)[0]["args"] == {"action": "click"}


def test_observation_payloads_are_never_recorded(journal_file):
    looked(window="0xabc")
    (entry,) = entries(journal_file)
    assert entry["kind"] == "observe"
    assert entry["args"] == {"window": "0xabc"}
    assert "result" not in entry  # that it happened, not what was on screen


def test_refusal_is_recorded_as_refused(journal_file):
    @journal.journaled("act")
    def refuses() -> str:
        raise TrustError("outside the confinement scope")

    with pytest.raises(TrustError):
        refuses()
    (entry,) = entries(journal_file)
    assert entry["outcome"] == "refused"
    assert "confinement scope" in entry["error"]


def test_other_failures_are_recorded_as_errors(journal_file):
    @journal.journaled("act")
    def breaks() -> str:
        raise ValueError("bad args")

    with pytest.raises(ValueError):
        breaks()
    assert entries(journal_file)[0]["outcome"] == "error"


def test_a_long_result_is_truncated(journal_file):
    @journal.journaled("act")
    def chatty() -> str:
        return "x" * 5000

    chatty()
    assert len(entries(journal_file)[0]["result"]) < 300


# --- redaction --------------------------------------------------------------


def test_typed_text_is_a_digest_by_default(journal_file):
    acted("type", text="hunter2")
    recorded = entries(journal_file)[0]["args"]["text"]
    assert recorded["redacted"] is True
    assert recorded["chars"] == 7
    assert "hunter2" not in json.dumps(recorded)


def test_the_digest_is_stable_across_calls(journal_file):
    acted("type", text="same")
    acted("type", text="same")
    a, b = (e["args"]["text"]["sha256"] for e in entries(journal_file))
    assert a == b


def test_text_is_kept_when_opted_in(journal_file, monkeypatch):
    monkeypatch.setenv("HYPRUSE_JOURNAL_TEXT", "1")
    acted("type", text="hunter2")
    assert entries(journal_file)[0]["args"]["text"] == "hunter2"


def test_text_inside_sequence_steps_is_redacted_too(journal_file):
    @journal.journaled("act")
    def sequence(steps: list) -> str:
        return "ran"

    sequence([{"op": "keyboard", "action": "type", "text": "hunter2"}])
    step = entries(journal_file)[0]["args"]["steps"][0]
    assert step["text"]["redacted"] is True
    assert "hunter2" not in json.dumps(entries(journal_file))


def test_redacted_text_is_recognisable_to_replay():
    assert journal.redacted_text({"redacted": True, "chars": 3}) is True
    assert journal.redacted_text("plain text") is False


# --- ordering and nesting ---------------------------------------------------


def test_seq_is_issue_order_even_when_lines_are_not(journal_file):
    inner = journal.journaled("act")(lambda: "step")
    inner.__name__ = "pointer"

    @journal.journaled("act")
    def sequence(steps: list) -> str:
        inner()
        return "ran"

    sequence([{"op": "pointer"}])
    written = entries(journal_file)
    # the step finishes first, so it is written first, but the sequence
    # was issued first and carries the lower seq
    assert [e["tool"] for e in written] == ["<lambda>", "sequence"]
    assert written[0]["seq"] > written[1]["seq"]


def test_sequence_steps_carry_their_parent(journal_file):
    inner = journal.journaled("act")(lambda: "step")

    @journal.journaled("act")
    def sequence(steps: list) -> str:
        inner()
        return "ran"

    sequence([{"op": "pointer"}])
    step, seq = entries(journal_file)
    assert step["parent"] == seq["seq"]
    assert "parent" not in seq


def test_a_standalone_call_has_no_parent(journal_file):
    acted("click")
    assert "parent" not in entries(journal_file)[0]


def test_parent_is_cleared_after_the_sequence(journal_file):
    @journal.journaled("act")
    def sequence(steps: list) -> str:
        return "ran"

    sequence([])
    acted("click")
    assert "parent" not in entries(journal_file)[1]


# --- dry-run marking --------------------------------------------------------


def test_dry_calls_are_marked(journal_file, monkeypatch):
    monkeypatch.setenv("HYPRUSE_DRYRUN", "1")
    acted("click")
    assert entries(journal_file)[0]["dry"] is True


def test_real_calls_are_not_marked(journal_file):
    acted("click")
    assert "dry" not in entries(journal_file)[0]


# --- session boundaries -----------------------------------------------------


def test_session_start_records_the_trust_flags(journal_file, monkeypatch):
    monkeypatch.setenv("HYPRUSE_CONFINE", "launched")
    monkeypatch.setenv("HYPRUSE_STRICT", "1")
    journal.start("9.9.9")
    (entry,) = entries(journal_file)
    assert entry["kind"] == "session" and entry["event"] == "start"
    assert entry["version"] == "9.9.9"
    assert entry["mode"]["confine"] == "launched"
    assert entry["mode"]["strict"] is True


def test_session_records_are_skipped_when_recording_is_off(monkeypatch):
    monkeypatch.delenv("HYPRUSE_JOURNAL", raising=False)
    journal.start("9.9.9")  # no path, no crash
    journal.stop()


# --- staying out of the way -------------------------------------------------


def test_an_unwritable_journal_does_not_break_the_action(monkeypatch, capsys, tmp_path):
    blocked = tmp_path / "afile"
    blocked.write_text("not a directory")
    monkeypatch.setenv("HYPRUSE_JOURNAL", str(blocked / "sub" / "journal.ndjson"))
    assert acted("click") == "did click"  # the action still happened
    assert "cannot write the journal" in capsys.readouterr().err


def test_the_write_failure_is_reported_once(monkeypatch, capsys, tmp_path):
    blocked = tmp_path / "afile"
    blocked.write_text("not a directory")
    monkeypatch.setenv("HYPRUSE_JOURNAL", str(blocked / "sub" / "journal.ndjson"))
    acted("click")
    capsys.readouterr()
    acted("click")
    assert capsys.readouterr().err == ""  # stderr is not a log sink


def test_rotation_keeps_one_generation(journal_file, monkeypatch):
    monkeypatch.setenv("HYPRUSE_JOURNAL_MAX_BYTES", "4096")
    journal_file.write_text("x" * 5000)
    acted("click")
    assert journal_file.with_suffix(".ndjson.1").read_text().startswith("x")
    assert len(entries(journal_file)) == 1


def test_a_torn_last_line_does_not_hide_the_history(journal_file):
    acted("click")
    with open(journal_file, "a") as fh:
        fh.write('{"v": 1, "seq": 2, "tool": "poin')  # killed mid-write
    assert len(entries(journal_file)) == 1


# --- the wrapper must be invisible to everything else -----------------------


def test_the_decorator_preserves_the_signature():
    # FastMCP builds each tool's JSON schema from this
    assert list(inspect.signature(srv.pointer).parameters) == [
        "action", "x", "y", "button", "to_x", "to_y",
        "scroll_dy", "scroll_dx", "double", "then", "allow_auth",
    ]
    assert srv.pointer.__doc__ and "Mouse in global coordinates" in srv.pointer.__doc__


def test_every_tool_is_journaled():
    tools = (srv.pointer, srv.keyboard, srv.click_ui, srv.hypr, srv.launch,
             srv.use_bind, srv.sequence, srv.clipboard, srv.desktop, srv.screenshot,
             srv.zoom, srv.ui, srv.marks, srv.binds, srv.wait_for)
    assert all(hasattr(t, "__wrapped__") for t in tools)


def test_the_sequence_tool_still_reaches_the_journaled_handlers():
    # _SEQ_HANDLERS is built from the module-level names, so a step run
    # inside a sequence goes through the same wrapper a direct call does
    assert all(hasattr(h, "__wrapped__") for h in srv._SEQ_HANDLERS.values())


# --- what replay will re-issue ----------------------------------------------


def test_replayable_takes_successful_actions():
    assert journal.replayable({"kind": "act", "outcome": "ok", "tool": "pointer"})


def test_replayable_skips_observations_and_failures():
    assert not journal.replayable({"kind": "observe", "outcome": "ok", "tool": "screenshot"})
    assert not journal.replayable({"kind": "act", "outcome": "refused", "tool": "pointer"})
    assert not journal.replayable({"kind": "session", "event": "start"})


def test_replayable_skips_the_sequence_wrapper():
    # its steps were recorded individually; replaying both runs everything twice
    assert not journal.replayable({"kind": "act", "outcome": "ok", "tool": "sequence"})


def test_a_clipboard_read_is_an_observation_and_a_write_is_not(journal_file, monkeypatch):
    monkeypatch.setattr(srv.clip, "read", lambda: "text")
    monkeypatch.setattr(srv.clip, "write", lambda t: None)
    monkeypatch.setattr(srv.safety, "touch", lambda *a: None)
    srv.clipboard("read")
    srv.clipboard("write", text="hello")
    assert [e["kind"] for e in entries(journal_file)] == ["observe", "act"]
