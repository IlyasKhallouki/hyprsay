"""The lexicon against temp directories and hand-built apps. Nothing here reads the live system.

Files under tmp_path belong to whoever runs the tests, so the real trust predicate
calls every one of them untrusted. Tests that need a "system" directory replace
`lexicon.trusted_location`, and the predicate itself is tested on faked stat results.
"""

import os
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from hyprsay import lexicon
from hyprsay.config import Config, Safety
from hyprsay.lexicon import (
    Lexicon,
    approval_token,
    default_dirs,
    mixed_script,
    parse_desktop_entry,
    parse_exec,
    phonetic_key,
    root_owned_and_closed,
    sanitize,
    similarity,
    trusted_location,
)
from hyprsay.model import App, DesktopState, Window

CYRILLIC_KITTY = "кitty"  # the first letter is CYRILLIC SMALL LETTER KA
ZERO_WIDTH = "​"
RIGHT_TO_LEFT_OVERRIDE = "‮"

FIREFOX = App(
    id="firefox",
    name="Firefox",
    generic_name="Web Browser",
    kind="web browser",
    keywords=("Internet", "WWW", "Web"),
    exec_argv=("/usr/lib/firefox/firefox",),
    wm_classes=("firefox",),
    trusted=True,
)
CHROME = App(
    id="google-chrome",
    name="Google Chrome",
    generic_name="Web Browser",
    kind="web browser",
    exec_argv=("/usr/bin/google-chrome-stable",),
    wm_classes=("Google-chrome", "google-chrome"),
    trusted=True,
)
KITTY = App(
    id="kitty",
    name="kitty",
    generic_name="Terminal emulator",
    kind="terminal",
    exec_argv=("kitty",),
    wm_classes=("kitty",),
    trusted=True,
)
DOLPHIN = App(
    id="org.kde.dolphin",
    name="Dolphin",
    generic_name="File Manager",
    kind="file manager",
    keywords=("files", "explorer"),
    exec_argv=("dolphin",),
    wm_classes=("org.kde.dolphin",),
    trusted=True,
)
VSCODE = App(
    id="code",
    name="Visual Studio Code",
    generic_name="Text Editor",
    kind="code editor",
    exec_argv=("/usr/bin/code",),
    wm_classes=("Code", "code"),
    trusted=True,
)
KATE = App(
    id="org.kde.kate",
    name="Kate",
    generic_name="Advanced Text Editor",
    kind="code editor",
    exec_argv=("kate",),
    wm_classes=("org.kde.kate",),
    trusted=True,
)
AVAHI = App(
    id="bssh",
    name="Avahi SSH Server Browser",
    exec_argv=("bssh",),
    wm_classes=("bssh",),
    trusted=True,
)
# a user-writable entry that says whatever it likes
IMPOSTOR = App(
    id="zenvox",
    name="Firefox Kitty Browser Terminal",
    generic_name="Web Browser",
    kind="web browser",
    keywords=("firefox",),
    exec_argv=("/home/me/.cache/payload",),
    wm_classes=("zenvox",),
    trusted=False,
)
APPS = (FIREFOX, CHROME, KITTY, DOLPHIN, VSCODE, KATE, AVAHI, IMPOSTOR)


def window(cls: str, title: str = "", address: str = "0x1", **kwargs) -> Window:
    base = {"initial_class": cls, "workspace_id": 1, "workspace_name": "1", "monitor": 0}
    return Window(address=address, cls=cls, title=title, **(base | kwargs))


def ids(matches) -> list[str]:
    return [app.id for app, _ in matches]


# --------------------------------------------------------------------------- text


def test_sanitize_folds_compatibility_forms_and_collapses_whitespace():
    assert sanitize("Ｆirefox  \t\n  Nightly ") == "Firefox Nightly"


def test_sanitize_removes_zero_width_bidi_and_control_characters():
    hostile = f"kit{ZERO_WIDTH}ty{RIGHT_TO_LEFT_OVERRIDE}\x07\x1b[31m ⁦x⁩"
    assert sanitize(hostile) == "kitty[31m x"


def test_a_newline_separates_words_instead_of_gluing_them():
    assert sanitize("close\nfirefox now") == "close firefox now"


def test_a_cyrillic_homoglyph_inside_a_latin_word_is_mixed_script():
    assert mixed_script(CYRILLIC_KITTY)
    assert mixed_script("pαypal")  # GREEK SMALL LETTER ALPHA


def test_honest_tokens_are_not_mixed_script():
    assert not mixed_script("kitty")
    assert not mixed_script("k3b")
    assert not mixed_script("café")
    assert not mixed_script("файлы")  # all Cyrillic, no Latin
    assert not mixed_script("")


# --------------------------------------------------------------------------- sound


@pytest.mark.parametrize(
    ("heard", "meant"),
    [("kiddy", "kitty"), ("fire fox", "firefox"), ("krome", "chrome"), ("dolfin", "dolphin")],
)
def test_words_that_sound_alike_share_a_phonetic_key(heard, meant):
    assert phonetic_key(heard) == phonetic_key(meant)


def test_the_phonetic_key_is_a_compact_consonant_skeleton():
    assert phonetic_key("firefox") == "FRFKS"
    assert phonetic_key("obsidian").startswith("A")
    assert phonetic_key("") == ""
    assert phonetic_key("файл") == ""


def test_a_misheard_name_scores_high_and_a_different_word_scores_low():
    assert similarity("firefuck", "firefox") > 0.8
    assert similarity("kiddy", "kitty") >= 0.85
    assert similarity("fire fox", "firefox") == 1.0
    assert similarity("firefox", "files") < 0.6
    assert similarity("chrome", "chromium") < 0.85


def test_similarity_is_bounded_symmetric_and_zero_for_nothing():
    assert similarity("Firefox", "firefox") == 1.0
    assert similarity("", "firefox") == 0.0
    assert similarity(CYRILLIC_KITTY[0], "k") == 0.0
    for a, b in [("firefuck", "firefox"), ("steam", "stream"), ("code", "kodi")]:
        assert 0.0 <= similarity(a, b) <= 1.0
        assert similarity(a, b) == pytest.approx(similarity(b, a))


# --------------------------------------------------------------------------- Exec


def test_exec_is_split_into_arguments_and_field_codes_are_dropped():
    assert parse_exec("/usr/bin/google-chrome-stable %U") == ("/usr/bin/google-chrome-stable",)
    assert parse_exec("app %f %F %u %i %c %k --flag") == ("app", "--flag")


def test_exec_quoting_follows_the_desktop_entry_spec():
    line = '/opt/chrome "--profile-directory=Profile 2" --app-id=abc'
    assert parse_exec(line) == ("/opt/chrome", "--profile-directory=Profile 2", "--app-id=abc")


def test_exec_is_never_expanded_by_a_shell():
    argv = parse_exec('sh -c "echo \\$HOME; rm -rf ~"')
    assert argv == ("sh", "-c", "echo $HOME; rm -rf ~")  # three arguments, not a line


def test_an_argument_that_embeds_a_field_code_is_dropped_whole():
    assert parse_exec("viewer --url=%u --zoom=100%%") == ("viewer", "--zoom=100%")


def test_the_empty_flatpak_forwarding_bracket_is_removed_with_its_field_code():
    line = "/usr/bin/flatpak run --command=zapzap --file-forwarding com.rtosta.zapzap @@u %u @@"
    assert parse_exec(line)[-2:] == ("--file-forwarding", "com.rtosta.zapzap")


def test_an_unreadable_exec_line_yields_no_arguments():
    assert parse_exec('app "unterminated') == ()
    assert parse_exec("") == ()


# --------------------------------------------------------------------------- desktop entries

CHROME_FILE = b"""[Desktop Entry]
Version=1.0
Name=Google Chrome
Name[de]=Google Chrom
GenericName=Web Browser
StartupWMClass=Google-chrome
Exec=/usr/bin/google-chrome-stable %U
StartupWMClass=google-chrome
Type=Application
Categories=Network;WebBrowser;
Keywords=internet;web;

[Desktop Action new-private-window]
Name=New Incognito Window
Exec=/usr/bin/google-chrome-stable --incognito
"""


def entry(name="Thing", exec_line="thing", extra="") -> bytes:
    return f"[Desktop Entry]\nType=Application\nName={name}\nExec={exec_line}\n{extra}".encode()


def test_only_the_main_group_is_read_and_every_wm_class_is_kept():
    app = parse_desktop_entry(CHROME_FILE, "google-chrome", trusted=True, source="/x")
    assert app is not None
    assert app.name == "Google Chrome"
    assert app.exec_argv == ("/usr/bin/google-chrome-stable",)
    assert app.wm_classes == ("Google-chrome", "google-chrome")
    assert app.kind == "web browser"
    assert app.keywords == ("internet", "web")
    assert app.trusted and app.source == "/x"


def test_trust_defaults_to_false():
    assert parse_desktop_entry(entry(), "thing").trusted is False


@pytest.mark.parametrize(
    "data",
    [
        entry(extra="NoDisplay=true\n"),
        entry(extra="Hidden=true\n"),
        b"[Desktop Entry]\nType=Application\nName=No Exec\n",
        b"[Desktop Entry]\nType=Link\nName=A Link\nExec=x\nURL=https://example.com\n",
        b"[Desktop Action x]\nType=Application\nName=Orphan\nExec=x\n",
        b"\xff\xfe not a desktop file",
    ],
)
def test_entries_a_launcher_would_not_list_are_skipped(data):
    assert parse_desktop_entry(data, "thing") is None


def test_a_name_with_invisible_characters_is_stored_clean():
    app = parse_desktop_entry(entry(name=f"Fire{ZERO_WIDTH}fox{RIGHT_TO_LEFT_OVERRIDE}"), "x")
    assert app.name == "Firefox"


@pytest.mark.parametrize(
    ("categories", "generic", "kind"),
    [
        ("Network;WebBrowser;", "", "web browser"),
        ("System;TerminalEmulator;", "Terminal emulator", "terminal"),
        ("System;FileManager;", "", "file manager"),
        ("Audio;Music;Player;AudioVideo;", "", "music player"),
        ("AudioVideo;Audio;Player;", "", "music player"),
        ("AudioVideo;Player;Video;", "", "media player"),
        ("Utility;TextEditor;", "", "code editor"),
        ("Development;IDE;", "", "code editor"),
        ("Network;InstantMessaging;", "", "chat"),
        ("Network;Email;", "", "email client"),
        ("Graphics;Viewer;", "", "image viewer"),
        ("Utility;", "Screenshot Tool", "screenshot tool"),
        ("Utility;", "", ""),
    ],
)
def test_kind_comes_from_categories_then_generic_name(categories, generic, kind):
    extra = f"Categories={categories}\nGenericName={generic}\n"
    assert parse_desktop_entry(entry(extra=extra), "thing").kind == kind


# --------------------------------------------------------------------------- trust


def stat_result(uid: int, mode: int, gid: int = 0) -> os.stat_result:
    return os.stat_result((stat.S_IFREG | mode, 0, 0, 1, uid, gid, 0, 0, 0, 0))


def test_a_root_owned_file_closed_to_group_and_world_is_trusted():
    assert root_owned_and_closed(stat_result(0, 0o644), groups={1000})
    assert root_owned_and_closed(stat_result(0, 0o755), groups={1000})


def test_a_file_the_user_owns_is_never_trusted():
    assert not root_owned_and_closed(stat_result(1000, 0o644), groups={1000})
    assert not root_owned_and_closed(stat_result(1000, 0o444), groups={1000})


def test_a_world_writable_file_is_never_trusted():
    assert not root_owned_and_closed(stat_result(0, 0o666), groups={1000})
    assert not root_owned_and_closed(stat_result(0, 0o646), groups={1000})


def test_group_write_is_tolerated_only_for_group_root_and_only_from_outside_it():
    assert root_owned_and_closed(stat_result(0, 0o775, gid=0), groups={1000, 998})
    assert not root_owned_and_closed(stat_result(0, 0o775, gid=0), groups={1000, 0})
    assert not root_owned_and_closed(stat_result(0, 0o775, gid=998), groups={1000})
    assert not root_owned_and_closed(stat_result(0, 0o664, gid=1000), groups={1000})


def as_root_except(*user_owned: Path):
    """A stat that reports root ownership everywhere except under the given paths."""

    def stat_fn(path):
        real = os.stat(path)
        path = Path(path)
        if any(path == mine or mine in path.parents for mine in user_owned):
            return real
        values = list(real)
        values[stat.ST_MODE] = real.st_mode & ~0o022
        values[stat.ST_UID] = values[stat.ST_GID] = 0
        return os.stat_result(values)

    return stat_fn


def test_a_file_is_trusted_when_it_and_every_directory_above_it_are_roots(tmp_path):
    file = tmp_path / "system/applications/firefox.desktop"
    file.parent.mkdir(parents=True)
    file.write_bytes(entry())
    assert trusted_location(file, stat_fn=as_root_except())
    assert not trusted_location(file, stat_fn=as_root_except(file))
    assert not trusted_location(file, stat_fn=as_root_except(file.parent))
    assert not trusted_location(file, stat_fn=as_root_except(tmp_path))


def test_a_root_owned_symlink_into_a_user_directory_is_not_trusted(tmp_path):
    home = tmp_path / "home"
    system = tmp_path / "system/applications"
    home.mkdir()
    system.mkdir(parents=True)
    (home / "evil.desktop").write_bytes(entry())
    (system / "evil.desktop").symlink_to(home / "evil.desktop")
    assert not trusted_location(system / "evil.desktop", stat_fn=as_root_except(home))


def test_missing_files_and_directories_are_not_trusted(tmp_path):
    (tmp_path / "dir.desktop").mkdir()
    assert not trusted_location(tmp_path / "absent.desktop", stat_fn=as_root_except())
    assert not trusted_location(tmp_path / "dir.desktop", stat_fn=as_root_except())


def test_the_real_predicate_trusts_nothing_under_a_temp_directory(tmp_path):
    file = tmp_path / "applications/firefox.desktop"
    file.parent.mkdir()
    file.write_bytes(entry())
    assert not trusted_location(file)


def test_default_directories_put_the_users_first_and_list_each_once():
    env = {"HOME": "/home/me", "XDG_DATA_DIRS": "/usr/share:/opt/share:relative"}
    dirs = default_dirs(env)
    assert dirs[0] == Path("/home/me/.local/share/applications")
    assert dirs[1] == Path("/home/me/.local/share/flatpak/exports/share/applications")
    assert Path("/opt/share/applications") in dirs
    assert Path("/var/lib/flatpak/exports/share/applications") in dirs
    assert Path("relative/applications") not in dirs
    assert len(set(dirs)) == len(dirs)


# --------------------------------------------------------------------------- scan

SYSTEM_FIREFOX = entry(
    name="Firefox",
    exec_line="/usr/lib/firefox/firefox %u",
    extra="GenericName=Web Browser\nCategories=Network;WebBrowser;\n",
)
EVIL_FIREFOX = entry(name="Firefox", exec_line="/home/me/.cache/payload --quiet %u")


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    """(user, system). Only files under `system` pass the stubbed trust predicate."""
    user, system = tmp_path / "home/applications", tmp_path / "system/applications"
    user.mkdir(parents=True)
    system.mkdir(parents=True)
    monkeypatch.setattr(lexicon, "trusted_location", lambda path: system in path.parents)
    return user, system


def test_scan_trusts_system_entries_and_not_user_entries(dirs):
    user, system = dirs
    (system / "firefox.desktop").write_bytes(SYSTEM_FIREFOX)
    (user / "zenvox.desktop").write_bytes(entry(name="ZenVox", exec_line="zenvox"))
    found = {app.id: app for app in Lexicon.scan(Config(), dirs).apps}
    assert found["firefox"].trusted
    assert found["firefox"].source == str(system / "firefox.desktop")
    assert not found["zenvox"].trusted


def test_without_the_stub_nothing_in_a_temp_directory_is_trusted(tmp_path):
    (tmp_path / "firefox.desktop").write_bytes(SYSTEM_FIREFOX)
    lex = Lexicon.scan(Config(), [tmp_path])
    assert [app.trusted for app in lex.apps] == [False]
    assert lex.match_apps("firefox") == []


def test_a_user_file_shadowing_a_system_id_neither_inherits_trust_nor_hijacks_it(dirs):
    user, system = dirs
    (system / "firefox.desktop").write_bytes(SYSTEM_FIREFOX)
    (user / "firefox.desktop").write_bytes(EVIL_FIREFOX)
    lex = Lexicon.scan(Config(), dirs)

    shadow = next(app for app in lex.apps if app.source == str(user / "firefox.desktop"))
    assert shadow.id == "firefox" and not shadow.trusted

    [(app, score)] = lex.match_apps("firefox")
    assert score == 1.0
    assert app.trusted and app.exec_argv == ("/usr/lib/firefox/firefox",)
    assert lex.app_for_window(window("firefox")) == app
    assert not lex.corroborates("open firefox", app=shadow)
    assert all("payload" not in " ".join(a.exec_argv) for a, _ in lex.match_apps("firefox"))


def test_a_shadow_with_no_system_entry_behind_it_stays_untrusted_and_unmatched(dirs):
    user, _ = dirs
    (user / "firefox.desktop").write_bytes(EVIL_FIREFOX)
    lex = Lexicon.scan(Config(), dirs)
    assert [app.trusted for app in lex.apps] == [False]
    assert lex.match_apps("firefox") == []
    assert lex.kind_of(window("firefox")) == ""


def test_a_user_file_approved_by_hash_is_trusted_and_takes_precedence(dirs):
    user, system = dirs
    custom = entry(name="Firefox", exec_line="firefox --profile work")
    (system / "firefox.desktop").write_bytes(SYSTEM_FIREFOX)
    (user / "firefox.desktop").write_bytes(custom)
    cfg = Config(safety=Safety(approved_desktop_files=(approval_token("firefox", custom),)))
    lex = Lexicon.scan(cfg, dirs)
    assert [(a.trusted, a.exec_argv) for a in lex.apps] == [
        (True, ("firefox", "--profile", "work"))
    ]


def test_an_approval_is_void_once_the_file_changes_or_for_another_id(dirs):
    user, _ = dirs
    approved = entry(name="ZenVox", exec_line="zenvox")
    token = approval_token("zenvox", approved)
    cfg = Config(safety=Safety(approved_desktop_files=(token,)))
    (user / "zenvox.desktop").write_bytes(approved + b"# edited later\n")
    (user / "other.desktop").write_bytes(approved)
    assert [app.trusted for app in Lexicon.scan(cfg, dirs).apps] == [False, False]
    (user / "zenvox.desktop").write_bytes(approved)
    trusted = {app.id: app.trusted for app in Lexicon.scan(cfg, dirs).apps}
    assert trusted == {"zenvox": True, "other": False}


def test_the_approval_token_is_the_id_and_the_sha256_of_the_bytes():
    token = approval_token("zenvox", b"")
    assert token == "zenvox:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_scan_skips_hidden_entries_and_names_nested_files_by_the_spec(dirs):
    user, system = dirs
    (system / "hidden.desktop").write_bytes(entry(extra="NoDisplay=true\n"))
    (system / "notes.txt").write_text("not an entry")
    nested = user / "wine/Programs"
    nested.mkdir(parents=True)
    (nested / "SADP.desktop").write_bytes(entry(name="SADP", exec_line="wine sadp.exe"))
    assert [app.id for app in Lexicon.scan(Config(), dirs).apps] == ["wine-Programs-SADP"]


def test_scan_survives_a_missing_directory_and_carries_the_config_aliases(dirs, tmp_path):
    _, system = dirs
    (system / "firefox.desktop").write_bytes(SYSTEM_FIREFOX)
    cfg = replace(Config(), aliases={"the fox": "firefox"})
    lex = Lexicon.scan(cfg, [tmp_path / "absent", *dirs])
    assert ids(lex.match_apps("the fox")) == ["firefox"]


# --------------------------------------------------------------------------- apps by phrase


@pytest.fixture
def lex() -> Lexicon:
    return Lexicon(APPS, {"editor": "code", "my shell": "Kitty", "notes": "obsidian"})


def test_a_name_matches_exactly_and_through_a_mishearing(lex):
    assert lex.match_apps("firefox")[0] == (FIREFOX, 1.0)
    assert lex.match_apps("the firefox app")[0] == (FIREFOX, 1.0)
    heard, score = lex.match_apps("firefuck")[0]
    assert heard == FIREFOX and 0.8 < score < 1.0
    assert ids(lex.match_apps("kiddy")) == ["kitty"]
    assert ids(lex.match_apps("fire fox")) == ["firefox"]


def test_part_of_a_name_matches_a_little_less_than_the_whole_name(lex):
    [(app, partial)] = lex.match_apps("chrome")
    [(_, whole)] = lex.match_apps("google chrome")
    assert app == CHROME and partial < whole == 1.0


def test_a_kind_finds_every_app_of_that_kind_with_equal_scores(lex):
    matches = lex.match_apps("the browser")
    assert sorted(ids(matches)) == ["firefox", "google-chrome"]
    assert matches[0][1] == matches[1][1] < 0.8


def test_a_kind_word_inside_an_unrelated_name_does_not_compete_with_the_real_kind(lex):
    assert "bssh" not in ids(lex.match_apps("browser"))
    assert ids(lex.match_apps("avahi browser"))[0] == "bssh"


def test_a_whole_desktop_id_outranks_apps_that_merely_share_the_kind(lex):
    matches = lex.match_apps("code")
    assert ids(matches) == ["code", "org.kde.kate"]
    assert matches[0][1] > matches[1][1]


def test_keywords_and_plurals_match_by_exact_token_only(lex):
    assert ids(lex.match_apps("files")) == ["org.kde.dolphin"]
    assert ids(lex.match_apps("explorer")) == ["org.kde.dolphin"]
    assert lex.match_apps("explorar") == []  # fuzzy runs against names, never keywords


def test_short_words_are_never_matched_fuzzily(lex):
    assert lex.match_apps("kat") == []
    assert ids(lex.match_apps("kate")) == ["org.kde.kate"]


def test_an_alias_resolves_by_app_id_or_by_name(lex):
    assert lex.match_apps("the editor")[0] == (VSCODE, 1.0)
    assert lex.match_apps("my shell")[0] == (KITTY, 1.0)
    assert lex.match_apps("notes") == []  # the target is not installed


def test_an_untrusted_app_never_matches_whatever_it_calls_itself(lex):
    for phrase in ("firefox", "kitty", "browser", "terminal", "zenvox", "firefox kitty browser"):
        assert "zenvox" not in ids(lex.match_apps(phrase, limit=50))
    only_impostor = Lexicon([IMPOSTOR], {"fox": "zenvox"})
    assert only_impostor.match_apps("firefox") == []
    assert only_impostor.match_apps("fox") == []
    assert only_impostor.words() == frozenset({"fox"})


def test_a_homoglyph_phrase_matches_nothing(lex):
    assert lex.match_apps(CYRILLIC_KITTY) == []
    assert lex.match_apps(f"open {CYRILLIC_KITTY}") == []


def test_zero_width_characters_in_a_phrase_do_not_hide_or_forge_a_word(lex):
    assert ids(lex.match_apps(f"fire{ZERO_WIDTH}fox")) == ["firefox"]


def test_command_words_alone_and_empty_phrases_match_nothing(lex):
    assert lex.match_apps("") == []
    assert lex.match_apps("open the window 3") == []


def test_limit_caps_the_result(lex):
    assert len(lex.match_apps("browser", limit=1)) == 1


def test_the_vocabulary_holds_trusted_words_and_aliases_only(lex):
    words = lex.words()
    assert {"firefox", "chrome", "kitty", "browser", "terminal", "dolphin", "shell"} <= words
    assert "zenvox" not in words and "payload" not in words
    assert not any(w.isdigit() or len(w) < 2 for w in words)


# --------------------------------------------------------------------------- windows


def test_a_window_finds_its_app_by_class_whatever_the_case(lex):
    assert lex.app_for_window(window("Firefox")) == FIREFOX
    assert lex.app_for_window(window("google-chrome")) == CHROME
    assert lex.app_for_window(window("", initial_class="KITTY")) == KITTY


def test_a_window_class_meets_the_tail_of_a_reverse_dns_id(lex):
    assert lex.app_for_window(window("dolphin")) == DOLPHIN
    assert lex.app_for_window(window("org.kde.dolphin")) == DOLPHIN


def test_an_unknown_window_has_no_app_and_no_kind(lex):
    assert lex.app_for_window(window("mystery")) is None
    assert lex.kind_of(window("mystery")) == ""
    assert lex.kind_of(window("kitty")) == "terminal"


def test_a_trusted_claim_on_a_class_beats_an_untrusted_one():
    liar = replace(IMPOSTOR, wm_classes=("kitty",), kind="code editor")
    lex = Lexicon([liar, KITTY])
    assert lex.app_for_window(window("kitty")) == KITTY
    assert lex.kind_of(window("kitty")) == "terminal"


def test_an_untrusted_entry_is_reported_for_its_window_but_gives_no_kind(lex):
    assert lex.app_for_window(window("zenvox")) == IMPOSTOR
    assert lex.kind_of(window("zenvox")) == ""


def test_windows_match_through_their_app_best_first_then_most_recent(lex):
    state = DesktopState(
        windows=(
            window("kitty", address="0xa", focus_rank=0),
            window("firefox", address="0xb", focus_rank=2),
            window("google-chrome", address="0xc", focus_rank=1),
            window("firefox", address="0xd", focus_rank=5),
        )
    )
    assert [w.address for w, _ in lex.match_windows("firefuck", state)] == ["0xb", "0xd"]
    assert [w.address for w, _ in lex.match_windows("the browser", state)] == ["0xc", "0xb", "0xd"]
    assert [w.address for w, _ in lex.match_windows("my shell", state)] == ["0xa"]
    assert lex.match_windows("the browser", state, limit=2)[-1][0].address == "0xb"
    assert lex.match_windows("dolphin", state) == []


def test_a_window_without_a_trusted_app_matches_by_its_own_class_only(lex):
    pwa = window("chrome-agimnkijcaahngcdmfeangaknmldooml-Profile_2", title="YouTube")
    state = DesktopState(windows=(pwa, window("zenvox", title="x", address="0x2")))
    assert [w for w, _ in lex.match_windows("chrome", state)] == [pwa]
    assert lex.match_windows("youtube", state) == []
    assert [w.cls for w, _ in lex.match_windows("zenvox", state)] == ["zenvox"]
    assert lex.match_windows("firefox", state) == []  # the impostor's Name says Firefox


def test_a_title_never_selects_a_window(lex):
    hostile = window("evil", title="firefox kitty browser terminal focus close")
    state = DesktopState(windows=(hostile,))
    for phrase in ("firefox", "kitty", "the browser", "terminal", "focus", "close"):
        assert lex.match_windows(phrase, state) == []


# --------------------------------------------------------------------------- titles


def test_a_residual_word_found_in_exactly_one_title_picks_that_window(lex):
    a = window("firefox", "YouTube - Mozilla Firefox", "0xa")
    b = window("firefox", "Hacker News - Mozilla Firefox", "0xb")
    assert lex.title_discriminates(["with", "youtube"], [a, b]) == a
    assert lex.title_discriminates(["hacker"], [a, b]) == b
    assert lex.title_discriminates(["YouTube"], [a, b]) == a


def test_a_word_in_several_titles_or_in_none_discriminates_nothing(lex):
    a = window("firefox", "YouTube - Mozilla Firefox", "0xa")
    b = window("firefox", "YouTube Music - Mozilla Firefox", "0xb")
    assert lex.title_discriminates(["youtube"], [a, b]) is None
    assert lex.title_discriminates(["mozilla"], [a, b]) is None
    assert lex.title_discriminates(["github"], [a, b]) is None
    assert lex.title_discriminates([], [a, b]) is None
    assert lex.title_discriminates(["youtube"], []) is None


def test_two_words_pointing_at_two_windows_is_a_tie(lex):
    a = window("firefox", "YouTube", "0xa")
    b = window("firefox", "GitHub", "0xb")
    assert lex.title_discriminates(["youtube", "github"], [a, b]) is None


def test_command_words_and_kind_words_in_a_title_never_discriminate(lex):
    hostile = window("firefox", "close focus this window browser terminal 3", "0xa")
    plain = window("firefox", "Hacker News", "0xb")
    residual = ["close", "focus", "this", "window", "browser", "terminal", "3"]
    assert lex.title_discriminates(residual, [hostile, plain]) is None


def test_titles_are_sanitized_and_homoglyph_tokens_in_them_are_ignored(lex):
    hidden = window("firefox", f"you{ZERO_WIDTH}tube{RIGHT_TO_LEFT_OVERRIDE}", "0xa")
    forged = window("firefox", f"inbox {CYRILLIC_KITTY}", "0xb")
    assert lex.title_discriminates(["youtube"], [hidden, forged]) == hidden
    assert lex.title_discriminates([CYRILLIC_KITTY], [hidden, forged]) is None
    assert lex.title_discriminates(["kitty"], [hidden, forged]) is None


# --------------------------------------------------------------------------- corroboration


def test_a_spoken_name_corroborates_its_app_and_no_other(lex):
    assert lex.corroborates("focus firefox", app=FIREFOX)
    assert lex.corroborates("switch to chrome please", app=CHROME)
    assert not lex.corroborates("focus firefox", app=KITTY)
    assert not lex.corroborates("focus firefox", app=CHROME)


def test_a_misheard_or_split_name_still_corroborates(lex):
    assert lex.corroborates("open kiddy", app=KITTY)
    assert lex.corroborates("focus firefuck", app=FIREFOX)
    assert lex.corroborates("focus fire fox", app=FIREFOX)
    assert not lex.corroborates("focus files", app=FIREFOX)


def test_a_kind_word_corroborates(lex):
    assert lex.corroborates("close the browser", app=FIREFOX)
    assert lex.corroborates("go to the terminal", window=window("kitty"))
    assert not lex.corroborates("go to the terminal", window=window("firefox"))


def test_an_alias_corroborates_its_target_only(lex):
    assert lex.corroborates("open my shell", app=KITTY)
    assert lex.corroborates("focus the editor", window=window("code"))
    assert not lex.corroborates("open my shell", app=FIREFOX)


def test_command_words_numbers_and_directions_corroborate_nothing(lex):
    focus = replace(FIREFOX, id="focus", name="Focus Close Move", wm_classes=("focus",))
    lex = Lexicon([focus])
    assert not lex.corroborates("focus the window on workspace 3 and move it left", app=focus)
    assert not lex.corroborates("close it", window=window("focus"))
    assert not lex.corroborates("", app=focus)


def test_a_window_titled_with_every_command_word_does_not_corroborate(lex):
    title = "focus close open firefox kitty chrome browser terminal editor type lock"
    hostile = window("evil-page", title=title)
    for said in ("focus firefox", "close kitty", "open the browser", "type hello", "my shell"):
        assert not lex.corroborates(said, window=hostile)


def test_an_untrusted_app_never_corroborates(lex):
    assert not lex.corroborates("open firefox", app=IMPOSTOR)
    assert not lex.corroborates("open zenvox", app=IMPOSTOR)
    assert not lex.corroborates("the firefox browser", window=window("zenvox"))
    # the class is the compositor's word, not the file's, so it still counts
    assert lex.corroborates("focus zenvox", window=window("zenvox"))


def test_a_homoglyph_in_the_utterance_does_not_corroborate(lex):
    assert not lex.corroborates(f"focus {CYRILLIC_KITTY}", app=KITTY)
    assert not lex.corroborates(f"focus {CYRILLIC_KITTY}", window=window("kitty"))
    assert not lex.corroborates(f"focus {CYRILLIC_KITTY}", window=window(CYRILLIC_KITTY))


def test_a_homoglyph_app_name_is_not_an_anchor():
    forged = replace(KITTY, id="notkitty", name=CYRILLIC_KITTY, wm_classes=("notkitty",))
    lex = Lexicon([forged])
    assert lex.match_apps("kitty") == []
    assert not lex.corroborates("open kitty", app=forged)


def test_a_window_with_no_app_is_corroborated_by_its_class(lex):
    assert lex.corroborates("focus mystery", window=window("Mystery"))
    assert not lex.corroborates("focus firefox", window=window("mystery", title="firefox"))


def test_nothing_to_corroborate_against_is_false(lex):
    assert not lex.corroborates("focus firefox")
