"""Count thresholds and integer settings must not silently truncate fractions or read booleans
from JSON numbers."""
import json

import pytest

from tokeneconomizer.plugin.config import (LOOPGUARD_KEYS, LiveConfig, load_config,
                                           write_config)


INTEGER_THRESHOLDS = [name for name, spec in LOOPGUARD_KEYS.items() if spec[0] is int]
INTEGER_SETTINGS = [key for key, value in LiveConfig().as_dict().items() if type(value) is int]


@pytest.mark.parametrize("name", INTEGER_THRESHOLDS)
@pytest.mark.parametrize("prefix", ["loopguard", "spiral"])
def test_fractional_numeric_count_is_rejected_without_changing_the_previous_value(name, prefix):
    cfg = LiveConfig()
    assert cfg.set(f"{prefix}.{name}", 8)
    assert not cfg.set(f"{prefix}.{name}", 12.5)
    assert cfg.loopguard_thresholds[name] == 8
    assert cfg.warnings


@pytest.mark.parametrize("value", [12, 12.0, "12", "12.0"])
def test_whole_number_representations_remain_supported(value):
    cfg = LiveConfig()
    assert cfg.set("loopguard.min_turns", value)
    assert cfg.loopguard_thresholds["min_turns"] == 12
    assert type(cfg.loopguard_thresholds["min_turns"]) is int


@pytest.mark.parametrize("source", ["file", "environment"])
def test_fractional_count_from_json_does_not_become_a_pinned_threshold(tmp_path, source):
    thresholds = {"min_turns": 12.5, "stagnation": 0.5}
    env = {}
    if source == "file":
        (tmp_path / "config.json").write_text(
            json.dumps({"loopguard_thresholds": thresholds}), encoding="utf-8")
    else:
        env["TE_LOOPGUARD_THRESHOLDS"] = json.dumps(thresholds)
    cfg = load_config(str(tmp_path), env=env)
    assert "min_turns" not in cfg.loopguard_thresholds
    assert cfg.loopguard_thresholds["stagnation"] == 0.5
    assert cfg.warnings


@pytest.mark.parametrize("updates", [
    {"loopguard.min_turns": 12.5},
    {"loopguard_thresholds": {"min_turns": 12.5}},
])
def test_fractional_update_leaves_the_saved_config_unchanged(tmp_path, updates):
    path = str(tmp_path / "config.json")
    write_config(path, {"loopguard.min_turns": 8})
    before = (tmp_path / "config.json").read_bytes()
    with pytest.raises(ValueError):
        write_config(path, updates)
    assert (tmp_path / "config.json").read_bytes() == before


@pytest.mark.parametrize("key", INTEGER_SETTINGS)
@pytest.mark.parametrize("value", [12.5, True])
def test_fractional_or_boolean_integer_setting_is_rejected_without_changing_it(key, value):
    cfg = LiveConfig()
    assert cfg.set(key, 8)
    assert not cfg.set(key, value)
    assert getattr(cfg, key) == 8
    assert cfg.warnings


@pytest.mark.parametrize("value", [4096, 4096.0, "4096"])
def test_whole_number_integer_settings_remain_supported(value):
    cfg = LiveConfig()
    assert cfg.set("repomap_tokens", value)
    assert cfg.repomap_tokens == 4096
    assert type(cfg.repomap_tokens) is int


def test_fractional_or_boolean_integer_settings_from_a_config_file_keep_the_defaults(tmp_path):
    (tmp_path / "config.json").write_text(
        json.dumps({"repomap_tokens": 4096.5, "microskills_propose_every": True}),
        encoding="utf-8")
    cfg = load_config(str(tmp_path), env={})
    assert cfg.repomap_tokens == LiveConfig().repomap_tokens
    assert cfg.microskills_propose_every == LiveConfig().microskills_propose_every
    assert len(cfg.warnings) == 2
