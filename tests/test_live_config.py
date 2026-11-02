"""Settings a person controls: each mechanism on or off, and its hyperparameters by hand.

Every setting is read on each hook call, so these drive real hook calls (`tests.test_live_hooks`)
with the setting in the environment or the repository's config file, and check that the
mechanism does what the setting says.
"""
import json
import os

import pytest

from tokeneconomizer import loopguard as guard
from tokeneconomizer.plugin.config import LiveConfig, load_config, unset_config, write_config
from tokeneconomizer.plugin.paths import repo_identity, repo_state_dir
from tests.test_live_hooks import (GREEN, _first, _turn, env, repo, run_session,  # noqa: F401
                                   stuck_calls, trap_session)


# ---- parsing ------------------------------------------------------------------------------
@pytest.mark.parametrize("key,on", [("microskills", "envfacts+distiller"), ("toolprune", "auto"),
                                    ("loopguard", "nudge")])
def test_each_mechanism_takes_a_plain_on_and_off(key, on):
    cfg = LiveConfig()
    assert cfg.set(key, "off") and getattr(cfg, key) == "off"
    assert cfg.set(key, "on") and getattr(cfg, key) == on
    assert cfg.set(key, False) and getattr(cfg, key) == "off"
    assert not cfg.set(key, "sometimes") and getattr(cfg, key) == "off"


def test_a_guard_threshold_is_typed_and_range_checked():
    cfg = LiveConfig()
    assert cfg.set("loopguard.min_turns", "12") and cfg.loopguard_thresholds["min_turns"] == 12
    assert (cfg.set("loopguard.backstop_mult", "2")
            and cfg.loopguard_thresholds["backstop_mult"] == 2.0)
    assert not cfg.set("loopguard.min_turns", "12.5")        # a count is a whole number
    assert not cfg.set("loopguard.stagnation", "1.5")        # a share is between 0 and 1
    assert not cfg.set("loopguard.min_turns", "0")
    assert not cfg.set("loopguard.no_such_threshold", "3")
    assert cfg.loopguard_thresholds == {"min_turns": 12, "backstop_mult": 2.0}
    assert len(cfg.warnings) == 4


def test_set_and_unset_keep_thresholds_in_their_own_object(tmp_path):
    path = str(tmp_path / "config.json")
    write_config(path, {"loopguard": "off", "loopguard.min_turns": "12",
                        "loopguard.edit_churn": "6"})
    assert json.load(open(path)) == {"loopguard": "off",
                                     "loopguard_thresholds": {"edit_churn": 6, "min_turns": 12}}
    with pytest.raises(ValueError):
        write_config(path, {"loopguard.min_turns": "-1"})     # nothing written on a rejection
    unset_config(path, ["loopguard.min_turns", "loopguard"])
    assert json.load(open(path)) == {"loopguard_thresholds": {"edit_churn": 6}}
    unset_config(path, ["loopguard.edit_churn"])
    assert json.load(open(path)) == {}
    with pytest.raises(ValueError):
        unset_config(path, ["loopguard.nope"])


def test_the_repository_file_beats_the_global_one_and_the_environment_beats_both(tmp_path):
    root, repo_dir = tmp_path / "root", tmp_path / "repo"
    write_config(str(root / "config.json"), {"loopguard_nudges": "3",
                                             "loopguard.min_turns": "30"})
    write_config(str(repo_dir / "config.json"), {"loopguard.min_turns": "10"})
    cfg = load_config(str(root), str(repo_dir), env={})
    assert cfg.loopguard_nudges == 3 and cfg.loopguard_thresholds == {"min_turns": 10}
    cfg = load_config(str(root), str(repo_dir), env={
        "TE_LOOPGUARD_NUDGES": "2",
        "TE_LOOPGUARD_THRESHOLDS": '{"min_turns": 8, "stagnation": 0.5}',
        "TE_MICROSKILLS_MAX_FACTS": "2", "TE_LOOPGUARD": "off"})
    assert cfg.loopguard_nudges == 2 and cfg.microskills_max_facts == 2
    assert cfg.loopguard == "off"
    assert cfg.loopguard_thresholds == {"min_turns": 8, "stagnation": 0.5}
    assert not cfg.warnings


def test_the_names_before_the_rename_still_work(tmp_path):
    """`inject`, `untool`, `spiral` and their settings and variables are read as the new names;
    a file holding both spellings takes the new one; writing a setting stores its new name and
    drops the old spelling; unsetting by either name removes both."""
    root, repo_dir = tmp_path / "root", tmp_path / "repo"
    os.makedirs(root)
    with open(root / "config.json", "w") as fh:
        json.dump({"loopguard": "off", "spiral": "guard", "inject_max_facts": 3,
                   "untool": "auto", "propose_every": 7, "spiral_aggr": "aggressive",
                   "spiral_thresholds": {"min_turns": 9}}, fh)
    cfg = load_config(str(root), str(repo_dir), env={
        "TE_SPIRAL_NUDGES": "2", "TE_UNTOOL_EVIDENCE": "4", "TE_INJECT": "off"})
    assert cfg.loopguard == "off"                       # the new name wins in one file
    assert (cfg.microskills_max_facts, cfg.toolprune, cfg.microskills_propose_every,
            cfg.loopguard_aggr) == (3, "auto", 7, "aggressive")
    assert cfg.loopguard_thresholds == {"min_turns": 9}
    assert (cfg.loopguard_nudges, cfg.toolprune_evidence, cfg.microskills) == (2, 4, "off")
    assert not cfg.warnings
    # a new variable beats its old spelling
    assert load_config(str(root), env={"TE_SPIRAL": "nudge",
                                       "TE_LOOPGUARD": "stop"}).loopguard == "stop"
    assert LiveConfig().set("spiral", "guard") and LiveConfig().set("loopguard", "guard")
    path = str(root / "config.json")
    write_config(path, {"spiral": "nudge", "spiral.edit_churn": "5"})
    written = json.load(open(path))
    assert written["loopguard"] == "nudge" and "spiral" not in written
    assert written["loopguard_thresholds"] == {"min_turns": 9, "edit_churn": 5}
    assert "spiral_thresholds" not in written
    unset_config(path, ["untool", "inject_max_facts", "spiral.min_turns"])
    written = json.load(open(path))
    assert "untool" not in written and "toolprune" not in written
    assert "inject_max_facts" not in written
    assert written["loopguard_thresholds"] == {"edit_churn": 5}


def test_the_fact_caps_default_to_the_cores_own():
    from tokeneconomizer.microskills import envfacts
    cfg = LiveConfig()
    assert (cfg.microskills_max_facts, cfg.microskills_max_tokens) == (envfacts.MAX_FACTS,
                                                                       envfacts.MAX_TOKENS)


# ---- thresholds: preset, then learned, then pinned ----------------------------------------
class _Store:
    def __init__(self, d):
        self.d = d

    def read(self, name):
        return self.d.get(name)


def test_a_pinned_threshold_wins_over_the_preset_and_over_what_was_learned():
    store = _Store({"envelope.json": {"resolved_turns": [6, 7, 8, 8, 9]}})
    learned = guard.thresholds(guard.load_envelope(store))
    assert learned["min_turns"] == 8                     # learned below the preset's 20
    tracker = guard.load_envelope(store, overrides={"min_turns": 30, "backstop_mult": 4.0})
    thr = guard.thresholds(tracker, {"min_turns": 30, "backstop_mult": 4.0})
    assert thr["min_turns"] == 30
    assert thr["backstop_turns"] == pytest.approx(tracker.envelope * 4.0)


def test_the_envelope_settings_reach_the_tracker():
    store = _Store({"envelope.json": {"resolved_turns": [6, 8]}})
    assert guard.load_envelope(store).envelope is None   # two samples: not trusted by default
    tracker = guard.load_envelope(store, overrides={"envelope_min_samples": 2,
                                                    "envelope_margin": 2.0, "envelope_pct": 50})
    assert tracker.envelope == pytest.approx(7 * 2.0)


def test_base_thresholds_show_the_preset_with_the_pinned_values_on_top():
    thr = guard.base_thresholds("aggressive", {"min_turns": 9})
    assert thr["min_turns"] == 9 and thr["backstop_mult"] == 1.8
    assert "restart_budget" not in thr                   # not used by the live guard


# ---- end to end, through the hooks --------------------------------------------------------
def _warm(env, repo):
    for k in range(5):
        _turn(env, repo, "s", f"g{k}", GREEN)


def _messages(outs):
    return [o.get("systemMessage") or "" for o in outs]


def test_a_guard_turned_off_in_the_repository_file_never_intervenes(env, repo):
    _warm(env, repo)
    path = os.path.join(repo_state_dir(repo_identity(repo, env), "claude", env), "..",
                        "config.json")
    write_config(os.path.normpath(path), {"spiral": "off"})
    assert all(o == {} for o in _turn(env, repo, "s", "stuck", stuck_calls(40)))


def test_more_redirects_are_given_before_the_notice_when_asked_for(env, repo):
    _warm(env, repo)
    outs = _turn(dict(env, TE_SPIRAL_NUDGES="2"), repo, "s", "stuck", stuck_calls(60))
    msgs = _messages(outs)
    nudges = [i for i, m in enumerate(msgs) if "nudged" in m]
    notice = _first(msgs, lambda m: "still looks stuck" in m)
    assert len(nudges) == 2 and notice is not None and notice > nudges[-1]


def test_a_higher_min_turns_holds_the_guard_back(env, repo):
    _warm(env, repo)
    outs = _turn(dict(env, TE_SPIRAL_THRESHOLDS='{"min_turns": 80}'), repo, "s", "stuck",
                 stuck_calls(40))
    assert all(o == {} for o in outs)                    # default thresholds nudge this turn


def test_injection_off_and_a_tight_fact_cap_keep_the_context_clean(env, repo, tmp_path):
    for i in range(4):
        run_session(env, repo, f"s{i}", trap_session(str(tmp_path / f"s{i}.jsonl"), repo, i))
    off = run_session(dict(env, TE_MICROSKILLS="off"), repo, "s4",
                      trap_session(str(tmp_path / "s4.jsonl"), repo, 4))
    assert off == {}                                     # `envfacts` injects here
    tight = dict(env, TE_INJECT_MAX_TOKENS="5")          # smaller than the block's header
    run_session(tight, repo, "s5", trap_session(str(tmp_path / "s5.jsonl"), repo, 5))
    after = run_session(tight, repo, "s6", trap_session(str(tmp_path / "s6.jsonl"), repo, 6))
    assert "hookSpecificOutput" not in after


def test_loopguard_stop_is_still_read_and_acts_as_nudge_with_a_warning(tmp_path):
    """`stop` once stopped a headless turn on Claude Code and Pi; the plugin no longer stops a
    turn on any agent, so an existing `stop` (or the old `spiral=guard`) keeps working as the
    redirect-then-notice ladder, and says so."""
    for env in ({"TE_LOOPGUARD": "stop"}, {"TE_SPIRAL": "guard"}):
        cfg = load_config(str(tmp_path), env=env)
        assert cfg.loopguard == "stop"
        assert any("loopguard=stop acts as nudge" in w for w in cfg.warnings)
    assert not load_config(str(tmp_path), env={"TE_LOOPGUARD": "nudge"}).warnings


def test_the_quiet_window_before_a_deny_list_is_written_may_be_zero():
    cfg = LiveConfig()
    assert cfg.toolprune_quiet_minutes == 60
    assert cfg.set("toolprune_quiet_minutes", "0") and cfg.toolprune_quiet_minutes == 0
    assert not cfg.set("toolprune_quiet_minutes", "-5")
    assert not cfg.set("toolprune_evidence", "0")             # other numbers still start at 1
    assert load_config("/nonexistent", env={"TE_TOOLPRUNE_QUIET_MINUTES": "15"}
                       ).toolprune_quiet_minutes == 15


def test_off_everywhere_and_on_in_one_repository_runs_only_there(env, repo, tmp_path):
    """`enabled=false --global`, then `enabled=true` in a repository: the plugin runs there, and
    a repository left off gets no state at all."""
    other = tmp_path / "other"
    (other / ".git" / "info").mkdir(parents=True)
    (other / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (other / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://github.com/acme/other.git\n')
    home = env["TOKENECONOMIZER_HOME"]
    on = repo_identity(repo, env).key
    os.makedirs(os.path.join(home, "repos", on))
    write_config(os.path.join(home, "config.json"), {"enabled": False})
    write_config(os.path.join(home, "repos", on, "config.json"), {"enabled": True})
    for checkout, sid in ((repo, "s-on"), (str(other), "s-off")):
        run_session(env, checkout, sid, str(tmp_path / f"{sid}.jsonl"))
    assert os.listdir(os.path.join(home, "repos")) == [on]
    assert os.listdir(os.path.join(home, "repos", on)) != ["config.json"]   # it ran there
