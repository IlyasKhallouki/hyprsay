"""Grounding an application against the live desktop, and correcting a wrong guess.

The bug this file exists for, reproduced on the owner's machine: Chrome open on
workspace 8, focus elsewhere, "open chrome" started a SECOND Chrome while "go to chrome"
went to the one that was there. Every test below is one sentence of that fix.

The real `Grammar`, the real `normalize` and the real `Lexicon` are used on purpose: all
three are pure, and the bug lived in exactly the seam between them. Nothing here reads a
compositor, a bus or the network, and a scripted evaluator fails the test if it is called
at all, since "open chrome" with chrome open must never leave the machine.
"""

import asyncio

import pytest

from hyprsay.config import Config
from hyprsay.lexicon import Lexicon
from hyprsay.model import App, DesktopState, Intent, Transcript, Verdict, Window
from hyprsay.nlu import bank, ground
from hyprsay.nlu.grammar import Grammar
from hyprsay.nlu.normalize import normalize
from hyprsay.nlu.understand import Understander

# --------------------------------------------------------------------------- fixtures


def make_app(id, name, kind, *classes, trusted=True) -> App:
    return App(
        id=id,
        name=name,
        kind=kind,
        wm_classes=classes or (id,),
        trusted=trusted,
        exec_argv=(id,),
    )


CHROME = make_app("google-chrome", "Google Chrome", "web browser", "Google-chrome", "google-chrome")
FIREFOX = make_app("firefox", "Firefox", "web browser")
SPOTIFY = make_app("spotify", "Spotify", "music player", "Spotify")
KITTY = make_app("kitty", "kitty", "terminal")
OBSIDIAN = make_app("obsidian", "Obsidian", "note taking")
DISCORD = make_app("discord", "Discord", "chat")
APPS = (CHROME, FIREFOX, SPOTIFY, KITTY, OBSIDIAN, DISCORD)
LEXICON = Lexicon(APPS)
CFG = Config()


def win(address, cls, rank, title="", workspace=1, pid=0, initial="") -> Window:
    return Window(
        address=address,
        cls=cls,
        initial_class=initial or cls,
        title=title,
        workspace_id=workspace,
        workspace_name=str(workspace),
        monitor=0,
        pid=pid,
        focus_rank=rank,
    )


# the owner's own desktop, reduced: the terminal is focused on workspace 6, chrome is on
# workspace 8, and the Chrome PWA shares chrome's pid, which is why pid is not evidence
TERMINAL = win("0x1", "kitty", 0, "zsh", workspace=6, pid=100)
CHROME_8 = win("0x2", "google-chrome", 1, "GitHub", workspace=8, pid=1325064)
PWA = win("0x3", "chrome-agimnkijcaahngcdmfeangaknmldooml-Profile_2", 2, "Mail", 11, pid=1325064)
NOTES = win("0x4", "obsidian", 3, "Meeting notes", workspace=2, pid=200)


def desktop(*windows, active="0x1", workspace=6) -> DesktopState:
    return DesktopState(
        windows=windows or (TERMINAL, CHROME_8, PWA, NOTES),
        active_address=active,
        active_workspace_id=workspace,
        locked=False,
    )


DESKTOP = desktop()


class Forbidden:
    """An evaluator that fails the test if anything asks it a question."""

    def __init__(self) -> None:
        self.calls = 0

    async def evaluate(self, state, questions):
        self.calls += 1
        raise AssertionError(f"Jev was asked {list(questions)} and should not have been")


def build(lexicon=LEXICON, cfg=CFG):
    evaluator = Forbidden()
    return Understander(cfg, lexicon, Grammar(), evaluator), evaluator


def say(understander, text, state=DESKTOP, **kwargs):
    return asyncio.run(understander.understand(Transcript(text, "test"), state, **kwargs))


# --------------------------------------------------------------------------- matching


def test_a_window_is_matched_to_its_app_by_class_and_never_by_title():
    hostile = win("0x9", "firefox", 5, "Spotify - Premium - Spotify", workspace=1)
    state = desktop(TERMINAL, hostile)
    assert ground.windows_of(SPOTIFY, state, LEXICON) == ()
    assert ground.windows_of(FIREFOX, state, LEXICON) == (hostile,)


def test_a_window_is_matched_by_its_initial_class_too():
    renamed = win("0x9", "", 5, "", workspace=1, initial="google-chrome")
    assert ground.windows_of(CHROME, desktop(renamed), LEXICON) == (renamed,)


def test_windows_that_only_share_a_pid_are_not_the_same_application():
    """Measured on the owner's machine: `hyprctl -j clients` gives a Chrome PWA and both
    google-chrome windows one pid, 1325064. For Chromium-family applications a pid is not
    weak evidence, it is false evidence, so it is not read here at all."""
    assert CHROME_8.pid == PWA.pid
    assert ground.windows_of(CHROME, DESKTOP, LEXICON) == (CHROME_8,)


def test_matched_windows_come_back_most_recently_used_first():
    older = win("0xa", "google-chrome", 7, workspace=3)
    state = desktop(TERMINAL, older, CHROME_8)
    assert ground.windows_of(CHROME, state, LEXICON) == (CHROME_8, older)


def test_an_untrusted_desktop_entry_cannot_claim_a_window_for_a_trusted_app():
    impostor = make_app("google-chrome", "Google Chrome", "web browser", "evilterm", trusted=False)
    lexicon = Lexicon((impostor, KITTY))
    assert ground.windows_of(CHROME, desktop(win("0xb", "evilterm", 0)), lexicon) == ()


# ----------------------------------------------------------------------- the class grade


def test_a_window_the_lexicon_routes_home_is_the_only_kind_that_is_confirmed():
    found = ground.matches(CHROME, DESKTOP, LEXICON)
    assert found.every == (CHROME_8,)
    assert found.confirmed == (CHROME_8,)
    assert found.class_only == ()


def test_a_class_no_trusted_entry_claims_is_matched_but_never_confirmed():
    """docs/PLAN.md 5.6. A window's class is written by that window's owner, exactly like
    its title, so a class nothing else vouches for is a candidate and not an answer."""
    fake = win("0x9", "Spotify", 0, "Spotify Premium", workspace=6)
    nothing_installed = Lexicon((KITTY,))  # no entry declares the class "Spotify"
    found = ground.matches(SPOTIFY, desktop(TERMINAL, fake), nothing_installed)
    assert found.every == (fake,)
    assert found.confirmed == ()
    assert found.class_only == (fake,)
    assert ground.confirms(fake, SPOTIFY, nothing_installed) is False


def test_a_window_whose_two_class_fields_name_two_apps_is_confirmed_for_neither():
    """`class` says kitty and `initialClass` says google-chrome. Either field is enough
    to match, so this window answers to Chrome; the lexicon reads `class` first and hands
    it to kitty, so the two readings disagree and Chrome gets no vouched-for window."""
    renamed = win("0x9", "kitty", 4, workspace=6, initial="google-chrome")
    state = desktop(TERMINAL, renamed)
    found = ground.matches(CHROME, state, LEXICON)
    assert found.every == (renamed,)
    assert found.confirmed == ()
    assert ground.matches(KITTY, state, LEXICON).confirmed == (TERMINAL, renamed)


def test_a_class_only_match_is_offered_and_never_silently_focused():
    """The security case, at the branch that decides it: a local process that sets its
    class to Spotify must not capture "open spotify" at tier 0, with no badge and no
    countdown, and stop the real Spotify from ever starting. It may be shown as a
    candidate; it may not be the answer."""
    fake = win("0x9", "Spotify", 0, "Spotify Premium", workspace=6)
    nothing_installed = Lexicon((KITTY,))  # nothing trusted declares the class "Spotify"
    reached = ground.reach(
        SPOTIFY, ground.Prefer.EITHER, desktop(TERMINAL, fake), nothing_installed, CFG.gates
    )
    assert reached.verdict is ground.Reached.AMBIGUOUS
    assert reached.window is None
    assert reached.windows == (fake,)
    assert "nothing but its own class" in reached.reason


def test_a_confirmed_window_answers_even_when_a_class_only_one_is_newer():
    """A window two readings agree about is the answer, and a window only its own owner
    vouches for does not get to make that a tie."""
    renamed = win("0x9", "kitty", 0, workspace=6, initial="google-chrome")
    state = desktop(TERMINAL, CHROME_8, renamed, active="0x1")
    found = ground.matches(CHROME, state, LEXICON)
    assert found.class_only == (renamed,)
    reached = ground.reach(CHROME, ground.Prefer.EITHER, state, LEXICON, CFG.gates)
    assert reached.verdict is ground.Reached.AMBIGUOUS
    assert reached.windows == (renamed, CHROME_8)  # both offered, neither acted on alone


# --------------------------------------------------------------------------- preference


@pytest.mark.parametrize(
    ("heard", "expected"),
    [
        ("open chrome", ground.Prefer.EITHER),
        ("launch the file manager", ground.Prefer.EITHER),
        ("go to chrome", ground.Prefer.EXISTING),
        ("show me the terminal", ground.Prefer.EXISTING),
        ("open another chrome", ground.Prefer.NEW),
        ("open a new terminal", ground.Prefer.NEW),
        ("open a second terminal", ground.Prefer.NEW),
        ("start a fresh browser", ground.Prefer.NEW),
        # the owner is French and the cloud recognizer writes French
        ("ouvre une autre fenetre chrome", ground.Prefer.NEW),
        ("lance un nouveau terminal", ground.Prefer.NEW),
        ("", ground.Prefer.NEW),
    ],
)
def test_the_verb_declares_a_preference_instead_of_deciding_the_action(heard, expected):
    assert ground.preference(heard) is expected


def test_a_parse_that_recorded_no_words_declares_nothing_and_means_launch():
    """Only code that saw the words can declare a preference. `Intent.LAUNCH_APP` with
    no utterance behind it is what every caller meant before this module existed."""
    assert ground.preference(()) is ground.Prefer.NEW
    assert ground.preference("") is ground.Prefer.NEW


def test_a_workspace_the_speaker_named_is_a_placement_a_focus_cannot_honour():
    assert ground.preference("open firefox in a new workspace", "empty") is ground.Prefer.NEW
    assert ground.preference("open firefox on workspace 3", "3") is ground.Prefer.NEW


def test_novelty_is_a_closed_list_and_not_a_similarity_score():
    assert ground.preference("open newsboat") is ground.Prefer.EITHER
    assert ground.preference("open seconds") is ground.Prefer.EITHER


@pytest.mark.parametrize(
    "heard", ["open a second chrome", "open a 2 chrome", "open a third chrome", "open 2 chrome"]
)
def test_an_ordinal_the_normalizer_turned_into_a_digit_still_asks_for_another_one(heard):
    """`normalize._numbers` rewrites every ordinal to a digit before the grammar runs, so
    "second" and "third" in the novelty list could never match what an utterance carries.
    The normalized spelling is what has to count."""
    assert ground.preference(normalize(heard).text) is ground.Prefer.NEW


def test_a_digit_is_novelty_only_where_a_novelty_word_would_have_been_spoken():
    """Position, not presence. A workspace number and an application whose name starts
    with a digit are both ordinary utterances and neither asks for a second copy."""
    assert ground.preference(normalize("open chrome on workspace 3").text) is ground.Prefer.EITHER
    assert ground.preference("open 1password") is ground.Prefer.EITHER
    assert ground.preference("go to workspace 2") is ground.Prefer.EXISTING


def test_the_definite_article_points_at_a_window_instead_of_asking_for_another():
    """ "a second chrome" asks for one more; "the second chrome" names one of the ones
    that are already there, and turning that into a launch would be the same bug the
    other way round."""
    assert ground.preference(normalize("open the 2 chrome").text) is ground.Prefer.EITHER


# --------------------------------------------------------------------------- reach


def test_no_window_of_that_app_is_open_so_it_starts_one():
    reached = ground.reach(DISCORD, ground.Prefer.EITHER, DESKTOP, LEXICON, CFG.gates)
    assert reached.verdict is ground.Reached.LAUNCH
    assert "no Discord window is open" in reached.reason


def test_exactly_one_window_is_open_so_it_goes_there():
    reached = ground.reach(CHROME, ground.Prefer.EITHER, DESKTOP, LEXICON, CFG.gates)
    assert reached.verdict is ground.Reached.FOCUS
    assert reached.window is CHROME_8
    assert reached.window.workspace_id == 8


def test_another_one_was_asked_for_so_nothing_open_is_consulted():
    reached = ground.reach(CHROME, ground.Prefer.NEW, DESKTOP, LEXICON, CFG.gates)
    assert reached.verdict is ground.Reached.LAUNCH
    assert reached.windows == ()


def test_the_only_window_of_that_app_being_the_one_you_are_in_means_you_want_another():
    """Reproduced before this branch moved: one kitty window, focused, "open a terminal"
    focused the window the speaker was already typing in and did nothing at all. The
    single-window test ran first, so the EITHER preference never got to decide."""
    only = win("0x5", "kitty", 0, "zsh", workspace=6)
    state = desktop(only, active=only.address)
    reached = ground.reach(KITTY, ground.Prefer.EITHER, state, LEXICON, CFG.gates)
    assert reached.verdict is ground.Reached.LAUNCH
    assert "already in front" in reached.reason
    # a focus verb still means the window that is there, even when it is this one
    assert (
        ground.reach(KITTY, ground.Prefer.EXISTING, state, LEXICON, CFG.gates).verdict
        is ground.Reached.FOCUS
    )


def test_opening_a_terminal_from_inside_the_only_terminal_starts_a_second_one():
    """The same case end to end, which is where the owner met it: "open kitty" said in
    the only kitty window used to be a focus of the window already in front, so the key
    press did nothing anyone could see."""
    only = win("0x5", "kitty", 0, "zsh", workspace=6)
    u, jev = build()
    decision = say(u, "open kitty", desktop(only, active=only.address))
    assert decision.action.intent is Intent.LAUNCH_APP
    assert decision.action.app is KITTY
    assert jev.calls == 0


def test_several_windows_with_one_already_in_front_means_you_want_the_next_one():
    """ "Open a terminal" said in a terminal is a request for another terminal. A focus
    verb means the opposite, so it falls through to the tie rule instead."""
    second = win("0x5", "kitty", 4, "htop", workspace=6)
    state = desktop(TERMINAL, second, active=TERMINAL.address)

    def verdict_for(prefer):
        return ground.reach(KITTY, prefer, state, LEXICON, CFG.gates).verdict

    assert verdict_for(ground.Prefer.EITHER) is ground.Reached.LAUNCH
    assert verdict_for(ground.Prefer.EXISTING) is ground.Reached.AMBIGUOUS


def test_several_windows_and_none_in_front_is_a_tie_for_the_caller_to_show():
    other = win("0x5", "google-chrome", 4, "Docs", workspace=2)
    state = desktop(TERMINAL, CHROME_8, other)
    reached = ground.reach(CHROME, ground.Prefer.EITHER, state, LEXICON, CFG.gates)
    assert reached.verdict is ground.Reached.AMBIGUOUS
    assert reached.windows == (CHROME_8, other)
    assert "pick a number" in reached.reason


# ------------------------------------------------------- the bug, end to end, no network


def test_open_chrome_with_chrome_open_goes_to_it_without_asking_jev():
    """The whole point. Chrome on workspace 8, the speaker on workspace 6: the strategy
    claims this stops needing Jev at all, so the evaluator failing on any call is the
    assertion that matters as much as the verdict."""
    u, jev = build()
    decision = say(u, "open chrome")
    assert (decision.verdict, decision.tier) == (Verdict.ACT, 0)
    assert decision.action.intent is Intent.FOCUS_WINDOW
    assert decision.action.window is CHROME_8
    assert jev.calls == 0


def test_go_to_chrome_still_works_exactly_as_it_did():
    u, jev = build()
    decision = say(u, "go to chrome")
    assert decision.action.intent is Intent.FOCUS_WINDOW
    assert decision.action.window is CHROME_8
    assert jev.calls == 0


def test_the_workspace_follows_the_window_instead_of_dragging_it_here():
    """The window is on workspace 8 and the speaker is on 6. A focus travels to the
    window; carrying a workspace onto the action would mean "bring it here", which is a
    different command at a different tier."""
    u, _ = build()
    decision = say(u, "open obsidian")
    assert decision.action.intent is Intent.FOCUS_WINDOW
    assert decision.action.window is NOTES
    assert decision.action.workspace is None
    assert decision.tier == 0


def test_open_an_app_that_is_not_running_still_launches_it():
    u, jev = build()
    decision = say(u, "open discord")
    assert (decision.verdict, decision.tier) == (Verdict.ACT, 1)
    assert decision.action.intent is Intent.LAUNCH_APP
    assert decision.action.app is DISCORD
    assert jev.calls == 0


def test_open_with_two_windows_of_that_app_offers_the_other_as_a_badge():
    other = win("0x5", "google-chrome", 4, "Docs", workspace=2)
    u, _ = build()
    decision = say(u, "open chrome", desktop(TERMINAL, CHROME_8, other))
    assert decision.verdict is Verdict.ACT_SWAP
    assert decision.action.intent is Intent.FOCUS_WINDOW
    assert decision.action.window is CHROME_8  # the most recently used of the two
    assert [c.window for c in decision.candidates] == [CHROME_8, other]


@pytest.mark.parametrize(
    "heard", ["open another chrome", "open a second chrome", "open a 2 chrome"]
)
def test_asking_for_another_one_launches_even_though_one_is_open(heard):
    """ "a second chrome" reaches `ground.preference` as "a 2 chrome", because the
    normalizer rewrote the ordinal, which is why the last two used to focus the chrome
    that was already open instead of starting the one that was asked for."""
    u, jev = build()
    decision = say(u, heard)
    assert decision.action.intent is Intent.LAUNCH_APP
    assert decision.action.app is CHROME
    assert decision.action.window is None
    assert jev.calls == 0


def test_a_new_workspace_is_still_a_launch_with_the_workspace_kept():
    u, _ = build()
    decision = say(u, "open chrome in a new workspace")
    assert decision.action.intent is Intent.LAUNCH_APP
    assert decision.action.workspace == "empty"


def test_a_named_workspace_is_still_a_launch_with_the_workspace_kept():
    u, _ = build()
    decision = say(u, "open chrome on workspace 3")
    assert decision.action.intent is Intent.LAUNCH_APP
    assert decision.action.workspace == "3"


def test_a_hostile_title_cannot_turn_a_launch_into_a_focus_of_its_own_window():
    """docs/PLAN.md 5.6. The attacker writes the title, so naming another application in
    it must buy nothing: "open spotify" starts Spotify and touches no firefox window."""
    hostile = win("0x9", "firefox", 1, "Spotify - Premium - Spotify", workspace=6)
    u, _ = build()
    decision = say(u, "open spotify", desktop(TERMINAL, hostile))
    assert decision.action.intent is Intent.LAUNCH_APP
    assert decision.action.app is SPOTIFY
    assert decision.action.window is None


def test_reaching_a_window_still_needs_the_utterance_to_name_it():
    """A focus that nothing said corroborates is hints, exactly as it is on the focus
    path: this branch lowers what an utterance costs, so it may not skip that check."""

    class Silent(Lexicon):
        def corroborates(self, utterance, *, app=None, window=None):
            return bool(app) and Lexicon.corroborates(self, utterance, app=app)

    u, _ = build(lexicon=Silent(APPS))
    decision = say(u, "open chrome")
    assert decision.verdict is Verdict.HINTS
    assert decision.candidates[0].window is CHROME_8


# --------------------------------------------------------------------------- corrections


TWO_CHROMES = desktop(TERMINAL, CHROME_8, win("0x5", "google-chrome", 4, "Docs", workspace=2))


def pending(understander, text="open chrome", state=TWO_CHROMES):
    """Act on something that leaves badges up, and hand the badges back."""
    shown = say(understander, text, state)
    assert len(shown.candidates) >= 2, "this fixture only works when two badges are showing"
    return shown


@pytest.mark.parametrize(
    "correction", ["no the other one", "not that one", "the other one", "the other window"]
)
def test_a_correction_re_targets_the_badge_the_swap_did_not_take(correction):
    u, _ = build()
    shown = pending(u)
    decision = say(u, correction, TWO_CHROMES, picking=shown.candidates)
    assert decision.verdict is Verdict.ACT
    assert decision.action.intent is Intent.FOCUS_WINDOW
    assert decision.action.window == shown.candidates[1].window


def test_undo_and_cancel_still_reach_the_machinery_they_always_did():
    u, _ = build()
    shown = pending(u)
    for phrase, intent in (("undo that", Intent.UNDO), ("cancel", Intent.CANCEL)):
        decision = say(u, phrase, TWO_CHROMES, picking=shown.candidates)
        assert (decision.verdict, decision.action.intent) == (Verdict.ACT, intent)


def test_a_correction_is_not_a_command_when_no_badge_is_showing():
    """Said cold, "the other one" names nothing, and a refusal would be worse than the
    semantic path, so these phrases are live only while the pick grammar is."""
    grammar = Grammar()
    from hyprsay.nlu.normalize import normalize

    norm = normalize("the other one", vocabulary=LEXICON.words())
    assert grammar.parse(norm, picking=False) is None
    assert grammar.parse(norm, picking=True).intent is Intent.PICK


def test_a_correction_with_only_one_candidate_says_so_instead_of_acting():
    u, _ = build()
    shown = say(u, "open chrome")  # one chrome window, so one badge at most
    decision = say(u, "the other one", DESKTOP, picking=shown.candidates)
    assert decision.verdict is Verdict.HINTS
    assert "no number 2" in decision.reason


# --------------------------------------------------------------------------- the bank


def test_the_question_bank_no_longer_denies_the_evidence_it_asks_about():
    """It used to tell Jev that launching was "not_for: going to a window that is already
    open" while sending nothing about the desktop. Code answers that now."""
    launch = bank.INTENTS[Intent.LAUNCH_APP]
    assert "going to a window that is already open" not in launch["not_for"]
    assert bank.VERSION >= "2026-09-22"
