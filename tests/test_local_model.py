"""The on-device model some side calls can run on (`local_model`, `local_roles`).

`fm` is faked throughout: these tests pin the routing, the fallback to the agent's own CLI, the
zero charge, and the call itself (argv, schema, stdin, ANSI stripping), never the model.
"""
import json
import subprocess

import pytest

from tokeneconomizer.loopguard import judge as loop_judge
from tokeneconomizer.plugin import config as live_config
from tokeneconomizer.plugin import sidecall


def cfg(**kw):
    c = live_config.LiveConfig()
    for k, v in kw.items():
        assert c.set(k, v), c.warnings
    return c


# ---- settings ------------------------------------------------------------------------------
def test_local_model_is_off_by_default_and_names_the_judge():
    c = live_config.LiveConfig()
    assert c.local_model == "off" and c.local_roles == ["judge"]


def test_local_settings_parse_and_reject_unknown_values():
    c = live_config.LiveConfig()
    assert c.set("local_model", "fm") and c.local_model == "fm"
    assert c.set("local_roles", "judge, nudge") and c.local_roles == ["judge", "nudge"]
    assert not c.set("local_model", "ollama") and c.local_model == "fm"
    assert not c.set("local_roles", "judge,summarizer") and c.local_roles == ["judge", "nudge"]
    assert len(c.warnings) == 2


def test_local_settings_come_from_the_environment(tmp_path):
    c = live_config.load_config(str(tmp_path), env={"TE_LOCAL_MODEL": "fm",
                                                    "TE_LOCAL_ROLES": "judge,proposer"})
    assert (c.local_model, c.local_roles) == ("fm", ["judge", "proposer"])


@pytest.fixture(autouse=True)
def _on_macos(monkeypatch):
    """`fm` runs only on macOS; these tests run everywhere, as if there."""
    monkeypatch.setattr(sidecall, "_macos", lambda: True)


# ---- routing -------------------------------------------------------------------------------
@pytest.fixture
def fm_ok(monkeypatch):
    calls = []

    def fake(prompt, schema=None, **k):
        calls.append((prompt, schema))
        return '{"stuck": true, "confidence": 0.9, "reason": "same failure"}', 0

    monkeypatch.setattr(sidecall, "fm_available", lambda cli="fm": True)
    monkeypatch.setattr(sidecall, "fm_call", fake)
    return calls


def test_no_local_runner_unless_turned_on_and_the_role_listed(fm_ok):
    assert sidecall.local_runner("judge", cfg()) is None
    assert sidecall.local_runner("nudge", cfg(local_model="fm")) is None
    assert sidecall.local_runner("judge", cfg(local_model="fm")) is not None


def test_local_routes_reads_only_the_settings_and_the_platform(monkeypatch):
    def probe(*a, **k):
        raise AssertionError("local_routes ran a subprocess")
    monkeypatch.setattr(sidecall, "fm_available", probe)
    monkeypatch.setattr(sidecall.subprocess, "run", probe)
    assert sidecall.local_routes("judge", cfg(local_model="fm"))
    assert not sidecall.local_routes("nudge", cfg(local_model="fm"))
    assert not sidecall.local_routes("judge", cfg())
    monkeypatch.setattr(sidecall, "_macos", lambda: False)
    assert not sidecall.local_routes("judge", cfg(local_model="fm"))


@pytest.mark.parametrize("local_model, expect", [("fm", "AsyncJudge"), ("off", "NoVerdict")])
def test_a_paused_hosts_hook_picks_its_judge_from_the_settings_without_probing_fm(
        monkeypatch, local_model, expect):
    # PostToolUse runs `_judge` in a new process on every tool call: it must not run `fm`.
    from tokeneconomizer.plugin import controller
    monkeypatch.setattr(sidecall, "paused", lambda host, root=None: {"at": 1.0})
    monkeypatch.setattr(sidecall, "fm_available", lambda *a, **k: pytest.fail("probed fm"))
    monkeypatch.setattr(sidecall.subprocess, "run", lambda *a, **k: pytest.fail("ran a process"))
    ctx = type("Ctx", (), {"config": cfg(local_model=local_model), "root": None})()
    host = type("Host", (), {"name": "claude"})()
    ctl = type("C", (), {"host": host, "ctx": ctx, "sid": "S"})()
    verdict = controller.LiveController._judge(ctl, {"judge_confidence": 0.7}, envelope={})
    assert type(verdict).__name__ == expect


def test_no_local_runner_off_macos(fm_ok, monkeypatch):
    monkeypatch.setattr(sidecall, "_macos", lambda: False)
    assert sidecall.local_runner("judge", cfg(local_model="fm")) is None


def test_no_local_runner_without_fm(monkeypatch):
    monkeypatch.setattr(sidecall, "fm_available", lambda cli="fm": False)
    assert sidecall.local_runner("judge", cfg(local_model="fm")) is None


def test_runner_for_a_local_role_uses_fm_with_its_schema_and_charges_nothing(fm_ok, monkeypatch):
    host = []
    monkeypatch.setattr(sidecall, "claude_call", lambda p, model="": host.append(p) or ("h", 9.0))
    run = sidecall.runner_for("claude", cfg(local_model="fm"), role="judge")
    text, eff = run("digest", "agent")
    assert json.loads(text)["stuck"] is True and eff == 0 and host == []
    assert fm_ok == [("digest", sidecall.JUDGE_SCHEMA)]


def test_runner_for_without_a_role_or_for_another_role_stays_on_the_host(fm_ok, monkeypatch):
    monkeypatch.setattr(sidecall, "claude_call", lambda p, model="": ("h", 9.0))
    c = cfg(local_model="fm")
    assert sidecall.runner_for("claude", c)("q") == ("h", 9.0)
    assert sidecall.runner_for("claude", c, role="proposer")("q") == ("h", 9.0)
    assert fm_ok == []


def test_a_failed_local_call_falls_back_to_the_agents_cli(monkeypatch):
    monkeypatch.setattr(sidecall, "fm_available", lambda cli="fm": True)
    monkeypatch.setattr(sidecall, "fm_call", lambda *a, **k: (None, 0))
    monkeypatch.setattr(sidecall, "claude_call", lambda p, model="": ("host verdict", 9.0))
    run = sidecall.runner_for("claude", cfg(local_model="fm"), role="judge")
    assert run("digest") == ("host verdict", 9.0)


def test_a_host_with_no_side_calls_gets_none_from_runner_for_and_an_empty_local_fallback(
        fm_ok, monkeypatch):
    monkeypatch.setattr(sidecall, "fm_call", lambda *a, **k: (None, 0))
    c = cfg(local_model="fm", local_roles="proposer")
    assert sidecall.runner_for("antigravity", cfg()) is None
    run = sidecall.runner_for("antigravity", c, role="proposer")
    assert run is not None and run("q") == ("", 0)


# ---- the call ------------------------------------------------------------------------------
@pytest.mark.real_sidecalls
def test_fm_call_runs_greedy_with_the_schema_file_and_the_prompt_on_stdin(monkeypatch):
    seen = {}

    def fake_run(cmd, **k):
        seen["cmd"], seen["input"] = cmd, k.get("input")
        seen["schema"] = json.load(open(cmd[cmd.index("--schema") + 1]))
        return subprocess.CompletedProcess(cmd, 0, stdout='\x1b[1m{"stuck": false}\x1b[0m\n',
                                           stderr="")

    monkeypatch.setattr(sidecall.subprocess, "run", fake_run)
    text, eff = sidecall.fm_call("PROMPT", schema=sidecall.JUDGE_SCHEMA)
    assert (text, eff) == ('{"stuck": false}', 0)
    assert seen["cmd"][:4] == ["/usr/bin/fm", "respond", "--no-stream", "--greedy"]
    assert seen["input"] == "PROMPT" and "PROMPT" not in seen["cmd"]
    assert seen["schema"]["required"] == ["stuck", "confidence", "reason"]


@pytest.mark.real_sidecalls
@pytest.mark.parametrize("rc, out", [(1, ""), (0, ""), (0, "Error: The session's transcript "
                                                           "exceeded the model's context size.")])
def test_fm_call_without_a_usable_reply_returns_none_and_logs(tmp_path, monkeypatch, rc, out):
    monkeypatch.setenv("TOKENECONOMIZER_HOME", str(tmp_path))
    monkeypatch.setattr(sidecall.subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(
        cmd, rc, stdout=out, stderr=""))
    assert sidecall.fm_call("SECRET prompt") == (None, 0)
    log = (tmp_path / "errors.log").read_text(encoding="utf-8")
    assert "sidecall fm: no reply" in log and "SECRET" not in log


@pytest.mark.real_sidecalls
def test_fm_call_that_cannot_start_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENECONOMIZER_HOME", str(tmp_path))

    def boom(cmd, **k):
        raise subprocess.TimeoutExpired(cmd, 60)

    monkeypatch.setattr(sidecall.subprocess, "run", boom)
    assert sidecall.fm_call("q") == (None, 0)


@pytest.mark.real_sidecalls
@pytest.mark.parametrize("macos, installed, rc, out, ok", [
    (True, False, 0, "System model available", False),
    (False, True, 0, "System model available", False),
    (True, True, 0, "System model available", True),
    (True, True, 0, "System model unavailable: modelNotReady", False),
    (True, True, 1, "", False),
])
def test_fm_available_runs_usr_bin_fm_on_macos_only(monkeypatch, macos, installed, rc, out, ok):
    ran = []
    monkeypatch.setattr(sidecall, "_fm_ok", {})
    monkeypatch.setattr(sidecall, "_macos", lambda: macos)
    monkeypatch.setattr(sidecall.os, "access", lambda p, m: installed and p == "/usr/bin/fm")
    monkeypatch.setattr(sidecall.shutil, "which", lambda *a, **k: pytest.fail("looked up PATH"))
    monkeypatch.setattr(sidecall.subprocess, "run", lambda cmd, **k: ran.append(cmd) or
                        subprocess.CompletedProcess(cmd, rc, stdout=out, stderr=""))
    assert sidecall.fm_available() is ok
    assert ran == ([["/usr/bin/fm", "available"]] if macos and installed else [])


# ---- the judge reads the local verdict -----------------------------------------------------
def test_the_stuck_judge_parses_a_schema_ordered_local_verdict_at_zero_cost(fm_ok):
    run = sidecall.runner_for("claude", cfg(local_model="fm"), role="judge")
    v = loop_judge.llm_stuck_judge("turns=20 ...", run)
    assert (v["stuck"], v["confidence"], v["_tokens"]) == (True, 0.9, 0)


# ---- the judge job: the local floor --------------------------------------------------------
class _Store:
    def __init__(self, req):
        self.files = {"judge/S.request.json": req}
        self.appended = []

    def read(self, k):
        return self.files.get(k)

    def write(self, k, v):
        self.files[k] = v

    def append(self, k, v):
        self.appended.append((k, v))


def _job(tmp_path, monkeypatch, confidence, **cfg_kw):
    from tokeneconomizer.plugin import judge as plugin_judge
    monkeypatch.setattr(sidecall, "fm_available", lambda cli="fm": True)
    verdict = json.dumps({"stuck": True, "confidence": confidence, "reason": "same failure"})
    monkeypatch.setattr(sidecall, "fm_call", lambda prompt, schema=None, **k: (
        verdict if schema else "Re-read the failing test before editing again.", 0))
    store = _Store({"id": "J", "digest": "turns=20 ...", "threshold": 0.7})
    host = type("H", (), {"name": "claude"})()
    ctx = type("C", (), {"host": host, "root": str(tmp_path), "store": store,
                         "config": cfg(local_model="fm", local_roles="judge,nudge", **cfg_kw)})()
    v = plugin_judge.run_job(ctx, "S", "J")
    return v, store


def test_a_local_stuck_verdict_below_the_floor_spares_the_turn(tmp_path, monkeypatch):
    v, store = _job(tmp_path, monkeypatch, 0.85)
    assert v["stuck"] is False and v["local"] == "fm" and "_nudge" not in v
    assert v["reason"].startswith("below local_judge_confidence")
    assert store.appended == []                       # nothing spent, nothing charged


def test_a_local_stuck_verdict_at_the_floor_redirects_with_a_local_nudge(tmp_path, monkeypatch):
    v, _ = _job(tmp_path, monkeypatch, 0.92)
    assert v["stuck"] is True and v["_nudge"].startswith("Re-read")


def test_the_floor_is_a_setting(tmp_path, monkeypatch):
    v, _ = _job(tmp_path, monkeypatch, 0.85, local_judge_confidence=0.8)
    assert v["stuck"] is True


def test_the_floor_does_not_apply_when_the_host_answered(tmp_path, monkeypatch):
    from tokeneconomizer.plugin import judge as plugin_judge
    monkeypatch.setattr(sidecall, "fm_available", lambda cli="fm": True)
    monkeypatch.setattr(sidecall, "fm_call", lambda *a, **k: (None, 0))
    monkeypatch.setattr(sidecall, "claude_call", lambda p, model="": (
        '{"stuck": true, "confidence": 0.75, "reason": "loop"}', 5.0))
    store = _Store({"id": "J", "digest": "turns=20 ...", "threshold": 0.7})
    ctx = type("C", (), {"host": type("H", (), {"name": "claude"})(), "root": str(tmp_path),
                         "store": store, "config": cfg(local_model="fm")})()
    v = plugin_judge.run_job(ctx, "S", "J")
    assert v["stuck"] is True and "local" not in v and store.appended   # host spend charged


def test_local_judge_confidence_must_be_a_probability():
    c = live_config.LiveConfig()
    assert c.set("local_judge_confidence", "0.8") and c.local_judge_confidence == 0.8
    assert not c.set("local_judge_confidence", 1.5) and not c.set("local_judge_confidence", True)
