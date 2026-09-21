"""The lock latch against fake sources. No compositor, no loginctl, no /proc."""

import subprocess

import pytest

from hyprsay import lock
from hyprsay.config import Config, Safety
from hyprsay.lock import Latch
from hyprsay.model import DesktopState, Layer, Window

UNLOCKED = DesktopState(locked=False)


class Desk:
    """A desktop whose every lock source can be flipped by a test."""

    def __init__(self) -> None:
        self.state: DesktopState | Exception = UNLOCKED
        self.compositor: object = {"locked": False}
        self.asked: list[str] = []
        self.hint: bool | None | Exception = False
        self.hint_reads = 0
        self.now = 100.0

    def provide(self) -> DesktopState:
        if isinstance(self.state, Exception):
            raise self.state
        return self.state

    def query(self, command: str) -> object:
        self.asked.append(command)
        if isinstance(self.compositor, Exception):
            raise self.compositor
        return self.compositor

    def logind(self) -> bool | None:
        self.hint_reads += 1
        if isinstance(self.hint, Exception):
            raise self.hint
        return self.hint

    def latch(self, cfg: Config | None = None) -> Latch:
        return Latch(
            cfg or Config(), self.provide, self.query, logind=self.logind, clock=lambda: self.now
        )


def window(cls: str) -> Window:
    return Window("0xabc", cls, cls, "untitled", 1, "1", 0)


def test_an_unlocked_desk_with_every_source_agreeing_is_unlocked():
    assert Desk().latch().locked() is False


def test_a_default_desktop_state_is_locked_because_nobody_read_it():
    desk = Desk()
    desk.state = DesktopState()
    assert desk.latch().locked() is True


def test_a_state_provider_that_raises_means_locked():
    desk = Desk()
    desk.state = RuntimeError("socket gone")
    assert desk.latch().locked() is True


@pytest.mark.parametrize(
    "namespace",
    [
        "hyprlock",
        "swaylock",
        "gtklock",
        "waylock",
        "LockScreen",
        "quickshell:lock",
        "my-hyprlock-2",
    ],
)
def test_a_lock_screen_layer_means_locked(namespace):
    desk = Desk()
    desk.state = DesktopState(locked=False, layers=(Layer(namespace, "eDP-1", 3),))
    assert desk.latch().locked() is True


@pytest.mark.parametrize("namespace", ["waybar", "clock", "hyprsay-hud", "wofi", "mako"])
def test_an_ordinary_layer_does_not_lock_the_desk(namespace):
    desk = Desk()
    desk.state = DesktopState(locked=False, layers=(Layer(namespace, "eDP-1", 2),))
    assert desk.latch().locked() is False


def test_a_configured_locker_name_matches_layers_and_window_classes_in_any_case():
    cfg = Config(safety=Safety(locker_namespaces=("OmaLock",)))
    as_layer, as_window = Desk(), Desk()
    as_layer.state = DesktopState(locked=False, layers=(Layer("shell-omalock", "eDP-1", 3),))
    as_window.state = DesktopState(locked=False, windows=(window("org.OMALOCK.greeter"),))
    assert as_layer.latch(cfg).locked() is True
    assert as_window.latch(cfg).locked() is True
    assert as_window.latch().locked() is False


def test_a_built_in_locker_name_is_not_matched_against_window_classes():
    desk = Desk()
    desk.state = DesktopState(locked=False, windows=(window("lockscreen-settings"),))
    assert desk.latch().locked() is False


@pytest.mark.parametrize("answer", [{"locked": True}, True])
def test_the_compositor_saying_locked_means_locked(answer):
    desk = Desk()
    desk.compositor = answer
    assert desk.latch().locked() is True


@pytest.mark.parametrize("answer", [RuntimeError("no socket"), {}, None, "unknown request", 0])
def test_a_compositor_query_that_fails_or_answers_nonsense_means_locked(answer):
    desk = Desk()
    desk.compositor = answer
    assert desk.latch().locked() is True


def test_logind_locked_hint_means_locked():
    desk = Desk()
    desk.hint = True
    assert desk.latch().locked() is True


@pytest.mark.parametrize("hint", [None, OSError("no loginctl")])
def test_logind_is_the_one_source_whose_failure_is_ignored(hint):
    desk = Desk()
    desk.hint = hint
    assert desk.latch().locked() is False


def test_logind_is_read_once_per_second_at_most():
    desk = Desk()
    latch = desk.latch()
    for _ in range(5):
        latch.locked()
    assert desk.hint_reads == 1
    desk.now += 0.9
    latch.locked()
    assert desk.hint_reads == 1
    desk.now += 0.2
    latch.locked()
    assert desk.hint_reads == 2


def test_the_compositor_is_asked_for_locked_again_on_every_call():
    desk = Desk()
    latch = desk.latch()
    assert latch.locked() is False
    desk.compositor = {"locked": True}
    assert latch.locked() is True
    assert desk.asked == ["locked", "locked"]


def test_a_query_callable_with_the_wrong_shape_fails_closed():
    latch = Latch(Config(), lambda: UNLOCKED, lambda: {"locked": False}, logind=None)
    assert latch.locked() is True


def test_on_lock_fires_once_per_unlocked_to_locked_edge():
    desk = Desk()
    latch = desk.latch()
    fired = []
    latch.on_lock(lambda: fired.append("dropped"))
    latch.locked()
    assert fired == []
    desk.compositor = {"locked": True}
    latch.locked()
    latch.locked()
    assert fired == ["dropped"]
    desk.compositor = {"locked": False}
    latch.locked()
    desk.compositor = {"locked": True}
    latch.locked()
    assert fired == ["dropped", "dropped"]


def test_a_daemon_started_on_a_locked_desk_gets_the_edge_on_its_first_look():
    desk = Desk()
    desk.compositor = {"locked": True}
    latch = desk.latch()
    fired = []
    latch.on_lock(lambda: fired.append(1))
    assert latch.locked() is True
    assert fired == [1]


def test_a_callback_that_raises_neither_hides_the_lock_nor_starves_the_next_callback():
    desk = Desk()
    desk.compositor = {"locked": True}
    latch = desk.latch()
    fired = []

    def broken() -> None:
        raise RuntimeError("audio stream already closed")

    latch.on_lock(broken)
    latch.on_lock(lambda: fired.append(1))
    assert latch.locked() is True
    assert fired == [1]


def test_why_names_the_first_source_that_objects():
    desk = Desk()
    assert desk.latch().why() == ""
    desk.hint = True
    assert desk.latch().why() == "logind"
    desk.compositor = RuntimeError("down")
    assert desk.latch().why() == "compositor"
    desk.state = DesktopState()
    assert desk.latch().why() == "state"


def test_the_default_query_asks_the_inherited_hyprctl_for_locked(monkeypatch):
    from hypruse import hyprctl

    asked = []

    def fake_query(command: str) -> dict:
        asked.append(command)
        return {"locked": False}

    monkeypatch.setattr(hyprctl, "query", fake_query)
    latch = Latch(Config(), lambda: UNLOCKED, logind=None)
    assert latch.locked() is False
    assert asked == ["locked"]


# ------------------------------------------------------------------ the loginctl reader


def fake_loginctl(monkeypatch, *, stdout: str = "", returncode: int = 0, raises=None) -> list:
    calls: list = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(lock.subprocess, "run", run)
    return calls


def test_the_locked_hint_is_read_with_an_argument_vector_and_no_shell(monkeypatch):
    calls = fake_loginctl(monkeypatch, stdout="yes\n")
    assert lock.logind_locked_hint() is True
    argv, kwargs = calls[0]
    assert argv == ["loginctl", "show-session", "auto", "-p", "LockedHint", "--value"]
    assert not kwargs.get("shell")


def test_the_locked_hint_reads_no_as_unlocked(monkeypatch):
    fake_loginctl(monkeypatch, stdout="no\n")
    assert lock.logind_locked_hint() is False


@pytest.mark.parametrize(
    "broken",
    [
        {"returncode": 1, "stdout": "yes\n"},
        {"stdout": ""},
        {"raises": FileNotFoundError("loginctl")},
        {"raises": subprocess.TimeoutExpired("loginctl", 1.0)},
    ],
)
def test_an_unreadable_locked_hint_is_none_not_a_guess(monkeypatch, broken):
    fake_loginctl(monkeypatch, **broken)
    assert lock.logind_locked_hint() is None
