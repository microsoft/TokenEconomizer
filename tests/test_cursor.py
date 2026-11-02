"""Cursor hook adapter: published payloads, private event capture and trace reconstruction."""
import json
import os
from pathlib import Path

import pytest

from tokeneconomizer import trace
from tokeneconomizer.plugin.hosts import (
    ClaudeCode, Codex, Copilot, Cursor, OpenCode, Pi,
)
from tokeneconomizer.plugin.hook import run
from tokeneconomizer.plugin.paths import repo_identity, repo_state_dir
from tokeneconomizer.plugin.store import Store


def test_cursor_translates_prompt_and_tool_hook_schemas(tmp_path):
    host = Cursor()
    env = {"TOKENECONOMIZER_HOME": str(tmp_path / "state"),
           "CURSOR_PROJECT_DIR": str(tmp_path / "repo")}

    prompt = host.prepare({
        "hook_event_name": "beforeSubmitPrompt", "conversation_id": "conv-1",
        "generation_id": "gen-1", "prompt": "Fix the bug",
        "workspace_roots": [str(tmp_path / "repo")],
    }, env)
    assert prompt["hook_event_name"] == "UserPromptSubmit"
    assert (prompt["session_id"], prompt["prompt_id"], prompt["cwd"]) == (
        "conv-1", "gen-1", str(tmp_path / "repo"))
    assert prompt["transcript_path"] == str(
        tmp_path / "state" / "cursor-sessions" / "conv-1.jsonl")

    success = host.prepare({
        "hook_event_name": "postToolUse", "conversation_id": "conv-1",
        "generation_id": "gen-1", "tool_name": "Shell",
        "tool_input": {"command": "pytest"},
        "tool_output": '{"exitCode":1,"stdout":"failed","stderr":"details"}',
    }, env)
    assert success["hook_event_name"] == "PostToolUse"
    assert success["tool_use_id"] == "gen-1"
    assert success["tool_response"] == {
        "content": "failed\ndetails", "exit_code": 1, "isError": False}

    failure = host.prepare({
        "hook_event_name": "postToolUseFailure", "conversation_id": "conv-1",
        "generation_id": "gen-1", "tool_name": "Shell",
        "tool_input": {"command": "rm -rf /"}, "error_message": "Denied",
        "failure_type": "permission_denied",
    }, env)
    assert failure["hook_event_name"] == "PostToolUseFailure"
    assert failure["error"] == "Permission denied: Denied"


def test_cursor_plugin_declares_all_supported_hook_events():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "plugin/.cursor-plugin/plugin.json").read_text())
    hooks = json.loads((root / "plugin/hooks/cursor-hooks.json").read_text())["hooks"]
    assert manifest["name"] == "tokeneconomizer"
    assert manifest["hooks"] == "./hooks/cursor-hooks.json"
    assert set(hooks) == {
        "sessionStart", "beforeSubmitPrompt", "postToolUse", "postToolUseFailure",
        "stop", "sessionEnd",
    }
    assert all("${CURSOR_PLUGIN_ROOT}/scripts/te-hook" in hook["command"]
               and "--host cursor" in hook["command"]
               for entries in hooks.values() for hook in entries)


def test_cursor_events_reconstruct_tool_results_and_count_prompts(tmp_path):
    source, target = tmp_path / "events.jsonl", tmp_path / "trace.jsonl"
    events = [
        {"hook_event_name": "UserPromptSubmit", "prompt": "Fix the bug"},
        {"hook_event_name": "PostToolUse", "tool_use_id": "t1", "tool_name": "Shell",
         "tool_input": {"command": "pytest"},
         "tool_response": {"content": "3 passed", "exit_code": 0}},
        {"hook_event_name": "PostToolUseFailure", "tool_use_id": "t2",
         "tool_name": "Read", "tool_input": {"file_path": "missing"},
         "error": "not found"},
    ]
    source.write_text("\n".join(json.dumps(event) for event in events) + "\n")

    assert trace.cursor_prompt_count(str(source)) == 1
    assert trace.cursor_events_to_claude(str(source), str(target)) == 5
    calls = trace.load_trajectory(str(target))
    assert [(name, args, ok) for name, args, _result, ok in calls] == [
        ("Bash", {"command": "pytest"}, True),
        ("Read", {"file_path": "missing"}, False),
    ]


@pytest.fixture
def cursor_env(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git" / "info").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (repo / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://github.com/acme/widget.git\n')
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    env = {
        "TOKENECONOMIZER_HOME": str(state),
        "CURSOR_PROJECT_DIR": str(repo),
        "TE_LOOPGUARD": "off",
        "PATH": os.environ.get("PATH", ""),
        "GIT_CEILING_DIRECTORIES": str(tmp_path),
    }
    store = Store(repo_state_dir(repo_identity(str(repo), env), "cursor", env))
    return str(repo), env, store


@pytest.mark.parametrize("host_cls", [ClaudeCode, Codex, Copilot, OpenCode, Pi])
def test_host_stopped_normally_default(host_cls):
    assert host_cls().stopped_normally({}) is True


@pytest.mark.parametrize("status,expected_normal", [
    ("completed", True), ("aborted", False), ("error", False), (None, False), ("unknown", False),
])
def test_cursor_stop_lifecycle_status_handling(cursor_env, status, expected_normal):
    repo, env, store = cursor_env
    env = dict(env, TE_LOOPGUARD="on")
    base = {"conversation_id": "c1", "generation_id": "g1", "workspace_roots": [repo]}
    run(dict(base, hook_event_name="beforeSubmitPrompt", prompt="check"), env, host_hint="cursor")
    run(dict(base, hook_event_name="postToolUse", tool_name="Shell", tool_use_id="t1",
             tool_input={"command": "pytest"},
             tool_output=json.dumps({"output": "1 passed", "exitCode": 0})), env, host_hint="cursor")
    stop = dict(base, hook_event_name="stop")
    if status is not None:
        stop["status"] = status
    assert run(stop, env, host_hint="cursor") == {}

    sess = store.read("sessions/c1.json")
    assert sess and sess.get("turn") is None and len(sess.get("turns", [])) == 1
    assert sess["turns"][0]["resolved_proxy"] is expected_normal
    envelope = store.read("envelope.json") or {}
    assert envelope.get("resolved_turns", []) == ([1] if expected_normal else [])


def test_cursor_native_shell_json_output_positive_and_negative(tmp_path, cursor_env):
    repo, env, _ = cursor_env
    base = {"conversation_id": "c-shell", "generation_id": "g-shell", "workspace_roots": [repo]}
    run(dict(base, hook_event_name="beforeSubmitPrompt", prompt="build & test"), env, host_hint="cursor")
    calls = [("t1", "make build", {"output": "ok\n", "exitCode": 0}),
             ("t2", "pytest -q", {"output": "failed\n", "exitCode": 1})]
    for tid, cmd, out in calls:
        run(dict(base, hook_event_name="postToolUse", tool_name="Shell", tool_use_id=tid,
                 tool_input={"command": cmd}, tool_output=json.dumps(out)), env, host_hint="cursor")

    host = Cursor()
    tp = host.transcript(host.prepare(dict(base, session_id="c-shell"), env))
    trace_path = host.mechanism_trace(tp, str(tmp_path))
    assert trace.load_trajectory(trace_path) == [
        ("Bash", {"command": "make build"}, "ok\n", True),
        ("Bash", {"command": "pytest -q"}, "failed\n", False),
    ]


def test_notices_name_each_agents_side_call_command():
    from tokeneconomizer.plugin import sidecall
    hosts = ("claude", "codex", "copilot", "pi", "opencode", "cursor")
    assert [sidecall.call_name(h) for h in hosts] == [
        "claude -p", "codex exec", "copilot -p", "pi -p", "opencode run", "cursor-agent -p"]
