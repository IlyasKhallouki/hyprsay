"""Cutting an utterance into commands, and refusing to cut the ones that only look cut.

The table below is the specification: an utterance, which of the seams code offered the
model said yes to, and the clauses that come out. Two families of rows matter most, and
they read identically to a regex: "open bits and bytes" is one command whose name carries
" and ", and "type hello and goodbye" is one command whose dictated tail carries it. The
first is settled by the answer, the second never reaches the model at all.
"""

import dataclasses

import pytest

from hyprsay.nlu import bank
from hyprsay.nlu.clauses import MAX_SPLITS, Clause, clauses_for, split_candidates
from hyprsay.nlu.normalize import normalize
from hyprsay.nlu.understand import DICTATION_OPENERS

# (utterance, the candidates the model said separate two commands, the clauses expected)
TABLE: tuple[tuple[str, tuple[int, ...], list[str]], ...] = (
    # nothing to cut: no connective is in the utterance at all
    ("focus firefox", (), ["focus firefox"]),
    ("close this", (), ["close this"]),
    ("switch to workspace 3", (), ["switch to workspace 3"]),
    ("make this bigger", (), ["make this bigger"]),
    ("move this to the left", (), ["move this to the left"]),
    ("open a file manager", (), ["open a file manager"]),
    ("volume 40", (), ["volume 40"]),
    ("bring the browser here", (), ["bring the browser here"]),
    ("toggle fullscreen", (), ["toggle fullscreen"]),
    ("go to the next workspace", (), ["go to the next workspace"]),
    # two commands, one seam
    ("open firefox and move it to workspace 3", (0,), ["open firefox", "move it to workspace 3"]),
    ("open zapzap and move it to workspace 4", (0,), ["open zapzap", "move it to workspace 4"]),
    ("open kitty then move it to workspace 2", (0,), ["open kitty", "move it to workspace 2"]),
    ("focus firefox and close it", (0,), ["focus firefox", "close it"]),
    (
        "switch to workspace 2 and then open a terminal",
        (0,),
        ["switch to workspace 2", "open a terminal"],
    ),
    ("float this and make it bigger", (0,), ["float this", "make it bigger"]),
    ("open chrome after that go to workspace 4", (0,), ["open chrome", "go to workspace 4"]),
    ("mute the sound also close the browser", (0,), ["mute the sound", "close the browser"]),
    (
        "open the file manager and go to workspace 5",
        (0,),
        ["open the file manager", "go to workspace 5"],
    ),
    ("close the terminal and focus firefox", (0,), ["close the terminal", "focus firefox"]),
    ("open firefox and also close kitty", (0,), ["open firefox", "close kitty"]),
    # three commands, two seams
    (
        "open firefox and move it to workspace 3 and make it fullscreen",
        (0, 1),
        ["open firefox", "move it to workspace 3", "make it fullscreen"],
    ),
    (
        "open kitty then float it then make it bigger",
        (0, 1),
        ["open kitty", "float it", "make it bigger"],
    ),
    (
        "switch to workspace 2 and open firefox and then close kitty",
        (0, 1),
        ["switch to workspace 2", "open firefox", "close kitty"],
    ),
    (
        "open chrome and go to workspace 4 after that mute the sound",
        (0, 1),
        ["open chrome", "go to workspace 4", "mute the sound"],
    ),
    (
        "focus firefox also close kitty also lower the volume",
        (0, 1),
        ["focus firefox", "close kitty", "lower the volume"],
    ),
    # the most a single hold of the key may say
    (
        "open firefox and move it to workspace 3 and make it fullscreen and mute the sound",
        (0, 1, 2),
        ["open firefox", "move it to workspace 3", "make it fullscreen", "mute the sound"],
    ),
    # the word is in a name: code offers the seam, the answer declines it
    ("open bits and bytes", (), ["open bits and bytes"]),
    ("focus bits and bytes", (), ["focus bits and bytes"]),
    ("open black and white", (), ["open black and white"]),
    ("open dungeons and dragons", (), ["open dungeons and dragons"]),
    ("open arts and crafts", (), ["open arts and crafts"]),
    ("go to the sound and video workspace", (), ["go to the sound and video workspace"]),
    # a name on one side of a real seam: the second candidate is the only true one
    (
        "open bits and bytes and move it to workspace 3",
        (1,),
        ["open bits and bytes", "move it to workspace 3"],
    ),
    # a dictated tail is opaque: these utterances offer no candidate to say yes to
    ("type hello and goodbye", (), ["type hello and goodbye"]),
    ("say yes and no", (), ["say yes and no"]),
    ("write milk and eggs and bread", (), ["write milk and eggs and bread"]),
    (
        "dictate meet me at five and bring the keys",
        (),
        ["dictate meet me at five and bring the keys"],
    ),
    ("i'll type hello and goodbye", (), ["i'll type hello and goodbye"]),
    ("close this and type hello and goodbye", (0,), ["close this", "type hello and goodbye"]),
    # a comma the speaker paused at, and the same utterance read as two commands
    (
        "open chrome, navigate to youtube and look up ltt",
        (0, 1),
        ["open chrome", "navigate to youtube", "look up ltt"],
    ),
    (
        "open chrome, navigate to youtube and look up ltt",
        (0,),
        ["open chrome", "navigate to youtube and look up ltt"],
    ),
    (
        "open firefox, open kitty, open thunar",
        (0, 1),
        ["open firefox", "open kitty", "open thunar"],
    ),
    ("open firefox, then close kitty", (0,), ["open firefox", "close kitty"]),
    (
        "focus firefox, and move it to workspace 2",
        (0,),
        ["focus firefox", "move it to workspace 2"],
    ),
    # talk, not orders. Past three seams nothing is offered; at three, nothing is taken
    (
        "we went to the store and bought milk and eggs and bread and cheese",
        (),
        ["we went to the store and bought milk and eggs and bread and cheese"],
    ),
    (
        "i was talking to him and then she left and then it rained and then we went home and ate",
        (),
        ["i was talking to him and then she left and then it rained and then we went home and ate"],
    ),
    (
        "yeah i know and then he laughed and i laughed and then we left and also we ate",
        (),
        ["yeah i know and then he laughed and i laughed and then we left and also we ate"],
    ),
    (
        "so she called me and i told her about the thing and then she laughed",
        (),
        ["so she called me and i told her about the thing and then she laughed"],
    ),
)

CONVERSATION = (
    "we went to the store and bought milk and eggs and bread and cheese",
    "i was talking to him and then she left and then it rained and then we went home and ate",
    "yeah i know and then he laughed and i laughed and then we left and also we ate",
)

DICTATED = (
    "type hello and goodbye",
    "say yes and no",
    "write milk and eggs and bread",
    "dictate meet me at five and bring the keys",
    "i'll type hello and goodbye",
    "typing hello and goodbye",
    "said i would call her and then forgot",
)


def cut(utterance: str, taken: tuple[int, ...]) -> list[Clause]:
    """What the caller does: offer the seams, take the ones that answered yes."""
    offered = split_candidates(utterance)
    assert all(i < len(offered) for i in taken), f"{utterance!r} offers no candidate {taken}"
    return clauses_for(utterance, [offered[i] for i in taken])


# --------------------------------------------------------------------------- the table


@pytest.mark.parametrize(("utterance", "taken", "expected"), TABLE)
def test_a_table_of_utterances_splits_into_the_clauses_the_answers_chose(
    utterance, taken, expected
):
    assert [c.text for c in cut(utterance, taken)] == expected


@pytest.mark.parametrize(("utterance", "taken", "expected"), TABLE)
def test_every_clause_is_a_verbatim_slice_of_the_utterance(utterance, taken, expected):
    for clause in cut(utterance, taken):
        assert utterance[clause.start : clause.end] == clause.text


@pytest.mark.parametrize(("utterance", "taken", "expected"), TABLE)
def test_clauses_are_numbered_in_the_order_they_were_spoken(utterance, taken, expected):
    clauses = cut(utterance, taken)
    assert [c.index for c in clauses] == list(range(len(clauses)))
    assert all(a.end <= b.start for a, b in zip(clauses, clauses[1:], strict=False))


# --------------------------------------------------------------------------- candidates


@pytest.mark.parametrize(
    "utterance",
    ["focus firefox", "close this window", "move the browser to workspace 3", "volume up"],
)
def test_an_utterance_without_a_connective_offers_nothing_to_ask_about(utterance):
    assert split_candidates(utterance) == []


def test_a_candidate_is_the_offset_of_the_connective_itself():
    utterance = "open firefox and move it to workspace 3"
    (at,) = split_candidates(utterance)
    assert utterance[at:] == " and move it to workspace 3"


def test_and_then_is_one_candidate_and_not_two():
    assert len(split_candidates("open firefox and then close kitty")) == 1
    assert len(split_candidates("open firefox and also close kitty")) == 1
    assert len(split_candidates("open firefox, and then close kitty")) == 1


def test_a_connective_with_nothing_after_it_is_not_a_seam():
    assert split_candidates("open firefox and") == []
    assert split_candidates("close the terminal and then") == []


def test_a_connective_that_opens_the_utterance_is_not_a_seam():
    assert split_candidates("and then close kitty") == []


def test_exactly_three_candidates_are_still_offered():
    utterance = "so she called me and i told her about the thing and then she laughed and left"
    assert len(split_candidates(utterance)) == MAX_SPLITS


@pytest.mark.parametrize("utterance", CONVERSATION)
def test_more_than_three_candidates_reads_as_conversation_and_offers_none(utterance):
    assert split_candidates(utterance) == []
    assert len(clauses_for(utterance, range(len(utterance)))) == 1


def test_four_clauses_is_the_most_an_utterance_can_become():
    utterance = "open firefox and move it to workspace 3 and make it fullscreen and mute the sound"
    assert len(clauses_for(utterance, split_candidates(utterance))) == MAX_SPLITS + 1


# --------------------------------------------------------------------------- dictation


@pytest.mark.parametrize("utterance", DICTATED)
def test_a_dictated_tail_offers_no_seam_to_cut_it_at(utterance):
    assert split_candidates(utterance) == []


def test_only_the_words_from_the_carrier_verb_on_are_opaque():
    utterance = "close this and type hello and goodbye"
    (at,) = split_candidates(utterance)
    assert at < utterance.index("type")
    kept = [c.text for c in clauses_for(utterance, [at])]
    assert kept == ["close this", "type hello and goodbye"]


def test_a_seam_inside_a_dictated_tail_cannot_be_cut_even_when_the_caller_asks_for_it():
    utterance = "type hello and goodbye"
    assert [c.text for c in clauses_for(utterance, [utterance.index(" and ")])] == [utterance]


def test_the_carrier_verbs_are_the_understander_s_closed_list_and_not_a_second_copy():
    assert "type" in DICTATION_OPENERS and "dictate" in DICTATION_OPENERS
    for opener in sorted(DICTATION_OPENERS):
        assert split_candidates(f"{opener} milk and eggs") == []


# --------------------------------------------------------------------------- the cut


def test_taking_no_candidate_leaves_the_whole_utterance_as_one_clause():
    utterance = "open bits and bytes"
    assert clauses_for(utterance, []) == [Clause(utterance, 0, len(utterance))]


def test_an_offset_nobody_offered_is_ignored_rather_than_cut_at():
    utterance = "open firefox and move it to workspace 3"
    assert [c.text for c in clauses_for(utterance, [5, 999, -1])] == [utterance]


def test_an_utterance_of_nothing_becomes_no_clauses():
    assert clauses_for("", []) == []
    assert clauses_for("   ", [1]) == []


def test_a_clause_cannot_be_edited_once_it_is_made():
    clause = clauses_for("focus firefox", [])[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        clause.text = "close firefox"


def test_a_normalized_utterance_may_be_passed_instead_of_its_text():
    norm = normalize("Open Firefox and move it to workspace 3.")
    clauses = clauses_for(norm, split_candidates(norm))
    assert [c.text for c in clauses] == ["open firefox", "move it to workspace 3"]
    assert norm.text[clauses[1].start : clauses[1].end] == clauses[1].text


def test_the_normalizer_drops_the_comma_the_speaker_paused_at():
    """The owner's own utterance. A comma seam exists only if raw text is passed in."""
    norm = normalize("Open Chrome, navigate to youtube and look up ltt")
    assert "," not in norm.text
    assert len(split_candidates(norm)) == 1


# --------------------------------------------------------------------------- binding


def test_it_and_that_and_there_point_back_at_the_clause_before():
    utterance = "open firefox and move it to workspace 3 and put that there"
    clauses = cut(utterance, (0, 1))
    assert [c.back_reference for c in clauses] == ["", "it", "that"]
    assert [c.refers_back for c in clauses] == [False, True, True]


def test_a_clause_that_names_its_own_target_points_back_at_nothing():
    clauses = cut("open firefox and close kitty", (0,))
    assert [c.back_reference for c in clauses] == ["", ""]


def test_a_pronoun_in_the_first_clause_is_not_a_back_reference():
    assert cut("close it and open firefox", (0,))[0].back_reference == ""
    assert clauses_for("close it", [])[0].back_reference == ""


def test_this_and_here_stay_with_the_window_pinned_at_key_down():
    clauses = cut("open firefox and make this fullscreen", (0,))
    assert [c.back_reference for c in clauses] == ["", ""]
    assert cut("open firefox and bring it here", (0,))[1].back_reference == "it"


# --------------------------------------------------------------------------- the question


def test_the_separates_question_carries_the_word_and_both_sides_of_it():
    question = bank.separates("and", "open firefox", "move it to workspace 3")
    assert question.instructions["word"] == "and"
    assert question.instructions["before"] == "open firefox"
    assert question.instructions["after"] == "move it to workspace 3"


def test_the_separates_question_tells_a_second_command_from_a_name_or_dictated_text():
    question = bank.separates("and", "open bits", "bytes")
    assert "action word" in question.true
    assert "name of an application" in question.false and "dictating" in question.false


def test_the_question_bank_version_moved_with_the_question_that_was_added():
    assert bank.VERSION >= "2026-09-21.4"
