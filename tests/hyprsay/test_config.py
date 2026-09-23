"""Configuration loading. No test reads the real config file."""

import pytest

from hyprsay import config


def test_an_absent_file_is_a_working_configuration(tmp_path):
    cfg = config.load(tmp_path / "missing.toml", env={})
    assert cfg.stt.backend == "hybrid"
    assert cfg.stt.local_model == "parakeet-110m"
    assert cfg.jev.zero_data_retention is True
    assert cfg.privacy.titles == "when_needed"
    # the interpreter is on by default and every one of its keys is optional
    assert cfg.interpret.enabled is True
    assert cfg.interpret.model == "meta/llama-3.1-8b"
    assert cfg.interpret.deadline_s == 0.8


def test_the_interpreter_can_be_turned_off_or_pointed_at_another_model(tmp_path):
    file = tmp_path / "config.toml"
    file.write_text('[interpret]\nenabled = false\nmodel = "google/gemini-2.5-flash-lite"\n')
    cfg = config.load(file, env={})
    assert cfg.interpret.enabled is False
    assert cfg.interpret.model == "google/gemini-2.5-flash-lite"
    overridden = config.load(file, env={"HYPRSAY_INTERPRET_DEADLINE_S": "0.5"})
    assert overridden.interpret.deadline_s == 0.5


def test_the_file_overrides_defaults_and_the_environment_overrides_the_file(tmp_path):
    file = tmp_path / "config.toml"
    file.write_text(
        '[stt]\nbackend = "cloud"\n[jev]\ndeadline_s = 1\n[aliases]\nBrowser = "firefox"\n'
    )
    assert config.load(file, env={}).stt.backend == "cloud"
    assert config.load(file, env={}).jev.deadline_s == 1.0
    assert config.load(file, env={}).aliases == {"browser": "firefox"}
    assert config.load(file, env={"HYPRSAY_STT_BACKEND": "local"}).stt.backend == "local"


def test_lists_become_tuples(tmp_path):
    file = tmp_path / "config.toml"
    file.write_text('[safety]\ntype_allow_classes = ["obsidian", "code"]\n')
    assert config.load(file, env={}).safety.type_allow_classes == ("obsidian", "code")


@pytest.mark.parametrize(
    "body",
    [
        '[stt]\nbackend = "telepathy"\n',  # not an allowed choice
        "[stt]\nbakend = 1\n",  # a misspelled key must not be silently ignored
        "[nonsense]\nx = 1\n",
        '[jev]\ndeadline_s = "soon"\n',
        "[safety]\ncountdown_s = true\n",
        "[interpret]\nenabled = 1\n",  # a boolean key that took a number
        '[interpret]\nmodell = "x"\n',  # a misspelled optional key is still an error
        '[jev]\npath = "both"\n',  # the decision path is single or fanout, nothing else
        "this is not toml [",
    ],
)
def test_a_bad_file_is_refused_loudly(tmp_path, body):
    file = tmp_path / "config.toml"
    file.write_text(body)
    with pytest.raises(config.ConfigError):
        config.load(file, env={})


def test_a_bad_environment_number_is_a_config_error(tmp_path):
    with pytest.raises(config.ConfigError, match="HYPRSAY_JEV_DEADLINE_S"):
        config.load(tmp_path / "none.toml", env={"HYPRSAY_JEV_DEADLINE_S": "fast"})


def test_unrelated_environment_variables_are_ignored(tmp_path):
    cfg = config.load(tmp_path / "none.toml", env={"HYPRSAY_UNKNOWN_THING": "1", "HOME": "/x"})
    assert cfg == config.Config()


def test_the_single_request_is_the_default_path_and_fanout_can_be_chosen(tmp_path):
    assert config.Config().jev.path == "single"
    file = tmp_path / "config.toml"
    file.write_text('[jev]\npath = "fanout"\n')
    assert config.load(file, env={}).jev.path == "fanout"
