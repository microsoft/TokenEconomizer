"""Regression tests for Cursor hook recording lifecycle: enabled vs disabled."""
import io
import json
import os

import pytest

from tokeneconomizer.plugin import hook
from tokeneconomizer.plugin.hosts import RESULT_CAP, Cursor
from tokeneconomizer.plugin.paths import repo_identity


def _call_hook(payload, env, host="cursor"):
    out = io.StringIO()
    ret = hook.main(["--host", host] if host else [], io.StringIO(json.dumps(payload)), out, env)
    return ret, json.loads(out.getvalue())


@pytest.fixture
def recording_env(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git" / "info").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (repo / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://github.com/acme/project.git\n'
    )
    state = tmp_path / "state"
    state.mkdir(parents=True)
    env = {
        "TOKENECONOMIZER_HOME": str(state),
        "GIT_CEILING_DIRECTORIES": str(tmp_path),
        "PATH": os.environ.get("PATH", ""),
        "TE_LOOPGUARD": "off",
        "TE_MICROSKILLS": "off",
        "TE_REPOMAP": "off",
    }
    return repo, state, env


def _make_prompt_payload(repo, conversation_id="conv-rec-1"):
    return {
        "hook_event_name": "beforeSubmitPrompt",
        "conversation_id": conversation_id,
        "generation_id": "gen-p-1",
        "prompt": "Fix bug in payment handler",
        "workspace_roots": [str(repo)],
        "cwd": str(repo),
    }


def _make_tool_payload(repo, conversation_id="conv-rec-1"):
    return {
        "hook_event_name": "postToolUse",
        "conversation_id": conversation_id,
        "generation_id": "gen-p-1",
        "tool_use_id": "tool-rec-1",
        "tool_name": "Shell",
        "tool_input": {"command": "npm test"},
        "tool_output": '{"exitCode":0,"output":"PASS test/pay.test.js"}',
        "workspace_roots": [str(repo)],
        "cwd": str(repo),
    }


def test_cursor_records_prompt_and_tool_when_enabled(recording_env):
    repo, state, env = recording_env
    ret1, res1 = _call_hook(_make_prompt_payload(repo), env)
    assert ret1 == 0 and res1 == {}

    ret2, res2 = _call_hook(_make_tool_payload(repo), env)
    assert ret2 == 0 and res2 == {}

    spool = state / "cursor-sessions" / "conv-rec-1.jsonl"
    assert spool.exists()
    rows = [json.loads(line) for line in spool.read_text().splitlines() if line.strip()]
    assert len(rows) == 2
    assert rows[0]["hook_event_name"] == "UserPromptSubmit"
    assert rows[0]["prompt"] == "Fix bug in payment handler"
    assert rows[1]["hook_event_name"] == "PostToolUse"
    assert rows[1]["tool_response"] == {
        "content": "PASS test/pay.test.js",
        "exit_code": 0,
        "isError": False,
    }


def test_global_config_disabled_creates_no_new_spool_and_does_not_append(recording_env):
    repo, state, env = recording_env
    (state / "config.json").write_text(json.dumps({"enabled": False}))

    ret1, res1 = _call_hook(_make_prompt_payload(repo, "conv-disabled-global"), env)
    assert ret1 == 0 and res1 == {}
    spool = state / "cursor-sessions" / "conv-disabled-global.jsonl"
    assert not spool.exists()

    spool.parent.mkdir(parents=True, exist_ok=True)
    spool.write_text('{"seed":true}\n')
    ret2, res2 = _call_hook(_make_tool_payload(repo, "conv-disabled-global"), env)
    assert ret2 == 0 and res2 == {}
    assert spool.read_text() == '{"seed":true}\n'


def test_repo_config_disabled_creates_no_new_spool_and_does_not_append(recording_env):
    repo, state, env = recording_env
    repo_id = repo_identity(str(repo), env)
    repo_cfg_dir = state / "repos" / repo_id.key
    repo_cfg_dir.mkdir(parents=True, exist_ok=True)
    (repo_cfg_dir / "config.json").write_text(json.dumps({"enabled": False}))

    ret1, res1 = _call_hook(_make_prompt_payload(repo, "conv-disabled-repo"), env)
    assert ret1 == 0 and res1 == {}
    spool = state / "cursor-sessions" / "conv-disabled-repo.jsonl"
    assert not spool.exists()

    spool.parent.mkdir(parents=True, exist_ok=True)
    spool.write_text('{"seed":true}\n')
    ret2, res2 = _call_hook(_make_tool_payload(repo, "conv-disabled-repo"), env)
    assert ret2 == 0 and res2 == {}
    assert spool.read_text() == '{"seed":true}\n'


def test_env_disable_and_sidecall_skip_recording(recording_env):
    repo, state, env = recording_env

    env_disable = dict(env, TE_DISABLE="1")
    ret1, res1 = _call_hook(_make_prompt_payload(repo, "conv-env-skip"), env_disable)
    assert ret1 == 0 and res1 == {}
    spool = state / "cursor-sessions" / "conv-env-skip.jsonl"
    assert not spool.exists()

    env_sidecall = dict(env, TE_SIDECALL="1")
    ret2, res2 = _call_hook(_make_tool_payload(repo, "conv-env-skip"), env_sidecall)
    assert ret2 == 0 and res2 == {}
    assert not spool.exists()


def test_recording_keeps_adapter_fields_and_drops_user_email(recording_env):
    repo, state, env = recording_env
    payload = _make_prompt_payload(repo, "conv-fields")
    payload["user_email"] = "person@example.com"
    payload["CURSOR_USER_EMAIL"] = "person@example.com"
    assert _call_hook(payload, env) == (0, {})
    spool = state / "cursor-sessions" / "conv-fields.jsonl"
    row = json.loads(spool.read_text().splitlines()[0])
    assert row["hook_event_name"] == "UserPromptSubmit"
    assert row["prompt"] == "Fix bug in payment handler"
    assert "user_email" not in row
    assert "CURSOR_USER_EMAIL" not in row
    assert "user_email" not in json.dumps(row)


def test_recording_caps_tool_output(recording_env):
    repo, state, env = recording_env
    payload = _make_tool_payload(repo, "conv-cap")
    payload["tool_output"] = json.dumps({"exitCode": 0, "output": "x" * (RESULT_CAP + 40)})
    assert _call_hook(payload, env) == (0, {})
    row = json.loads((state / "cursor-sessions" / "conv-cap.jsonl").read_text().splitlines()[0])
    assert row["tool_response"]["content"] == "x" * RESULT_CAP
    assert "user_email" not in row


def test_recording_caps_tool_input_and_keeps_its_short_fields(recording_env):
    repo, state, env = recording_env
    payload = _make_tool_payload(repo, "conv-input")
    payload.update(tool_name="Write",
                   tool_input={"file_path": "src/big.py", "contents": "y" * (RESULT_CAP + 40)})
    assert _call_hook(payload, env) == (0, {})
    row = json.loads((state / "cursor-sessions" / "conv-input.jsonl").read_text().splitlines()[0])
    assert row["tool_input"] == {"file_path": "src/big.py", "contents": "y" * RESULT_CAP}


def test_settle_deletes_processed_session_file(tmp_path):
    spool = tmp_path / "cursor-sessions" / "conv.jsonl"
    spool.parent.mkdir()
    spool.write_text("{}\n")
    other = tmp_path / "elsewhere" / "conv.jsonl"
    other.parent.mkdir()
    other.write_text("{}\n")
    host = Cursor()
    host.settle(str(spool))
    host.settle(str(other))
    assert not spool.exists()
    assert other.exists()
