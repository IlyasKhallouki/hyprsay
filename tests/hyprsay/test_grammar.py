"""The grammar, held to a table of things people say and to what recognizers wrote.

Every expectation is a whole `Slots`, not a subset, so a rule that starts filling a
slot it should not is caught here.
"""

import json
import sys
import time
import types
from pathlib import Path

import pytest

from hyprsay.model import Direction, Intent, Parse, Slots
from hyprsay.nlu.grammar import Grammar
from hyprsay.nlu.normalize import Normalized, normalize

VOCABULARY = ("firefox", "kitty", "obsidian", "spotify", "thunar")
NOISE_STUDY = Path(__file__).parents[2] / "docs/research/live/stt_noise_out.json"

GRAMMAR = Grammar()
THIS = Slots(deictic=True)


@pytest.fixture(autouse=True)
def _no_lexicon(monkeypatch):
    """Pin variants to the difflib fallback, whatever state hyprsay.lexicon is in."""
    monkeypatch.setitem(sys.modules, "hyprsay.lexicon", None)


def parse(raw, *, picking=False, aliases=None):
    return GRAMMAR.parse(normalize(raw, VOCABULARY, aliases), picking=picking)


def ws(number, **slots):
    return Slots(workspace=str(number), **slots)


UTTERANCES = [
    # ----------------------------------------------------------- switch workspace
    ("workspace 3", Intent.SWITCH_WORKSPACE, ws(3)),
    ("Go to workspace 3.", Intent.SWITCH_WORKSPACE, ws(3)),
    ("desktop 3", Intent.SWITCH_WORKSPACE, ws(3)),
    ("Switch to Workspace Three.", Intent.SWITCH_WORKSPACE, ws(3)),
    ("next workspace", Intent.SWITCH_WORKSPACE, ws("next")),
    ("previous workspace", Intent.SWITCH_WORKSPACE, ws("previous")),
    ("go to the next desktop", Intent.SWITCH_WORKSPACE, ws("next")),
    ("go to the third workspace", Intent.SWITCH_WORKSPACE, ws(3)),
    ("go to 5", Intent.SWITCH_WORKSPACE, ws(5)),
    ("workspace ten", Intent.SWITCH_WORKSPACE, ws(10)),
    ("switch workspace to four", Intent.SWITCH_WORKSPACE, ws(4)),
    ("take me to desktop two", Intent.SWITCH_WORKSPACE, ws(2)),
    ("workspace number seven", Intent.SWITCH_WORKSPACE, ws(7)),
    ("can you switch to workspace 9 please", Intent.SWITCH_WORKSPACE, ws(9)),
    # ----------------------------------------------------------- move to workspace
    ("move this to workspace 2", Intent.MOVE_TO_WORKSPACE, ws(2, deictic=True)),
    ("send firefox to 3", Intent.MOVE_TO_WORKSPACE, ws(3, window_ref="firefox")),
    (
        "put the browser on workspace three",
        Intent.MOVE_TO_WORKSPACE,
        ws(3, window_ref="the browser"),
    ),
    ("Move window to workspace two.", Intent.MOVE_TO_WORKSPACE, ws(2, deictic=True)),
    ("move it to the next workspace", Intent.MOVE_TO_WORKSPACE, ws("next", deictic=True)),
    ("send kitty over to desktop 4", Intent.MOVE_TO_WORKSPACE, ws(4, window_ref="kitty")),
    ("move to workspace 6", Intent.MOVE_TO_WORKSPACE, ws(6, deictic=True)),
    ("throw spotify onto workspace nine", Intent.MOVE_TO_WORKSPACE, ws(9, window_ref="spotify")),
    (
        "move the terminal to the second workspace",
        Intent.MOVE_TO_WORKSPACE,
        ws(2, window_ref="the terminal"),
    ),
    (
        "send this window to the previous desktop",
        Intent.MOVE_TO_WORKSPACE,
        ws("previous", deictic=True),
    ),
    # ----------------------------------------------------------- focus
    ("focus firefox", Intent.FOCUS_WINDOW, Slots(window_ref="firefox")),
    ("go to the terminal", Intent.FOCUS_WINDOW, Slots(window_ref="the terminal")),
    ("switch to kitty", Intent.FOCUS_WINDOW, Slots(window_ref="kitty")),
    ("show me spotify", Intent.FOCUS_WINDOW, Slots(window_ref="spotify")),
    ("Focus, Kitty.", Intent.FOCUS_WINDOW, Slots(window_ref="kitty")),
    ("bring up the file manager", Intent.FOCUS_WINDOW, Slots(window_ref="the file manager")),
    ("jump to obsidian", Intent.FOCUS_WINDOW, Slots(window_ref="obsidian")),
    ("focus on the browser", Intent.FOCUS_WINDOW, Slots(window_ref="the browser")),
    (
        "switch to the firefox with youtube",
        Intent.FOCUS_WINDOW,
        Slots(window_ref="the firefox with youtube"),
    ),
    # ----------------------------------------------------------- close
    ("close this", Intent.CLOSE_WINDOW, THIS),
    ("close the window", Intent.CLOSE_WINDOW, THIS),
    ("Close this window.", Intent.CLOSE_WINDOW, THIS),
    ("close", Intent.CLOSE_WINDOW, THIS),
    ("close the current window", Intent.CLOSE_WINDOW, THIS),
    ("close that one", Intent.CLOSE_WINDOW, THIS),
    ("close firefox", Intent.CLOSE_WINDOW, Slots(window_ref="firefox")),
    ("quit spotify", Intent.CLOSE_WINDOW, Slots(window_ref="spotify")),
    # ----------------------------------------------------------- fullscreen
    ("fullscreen", Intent.FULLSCREEN, THIS),
    ("Toggle full screen.", Intent.FULLSCREEN, THIS),
    ("Toggle fullscreen.", Intent.FULLSCREEN, THIS),
    ("make it fullscreen", Intent.FULLSCREEN, THIS),
    ("exit full screen", Intent.FULLSCREEN, THIS),
    ("maximize this", Intent.FULLSCREEN, THIS),
    ("make firefox full screen", Intent.FULLSCREEN, Slots(window_ref="firefox")),
    ("fullscreen kitty", Intent.FULLSCREEN, Slots(window_ref="kitty")),
    # ----------------------------------------------------------- floating
    ("float this", Intent.TOGGLE_FLOATING, THIS),
    ("tile it", Intent.TOGGLE_FLOATING, THIS),
    ("toggle floating", Intent.TOGGLE_FLOATING, THIS),
    ("make this float", Intent.TOGGLE_FLOATING, THIS),
    ("float the terminal", Intent.TOGGLE_FLOATING, Slots(window_ref="the terminal")),
    # ----------------------------------------------------------- launch
    ("open firefox", Intent.LAUNCH_APP, Slots(app_ref="firefox")),
    ("launch the file manager", Intent.LAUNCH_APP, Slots(app_ref="the file manager")),
    ("start a terminal", Intent.LAUNCH_APP, Slots(app_ref="a terminal")),
    ("Open Obsidian.", Intent.LAUNCH_APP, Slots(app_ref="obsidian")),
    ("hey can you please open fire fox", Intent.LAUNCH_APP, Slots(app_ref="firefox")),
    ("run spotify", Intent.LAUNCH_APP, Slots(app_ref="spotify")),
    ("fire up kitty", Intent.LAUNCH_APP, Slots(app_ref="kitty")),
    ("open firefox on workspace 3", Intent.LAUNCH_APP, ws(3, app_ref="firefox")),
    # ----------------------------------------------------------- resize
    ("make it bigger", Intent.RESIZE_WINDOW, Slots(deictic=True, verb="grow")),
    ("shrink this a lot", Intent.RESIZE_WINDOW, Slots(deictic=True, verb="shrink", amount=3)),
    ("wider", Intent.RESIZE_WINDOW, Slots(deictic=True, verb="grow", direction=Direction.RIGHT)),
    (
        "narrower",
        Intent.RESIZE_WINDOW,
        Slots(deictic=True, verb="shrink", direction=Direction.RIGHT),
    ),
    (
        "a bit taller",
        Intent.RESIZE_WINDOW,
        Slots(deictic=True, verb="grow", direction=Direction.DOWN, amount=1),
    ),
    (
        "shorter",
        Intent.RESIZE_WINDOW,
        Slots(deictic=True, verb="shrink", direction=Direction.DOWN),
    ),
    (
        "make this slightly smaller",
        Intent.RESIZE_WINDOW,
        Slots(deictic=True, verb="shrink", amount=0),
    ),
    (
        "make firefox bigger all the way",
        Intent.RESIZE_WINDOW,
        Slots(window_ref="firefox", verb="grow", amount=4),
    ),
    ("make it some bigger", Intent.RESIZE_WINDOW, Slots(deictic=True, verb="grow", amount=2)),
    ("grow", Intent.RESIZE_WINDOW, Slots(deictic=True, verb="grow")),
    ("shrink a little", Intent.RESIZE_WINDOW, Slots(deictic=True, verb="shrink", amount=1)),
    # ----------------------------------------------------------- move window
    ("move this left", Intent.MOVE_WINDOW, Slots(deictic=True, direction=Direction.LEFT)),
    (
        "move firefox to the right",
        Intent.MOVE_WINDOW,
        Slots(window_ref="firefox", direction=Direction.RIGHT),
    ),
    ("move left", Intent.MOVE_WINDOW, Slots(deictic=True, direction=Direction.LEFT)),
    ("push this window up", Intent.MOVE_WINDOW, Slots(deictic=True, direction=Direction.UP)),
    ("move it down", Intent.MOVE_WINDOW, Slots(deictic=True, direction=Direction.DOWN)),
    # ----------------------------------------------------------- focus direction
    ("focus left", Intent.FOCUS_DIRECTION, Slots(direction=Direction.LEFT)),
    ("window to the right", Intent.FOCUS_DIRECTION, Slots(direction=Direction.RIGHT)),
    ("go up", Intent.FOCUS_DIRECTION, Slots(direction=Direction.UP)),
    ("focus the window on the left", Intent.FOCUS_DIRECTION, Slots(direction=Direction.LEFT)),
    ("the window below", Intent.FOCUS_DIRECTION, Slots(direction=Direction.DOWN)),
    ("move focus right", Intent.FOCUS_DIRECTION, Slots(direction=Direction.RIGHT)),
    # ----------------------------------------------------------- volume
    ("volume up", Intent.VOLUME, Slots(verb="up")),
    ("louder", Intent.VOLUME, Slots(verb="up")),
    ("a lot louder", Intent.VOLUME, Slots(verb="up", amount=3)),
    ("volume up by ten", Intent.VOLUME, Slots(verb="up", number=10)),
    ("turn it down", Intent.VOLUME, Slots(verb="down")),
    ("quieter", Intent.VOLUME, Slots(verb="down")),
    ("turn the volume down a bit", Intent.VOLUME, Slots(verb="down", amount=1)),
    ("lower the volume", Intent.VOLUME, Slots(verb="down")),
    ("mute", Intent.VOLUME, Slots(verb="mute")),
    ("unmute", Intent.VOLUME, Slots(verb="unmute")),
    ("un mute", Intent.VOLUME, Slots(verb="unmute")),
    ("set volume to 40 percent", Intent.VOLUME, Slots(verb="set", number=40)),
    ("set the volume to 40%", Intent.VOLUME, Slots(verb="set", number=40)),
    ("volume 40", Intent.VOLUME, Slots(verb="set", number=40)),
    ("volume one hundred", Intent.VOLUME, Slots(verb="set", number=100)),
    ("volume zero", Intent.VOLUME, Slots(verb="set", number=0)),
    # ----------------------------------------------------------- media
    ("play", Intent.MEDIA, Slots(verb="play_pause")),
    ("pause", Intent.MEDIA, Slots(verb="play_pause")),
    ("pause the music", Intent.MEDIA, Slots(verb="play_pause")),
    ("next song", Intent.MEDIA, Slots(verb="next")),
    ("next track", Intent.MEDIA, Slots(verb="next")),
    ("skip", Intent.MEDIA, Slots(verb="next")),
    ("previous track", Intent.MEDIA, Slots(verb="previous")),
    # ----------------------------------------------------------- in-app reach
    ("scroll down", Intent.SCROLL, Slots(direction=Direction.DOWN)),
    ("Scroll down.", Intent.SCROLL, Slots(direction=Direction.DOWN)),
    ("scroll up a lot", Intent.SCROLL, Slots(direction=Direction.UP, amount=3)),
    ("scroll the page down", Intent.SCROLL, Slots(direction=Direction.DOWN)),
    ("page down", Intent.PRESS_CHORD, Slots(verb="Page_Down")),
    ("page up", Intent.PRESS_CHORD, Slots(verb="Page_Up")),
    ("top", Intent.PRESS_CHORD, Slots(verb="Home")),
    ("go to the top", Intent.PRESS_CHORD, Slots(verb="Home")),
    ("bottom", Intent.PRESS_CHORD, Slots(verb="End")),
    # a tab or a page step is one chord, so it is named from `inapp.CHORD_TIERS`
    ("new tab", Intent.PRESS_CHORD, Slots(verb="ctrl+t")),
    ("close tab", Intent.PRESS_CHORD, Slots(verb="ctrl+w")),
    ("next tab", Intent.PRESS_CHORD, Slots(verb="ctrl+Tab")),
    ("previous tab", Intent.PRESS_CHORD, Slots(verb="ctrl+shift+Tab")),
    ("back", Intent.PRESS_CHORD, Slots(verb="alt+Left")),
    ("forward", Intent.PRESS_CHORD, Slots(verb="alt+Right")),
    ("reload", Intent.PRESS_CHORD, Slots(verb="ctrl+r")),
    ("reload this page", Intent.PRESS_CHORD, Slots(verb="ctrl+r")),
    ("find hyprland", Intent.RUN_RECIPE, Slots(verb="find", text="hyprland")),
    (
        "search the web for lofi beats",
        Intent.RUN_RECIPE,
        Slots(verb="search_web", text="lofi beats"),
    ),
    ("look up ltt", Intent.RUN_RECIPE, Slots(verb="search_web", text="ltt")),
    (
        "search youtube for lofi",
        Intent.RUN_RECIPE,
        Slots(verb="search_youtube", text="lofi"),
    ),
    ("navigate to youtube", Intent.RUN_RECIPE, Slots(verb="go_to_url", text="youtube")),
    # the normalizer splits "youtube.com" in two; the RAW transcript keeps the dot
    ("go to youtube.com", Intent.RUN_RECIPE, Slots(verb="go_to_url", text="youtube.com")),
    ("click send", Intent.CLICK_CONTROL, Slots(text="send")),
    ("click the send button", Intent.CLICK_CONTROL, Slots(text="the send button")),
    # ----------------------------------------------------------- session words
    ("lock the screen", Intent.LOCK_SCREEN, Slots()),
    ("lock screen", Intent.LOCK_SCREEN, Slots()),
    ("Lock my computer.", Intent.LOCK_SCREEN, Slots()),
    ("undo", Intent.UNDO, Slots()),
    ("undo that", Intent.UNDO, Slots()),
    ("nope", Intent.UNDO, Slots()),
    ("go back", Intent.UNDO, Slots()),
    ("again", Intent.AGAIN, Slots()),
    ("do that again", Intent.AGAIN, Slots()),
    ("repeat", Intent.AGAIN, Slots()),
    ("one more time", Intent.AGAIN, Slots()),
    ("cancel", Intent.CANCEL, Slots()),
    ("never mind", Intent.CANCEL, Slots()),
    ("Nevermind.", Intent.CANCEL, Slots()),
    ("stop", Intent.CANCEL, Slots()),
    ("what can i say", Intent.HELP, Slots()),
    ("What can I say?", Intent.HELP, Slots()),
    ("help", Intent.HELP, Slots()),
]


@pytest.mark.parametrize(("raw", "intent", "slots"), UTTERANCES)
def test_an_utterance_parses_to_its_intent_and_slots(raw, intent, slots):
    parsed = parse(raw)
    assert parsed is not None, raw
    assert (parsed.intent, parsed.slots) == (intent, slots)


def test_the_table_is_big_enough_to_mean_something():
    assert len(UTTERANCES) >= 80
    assert {intent for _, intent, _ in UTTERANCES} >= set(Intent) - {
        Intent.NONE,
        Intent.PICK,
        Intent.TYPE_TEXT,
    }


def test_a_grammar_parse_is_exact_and_carries_both_spellings():
    parsed = parse("Switch to Workspace Three, please.")
    assert parsed == Parse(
        Intent.SWITCH_WORKSPACE,
        Slots(workspace="3"),
        source="grammar",
        confidence=1.0,
        utterance="switch to workspace 3",
        raw="Switch to Workspace Three, please.",
    )


def test_referring_words_are_verbatim_from_the_normalized_text():
    parsed = parse("Focus THE Browser", aliases={"browser": "firefox"})
    assert parsed.slots.window_ref == "the firefox"
    assert parsed.slots.window_ref in parsed.utterance


def test_focus_never_becomes_launch_the_resolver_decides_that():
    assert parse("focus thunar").intent is Intent.FOCUS_WINDOW
    assert parse("focus thunar").slots.app_ref is None


def test_a_launch_needs_a_name():
    assert parse("open this") is None
    assert parse("launch it") is None


# --------------------------------------------------------------------------- real ASR output

# what was said, by position in every list of the noise study
SPOKEN = [
    (Intent.SWITCH_WORKSPACE, Slots(workspace="3")),
    (Intent.LAUNCH_APP, Slots(app_ref="firefox")),
    (Intent.CLOSE_WINDOW, THIS),
    (Intent.MOVE_TO_WORKSPACE, Slots(workspace="2", deictic=True)),
    (Intent.FOCUS_WINDOW, Slots(window_ref="kitty")),
    (Intent.FULLSCREEN, THIS),
    (Intent.LAUNCH_APP, Slots(app_ref="obsidian")),
    # the eighth clip was recorded when scrolling was not something hyprsay did. It is
    # now, and all twenty transcripts of it say "scroll down"
    (Intent.SCROLL, Slots(direction=Direction.DOWN)),
]
# nothing of the command survived in these; the right answer is to ask someone else
HOPELESS = {"Toggle folks, Korean.", "Toggle both screens."}
# the verb survived and the name did not: a literal parse, and the resolver's problem
MISHEARD_NAMES = {"Open firefuck.": Slots(app_ref="firefuck")}


def _study():
    if not NOISE_STUDY.exists():
        return []
    conditions = json.loads(NOISE_STUDY.read_text())
    return [
        pytest.param(heard, SPOKEN[i], id=f"{condition}/{backend}/{i}")
        for condition, backends in conditions.items()
        for backend, transcripts in backends.items()
        for i, heard in enumerate(transcripts)
    ]


@pytest.mark.parametrize(("heard", "spoken"), _study())
def test_every_transcript_of_the_noise_study_parses_or_is_declined(heard, spoken):
    parsed = parse(heard)
    if spoken is None or heard in HOPELESS:
        assert parsed is None
        return
    intent, slots = spoken
    assert parsed is not None, heard
    assert (parsed.intent, parsed.slots) == (intent, MISHEARD_NAMES.get(heard, slots))


@pytest.mark.parametrize(
    ("heard", "intent", "slots"),
    [
        ("Move window to workspace too.", Intent.MOVE_TO_WORKSPACE, ws(2, deictic=True)),
        ("Switch to work space three.", Intent.SWITCH_WORKSPACE, ws(3)),
        ("Move window to work space two.", Intent.MOVE_TO_WORKSPACE, ws(2, deictic=True)),
        ("Move Window to Workspace two.", Intent.MOVE_TO_WORKSPACE, ws(2, deictic=True)),
        ("Taggle full screen.", Intent.FULLSCREEN, THIS),
        ("Focus, Kitty.", Intent.FOCUS_WINDOW, Slots(window_ref="kitty")),
        ("Open firefuck.", Intent.LAUNCH_APP, Slots(app_ref="firefuck")),
    ],
)
def test_the_measured_recognizer_errors_are_repaired(heard, intent, slots):
    parsed = parse(heard)
    assert (parsed.intent, parsed.slots) == (intent, slots)


def test_a_parse_through_a_variant_says_so_in_its_utterance():
    parsed = parse("Taggle full screen.")
    assert parsed.utterance == "toggle fullscreen"
    assert parsed.raw == "Taggle full screen."
    assert parsed.confidence == 1.0


def test_the_literal_reading_wins_over_a_variant():
    norm = Normalized("open firefuck", "Open firefuck.", (), ("open firefox",))
    assert GRAMMAR.parse(norm).slots.app_ref == "firefuck"


# --------------------------------------------------------------------------- variants and safety


def _hand_made(text, *variants):
    return Normalized(text, text, (), variants)


@pytest.mark.parametrize(
    ("heard", "variant"),
    [
        ("clothes this", "close this"),
        ("clothes firefox", "close firefox"),
        ("look the screen", "lock the screen"),
        ("look", "lock"),
        ("tape hello world", "type hello world"),
    ],
)
def test_a_variant_never_supplies_the_verb_of_a_disruptive_operation(heard, variant):
    assert GRAMMAR.parse(_hand_made(heard, variant)) is None


def test_a_variant_may_supply_a_harmless_verb():
    parsed = GRAMMAR.parse(_hand_made("lunch firefox", "launch firefox"))
    assert (parsed.intent, parsed.slots.app_ref) == (Intent.LAUNCH_APP, "firefox")


def test_the_real_lexicon_does_not_turn_look_into_lock(monkeypatch):
    # hyprsay.lexicon scores look/lock and tape/type above 0.9
    monkeypatch.delitem(sys.modules, "hyprsay.lexicon")
    pytest.importorskip("hyprsay.lexicon")
    assert parse("look") is None
    assert parse("look the screen") is None
    assert parse("tape hello world") is None


def test_a_lexicon_that_offers_lock_for_look_is_still_refused(monkeypatch):
    fake = types.ModuleType("hyprsay.lexicon")
    fake.similarity = lambda a, b: 1.0 if (a, b) == ("look", "lock") else 0.0
    monkeypatch.setitem(sys.modules, "hyprsay.lexicon", fake)
    assert GRAMMAR.parse(normalize("look", ("lock",))) is None


# --------------------------------------------------------------------------- dictation


@pytest.mark.parametrize(
    ("raw", "typed"),
    [
        ("type hello world", "hello world"),
        ("Type Hello World.", "Hello World"),
        ("Say: See you at 5 PM!", "See you at 5 PM!"),
        ("dictate Dear Team, the build is green", "Dear Team, the build is green"),
        ("Please type Two tickets to Workspace Three", "Two tickets to Workspace Three"),
        ("write it's done, thanks", "it's done, thanks"),
        ("type please", "please"),
        ("type uh what", "uh what"),
        ("Type, wait...", "wait..."),
    ],
)
def test_dictated_text_is_cut_from_the_raw_transcript(raw, typed):
    parsed = parse(raw)
    assert parsed.intent is Intent.TYPE_TEXT
    assert parsed.slots == Slots(text=typed)


def test_dictated_text_sits_at_its_offsets_in_the_raw_transcript():
    raw = "Okay, type Meet me at Workspace Two"
    norm = normalize(raw, VOCABULARY)
    typed = GRAMMAR.parse(norm).slots.text
    assert norm.tokens[0].text == "type"
    assert raw[norm.tokens[0].end :].strip() == typed == "Meet me at Workspace Two"
    # the normalized spelling never leaks into what is typed
    assert "2" not in typed
    assert "2" in norm.text


@pytest.mark.parametrize("raw", ["type", "Say.", "dictate,"])
def test_a_carrier_verb_with_nothing_after_it_types_nothing(raw):
    assert parse(raw) is None


@pytest.mark.parametrize(
    "raw",
    ["hello world", "i would type hello", "firefox type hello", "the word type is short"],
)
def test_text_is_typed_only_after_a_leading_carrier_verb(raw):
    parsed = parse(raw)
    assert parsed is None or parsed.intent is not Intent.TYPE_TEXT


def test_a_command_inside_dictation_is_dictation():
    parsed = parse("type close firefox")
    assert (parsed.intent, parsed.slots.text) == (Intent.TYPE_TEXT, "close firefox")


def test_a_normalized_built_by_hand_still_dictates_from_the_raw():
    norm = Normalized("type hello world", "Type Hello World.", (), ())
    assert GRAMMAR.parse(norm).slots.text == "Hello World"


# --------------------------------------------------------------------------- picking


@pytest.mark.parametrize(
    ("raw", "number"),
    [
        ("2", 2),
        ("two", 2),
        ("Two.", 2),
        ("to", 2),
        ("for", 4),
        ("number two", 2),
        ("the second one", 2),
        ("the third", 3),
        ("pick 1", 1),
        ("one", 1),
        ("option nine", 9),
    ],
)
def test_while_picking_a_number_is_a_badge(raw, number):
    parsed = parse(raw, picking=True)
    assert (parsed.intent, parsed.slots) == (Intent.PICK, Slots(number=number))


@pytest.mark.parametrize("raw", ["0", "10", "twelve", "number zero", "2 3"])
def test_only_badges_one_to_nine_exist(raw):
    assert parse(raw, picking=True) is None


@pytest.mark.parametrize(
    ("raw", "intent"),
    [("cancel", Intent.CANCEL), ("never mind", Intent.CANCEL), ("undo", Intent.UNDO)],
)
def test_cancel_and_undo_stay_live_while_picking(raw, intent):
    assert parse(raw, picking=True).intent is intent


@pytest.mark.parametrize(
    "raw",
    [raw for raw, intent, _ in UTTERANCES if intent not in {Intent.CANCEL, Intent.UNDO}]
    + ["type hello world", "lock the screen"],
)
def test_nothing_else_is_live_while_picking(raw):
    assert parse(raw, picking=True) is None


@pytest.mark.parametrize("raw", ["2", "two", "number two", "the second one"])
def test_a_bare_number_means_nothing_when_no_badges_are_showing(raw):
    assert parse(raw) is None


# --------------------------------------------------------------------------- declining


@pytest.mark.parametrize(
    "raw",
    [
        "i think the workspace idea is good",
        "can you believe that",
        "what time is it",
        "i want to send an email to john",
        "play that song i like from last summer",
        "the meeting moved to workspace planning next week",
        "open the door and let the dogs out would you",
        "set volume to 150",
        "workspace zero",
        "focus",
        "move this",
        "okay",
        "",
        "   ",
        "...",
    ],
)
def test_what_is_not_a_command_is_declined_not_guessed(raw):
    assert parse(raw) is None


def test_declining_a_long_utterance_is_cheap():
    norm = normalize("so i was telling him about the thing we saw at lunch yesterday", VOCABULARY)
    worst = Normalized(norm.text, norm.raw, norm.tokens, (norm.text + " a", norm.text + " b", "x"))
    assert len(norm.text.split()) == 12
    assert GRAMMAR.parse(worst) is None
    runs = 200
    started = time.perf_counter()
    for _ in range(runs):
        GRAMMAR.parse(worst)
    assert (time.perf_counter() - started) / runs < 0.002


def test_a_long_utterance_that_starts_like_a_command_is_cheap_too():
    norm = _hand_made("move " + "the very long thing " * 3 + "to", "open " + "a b c d e f g " * 2)
    runs = 200
    started = time.perf_counter()
    for _ in range(runs):
        GRAMMAR.parse(norm)
    assert (time.perf_counter() - started) / runs < 0.002


# --------------------------------------------------------------------------- the table itself


def test_every_intent_but_none_is_covered():
    assert set(GRAMMAR.intents()) == set(Intent) - {Intent.NONE}
    assert len(GRAMMAR.intents()) == len(set(GRAMMAR.intents()))


@pytest.mark.parametrize("intent", [i for i in Intent if i is not Intent.NONE])
def test_every_example_parses_to_the_intent_it_illustrates(intent):
    examples = GRAMMAR.examples(intent)
    assert examples
    for example in examples:
        parsed = parse(example, picking=intent is Intent.PICK)
        assert parsed is not None, example
        assert parsed.intent is intent, example


def test_none_has_no_examples():
    assert GRAMMAR.examples(Intent.NONE) == ()
