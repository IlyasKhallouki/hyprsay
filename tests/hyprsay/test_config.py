"""Configuration loading. No test reads the real config file."""

import pytest

from hyprsay import config


def test_an_absent_file_is_a_working_configuration(tmp_path):
    cfg = config.load(tmp_path / "missing.toml", env={})
    assert cfg.stt.backend == "hybrid"
    assert cfg.stt.local_model == "parakeet-110m"
    assert cfg.jev.zero_data_retention is True
    assert cfg.privacy.titles == "when_needed"


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
