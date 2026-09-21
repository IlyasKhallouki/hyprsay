"""The normalizer, held to what the recognizers really wrote.

The transcripts quoted here are from docs/research/live/stt_noise_out.json: eight
commands, five recognizers, four audio conditions, recorded on this machine.
"""

import difflib
import sys
import types

import pytest

from hyprsay.nlu.normalize import Normalized, Token, normalize

VOCABULARY = ("firefox", "kitty", "obsidian", "spotify", "Visual Studio Code", "openoffice")


@pytest.fixture(autouse=True)
def _no_lexicon(monkeypatch):
    """Pin the similarity to the difflib fallback, whatever state hyprsay.lexicon is in."""
    monkeypatch.setitem(sys.modules, "hyprsay.lexicon", None)


def _lexicon_scoring(monkeypatch, scores):
    """A stand-in hyprsay.lexicon whose similarity is a lookup table over difflib."""

    def similarity(a, b):
        return scores.get((a, b), difflib.SequenceMatcher(None, a, b).ratio())

    fake = types.ModuleType("hyprsay.lexicon")
    fake.similarity = similarity
    monkeypatch.setitem(sys.modules, "hyprsay.lexicon", fake)


def text(raw, vocabulary=VOCABULARY, aliases=None):
    return normalize(raw, vocabulary, aliases).text


# --------------------------------------------------------------------------- surface


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Switch to Workspace 3.", "switch to workspace 3"),
        ("Focus, Kitty.", "focus kitty"),
        ("Open Firefox.", "open firefox"),
        ("Close this window.", "close this window"),
        ("What can I say?", "what can i say"),
        ("  volume   UP!  ", "volume up"),
        ("full-screen", "fullscreen"),
    ],
)
def test_case_and_recognizer_punctuation_carry_nothing(raw, expected):
    assert text(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("please open firefox", "open firefox"),
        ("open firefox please", "open firefox"),
        ("can you open firefox", "open firefox"),
        ("Could you please close this?", "close this"),
        ("hey, can you, uh, focus kitty", "focus kitty"),
        ("okay um switch to workspace 3", "switch to workspace 3"),
        ("i want you to mute", "mute"),
        ("volume up, thank you", "volume up"),
        ("just close it for me", "close it"),
    ],
)
def test_fillers_and_politeness_are_dropped(raw, expected):
    assert text(raw) == expected


def test_a_transcript_that_is_only_politeness_normalizes_to_nothing():
    norm = normalize("Okay, thanks.")
    assert norm.text == ""
    assert norm.tokens == ()


def test_politeness_is_only_stripped_from_the_edges_of_the_command():
    # "okay" and "hey" are words once the command has started
    assert text("type okay see you") == "type okay see you"
    assert text("say hey there") == "say hey there"


# --------------------------------------------------------------------------- numbers


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Switch to workspace three.", "switch to workspace 3"),
        ("Switch to Workspace Three.", "switch to workspace 3"),
        ("Move window to workspace two.", "move window to workspace 2"),
        ("workspace ten", "workspace 10"),
        ("set volume to forty percent", "set volume to 40 percent"),
        ("volume twenty five", "volume 25"),
        ("volume one hundred", "volume 100"),
        ("set volume to a hundred percent", "set volume to 100 percent"),
        ("set volume to 40%", "set volume to 40 percent"),
        ("go to the third workspace", "go to the 3 workspace"),
        ("the 3rd workspace", "the 3 workspace"),
        ("volume zero", "volume 0"),
    ],
)
def test_number_words_and_ordinals_become_digits(raw, expected):
    assert text(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # moonshine-tiny at 10 dB SNR
        ("Move window to workspace too.", "move window to workspace 2"),
        ("switch to workspace to", "switch to workspace 2"),
        ("workspace for", "workspace 4"),
        ("desktop for please", "desktop 4"),
        ("send firefox to for", "send firefox to 4"),
        ("move this to too", "move this to 2"),
        ("number to", "number 2"),
        ("workspace won", "workspace 1"),
        ("volume for percent", "volume 4 percent"),
        # alone, a homophone can only be an answer to numbered badges
        ("to", "2"),
        ("For.", "4"),
    ],
)
def test_a_homophone_in_a_number_slot_is_a_number(raw, expected):
    assert text(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("switch to workspace 3", "switch to workspace 3"),
        ("move this to workspace 2", "move this to workspace 2"),
        ("switch workspace to three", "switch workspace to 3"),
        ("go to the terminal", "go to the terminal"),
        ("what is this for", "what is this for"),
        ("where do i go to", "where do i go to"),
        ("type a note to self", "type a note to self"),
        ("i ate the free tree", "i ate the free tree"),
    ],
)
def test_a_homophone_anywhere_else_stays_a_word(raw, expected):
    assert text(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("the second one", "the 2 one"),
        ("close this one", "close this one"),
        ("focus the other one", "focus the other one"),
        ("workspace one", "workspace 1"),
        ("send it to one", "send it to 1"),
        ("number one", "number 1"),
        ("one", "1"),
        ("volume twenty one", "volume 21"),
    ],
)
def test_one_is_a_digit_only_where_a_number_is_expected(raw, expected):
    assert text(raw) == expected


# --------------------------------------------------------------------------- split words


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # parakeet-110m at 3 dB SNR
        ("Switch to work space three.", "switch to workspace 3"),
        ("Move window to work space two.", "move window to workspace 2"),
        # the majority spelling on all five backends
        ("Toggle full screen.", "toggle fullscreen"),
        ("open fire fox", "open firefox"),
        ("un mute", "unmute"),
        ("never mind", "nevermind"),
    ],
)
def test_split_words_are_joined_when_the_joined_form_is_known(raw, expected):
    assert text(raw) == expected


def test_words_are_not_joined_into_something_nobody_knows():
    assert text("open fire fox", vocabulary=()) == "open fire fox"


def test_a_command_word_is_never_swallowed_by_an_app_name():
    # "openoffice" is installed, and "open office" is still a verb and a name
    assert text("open office") == "open office"


def test_words_of_a_multi_word_entry_are_not_joined():
    assert text("visual studio code") == "visual studio code"
    assert text("focus visual studio code") == "focus visual studio code"


# --------------------------------------------------------------------------- aliases


def test_a_single_word_alias_is_applied():
    assert text("focus the browser", aliases={"browser": "firefox"}) == "focus the firefox"


def test_an_alias_may_expand_to_several_words_that_share_the_spoken_span():
    norm = normalize("focus editor", aliases={"editor": "Visual Studio Code"})
    assert norm.text == "focus visual studio code"
    assert [(t.start, t.end) for t in norm.tokens[1:]] == [(6, 12)] * 3


def test_aliases_are_not_applied_recursively():
    assert text("open a", aliases={"a": "b", "b": "c"}) == "open b"


# --------------------------------------------------------------------------- offsets


@pytest.mark.parametrize(
    "raw",
    [
        "Switch to Workspace 3.",
        "hey, can you please open fire fox?",
        "Move window to work space twenty five.",
        "Type Hello World, it's 5 PM!",
        "set volume to 40%",
    ],
)
def test_tokens_spell_the_text_and_point_into_the_raw_transcript(raw):
    norm = normalize(raw, VOCABULARY)
    assert " ".join(t.text for t in norm.tokens) == norm.text
    assert norm.raw == raw
    starts = [t.start for t in norm.tokens]
    assert starts == sorted(starts)
    assert all(0 <= t.start < t.end <= len(raw) for t in norm.tokens)


def test_a_plain_token_is_its_raw_slice_lowercased():
    raw = "Focus, Kitty."
    assert [(t.text, raw[t.start : t.end]) for t in normalize(raw).tokens] == [
        ("focus", "Focus"),
        ("kitty", "Kitty"),
    ]


def test_a_joined_or_converted_token_spans_everything_it_replaced():
    raw = "Open Fire Fox on work space Twenty Five"
    spans = {t.text: raw[t.start : t.end] for t in normalize(raw, VOCABULARY).tokens}
    assert spans["firefox"] == "Fire Fox"
    assert spans["workspace"] == "work space"
    assert spans["25"] == "Twenty Five"


def test_dictated_words_can_be_cut_from_the_raw_with_their_casing():
    raw = "Please type Hello World"
    norm = normalize(raw)
    assert norm.text == "type hello world"
    assert raw[norm.tokens[1].start : norm.tokens[-1].end] == "Hello World"


# --------------------------------------------------------------------------- variants


def test_a_misheard_command_word_is_offered_as_a_variant_not_rewritten():
    # parakeet-110m through a bad laptop microphone
    norm = normalize("Taggle full screen.")
    assert norm.text == "taggle fullscreen"
    assert norm.variants == ("toggle fullscreen",)


def test_a_misheard_app_name_is_offered_as_a_variant(monkeypatch):
    # moonshine-tiny through a bad laptop microphone
    _lexicon_scoring(monkeypatch, {("firefuck", "firefox"): 0.86})
    norm = normalize("Open firefuck.", VOCABULARY)
    assert norm.text == "open firefuck"
    assert norm.variants == ("open firefox",)


def test_the_real_lexicon_similarity_repairs_the_measured_misses(monkeypatch):
    monkeypatch.delitem(sys.modules, "hyprsay.lexicon")
    pytest.importorskip("hyprsay.lexicon")
    assert normalize("Open firefuck.", VOCABULARY).variants == ("open firefox",)
    assert normalize("Taggle full screen.").variants == ("toggle fullscreen",)
    assert normalize("focus kiddy", VOCABULARY).variants == ("focus kitty",)


def test_similarity_falls_back_to_difflib_when_the_lexicon_is_missing():
    # plain difflib scores firefuck/firefox at 0.67, below the threshold
    assert normalize("Open firefuck.", VOCABULARY).variants == ()
    assert normalize("focus spotifi", VOCABULARY).variants == ("focus spotify",)


def test_a_similarity_below_the_threshold_is_not_a_variant(monkeypatch):
    _lexicon_scoring(monkeypatch, {("firefuck", "firefox"): 0.79})
    assert normalize("open firefuck", VOCABULARY).variants == ()


def test_at_most_three_variants_best_first(monkeypatch):
    _lexicon_scoring(
        monkeypatch,
        {
            ("kiddy", "kitty"): 0.85,
            ("firefuck", "firefox"): 0.95,
            ("spotifi", "spotify"): 0.9,
            ("obsidiam", "obsidian"): 0.81,
        },
    )
    norm = normalize("kiddy firefuck spotifi obsidiam", VOCABULARY)
    assert norm.variants == (
        "kiddy firefox spotifi obsidiam",
        "kiddy firefuck spotify obsidiam",
        "kitty firefuck spotifi obsidiam",
    )


def test_each_variant_replaces_exactly_one_token(monkeypatch):
    _lexicon_scoring(monkeypatch, {("kiddy", "kitty"): 0.9, ("firefuck", "firefox"): 0.9})
    norm = normalize("move kiddy next to firefuck", VOCABULARY)
    for variant in norm.variants:
        changed = [a for a, b in zip(norm.text.split(), variant.split(), strict=True) if a != b]
        assert len(changed) == 1


def test_known_words_and_short_words_are_left_alone(monkeypatch):
    _lexicon_scoring(monkeypatch, {("kit", "kitty"): 0.99, ("firefox", "firebox"): 0.99})
    assert normalize("kit", VOCABULARY).variants == ()
    assert normalize("open firefox", (*VOCABULARY, "firebox")).variants == ()


@pytest.mark.parametrize("heard", ["clos firefox", "look", "tipe hello", "quitt spotify"])
def test_a_near_miss_is_never_corrected_toward_a_disruptive_verb(monkeypatch, heard):
    verbs = ("close", "lock", "type", "quit")
    scores = {(heard.split()[0], verb): 0.99 for verb in verbs}
    _lexicon_scoring(monkeypatch, scores)
    for variant in normalize(heard, VOCABULARY).variants:
        assert variant.split()[0] not in verbs


def test_the_hopeless_transcripts_gain_no_command_reading():
    # moonshine-tiny and grok-stt through a bad laptop microphone, both for
    # "toggle full screen"
    assert "fullscreen" not in " ".join(normalize("Toggle folks, Korean.").variants)
    assert "fullscreen" not in " ".join(normalize("Toggle both screens.").variants)


def test_vocabulary_may_be_any_iterable_and_desktop_strings_are_split_into_words(monkeypatch):
    _lexicon_scoring(monkeypatch, {("mozila", "mozilla"): 0.9})
    norm = normalize("focus mozila", iter(["org.mozilla.Firefox"]))
    assert norm.variants == ("focus mozilla",)


def test_the_result_is_frozen_and_typed():
    norm = normalize("focus kitty")
    assert isinstance(norm, Normalized)
    assert norm.tokens[0] == Token("focus", 0, 5)
    with pytest.raises(AttributeError):
        norm.text = "close kitty"
