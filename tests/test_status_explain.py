"""Status explains saved evidence on request, without guessing about past sessions."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tokeneconomizer.plugin import cli, hosts
from tokeneconomizer.plugin.config import LiveConfig
from tokeneconomizer.plugin.context import LiveContext
from tokeneconomizer.plugin.store import Store


@pytest.fixture
def status(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {"TOKENECONOMIZER_HOME": str(tmp_path / "state"),
           "GIT_CEILING_DIRECTORIES": str(tmp_path)}
    ctx = LiveContext.build(hosts.Codex(), str(repo), env, settle=False)
    ctx.config = LiveConfig(microskills="envfacts", toolprune="propose")
    monkeypatch.setenv("TOKENECONOMIZER_HOME", ctx.root)
    monkeypatch.setattr(cli, "_ctx", lambda args: ctx)

    def run(*options):
        assert cli.main(["--host", ctx.host.name, "--cwd", str(repo), "status", *options]) == 0
        return capsys.readouterr().out

    return ctx, run


def explanations(output):
    return [line for line in output.splitlines() if line.startswith("  explain ")]


def test_explanation_is_opt_in_and_json_remains_machine_readable(status):
    ctx, run = status
    ordinary = run()
    detailed = run("--explain")
    assert len(explanations(detailed)) == 3
    assert all(len(line) <= 160 for line in explanations(detailed))
    assert "\n".join(line for line in detailed.splitlines()
                     if not line.startswith("  explain ")) + "\n" == ordinary
    plain = json.loads(run("--json"))
    assert "explanations" not in plain
    extended = json.loads(run("--json", "--explain"))
    assert set(extended.pop("explanations")) == {"microskills", "toolprune", "loopguard"}
    assert extended == plain


def test_cold_start_does_not_blame_a_firing_gate_without_any_facts(status):
    ctx, run = status
    out = run("--explain")
    assert "no learning snapshot recorded" in out
    assert "no tool-usage snapshot recorded" in out
    assert "0/5 ended-green turns" in out
    assert "firing gate" not in out
    assert "config set microskills=on" not in out  # would enable paid calls


def test_disabled_plugin_has_one_explanation_even_with_old_state(status):
    ctx, run = status
    ctx.config.enabled = False
    ctx.store.write("injection.json", {"rendered": "- old fact", "sessions": 4})
    lines = explanations(run("--explain"))
    assert len(lines) == 1
    assert "plugin is disabled" in lines[0]


def test_off_and_shadow_are_not_described_as_live_optimization(status):
    ctx, run = status
    ctx.config.microskills = ctx.config.toolprune = ctx.config.loopguard = "off"
    ctx.store.write("injection.json", {"rendered": "- old fact", "sessions": 4})
    assert all("off" in line for line in explanations(run("--explain")))
    ctx.config.microskills = "envfacts"
    ctx.config.shadow = True
    lines = explanations(run("--explain"))
    assert len(lines) == 4
    assert "shadow mode" in lines[0] and "not applied" in lines[0]
    assert "cached facts" in " ".join(lines)


@pytest.mark.parametrize("facts,expected", [
    ({"_profile": "envfacts"}, "no facts tracked in the last learning snapshot"),
    ({"_private-id": {"state": "CANDIDATE", "fire": "1/1", "inject": False},
      "_task": {"state": "metadata"}}, "1 candidate, 0 active, 0 retired"),
    ({"a": {"state": "CANDIDATE", "fire": "1/1", "inject": False},
      "b": {"state": "ACTIVE", "fire": "1/4", "inject": False},
      "c": {"state": "RETIRED", "fire": "2/5", "inject": False}},
     "1 candidate, 1 active, 1 retired; no facts rendered"),
])
def test_fact_explanations_use_saved_lifecycle_states(status, facts, expected):
    ctx, run = status
    ctx.store.write("injection.json", {"sessions": 6, "rendered": "", "facts": facts})
    out = run("--explain")
    assert expected in out
    assert "see microskills with the same --host/--cwd" in out


@pytest.mark.parametrize("mode,extra,expected", [
    ("propose", {}, "proposal awaiting"),
    ("auto", {"auto_pending": {"items": ["goals"]}}, "proposal held for the idle wait"),
    ("auto", {"auto_apply_refused": "private path or a very long exception"},
     "automatic apply was refused"),
    ("auto", {}, "unapplied proposal recorded"),
])
def test_unapplied_proposals_report_the_recorded_state(status, mode, extra, expected):
    ctx, run = status
    ctx.config.toolprune = mode
    ctx.store.write("untool.json", {"sessions": 6, "proposal": ["goals"], **extra})
    out = run("--explain")
    assert expected in out
    assert "private path or a very long exception" not in out


def test_shadow_proposal_does_not_claim_an_automatic_apply_is_pending(status):
    ctx, run = status
    ctx.config.shadow = True
    ctx.store.write("untool.json", {"sessions": 6, "proposal": ["goals"]})
    lines = "\n".join(explanations(run("--explain")))
    assert "not applied" in lines
    assert "awaiting" not in lines and "idle wait" not in lines


def test_restored_items_are_not_reported_as_waiting_to_be_applied(status):
    from tokeneconomizer.plugin import denylist
    ctx, run = status
    ctx.store.write("untool.json", {"sessions": 6, "proposal": ["goals"],
                                  "auto_pending": {"items": ["goals"]}})
    denylist.set_kept(ctx.store, ctx.repo.root, ctx.host.name, add=["goals"])
    out = run("--explain")
    assert "awaiting" not in out and "held for the idle wait" not in out
    assert "no eligible proposal recorded" in out


@pytest.mark.parametrize("same_checkout", [True, False])
def test_applied_rules_belong_to_the_selected_checkout(status, same_checkout):
    from tokeneconomizer.plugin import denylist
    ctx, run = status
    ctx.store.write("untool.json", {"sessions": 6, "proposal": ["goals"]})
    root = ctx.repo.root if same_checkout else os.path.join(ctx.repo.root, "other-clone")
    denylist.write_applied(ctx.store, {denylist.settings_path(root, "codex"): ["goals"]})
    out = run("--explain")
    assert ("deny rules recorded as applied for this checkout" in out) == same_checkout
    assert ("proposal awaiting approval" in out) != same_checkout


@pytest.mark.parametrize("pending", [None, "unexpected", {"items": 3}, {"items": "goals"}])
def test_unavailable_pending_details_do_not_claim_an_idle_wait(status, pending):
    ctx, run = status
    ctx.config.toolprune = "auto"
    ctx.store.write("untool.json", {"sessions": 6, "proposal": ["goals"],
                                  "auto_pending": pending})
    out = run("--explain")
    assert "unapplied proposal recorded" in out
    assert "held for the idle wait" not in out


def test_no_proposal_does_not_automatically_mean_insufficient_evidence(status):
    ctx, run = status
    ctx.store.write("untool.json", {"sessions": 50, "proposal": [],
                                  "capabilities": {"goals": {"used": True}}})
    out = run("--explain")
    assert "no eligible proposal recorded" in out
    assert "see toolprune with the same --host/--cwd" in out
    assert "insufficient" not in out


def test_unsupported_host_is_not_told_to_collect_a_catalog(status):
    ctx, run = status
    ctx.host = hosts.Pi()
    out = run("--explain")
    assert "tool pruning is not available on pi" in out
    assert "refresh-catalog" not in out


def test_loopguard_uses_the_configured_warmup_and_does_not_infer_productivity(status):
    ctx, run = status
    ctx.config.loopguard_thresholds["envelope_min_samples"] = 2
    ctx.store.write("envelope.json", {"resolved_turns": [10]})
    assert "1/2 ended-green turns" in run("--explain")
    ctx.store.write("envelope.json", {"resolved_turns": [10, 12]})
    out = run("--explain")
    assert "baseline ready; per-turn reasons cannot be inferred from these totals" in out
    assert "productive" not in out and "no failures" not in out


def test_paused_model_calls_are_reported_without_exposing_the_error(status):
    ctx, run = status
    Store(ctx.root).write("hosts/codex/sidecall.json", {"at": 1, "reason": "private error"})
    ctx.store.write("envelope.json", {"resolved_turns": [10] * 5})
    out = run("--explain")
    assert "judge calls paused after a sign-in failure; backstop remains active" in out
    assert "private error" not in out


def test_paused_proposer_does_not_hide_facts_that_can_still_be_injected(status):
    ctx, run = status
    ctx.config.microskills = "envfacts+distiller"
    ctx.config.loopguard = "off"
    Store(ctx.root).write("hosts/codex/sidecall.json", {"at": 1, "reason": "not signed in"})
    assert "model proposer paused after a sign-in failure" in run("--explain")
    ctx.store.write("injection.json", {"sessions": 4, "rendered": "- a cached fact"})
    out = run("--explain")
    assert "cached facts are available for session start; model proposer paused" in out


def test_older_snapshot_without_fact_details_does_not_claim_there_are_no_facts(status):
    ctx, run = status
    ctx.store.write("injection.json", {"sessions": 4, "rendered": ""})
    out = run("--explain")
    assert "no fact details recorded" in out
    assert "no facts tracked" not in out


@pytest.mark.parametrize("facts", [None, [], ["unexpected"], "unexpected"])
def test_unavailable_fact_details_do_not_crash_or_claim_zero_facts(status, facts):
    ctx, run = status
    ctx.store.write("injection.json", {"sessions": 4, "rendered": "", "facts": facts})
    out = run("--explain")
    assert "no fact details recorded" in out
    assert "no facts tracked" not in out


def test_unknown_lifecycle_states_are_not_counted_as_zero_facts(status):
    ctx, run = status
    ctx.store.write("injection.json", {"sessions": 4, "rendered": "", "facts": {
        "legacy": {"state": "UNKNOWN"}}})
    out = run("--explain")
    assert "fact lifecycle details unavailable" in out
    assert "0 candidate, 0 active, 0 retired" not in out


def test_followup_guidance_keeps_the_selected_repository_and_host(status):
    ctx, run = status
    ctx.store.write("injection.json", {"sessions": 4, "rendered": "", "facts": {}})
    ctx.store.write("untool.json", {"sessions": 4, "proposal": []})
    lines = explanations(run("--explain"))
    assert any("microskills with the same --host/--cwd" in line for line in lines)
    assert any("toolprune with the same --host/--cwd" in line for line in lines)


def test_explanation_does_not_spawn_jobs_call_models_or_change_saved_state(status, monkeypatch):
    from tokeneconomizer.plugin import fold, sidecall, toolcheck
    ctx, run = status
    ctx.store.write("injection.json", {"sessions": 4, "rendered": ""})
    ctx.store.write("untool.json", {"sessions": 4, "proposal": ["goals"]})

    def forbidden(*args, **kwargs):
        pytest.fail("explaining status must only inspect saved state")

    for obj, name in ((ctx, "spawn"), (fold, "fold_session"), (sidecall, "runner_for"),
                      (toolcheck, "measure")):
        monkeypatch.setattr(obj, name, forbidden)

    def snapshot():
        return {str(p): p.read_bytes() for p in Path(ctx.root).rglob("*") if p.is_file()}

    before = snapshot()
    run("--explain")
    run("--json", "--explain")
    assert snapshot() == before


def test_explanations_follow_real_session_learning(status, tmp_path):
    from tests.test_live_hooks import run_session, trap_session
    ctx, run = status
    ctx.host = hosts.ClaudeCode()
    ctx.env.update(TE_FOLD_SYNC="1", TE_MICROSKILLS="envfacts",
                   TE_TOOLPRUNE="off", TE_LOOPGUARD="off")
    # Rebuild for Claude's own state; each agent learns separately.
    learned = LiveContext.build(ctx.host, ctx.repo.root, ctx.env, settle=False)
    ctx.store, ctx.config = learned.store, learned.config
    assert "no learning snapshot recorded" in run("--explain")
    for i in range(4):
        transcript = trap_session(str(tmp_path / f"s{i}.jsonl"), ctx.repo.root, i)
        run_session(ctx.env, ctx.repo.root, f"s{i}", transcript)
        if i == 0:
            assert "1 candidate, 0 active, 0 retired" in run("--explain")
    assert "cached facts are available for session start" in run("--explain")


def test_real_cli_selects_the_requested_repository_without_applying_a_pending_proposal(tmp_path):
    repo = tmp_path / "repo with spaces and 'quotes'"
    elsewhere = tmp_path / "elsewhere"
    repo.mkdir()
    elsewhere.mkdir()
    env = dict(os.environ, TOKENECONOMIZER_HOME=str(tmp_path / "state"),
               GIT_CEILING_DIRECTORIES=str(tmp_path), TE_MICROSKILLS="envfacts",
               TE_TOOLPRUNE="auto", TE_LOOPGUARD="nudge", TE_SHADOW="0", TE_DISABLE="0",
               PYTHONPATH=str(Path(__file__).resolve().parents[1]), PYTHONUTF8="1")
    ctx = LiveContext.build(hosts.Codex(), str(repo), env, settle=False)
    ctx.store.write("injection.json", {"sessions": 2, "rendered": "", "facts": {
        "test-command": {"state": "CANDIDATE", "fire": "2/2", "inject": False}}})
    ctx.store.write("untool.json", {"sessions": 5, "proposal": ["goals"],
                                  "auto_pending": {"items": ["goals"]}})
    ctx.store.write("envelope.json", {"resolved_turns": [10, 12]})
    before = {p.name: p.read_bytes() for p in Path(ctx.store.base).glob("*.json")}

    def run(*options):
        result = subprocess.run(
            [sys.executable, "-m", "tokeneconomizer.plugin.cli", "--host", "codex",
             "--cwd", str(repo), "status", *options], env=env, cwd=elsewhere,
            capture_output=True, text=True, encoding="utf-8", timeout=30)
        assert result.returncode == 0, result.stderr
        return result.stdout

    plain = json.loads(run("--json"))
    explained = json.loads(run("--json", "--explain"))
    assert explained["repo_root"] == str(repo)
    assert "1 candidate" in explained["explanations"]["microskills"]
    assert "held for the idle wait" in explained["explanations"]["toolprune"]
    assert "same --host/--cwd" in explained["explanations"]["microskills"]
    explained.pop("explanations")
    assert explained == plain
    text = run("--explain")
    assert len(explanations(text)) == 3
    assert all(len(line) <= 160 for line in explanations(text))
    assert not (repo / ".codex" / "config.toml").exists()
    assert {p.name: p.read_bytes() for p in Path(ctx.store.base).glob("*.json")} == before
