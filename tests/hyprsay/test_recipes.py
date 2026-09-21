"""The app-recipe table: what it may press, what it may type, and what it refuses.

Nothing here touches a desktop. `hyprsay.inapp` is written in parallel with this file,
so `execute` is exercised against a fake that answers to the same names.
"""

import pytest

from hyprsay import recipes
from hyprsay.model import Window
from hyprsay.recipes import Fill, Recipe, RecipeError, Step, StepKind


def win(cls="google-chrome", address="0x1", **kw) -> Window:
    return Window(
        address=address,
        cls=cls,
        initial_class=cls,
        title=kw.pop("title", "whatever the page says"),
        workspace_id=kw.pop("workspace_id", 1),
        workspace_name=kw.pop("workspace_name", "1"),
        monitor=kw.pop("monitor", 0),
        **kw,
    )


BROWSER_WINDOW = win()
EDITOR_WINDOW = win(cls="code", address="0x2")
FILES_WINDOW = win(cls="org.gnome.Nautilus", address="0x3")
TERMINAL_WINDOW = win(cls="kitty", address="0x4")


class FakeKeys:
    """Stands in for `hyprsay.inapp`: press, scroll, perform, and a call log."""

    def __init__(self):
        self.calls: list[tuple] = []

    def press(self, keys, window=""):
        return ("press", keys, window)

    def scroll(self, direction, amount, window=""):
        return ("scroll", direction, amount, window)

    def perform(self, gesture, window):
        # a gesture only reaches the keyboard through perform, so only this records
        self.calls.append(gesture)


def names(offered) -> list[str]:
    return [recipe.name for recipe in offered]


def typed(steps) -> list[str]:
    return [step.text for step in steps if step.kind is StepKind.TYPE]


def pressed(steps) -> list[str]:
    return [step.keys for step in steps if step.kind is StepKind.PRESS]


# ----------------------------------------------------------------- the table's shape


def test_every_recipe_is_filed_under_its_own_kind_and_its_own_name():
    for kind, table in recipes.RECIPES.items():
        for name, recipe in table.items():
            assert recipe.name == name
            assert recipe.kind == kind


def test_every_recipe_starts_by_focusing_the_window_it_acts_on():
    for recipe in every_recipe():
        assert recipe.steps[0].kind is StepKind.FOCUS


def test_a_recipe_that_types_is_marked_needs_text_and_is_never_free_to_reverse():
    for recipe in every_recipe():
        types = any(step.kind is StepKind.TYPE for step in recipe.steps)
        assert types is recipe.needs_text
        if types:
            assert recipe.tier >= 1


def test_every_typed_url_is_written_https_by_code():
    for recipe in every_recipe():
        for step in recipe.steps:
            if step.fill in (Fill.HOST, Fill.QUERY):
                assert step.text.startswith("https://")


def test_the_terminal_table_is_empty_on_purpose():
    assert recipes.RECIPES[recipes.TERMINAL] == {}


def test_every_chord_is_one_the_input_layer_can_parse():
    # the strings in this table are only ever as good as hypruse's parse_combo finds
    # them: a typo like "ctrl+pagedown" would fail at the keyboard, not here
    from hypruse.input import parse_combo

    for recipe in every_recipe():
        for keys in pressed(recipe.steps):
            mods, key = parse_combo(keys)
            assert key, f"{recipe.name}: {keys} resolved to modifiers only"


def test_the_browser_table_covers_the_verbs_the_owner_asked_for():
    browser = set(recipes.RECIPES[recipes.BROWSER])
    assert {
        "open_url",
        "go_to_url",
        "search_web",
        "find",
        "new_tab",
        "close_tab",
        "next_tab",
        "previous_tab",
        "back",
        "forward",
        "reload",
        "zoom_in",
        "zoom_out",
    } <= browser


def every_recipe():
    return [recipe for table in recipes.RECIPES.values() for recipe in table.values()]


# --------------------------------------------------------------------- what is offered


def test_a_browser_window_is_offered_the_browser_recipes_and_the_generic_ones():
    offered = names(recipes.for_window(BROWSER_WINDOW, "web browser"))
    assert "open_url" in offered
    assert "scroll_down" in offered


def test_a_recipe_whose_kind_does_not_match_the_window_is_not_offered():
    offered = names(recipes.for_window(EDITOR_WINDOW, "code editor"))
    assert "open_url" not in offered
    assert "new_tab" not in offered
    assert "save" in offered


def test_a_file_manager_is_offered_find_but_nothing_that_saves():
    offered = names(recipes.for_window(FILES_WINDOW, "file manager"))
    assert "find" in offered
    assert "save" not in offered


def test_the_kind_s_own_recipe_shadows_the_generic_one_of_the_same_name():
    offered = recipes.for_window(BROWSER_WINDOW, "web browser")
    finds = [recipe for recipe in offered if recipe.name == "find"]
    assert len(finds) == 1
    assert finds[0].kind == recipes.BROWSER
    # and the generic one is a tier higher, because it is aimed at an app nobody knows
    assert recipes.RECIPES[recipes.GENERIC]["find"].tier > finds[0].tier


def test_an_app_with_no_table_of_its_own_still_gets_the_generic_recipes():
    chat = win(cls="com.rtosta.zapzap", address="0x5")
    offered = names(recipes.for_window(chat, "chat"))
    assert offered == ["find", "scroll_down", "scroll_up", "to_top", "to_bottom"]


def test_an_app_of_unknown_kind_is_offered_nothing_that_presses_a_key():
    offered = recipes.for_window(win(cls="some-appimage", address="0x6"), "")
    assert names(offered) == ["scroll_down", "scroll_up", "to_top", "to_bottom"]
    assert not any(recipe.presses_keys for recipe in offered)


def test_scrolling_to_the_top_is_a_sweep_and_not_a_key():
    steps = recipes.RECIPES[recipes.GENERIC]["to_top"].steps
    assert [step.kind for step in steps] == [StepKind.FOCUS, StepKind.SCROLL]
    assert steps[1].direction == "up"
    assert steps[1].amount == recipes.SWEEP_NOTCHES


# ------------------------------------------------------------------------- terminals


def test_no_recipe_offered_for_a_terminal_presses_a_key():
    offered = recipes.for_window(TERMINAL_WINDOW, "terminal")
    assert offered
    assert not any(recipe.presses_keys for recipe in offered)
    assert not any(recipe.needs_text for recipe in offered)


def test_a_kind_that_calls_a_terminal_an_editor_does_not_unlock_its_keyboard():
    offered = recipes.for_window(TERMINAL_WINDOW, "code editor")
    assert not any(recipe.presses_keys for recipe in offered)


def test_rendering_a_key_sequence_against_a_terminal_is_refused():
    for name in ("find", "save"):
        with pytest.raises(RecipeError, match="terminal runs what it receives"):
            recipes.render(recipes.RECIPES[recipes.EDITOR][name], "hello", TERMINAL_WINDOW)


def test_scrolling_a_terminal_is_still_allowed():
    steps = recipes.render(recipes.RECIPES[recipes.GENERIC]["scroll_down"], None, TERMINAL_WINDOW)
    assert [step.kind for step in steps] == [StepKind.FOCUS, StepKind.SCROLL]


def test_refusal_for_explains_the_terminal_the_unknown_kind_and_the_wrong_kind():
    open_url = recipes.RECIPES[recipes.BROWSER]["open_url"]
    assert "terminal" in recipes.refusal_for(open_url, TERMINAL_WINDOW, "terminal")
    assert "not known" in recipes.refusal_for(open_url, BROWSER_WINDOW, "")
    assert "is for a browser" in recipes.refusal_for(open_url, EDITOR_WINDOW, "code editor")
    assert recipes.refusal_for(open_url, BROWSER_WINDOW, "web browser") == ""


# --------------------------------------------------------------------- rendered steps


def test_opening_a_url_lands_in_a_new_tab_types_the_address_and_presses_enter():
    steps = recipes.render(recipes.RECIPES[recipes.BROWSER]["open_url"], "youtube", BROWSER_WINDOW)
    assert [step.kind for step in steps] == [
        StepKind.FOCUS,
        StepKind.PRESS,
        StepKind.SETTLE,
        StepKind.PRESS,
        StepKind.SETTLE,
        StepKind.TYPE,
        StepKind.PRESS,
    ]
    assert pressed(steps) == ["ctrl+t", "ctrl+l", "enter"]
    assert typed(steps) == ["https://youtube.com/"]


def test_going_to_a_url_uses_the_address_bar_of_the_tab_you_are_on():
    steps = recipes.render(recipes.RECIPES[recipes.BROWSER]["go_to_url"], "github", BROWSER_WINDOW)
    assert pressed(steps) == ["ctrl+l", "enter"]
    assert typed(steps) == ["https://github.com/"]


def test_leaving_the_page_you_are_on_costs_a_tier_more_than_a_new_tab():
    assert recipes.RECIPES[recipes.BROWSER]["go_to_url"].tier == 2
    assert recipes.RECIPES[recipes.BROWSER]["open_url"].tier == 1


def test_a_settle_separates_the_chord_that_opens_a_bar_from_the_text():
    steps = recipes.render(recipes.RECIPES[recipes.BROWSER]["find"], "llama", BROWSER_WINDOW)
    kinds = [step.kind for step in steps]
    assert kinds == [StepKind.FOCUS, StepKind.PRESS, StepKind.SETTLE, StepKind.TYPE]
    assert steps[2].seconds == recipes.BAR_SETTLE_S


def test_find_types_the_words_and_never_presses_enter():
    find = recipes.RECIPES[recipes.FILE_MANAGER]["find"]
    steps = recipes.render(find, "  two   words ", FILES_WINDOW)
    assert typed(steps) == ["two words"]
    assert "enter" not in pressed(steps)


def test_searching_the_web_types_a_search_url_and_never_the_bare_words():
    steps = recipes.render(
        recipes.RECIPES[recipes.BROWSER]["search_web"], "ltt, please!", BROWSER_WINDOW
    )
    (text,) = typed(steps)
    assert text == "https://duckduckgo.com/?q=ltt%2C%20please%21"
    assert "ltt, please!" not in text


def test_searching_youtube_goes_through_youtube_s_own_search_url():
    search = recipes.RECIPES[recipes.BROWSER]["search_youtube"]
    steps = recipes.render(search, "ltt", BROWSER_WINDOW)
    assert typed(steps) == ["https://www.youtube.com/results?search_query=ltt"]


def test_a_query_is_percent_encoded_including_its_punctuation():
    url = recipes.search_url("linus tech tips & 50% / done")
    assert url.endswith("linus%20tech%20tips%20%26%2050%25%20%2F%20done")


def test_a_search_on_a_site_with_no_recipe_is_refused():
    with pytest.raises(RecipeError, match="no search recipe"):
        recipes.search_url("anything", site="askjeeves")


def test_a_search_for_nothing_is_refused():
    with pytest.raises(RecipeError, match="nothing to search for"):
        recipes.search_url("   ")


def test_render_leaves_no_step_still_waiting_for_its_text():
    for recipe in every_recipe():
        steps = recipes.render(recipe, "youtube" if recipe.needs_text else None, BROWSER_WINDOW)
        assert all(step.fill is Fill.NONE for step in steps)


def test_a_recipe_that_needs_text_refuses_an_empty_span():
    with pytest.raises(RecipeError, match="nothing to type"):
        recipes.render(recipes.RECIPES[recipes.BROWSER]["find"], "   ", BROWSER_WINDOW)


def test_words_that_came_with_a_recipe_that_types_nothing_are_simply_not_performed():
    steps = recipes.render(recipes.RECIPES[recipes.BROWSER]["new_tab"], "rm -rf /", BROWSER_WINDOW)
    assert typed(steps) == []
    assert steps == list(recipes.RECIPES[recipes.BROWSER]["new_tab"].steps)


# --------------------------------------------------------------- the host normalizer


@pytest.mark.parametrize(
    ("spoken", "host"),
    [
        ("youtube", "youtube.com"),
        ("YouTube", "youtube.com"),
        ("  github   dot   com  ", "github.com"),
        ("github.com", "github.com"),
        # the table is a lookup and not "append .com", which these two would get wrong
        ("wikipedia", "wikipedia.org"),
        ("gmail", "mail.google.com"),
        ("news dot ycombinator dot com", "news.ycombinator.com"),
        ("my dash site dot org", "my-site.org"),
    ],
)
def test_a_spoken_address_becomes_a_host(spoken, host):
    assert recipes.normalize_host(spoken) == host


@pytest.mark.parametrize(
    ("spoken", "why"),
    [
        ("", "nothing was said"),
        ("   ", "nothing was said"),
        # the one that matters: javascript: typed into an address bar runs in the page
        # that is already open, so a sentence anyone can speak would become code
        ("javascript colon alert one", "never a scheme or a path"),
        ("https colon slash slash github dot com", "never a scheme or a path"),
        ("data colon text slash html comma hello", "never a scheme or a path"),
        ("github dot com slash issues", "never a scheme or a path"),
        ("user at github dot com", "never a scheme or a path"),
        ("github.com/issues", "never a scheme or a path"),
        ("linus tech tips", "sounded like a phrase"),
        ("ltt", "not a site I know"),
        ("xn dash dash 80ak6aa92e dot com", "encoded address is refused"),
        ("192 dot 168 dot 1 dot 1", "no domain ending"),
        ("github dot 3", "no domain ending"),
        ("dash github dot com", "does not look like a web address"),
        ("gіthub dot com", "another alphabet"),
        ("a" * 64 + " dot com", "does not look like a web address"),
    ],
)
def test_a_spoken_address_that_is_not_one_is_refused(spoken, why):
    with pytest.raises(RecipeError, match=why):
        recipes.normalize_host(spoken)


def test_a_host_is_bounded_by_the_cap_on_dictated_text_and_not_by_a_rule_of_its_own():
    assert len(recipes.normalize_host("abcdefghij dot " * 40)) <= recipes.MAX_TEXT_CHARS


def test_a_refused_host_stops_the_whole_recipe_before_any_key_is_sent():
    with pytest.raises(RecipeError):
        recipes.render(recipes.RECIPES[recipes.BROWSER]["open_url"], "javascript colon x", win())


# ------------------------------------------------------------------------ the tiering


def test_a_recipe_that_types_is_raised_to_the_tier_typing_earns_on_that_window():
    find = recipes.RECIPES[recipes.BROWSER]["find"]
    assert find.tier == 1
    assert recipes.tier_for(find, typing_tier=2) == 2


def test_a_recipe_that_types_nothing_keeps_its_own_tier():
    close_tab = recipes.RECIPES[recipes.BROWSER]["close_tab"]
    assert recipes.tier_for(close_tab, typing_tier=2) == 2
    assert recipes.tier_for(recipes.RECIPES[recipes.BROWSER]["next_tab"], typing_tier=2) == 0


def test_save_is_disruptive_because_the_file_on_disk_has_no_undo():
    save = recipes.RECIPES[recipes.EDITOR]["save"]
    assert save.tier == 2
    assert not save.needs_text
    assert "save" in recipes.SPOKEN_VERBS["save"]


def test_a_disruptive_recipe_needs_its_verb_literally_said():
    save = recipes.RECIPES[recipes.EDITOR]["save"]
    assert recipes.verb_said(save, ["save", "this"])
    assert not recipes.verb_said(save, ["do", "the", "thing"])


def test_every_tier_two_recipe_has_words_that_can_authorize_it():
    for recipe in every_recipe():
        if recipe.tier >= 2:
            assert recipes.SPOKEN_VERBS.get(recipe.name), recipe.name


# ------------------------------------------------------------------ what a recipe may be


@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        ({"steps": (Step(StepKind.PRESS, keys="ctrl+s"),)}, "starts by focusing"),
        ({"steps": ()}, "starts by focusing"),
        ({"tier": 4}, "not a tier"),
        (
            {"steps": (Step(StepKind.FOCUS), Step(StepKind.TYPE, text="rm -rf /"))},
            "literals of its own",
        ),
        (
            {"steps": (Step(StepKind.FOCUS), Step(StepKind.TYPE, text="hi", fill=Fill.LITERAL))},
            "no literal in it",
        ),
        (
            {
                "steps": (
                    Step(StepKind.FOCUS),
                    Step(StepKind.TYPE, text="http://{host}/", fill=Fill.HOST),
                )
            },
            "written https:// by code",
        ),
        ({"needs_text": True}, "needs_text does not match"),
    ],
)
def test_a_recipe_the_table_should_never_hold_is_rejected_at_import(kwargs, why):
    fields = {
        "name": "whatever",
        "kind": recipes.GENERIC,
        "steps": (Step(StepKind.FOCUS), Step(StepKind.PRESS, keys="ctrl+s")),
        "tier": 1,
        "needs_text": False,
        "description": "a thing",
    }
    with pytest.raises(ValueError, match=why):
        Recipe(**{**fields, **kwargs})


def test_a_typing_recipe_may_not_be_free_to_reverse():
    with pytest.raises(ValueError, match="never free to reverse"):
        Recipe(
            name="whatever",
            kind=recipes.GENERIC,
            steps=(Step(StepKind.FOCUS), Step(StepKind.TYPE, text="{text}", fill=Fill.LITERAL)),
            tier=0,
            needs_text=True,
            description="a thing",
        )


def test_a_terminal_recipe_may_not_press_a_key():
    with pytest.raises(ValueError, match="runs what it receives"):
        Recipe(
            name="whatever",
            kind=recipes.TERMINAL,
            steps=(Step(StepKind.FOCUS), Step(StepKind.PRESS, keys="ctrl+c")),
            tier=1,
            needs_text=False,
            description="a thing",
        )


# -------------------------------------------------------------------------- performing


def test_each_step_reaches_the_keystroke_module_in_order_and_with_the_window():
    keys = FakeKeys()
    focused, typed_out = [], []
    steps = recipes.render(recipes.RECIPES[recipes.BROWSER]["open_url"], "youtube", BROWSER_WINDOW)
    said = recipes.execute(
        steps,
        BROWSER_WINDOW,
        keys=keys,
        focus=focused.append,
        typist=lambda text, window: typed_out.append((text, window.address)),
    )
    assert focused == [BROWSER_WINDOW]
    assert typed_out == [("https://youtube.com/", "0x1")]
    assert keys.calls == [
        ("press", "ctrl+t", "0x1"),
        ("press", "ctrl+l", "0x1"),
        ("press", "enter", "0x1"),
    ]
    assert said[-1] == "press enter"


def test_scrolling_reaches_the_keystroke_module_with_a_direction_and_an_amount():
    keys = FakeKeys()
    steps = recipes.render(recipes.RECIPES[recipes.GENERIC]["to_bottom"], None, BROWSER_WINDOW)
    recipes.execute(steps, BROWSER_WINDOW, keys=keys, focus=lambda w: None)
    assert keys.calls == [("scroll", "down", recipes.SWEEP_NOTCHES, "0x1")]


def test_a_step_that_was_never_rendered_is_refused_before_anything_is_sent():
    keys = FakeKeys()
    unrendered = list(recipes.RECIPES[recipes.BROWSER]["find"].steps)
    with pytest.raises(RecipeError, match="never rendered"):
        recipes.execute(unrendered, BROWSER_WINDOW, keys=keys, focus=lambda w: None)
    assert keys.calls == [("press", "ctrl+f", "0x1")]


def test_performing_a_recipe_waits_for_the_bar_it_opened(monkeypatch):
    slept = []
    monkeypatch.setattr(recipes.time, "sleep", slept.append)
    steps = recipes.render(recipes.RECIPES[recipes.BROWSER]["find"], "llama", BROWSER_WINDOW)
    recipes.execute(steps, BROWSER_WINDOW, keys=FakeKeys(), focus=lambda w: None, typist=noop)
    assert slept == [recipes.BAR_SETTLE_S]


def noop(*_args, **_kwargs) -> None:
    return None


# ------------------------------------------------------------------------- the HUD line


def test_the_line_for_a_typed_step_counts_the_characters_instead_of_showing_them():
    steps = recipes.render(recipes.RECIPES[recipes.BROWSER]["find"], "my diary", BROWSER_WINDOW)
    lines = [step.describe() for step in steps]
    assert lines == ["focus the window", "press ctrl+f", "wait 0.15s", "type 8 characters"]
    assert not any("diary" in line for line in lines)


def test_an_unrendered_typing_step_says_what_it_is_waiting_for():
    step = recipes.RECIPES[recipes.BROWSER]["open_url"].steps[-2]
    assert step.describe() == "type the host"


# ---------------------------------------------------------------- what the review found


def test_a_chord_is_actually_delivered_not_merely_built():
    """press() builds a gesture; perform() sends it. Calling only the constructor meant
    the find bar never opened and the text went into the document instead."""
    keys = FakeKeys()
    window = win(cls="code", address="0x9")
    steps = recipes.render(recipes.RECIPES["editor"]["find"], "hunter two", window)
    recipes.execute(steps, window, keys=keys, focus=lambda w: None, typist=lambda t, w: None)
    assert ("press", "ctrl+f", "0x9") in keys.calls


def test_typing_in_a_recipe_goes_through_the_executor_and_stops_when_it_refuses():
    """A recipe must not be a second way to reach the keyboard: the executor is where the
    key-down pin, the lock latch and the character cap live."""
    import pytest

    from hyprsay import executor as executor_module
    from hyprsay.model import Outcome

    class Refusing:
        def __init__(self):
            self.seen = []

        def execute(self, action, *, pinned_address=""):
            self.seen.append((action.intent, pinned_address))
            return Outcome(False, "the window changed")

    guard = Refusing()
    executor_module.use(guard)
    try:
        window = win(cls="code", address="0x9")
        steps = recipes.render(recipes.RECIPES["editor"]["find"], "hello", window)
        with pytest.raises(recipes.RecipeError, match="the window changed"):
            recipes.execute(steps, window, keys=FakeKeys())
        assert guard.seen and guard.seen[0][1] == "0x9"  # pinned to the window it authorized
    finally:
        executor_module.use(None)


def test_a_recipe_refuses_outright_when_no_engine_owns_the_guards():
    import pytest

    from hyprsay import executor as executor_module

    executor_module.use(None)
    window = win(cls="code", address="0x9")
    steps = recipes.render(recipes.RECIPES["editor"]["find"], "hello", window)
    with pytest.raises(recipes.RecipeError, match="guards"):
        recipes.execute(steps, window, keys=FakeKeys())
