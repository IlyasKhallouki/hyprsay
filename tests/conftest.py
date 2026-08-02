"""Shared test wiring.

The one rule here: a unit test must never read live system state. The
0.9.3 audit found a guard that was dead code against the real
compositor while its tests passed happily on fabricated fixtures, so
anything that touches the machine gets neutralized by default and a
test that wants the interesting state opts in explicitly.
"""

import pytest

from hypruse import journal, trust


@pytest.fixture(autouse=True)
def no_ambient_journal(monkeypatch):
    """No recording, no dry run, unless a test asks for it.

    Same rule as below, pointed at the developer's environment rather
    than the machine's: a suite run with HYPRUSE_JOURNAL exported would
    append hundreds of fabricated entries to a real audit trail, and one
    with HYPRUSE_DRYRUN set would pass while every acting tool did
    nothing.
    """
    for var in ("HYPRUSE_JOURNAL", "HYPRUSE_JOURNAL_TEXT", "HYPRUSE_DRYRUN"):
        monkeypatch.delenv(var, raising=False)
    journal._broken = False


@pytest.fixture(autouse=True)
def unlocked_session(monkeypatch):
    """No session locker, unless a test says otherwise.

    trust.session_locked() scans /proc, so without this the suite's
    result would depend on whether the machine running it happens to be
    locked: green on a developer's desk, red in a locked session.
    """
    monkeypatch.setattr(trust, "session_locked", lambda: None)
