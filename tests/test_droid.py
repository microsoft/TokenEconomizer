"""Factory Droid: the session reader, its host adapter, usage, and history import."""
import json
import os
import subprocess
import time

import pytest

from tokeneconomizer import trace
from tokeneconomizer.plugin import accounting, denylist, history, hosts
from tokeneconomizer.plugin.context import LiveContext
from tokeneconomizer.plugin.paths import repo_identity


def _write_session(root, sid, cwd, calls, parent=None, workspace="ws", age_s=7200,
                   usage=None, model="claude-sonnet-5"):
    """A synthetic Droid session: `calls` is [(tool, input, result_text, is_error)]."""
    folder = os.path.join(str(root), workspace)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{sid}.jsonl")
    head = {"type": "session_start", "id": sid, "cwd": cwd, "version": 2}
    if parent:
        head["callingSessionId"] = parent
    lines = [head,
             {"type": "message", "timestamp": "2026-01-01T00:00:00.000Z", "message": {
                 "role": "user", "content": [{"type": "text", "text": "run the tests"}]}}]
    for i, (name, inp, res, is_error) in enumerate(calls):
        tid = f"t{i}"
        lines.append({"type": "message", "message": {
            "role": "assistant", "modelId": model,
            "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}})
        lines.append({"type": "message", "message": {
            "role": "user", "content": [{"type": "tool_result", "tool_use_id": tid,
                                          "is_error": is_error, "content": res}]}})
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(x) for x in lines) + "\n")
    old = time.time() - age_s
    os.utime(path, (old, old))
    # Droid keeps usage beside the log. Only the sessions that ask for it have one.
    if usage is not None:
        settings = path[:-len(".jsonl")] + ".settings.json"
        with open(settings, "w", encoding="utf-8") as fh:
            json.dump({"model": model, "tokenUsage": usage}, fh)
        os.utime(settings, (old, old))
    return path


FAIL = ("python: can't open file 'tests/runtests.py': [Errno 2] No such file or directory")
OK = "Ran 3 tests in 0.01s\nOK"
TEST_CALLS = [("Execute", {"command": "python tests/runtests.py"}, FAIL, True),
              ("Execute", {"command": "python runtests.py"}, OK, False)]

USAGE = {"inputTokens": 1200, "outputTokens": 400, "cacheCreationTokens": 3000,
         "cacheReadTokens": 9000, "thinkingTokens": 20, "factoryCredits": 700}


def _env(state):
    return {"TOKENECONOMIZER_HOME": str(state), "TE_FOLD_SYNC": "1",
            "TE_MICROSKILLS": "envfacts", "TE_TOOLPRUNE": "off"}


# ---- the reader -----------------------------------------------------------------------------
def test_reader_maps_droid_names_and_skips_reminders(tmp_path):
    path = _write_session(tmp_path, "s1", str(tmp_path), TEST_CALLS)
    # Injected reminders, a hidden message and a tool result are not prompts; the one typed
    # prompt is.
    assert trace.droid_prompt_count(path) == 1
    out = str(tmp_path / "claude.jsonl")
    assert trace.droid_session_to_claude(path, out) == 2
    calls = trace.load_trajectory(out)
    assert [c[0] for c in calls] == ["Bash", "Bash"]
    assert calls[0][1]["command"] == "python tests/runtests.py"
    assert calls[0][3] is False and calls[1][3] is True
    assert trace.call_cwds(out) == [str(tmp_path), str(tmp_path)]


def test_started_is_epoch_seconds_in_the_head_and_in_the_row(tmp_path):
    """`te history import` sorts and prints these with `time.localtime`: Droid's own ISO
    timestamp there raised TypeError once the plan was printed."""
    path = _write_session(tmp_path / "sessions", "s1", str(tmp_path), TEST_CALLS)
    head = trace.droid_log_head(path)
    assert head["started"] == 1767225600.0          # 2026-01-01T00:00:00Z
    row = trace.droid_sessions(str(tmp_path / "sessions"))[0]
    assert row["started"] == head["started"]


def test_prompt_count_ignores_hidden_and_reminder_messages(tmp_path):
    path = _write_session(tmp_path, "s2", str(tmp_path), [])
    with open(path, "a", encoding="utf-8") as fh:
        for msg in ({"role": "user", "visibility": "llm_only",
                     "content": [{"type": "text", "text": "a reminder"}]},
                    {"role": "user",
                     "content": [{"type": "text", "text": "<system-reminder>meta"}]},
                    {"role": "user", "content": [{"type": "text", "text": "a real prompt"}]}):
            fh.write(json.dumps({"type": "message", "message": msg}) + "\n")
    assert trace.droid_prompt_count(path) == 2       # the fixture's own + the real one


# ---- the host ---------------------------------------------------------------------------------
def test_host_tool_mapping_and_usage():
    host = hosts.Droid()
    assert host.name == "droid"
    name, args, _res, code = host.tool_call({"tool_name": "Execute", "tool_input": {
        "command": "pytest -q"}, "tool_response": {"output": "ok", "exitCode": 0}})
    assert (name, args["command"], code) == ("bash", "pytest -q", 0)
    name, args, _res, _ = host.tool_call({"tool_name": "Create", "tool_input": {
        "file_path": "/tmp/x"}, "tool_response": "written"})
    assert (name, args["path"]) == ("create", "/tmp/x")
    name, _args, _res, _ = host.tool_call({"tool_name": "Edit", "tool_input": {
        "file_path": "/tmp/x"}, "tool_response": "edited"})
    assert name == "edit"
    # A log with no settings file beside it is not guessed at.
    assert host.session_usage("/nonexistent")["input_tokens"] is None
    assert accounting.effective(host.session_usage("/nonexistent")) == (None, "no_usage")


def test_usage_comes_from_the_settings_file_beside_the_log(tmp_path):
    path = _write_session(tmp_path, "s1", str(tmp_path), TEST_CALLS, usage=USAGE)
    usage = trace.droid_session_usage(path)
    assert usage["input_tokens"] == 1200
    assert usage["cache_read_input_tokens"] == 9000
    assert usage["cache_creation_input_tokens"] == 3000
    assert usage["output_tokens"] == 400
    assert usage["model"] == "claude-sonnet-5"
    assert usage["requests"] == 2                    # two assistant messages
    assert usage["factory_credits"] == 700
    effective, priced_by = accounting.effective(usage)
    assert priced_by == "model" and effective > 0


def test_an_unpriced_model_is_labelled_fallback(tmp_path):
    path = _write_session(tmp_path, "s1", str(tmp_path), TEST_CALLS, usage=USAGE,
                          model="custom:cline-sub:glm-5-3-flash")
    effective, priced_by = accounting.effective(trace.droid_session_usage(path))
    assert priced_by == "fallback" and effective > 0


def test_detect_finds_droid_from_the_session_path(tmp_path):
    tp = os.path.join(str(tmp_path), ".factory", "sessions", "ws", "s.jsonl")
    assert isinstance(hosts.detect({"transcript_path": tp}, {}), hosts.Droid)
    assert isinstance(hosts.detect({}, {"DROID_PLUGIN_ROOT": "/x"}), hosts.Droid)


def test_a_droid_shell_is_about_droid(tmp_path, monkeypatch):
    for k in ("TE_HOST", "CLAUDECODE", "COPILOT_CLI", "CURSOR_AGENT", "CURSOR_PROJECT_DIR"):
        monkeypatch.delenv(k, raising=False)
    assert hosts.default_host({"FACTORY_SESSION_ID": "s"}) == "droid"


def test_tool_pruning_is_not_supported_for_droid():
    assert denylist.supports("droid") is False


def test_side_calls_are_off_unless_the_person_enables_them(tmp_path, monkeypatch):
    from tokeneconomizer.plugin import sidecall
    from tokeneconomizer.plugin.config import load_config
    assert sidecall.droid_enabled({}) is False
    assert sidecall.droid_enabled({"TE_DROID_SIDECALL": "1"}) is True
    cfg = load_config(str(tmp_path), env={})
    monkeypatch.delenv("TE_DROID_SIDECALL", raising=False)
    assert sidecall.runner_for("droid", cfg) is None
    monkeypatch.setenv("TE_DROID_SIDECALL", "1")
    assert callable(sidecall.runner_for("droid", cfg))


def test_the_setting_turns_side_calls_on_without_a_variable(tmp_path, monkeypatch):
    """The desktop app cannot set an environment variable for its hooks, so the same switch
    is a setting."""
    from tokeneconomizer.plugin import sidecall
    from tokeneconomizer.plugin.config import load_config, write_config
    monkeypatch.delenv("TE_DROID_SIDECALL", raising=False)
    write_config(str(tmp_path / "config.json"), {"droid_sidecall": "on"})
    cfg = load_config(str(tmp_path), env={})
    assert cfg.droid_sidecall is True
    assert callable(sidecall.runner_for("droid", cfg))


def test_the_setting_is_off_by_default_and_shown_by_config_show():
    from tokeneconomizer.plugin.cli import _config_view
    from tokeneconomizer.plugin.config import LiveConfig
    assert LiveConfig().droid_sidecall is False
    assert _config_view(LiveConfig())["droid"] == {"state": "off", "droid_sidecall": False}


@pytest.mark.real_sidecalls
def test_a_droid_side_call_allows_only_the_read_tool(monkeypatch):
    """Droid's default read-only autonomy still allows its shell, the web tools and every MCP
    tool, so the side call names the one tool it may use instead of asking the model not to."""
    from tokeneconomizer.plugin import sidecall
    monkeypatch.setattr(sidecall, "program", lambda cli: [cli])
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(cmd=cmd, **kw)
        out = json.dumps({"type": "result", "result": "- use python -m pytest",
                          "session_id": "s1",
                          "usage": {"input_tokens": 900, "output_tokens": 40}})
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(sidecall.subprocess, "run", fake_run)
    text, eff = sidecall.droid_call("propose facts", model="claude-sonnet-5")
    assert text == "- use python -m pytest" and eff and eff > 0
    cmd = seen["cmd"]
    assert cmd[:2] == ["droid", "exec"] and cmd[-1] == "propose facts"
    assert cmd[cmd.index("--only-tools") + 1] == sidecall.DROID_TOOLS == "Read"
    assert "--auto" not in cmd and "--skip-permissions-unsafe" not in cmd
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-5"
    assert seen["env"]["TE_SIDECALL"] == "1" and seen["stdin"] == subprocess.DEVNULL
    assert seen["cwd"] != os.getcwd()                       # a throwaway directory


def test_the_settings_usage_is_normalized_for_pricing():
    from tokeneconomizer.plugin import sidecall
    usage = sidecall._droid_usage({"inputTokens": 5, "outputTokens": 7,
                                   "cacheReadTokens": 11, "cacheCreationTokens": 3})
    assert usage["input_tokens"] == 5 and usage["output_tokens"] == 7
    assert usage["cache_read_input_tokens"] == 11
    assert usage["cache_creation_input_tokens"] == 3
    assert sidecall._droid_usage({"outputTokens": 7}) is None


# ---- history import ---------------------------------------------------------------------------
def _droid_ctx(env, repo):
    return LiveContext.build(hosts.Droid(), str(repo), env)


def test_history_lists_droid_sessions_and_leaves_subagents_out(tmp_path):
    sessions = tmp_path / "sessions"
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_session(sessions, "old", str(repo), TEST_CALLS)
    _write_session(sessions, "sub", str(repo), TEST_CALLS, parent="old")
    rows = hosts.Droid().history({"DROID_SESSIONS_DIR": str(sessions)})
    assert [r["session_id"] for r in rows] == ["old"]
    assert rows[0]["folder"] == "ws"


def test_history_import_learns_facts_and_prices_the_sessions(tmp_path):
    state, sessions = tmp_path / "state", tmp_path / "sessions"
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "runtests.py").write_text("print('OK')\n")
    for i in range(5):
        _write_session(sessions, f"s{i}", str(repo), TEST_CALLS, age_s=7200 + i * 60,
                       usage=USAGE)
    env = dict(_env(state), DROID_SESSIONS_DIR=str(sessions))
    ctx = _droid_ctx(env, repo)
    assert len(history.plan(ctx)["sessions"]) == 5
    res = history.run(ctx, now=time.time())
    assert res["state"] == "done" and res["imported"] == 5

    assert history.describe(history.summary(ctx), "droid").startswith(
        "read 5 earlier session(s) from Droid's history")

    injected = ctx.store.read("injection.json")
    assert injected["profile"] == "envfacts"
    assert injected["sessions"] == 5
    assert "`python runtests.py" in injected["rendered"]

    rows = [r for r in ctx.store.read_lines("ledger.jsonl") if r.get("kind") == "session"]
    assert len(rows) == 5
    assert all(r["host"] == "droid" and r["prompts"] == 1 for r in rows)
    assert all(r["effective_tokens"] and r["priced_by"] == "model" for r in rows)


def test_history_plan_skips_subagents_and_sessions_still_being_written(tmp_path):
    state, sessions = tmp_path / "state", tmp_path / "sessions"
    repo, other = tmp_path / "repo", tmp_path / "other"
    repo.mkdir()
    other.mkdir()
    _write_session(sessions, "mine", str(repo), TEST_CALLS)
    _write_session(sessions, "sub", str(repo), TEST_CALLS, parent="mine")
    _write_session(sessions, "live", str(repo), TEST_CALLS, age_s=0)
    _write_session(sessions, "elsewhere", str(other), TEST_CALLS)
    env = dict(_env(state), DROID_SESSIONS_DIR=str(sessions))
    plan = history.plan(_droid_ctx(env, repo))
    assert [s["id"] for s in plan["sessions"]] == ["mine"]
    assert plan["skipped"] == {"recent": 1}


def test_the_session_start_hook_returns_the_learned_fact(tmp_path):
    """The offline end to end: five finished sessions are imported, and the next session
    start's own stdout carries the fact Droid appends to the model's context."""
    from test_live_hooks import call

    state, sessions = tmp_path / "state", tmp_path / "sessions"
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "runtests.py").write_text("print('OK')\n")
    for i in range(5):
        _write_session(sessions, f"s{i}", str(repo), TEST_CALLS, age_s=7200 + i * 60)
    env = dict(_env(state), DROID_SESSIONS_DIR=str(sessions))
    assert history.run(_droid_ctx(env, repo), now=time.time())["imported"] == 5

    transcript = str(sessions / "ws" / "s5.jsonl")
    out = call(env, {"session_id": "s5", "transcript_path": transcript, "cwd": str(repo),
                     "hook_event_name": "SessionStart", "source": "startup"}, "droid")
    assert "`python runtests.py" in out["hookSpecificOutput"]["additionalContext"]


# ---- packaging --------------------------------------------------------------------------------
def test_droid_plugin_packaging(tmp_path):
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    manifest = os.path.join(repo_root, "plugin", ".factory-plugin", "plugin.json")
    assert json.load(open(manifest))["name"] == "tokeneconomizer"
    hooks = json.load(open(os.path.join(repo_root, "plugin", "hooks", "droid-hooks.json")))
    assert hooks["SessionStart"][0]["hooks"][0]["command"].endswith("--host droid")
    assert "PostToolUseFailure" not in hooks        # Droid has no such event

    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "build_plugin", os.path.join(repo_root, "scripts", "build_plugin.py"))
    build_plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build_plugin)
    out = tmp_path / "marketplace"
    build_plugin.build_droid(str(out))
    plugin = out / "tokeneconomizer"
    built_hooks = json.load(open(plugin / "hooks" / "hooks.json"))
    assert set(built_hooks) == {"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop",
                                "SessionEnd"}
    for name in ("config", "facts", "microskills", "repos", "status", "toolprune", "untool"):
        command = (plugin / "commands" / f"tokeneconomizer-{name}.md").read_text(encoding="utf-8")
        assert "${DROID_PLUGIN_ROOT}" in command and "Execute tool" in command
        assert "Bash tool" not in command
    assert (plugin / "lib" / "tokeneconomizer" / "plugin" / "hook.py").exists()
    # Antigravity's files, which sit at the plugin root beside Droid's: Droid reads neither (its
    # hooks are `hooks/hooks.json`), and the bundle ships only what Droid loads.
    assert not (plugin / "hooks.json").exists()
    assert not (plugin / ".antigravity-plugin").exists()
    assert json.load(open(out / ".factory-plugin" / "marketplace.json"))["plugins"][0][
        "name"] == "tokeneconomizer"
