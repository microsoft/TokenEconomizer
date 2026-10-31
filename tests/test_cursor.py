"""Cursor hook adapter: published payloads, private event capture and trace reconstruction."""
import json
import os
from pathlib import Path

from tokeneconomizer import trace
from tokeneconomizer.plugin.config import LiveConfig
from tokeneconomizer.plugin.hosts import Cursor
from tokeneconomizer.plugin.hook import run
from tokeneconomizer.plugin.paths import state_root


def test_tool_pruning_can_be_disabled_by_a_user_config():
    config = LiveConfig()
    assert config.toolprune == "auto"
    assert config.set("toolprune", "off") and config.toolprune == "off"


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


def test_cursor_records_only_allowlisted_repository_events(tmp_path):
    allowed = tmp_path / "allowed"
    (allowed / ".git" / "info").mkdir(parents=True)
    (allowed / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (allowed / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://github.com/acme/widget.git\n')
    excluded = tmp_path / "excluded"
    (excluded / ".git" / "info").mkdir(parents=True)
    (excluded / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (excluded / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://github.com/acme/other.git\n')
    state = tmp_path / "state"
    os.makedirs(state)
    (state / "config.json").write_text(json.dumps({"repositories": [str(allowed)]}))
    env = {"TOKENECONOMIZER_HOME": str(state), "TE_LOOPGUARD": "off",
           "GIT_CEILING_DIRECTORIES": str(tmp_path), "PATH": os.environ.get("PATH", "")}

    event = {"hook_event_name": "beforeSubmitPrompt", "conversation_id": "conversation-a",
             "generation_id": "generation-a", "prompt": "Fix the issue",
             "workspace_roots": [str(excluded)]}
    assert run(event, env, host_hint="cursor") == {}
    assert not os.path.exists(state_root(env) + "/cursor-sessions")

    event["workspace_roots"] = [str(allowed)]
    assert run(event, env, host_hint="cursor") == {}
    spool = state / "cursor-sessions" / "conversation-a.jsonl"
    rows = [json.loads(line) for line in spool.read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["prompt"] == "Fix the issue"


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
