"""Kimi Code 2.6.2, recorded against a scripted loopback model."""
import json
from pathlib import Path

from tokeneconomizer.trace import kimi, load_trajectory


WIRE = Path(__file__).parent / "fixtures" / "kimi-2.6.2-wire.jsonl"


def test_two_turns_with_a_failed_shell_and_a_successful_read(tmp_path):
    assert kimi.kimi_prompt_count(WIRE) == 2

    projected = tmp_path / "claude-format.jsonl"
    assert kimi.kimi_session_to_claude(WIRE, projected) == 2
    call, read = load_trajectory(projected)
    assert call[0] == "Bash"
    assert call[3] is False
    assert "exit code: 7" in call[2]
    assert read == ("Read", {"file_path": "/fixture/fixture.txt"}, "1\tTE_KIMI_READ", True)
    assert "SECOND_TURN_126" not in projected.read_text()


def test_cache_read_is_not_counted_twice():
    assert kimi.kimi_session_usage(WIRE) == {
        "input_tokens": 400,
        "cache_read_input_tokens": 92,
        "cache_creation_input_tokens": 0,
        "output_tokens": 32,
        "requests": 4,
        "model": "fixture",
    }


def test_incomplete_and_unknown_data_do_not_claim_a_session(tmp_path):
    wire = tmp_path / "wire.jsonl"
    wire.write_text('{"type":"unknown"}\n{"type":\n')
    assert kimi.kimi_prompt_count(wire) == 0
    assert kimi.kimi_session_to_claude(wire, tmp_path / "projected") is None
    assert kimi.kimi_session_usage(wire)["requests"] == 0


def test_usage_ignores_other_scopes_invalid_counts_and_partial_rows(tmp_path):
    wire = tmp_path / "wire.jsonl"
    rows = [
        {"type":"usage.record","usageScope":"session","usage":{"output":3000}},
        {"type":"usage.record","usageScope":"turn","usage":[]},
        {"type":"usage.record","usageScope":"turn","usage":{"inputOther":True,"output":-4}},
        {"type":"usage.record","usageScope":"turn","usage":{"output":11},"model":"real"},
    ]
    wire.write_text("".join(json.dumps(row) + "\n" for row in rows))
    result = kimi.kimi_session_usage(wire)
    assert (result["input_tokens"], result["output_tokens"], result["model"]) == (0, 11, "real")
    assert result["requests"] == 1


def test_kimi_hook_path_is_looked_up_by_its_own_session_id(tmp_path):
    from tokeneconomizer.plugin import hosts

    sid = "session_412bb6f9-ab5b-448f-b20a-a79e294284e4"
    transcript = tmp_path / "sessions" / "wd_fixture_0123" / sid / "agents" / "main" / "wire.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(WIRE.read_text())
    payload = {"client_type": "kimi_code_cli", "session_id": sid, "hook_event_name": "PostToolUse"}
    host = hosts.detect(payload, {})
    assert host.name == "kimi"
    assert host.transcript(host.prepare(payload, {"KIMI_CODE_HOME": str(tmp_path)})) == str(transcript)
    assert host.transcript(host.prepare({**payload, "session_id": "../anything"}, {
        "KIMI_CODE_HOME": str(tmp_path)
    })) == ""
    assert hosts.detect({**payload, "session_id": "unrelated"}, {}).name != "kimi"


def test_failed_tool_payload_and_no_sidecall():
    from tokeneconomizer.plugin import config, hosts, sidecall

    adapter = hosts.HOSTS["kimi"]()
    args = {"command": "exit 7"}
    assert adapter.tool_call({
        "tool_name": "Bash", "tool_input": args,
        "error": {"message": "Command failed with exit code: 7.", "retryable": False},
    }) == ("Bash", args, "Command failed with exit code: 7.", 7)
    assert sidecall.runner_for("kimi", config.LiveConfig()) is None


def test_kimi_hook_never_writes_json_or_context_to_stdout(tmp_path):
    from io import StringIO
    from tokeneconomizer.plugin import hook

    payload = StringIO('{"client_type":"kimi_code_cli","hook_event_name":"UserPromptSubmit"}')
    stdout = StringIO()
    assert hook.main(["--host", "kimi"], payload, stdout, {"TE_DISABLE": "1"}) == 0
    assert stdout.getvalue() == ""


def test_shadow_hooks_fold_into_usage_without_changing_kimi_transcript(tmp_path):
    from io import StringIO
    from tokeneconomizer.plugin import fold, hook, hosts
    from tokeneconomizer.plugin.context import LiveContext

    sid = "session_412bb6f9-ab5b-448f-b20a-a79e294284e4"
    home = tmp_path / "kimi"
    wire = home / "sessions" / "wd_fixture_0123" / sid / "agents" / "main" / "wire.jsonl"
    wire.parent.mkdir(parents=True)
    wire.write_text(WIRE.read_text())
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    env = {"KIMI_CODE_HOME": str(home), "TOKENECONOMIZER_HOME": str(tmp_path / "te"), "TE_SHADOW": "1"}
    payload = {"session_id": sid, "client_type": "kimi_code_cli", "cwd": str(repo)}
    for event in ("SessionStart", "UserPromptSubmit"):
        stdin = StringIO(json.dumps({**payload, "hook_event_name": event}))
        stdout = StringIO()
        assert hook.main(["--host", "kimi"], stdin, stdout, env) == 0
        assert stdout.getvalue() == ""

    ctx = LiveContext.build(hosts.Kimi(), str(repo), env)
    session = ctx.store.read(f"sessions/{sid}.json")
    assert session["transcript"] == str(wire)
    record = fold.fold_session(ctx, sid)
    assert record["host"] == "kimi" and record["prompts"] == 2
    assert record["effective_tokens"] and "error" not in record
    assert wire.read_text() == WIRE.read_text()
