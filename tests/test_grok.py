"""Grok Build CLI host: detection, payload translation, the start context delivered at the first
tool, session-log conversion, usage, the side call, and the loop guard end to end.

Payloads are synthetic, in the shape grok 1.0.70 sends: every field under both grok's camelCase
name and Claude's alias, with fake ids and paths."""
import io
import json
import os
import subprocess
from pathlib import Path

import pytest

from tokeneconomizer import trace
from tokeneconomizer.plugin import hook, hosts, sidecall
from tokeneconomizer.plugin.hosts import ClaudeCode, Grok
from tokeneconomizer.plugin.paths import repo_identity, repo_state_dir
from tokeneconomizer.plugin.store import Store

SID = "grok-session-1"
SNAKE = {"SessionStart": "session_start", "UserPromptSubmit": "user_prompt_submit",
         "PostToolUse": "post_tool_use", "PostToolUseFailure": "post_tool_use_failure",
         "Stop": "stop", "StopCancelled": "stop_cancelled", "StopFailure": "stop_failure",
         "SessionEnd": "session_end"}


def payload(event, repo="/work/repo", sid=SID, prompt_id=None, **kw):
    """One grok hook payload, with both spellings of the fields a test sets."""
    p = {"hookEventName": SNAKE[event], "hook_event_name": event, "sessionId": sid,
         "session_id": sid, "cwd": repo, "workspaceRoot": repo,
         "transcriptPath": f"/home/u/.grok/sessions/%2Fwork%2Frepo/{sid}/updates.jsonl",
         "transcript_path": f"/home/u/.grok/sessions/%2Fwork%2Frepo/{sid}/updates.jsonl"}
    if prompt_id:
        p.update(promptId=prompt_id, prompt_id=prompt_id)
    for key, value in kw.items():
        p[key] = value
        alias = {"toolName": "tool_name", "toolInput": "tool_input",
                 "toolResult": "tool_response"}.get(key)
        if alias:
            p[alias] = value
    return p


def bash_result(text, code=0, **kw):
    """grok's shell result: the output twice, once as a list of byte values."""
    return dict({"type": "Bash", "output": list(text.encode()), "exit_code": code,
                 "output_for_prompt": f"exit: {code}\n{text}", "command": "x", "truncated": False,
                 "timed_out": False, "current_dir": "/work/repo", "total_bytes": len(text)}, **kw)


def bash(command, text, code=0, **kw):
    return dict(toolName="run_terminal_command", toolInput={"command": command},
                toolResult=bash_result(text, code, **kw))


@pytest.fixture
def env(tmp_path):
    return {"TOKENECONOMIZER_HOME": str(tmp_path / "home"), "TE_FOLD_SYNC": "1",
            "TE_MICROSKILLS": "envfacts", "GIT_CEILING_DIRECTORIES": str(tmp_path),
            "PATH": os.environ.get("PATH", "")}


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / ".git" / "info").mkdir(parents=True)
    (r / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (r / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://github.com/acme/widget.git\n')
    return str(r)


def call(env, p, host=None):
    out = io.StringIO()
    hook.main(["--host", host] if host else [], io.StringIO(json.dumps(p)), out, env)
    return json.loads(out.getvalue().splitlines()[-1])


def store(env, repo):
    return Store(repo_state_dir(repo_identity(repo, env), "grok", env))


# ---- detection ----------------------------------------------------------------------------
def test_grok_is_recognised_before_the_claude_fallback_with_no_host_flag():
    """The Claude hook file passes no `--host`, and grok runs it."""
    assert isinstance(hosts.detect(payload("PostToolUse")), Grok)
    claude_shape = {k: v for k, v in payload("PostToolUse").items() if k != "hookEventName"}
    assert isinstance(hosts.detect(claude_shape), Grok)          # by its session directory
    assert isinstance(hosts.detect({"session_id": "s"}, {"GROK_HOOK_EVENT": "post_tool_use"}), Grok)
    assert isinstance(hosts.detect({"session_id": "s"}, {"GROK_SESSION_ID": "s"}), Grok)
    assert isinstance(hosts.detect(payload("Stop"), hint="claude"), ClaudeCode)   # a flag wins
    assert isinstance(hosts.detect(payload("Stop"), hint="grok"), Grok)
    assert "grok" in hosts.HOSTS


def test_claude_code_stays_claude_code_even_inside_a_grok_shell():
    claude = {"session_id": "s", "hook_event_name": "PostToolUse",
              "transcript_path": "/home/u/.claude/projects/-work-repo/s.jsonl"}
    assert isinstance(hosts.detect(claude, {"GROK_SESSION_ID": "g"}), ClaudeCode)
    assert isinstance(hosts.detect({"session_id": "s"}, {}), ClaudeCode)


def test_a_te_command_in_a_grok_shell_is_about_grok():
    assert hosts.default_host({"GROK_SESSION_ID": "g"}) == "grok"
    assert hosts.default_host({"GROK_SESSION_ID": "g", "CLAUDECODE": "1"}) == "claude"
    assert Grok().env_session_id({"GROK_SESSION_ID": "g"}) == "g"


# ---- payload translation ------------------------------------------------------------------
def test_stop_is_a_turns_end_only_for_end_turn_with_a_prompt_id():
    host = Grok()
    end = host.prepare(payload("Stop", prompt_id="p1", reason="end_turn"), {})
    assert end["hook_event_name"] == "Stop" and host.stopped_normally(end)
    assert host.turn_key(end) == "p1"
    shutdown = host.prepare(payload("Stop", reason="shutdown"), {})       # no promptId
    assert shutdown["hook_event_name"] == ""
    odd = host.prepare(payload("Stop", prompt_id="p1", reason="shutdown"), {})
    assert odd["hook_event_name"] == ""


@pytest.mark.parametrize("event,reason", [("StopCancelled", "user_interrupt"),
                                          ("StopCancelled", None), ("StopFailure", None)])
def test_a_cancelled_or_failed_turn_stops_abnormally(event, reason):
    host = Grok()
    extra = {"reason": reason} if reason else {}
    p = host.prepare(payload(event, prompt_id="p1", **extra), {})
    assert p["hook_event_name"] == "Stop" and not host.stopped_normally(p)


def test_events_the_plugin_does_not_use_are_ignored():
    p = Grok().prepare({"hookEventName": "pre_tool_use", "hook_event_name": "PreToolUse",
                        "sessionId": "s"}, {})
    assert p["hook_event_name"] == ""
    assert Grok().prepare({"sessionId": "s", "hookEventName": "post_tool_use"}, {})[
        "session_id"] == "s"


def test_a_shell_result_is_read_from_the_text_grok_showed_and_its_exit_code():
    host = Grok()
    p = host.prepare(payload("PostToolUse", **bash(
        "rtk ls nope", "ls: nope: No such file or directory\n", 1)), {})
    name, args, text, code = host.tool_call(p)
    assert (name, args["command"], code) == ("Bash", "ls nope", 1)
    assert text == "ls: nope: No such file or directory\n"          # no byte values, no exit line
    ok = host.tool_call(host.prepare(payload("PostToolUse", **bash("true", "", 0)), {}))
    assert ok[3] == 0
    timed = host.tool_call(host.prepare(payload("PostToolUse", **bash(
        "sleep 9", "", 0, timed_out=True)), {}))
    assert timed[3] == trace.grok.TIMEOUT_CODE


def test_a_result_with_only_the_byte_list_is_decoded_not_listed():
    resp = {"type": "Bash", "output": list("héllo".encode()), "exit_code": 0}
    assert trace.grok_result(resp) == ("héllo", 0)


def test_grok_tools_are_named_as_the_mechanisms_know_them():
    host = Grok()

    def mapped(tool, args, result=None):
        return host.tool_call(host.prepare(payload(
            "PostToolUse", toolName=tool, toolInput=args, toolResult=result), {}))[:2]

    assert mapped("read_file", {"target_file": "/work/repo/a.py", "limit": 5}) == (
        "Read", {"file_path": "/work/repo/a.py", "limit": 5})
    assert mapped("search_replace", {"file_path": "/work/repo/a.py", "old_string": "a",
                                     "new_string": "b"}) == ("Edit", {"file_path": "/work/repo/a.py"})
    assert mapped("grep", {"pattern": "x", "path": "src", "head_limit": 3}) == (
        "Grep", {"pattern": "x", "path": "src"})
    assert mapped("list_dir", {"target_directory": "/work/repo"}) == (
        "Glob", {"path": "/work/repo"})
    assert mapped("web_search", {"query": "q"}) == ("WebSearch", {"query": "q"})
    assert mapped("spawn_subagent", {})[0] == "Task"
    assert mapped("acme__lookup", {"id": 1}) == ("acme__lookup", {"id": 1})


def test_other_results_are_read_by_their_tag_and_a_no_match_grep_is_not_a_failure():
    host = Grok()

    def result(tool, args, resp):
        return host.tool_call(host.prepare(payload(
            "PostToolUse", toolName=tool, toolInput=args, toolResult=resp), {}))[2:]

    assert result("read_file", {}, {"type": "ReadFile", "FileContent": {
        "content": "1→hi\n", "raw_output": "hi\n"}}) == ("hi\n", None)
    assert result("grep", {}, {"type": "GrepSearch", "stdout": [], "stderr": [], "exit_code": 1,
                               "match_count": 0}) == ("", 0)
    assert result("grep", {}, {"type": "GrepSearch", "stdout": [], "stderr": list(b"bad regex"),
                               "exit_code": 2})[1] == 2
    assert result("web_fetch", {}, "plain text of a result past the size limit") == (
        "plain text of a result past the size limit", None)
    failed = host.tool_call(host.prepare(payload(
        "PostToolUseFailure", toolName="acme__lookup", toolInput={}, error="MCP error"), {}))
    assert failed[0] == "acme__lookup" and "MCP error" in failed[2]


def test_a_subagents_tool_events_are_not_the_sessions():
    host = Grok()
    assert host.is_subagent(payload("PostToolUse", subagentType="explore"))
    assert not host.is_subagent(payload("PostToolUse", subagentType=None))


# ---- turns --------------------------------------------------------------------------------
def test_tool_events_without_a_prompt_id_stay_in_the_turn_and_the_shutdown_stop_changes_nothing(
        env, repo):
    call(env, payload("UserPromptSubmit", repo, prompt_id="p1", prompt="go"))
    for i in range(3):
        call(env, payload("PostToolUse", repo, **bash(f"echo {i}", f"{i}\n")))
    sess = store(env, repo).read(f"sessions/{SID}.json")
    assert sess["turn"]["turn_key"] == "p1" and sess["turn"]["cum_turns"] == 3
    assert sess["host"] == "grok" and not sess["turns"]

    call(env, payload("Stop", repo, reason="shutdown"))          # session end's extra Stop
    sess = store(env, repo).read(f"sessions/{SID}.json")
    assert sess["turn"] is not None and not sess["turns"]        # the open turn is not closed

    call(env, payload("Stop", repo, prompt_id="p1", reason="end_turn", stop_hook_active=False))
    call(env, payload("Stop", repo, reason="shutdown"))
    sess = store(env, repo).read(f"sessions/{SID}.json")
    assert sess["turn"] is None and len(sess["turns"]) == 1       # counted once


def test_a_cancelled_turn_is_recorded_as_not_resolved(env, repo):
    call(env, payload("UserPromptSubmit", repo, prompt_id="p1", prompt="go"))
    call(env, payload("PostToolUse", repo, **bash("make", "ok\n")))
    call(env, payload("StopCancelled", repo, prompt_id="p1", reason="user_interrupt"))
    (turn,) = store(env, repo).read(f"sessions/{SID}.json")["turns"]
    assert turn["resolved_proxy"] is False


# ---- the start context --------------------------------------------------------------------
FACTS = "Facts learned about this repository:\n- run tests with python -m pytest\n- no bare pytest"


def test_the_start_context_reaches_the_model_once_with_the_first_tool_result(env, repo):
    store(env, repo).write("injection.json", {"rendered": FACTS, "rendered_ids": ["a", "b"]})
    start = call(env, payload("SessionStart", repo, source="new"))
    assert "hookSpecificOutput" not in start        # grok would drop it at SessionStart
    assert "learned fact" in start["systemMessage"]   # the person's one-line announcement
    assert call(env, payload("UserPromptSubmit", repo, prompt_id="p1", prompt="go")) == {}

    first = call(env, payload("PostToolUse", repo, **bash("ls", "a\n")))
    assert first["hookSpecificOutput"] == {"hookEventName": "PostToolUse",
                                           "additionalContext": FACTS}
    assert "hookSpecificOutput" not in call(env, payload("PostToolUse", repo, **bash("ls", "a\n")))
    assert store(env, repo).read(f"sessions/{SID}.json")["injected_chars"] == len(FACTS)


def test_a_failed_first_call_delivers_it_under_the_failure_events_name(env, repo):
    store(env, repo).write("injection.json", {"rendered": FACTS})
    call(env, payload("SessionStart", repo, source="new"))
    out = call(env, payload("PostToolUseFailure", repo, toolName="acme__lookup", toolInput={},
                            error="MCP error"))
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUseFailure"
    assert out["hookSpecificOutput"]["additionalContext"] == FACTS


def test_a_subagents_tool_call_does_not_take_the_start_context(env, repo):
    store(env, repo).write("injection.json", {"rendered": FACTS})
    call(env, payload("SessionStart", repo, source="new"))
    assert call(env, payload("PostToolUse", repo, subagentType="explore",
                             **bash("ls", "a\n"))) == {}
    assert "additionalContext" in call(env, payload("PostToolUse", repo, **bash("ls", "a\n"))
                                       ).get("hookSpecificOutput", {})


def test_a_resumed_session_is_not_given_the_context_again_and_a_session_end_drops_it(env, repo):
    store(env, repo).write("injection.json", {"rendered": FACTS})
    call(env, payload("SessionStart", repo, source="new"))
    spool = Path(env["TOKENECONOMIZER_HOME"]) / "grok-start" / f"{SID}.txt"
    assert spool.read_text() == FACTS
    call(env, payload("SessionEnd", repo, reason="shutdown"))
    assert not spool.exists()                                   # no tool call ever came for it
    call(env, payload("SessionStart", repo, sid="resumed", source="load"))
    out = call(env, payload("PostToolUse", repo, sid="resumed", **bash("ls", "a\n")))
    assert "hookSpecificOutput" not in out        # the saved conversation carries the reminder


def test_the_start_context_gives_way_to_the_calls_own_context_within_groks_cap(tmp_path):
    ctx = type("C", (), {"env": {"TOKENECONOMIZER_HOME": str(tmp_path)}})()
    spool = tmp_path / "grok-start" / f"{SID}.txt"
    spool.parent.mkdir()
    spool.write_text("s" * 5000)
    own = "n" * 9990
    out = Grok().render(payload("PostToolUse"), {"hookSpecificOutput": {
        "hookEventName": "PostToolUse", "additionalContext": own}}, ctx)
    text = out["hookSpecificOutput"]["additionalContext"]
    assert len(text) <= Grok.CONTEXT_CAP and text.endswith(own) and not spool.exists()


# ---- the loop guard, end to end -----------------------------------------------------------
def grok_turn(env, repo, key, calls):
    call(env, payload("UserPromptSubmit", repo, prompt_id=key, prompt="go"))
    outs = [call(env, payload("PostToolUse", repo, **c)) for c in calls]
    call(env, payload("Stop", repo, prompt_id=key, reason="end_turn", stop_hook_active=False))
    return outs


def test_a_stuck_turn_is_nudged_through_the_post_tool_use_context(env, repo):
    read = dict(toolName="read_file", toolInput={"target_file": "/work/repo/a.py"},
                toolResult={"type": "ReadFile", "FileContent": {"raw_output": "ok\n"}})
    for k in range(5):
        grok_turn(env, repo, f"g{k}", [read] * 8)
    fail = "FAILED tests/test_a.py::test_x - AssertionError: assert 1 == 2\n"
    edit = dict(toolName="search_replace",
                toolInput={"file_path": "/work/repo/pkg/a.py", "old_string": "x",
                           "new_string": "y"},
                toolResult={"type": "SearchReplace", "EditsApplied": {
                    "tool_output_for_prompt": "edited"}})
    stuck = [edit if k % 2 == 0 else bash("pytest -q", fail, 1) for k in range(40)]
    outs = grok_turn(env, repo, "stuck", stuck)
    nudges = [o for o in outs if "hookSpecificOutput" in o]
    assert nudges
    hso = nudges[0]["hookSpecificOutput"]
    assert hso["hookEventName"] == "PostToolUse" and hso["additionalContext"]
    assert [e for e in store(env, repo).read_lines("events.jsonl") if e["kind"] == "nudge"]


# ---- the session log ----------------------------------------------------------------------
def update(session_update, method="session/update", **fields):
    return {"method": method, "params": {"sessionId": SID, "update": dict(
        sessionUpdate=session_update, **fields)}}


def write_session(directory, usage=None):
    path = directory / "updates.jsonl"
    cid = lambda n: f"call-{n}"     # noqa: E731
    rows = [
        update("hook_execution", "_x.ai/session/update", event_name="user_prompt_submit"),
        update("user_message_chunk", content={"type": "text", "text": "fix "},
               _meta={"promptIndex": 0}),
        update("user_message_chunk", content={"type": "text", "text": "the test"},
               _meta={"promptIndex": 0}),
        update("agent_thought_chunk", content={"type": "text", "text": "thinking"}),
        update("tool_call", toolCallId=cid(1), title="run_terminal_command",
               rawInput={"command": "rtk pytest -q"}),
        update("tool_call_update", toolCallId=cid(1), kind="execute",
               rawInput={"variant": "Bash", "command": "rtk pytest -q"}),
        update("tool_call_update", toolCallId=cid(1), status="completed",
               rawOutput=bash_result("1 failed\n", 1, current_dir="/work/repo")),
        update("tool_call", toolCallId=cid(2), title="read_file",
               rawInput={"target_file": "/work/repo/a.py"}),
        update("tool_call_update", toolCallId=cid(2), status="completed",
               rawOutput={"type": "ReadFile", "FileContent": {"raw_output": "code\n"}}),
        update("tool_call", toolCallId=cid(3), title="acme__lookup", rawInput={"id": 1}),
        update("tool_call_update", toolCallId=cid(3), status="failed",
               content=[{"type": "content", "content": {"type": "text", "text": "boom"}}]),
        update("agent_message_chunk", content={"type": "text", "text": "done"}),
        update("turn_completed", "_x.ai/session/update", stop_reason="end_turn"),
        update("user_message_chunk", content={"type": "text", "text": "thanks"},
               _meta={"promptIndex": 1}),
        update("agent_message_chunk", content={"type": "text", "text": "welcome"}),
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    if usage:
        (directory / "usage.json").write_text(json.dumps(usage))
    return str(path)


def test_a_session_log_becomes_a_trajectory_the_mechanisms_read(tmp_path):
    src = write_session(tmp_path)
    out = str(tmp_path / "trace.jsonl")
    assert trace.grok_session_to_claude(src, out) == 3
    assert trace.grok_prompt_count(src) == 2
    assert trace.load_trajectory(out) == [
        ("Bash", {"command": "pytest -q"}, "1 failed\n", False),
        ("Read", {"file_path": "/work/repo/a.py"}, "code\n", True),
        ("acme__lookup", {"id": 1}, "boom", False),
    ]
    assert trace.tool_usage(out) == {"Bash": 1, "Read": 1, "acme__lookup": 1}
    assert Grok().mechanism_trace(src, str(tmp_path)) == str(tmp_path / "grok_trace.jsonl")


def test_a_missing_or_empty_log_has_no_trace(tmp_path):
    assert trace.grok_session_to_claude(str(tmp_path / "none.jsonl"), str(tmp_path / "t")) is None
    (tmp_path / "e.jsonl").write_text("not json\n")
    assert trace.grok_session_to_claude(str(tmp_path / "e.jsonl"), str(tmp_path / "t")) is None
    assert Grok().mechanism_trace("", str(tmp_path)) is None
    assert trace.grok_prompt_count(str(tmp_path / "none.jsonl")) == 0


def test_session_usage_is_fresh_input_cached_reads_and_output(tmp_path):
    usage = {"sessionId": SID, "session": {
        "inputTokens": 40650, "outputTokens": 913, "cachedReadTokens": 30208,
        "cacheCreationTokens": 0, "reasoningTokens": 806, "modelCalls": 2,
        "costUsdTicks": 414660000, "turnCount": 1, "primaryModelId": "grok-4.7-build"}}
    got = Grok().session_usage(write_session(tmp_path, usage))
    assert got["input_tokens"] == 40650 - 30208 == 10442
    assert (got["cache_read_input_tokens"], got["cache_creation_input_tokens"]) == (30208, 0)
    assert got["output_tokens"] == 913                    # reasoning is already inside it
    assert (got["requests"], got["model"]) == (2, "grok-4.7-build")
    assert got["cost_usd_vendor"] == 0.041466
    from tokeneconomizer.plugin import accounting
    eff, priced_by = accounting.effective(got)
    assert eff and priced_by == "fallback"                 # xAI prices are not in the table
    assert Grok().session_usage(str(tmp_path / "nowhere" / "updates.jsonl")) == {}


# ---- the side call ------------------------------------------------------------------------
@pytest.mark.real_sidecalls
def test_a_grok_side_call_is_a_headless_completion_that_removes_what_acts(monkeypatch):
    monkeypatch.setattr(sidecall, "program", lambda cli: [cli])
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(cmd=cmd, **kw)
        out = json.dumps({"text": "- use python -m pytest\n", "stopReason": "end_turn",
                          "usage": {"input_tokens": 10319, "cache_read_input_tokens": 30208,
                                    "cache_creation_input_tokens": 0, "output_tokens": 252},
                          "modelUsage": {"grok-4.7-build": {}}})
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(sidecall.subprocess, "run", fake_run)
    text, eff = sidecall.grok_call("propose facts", model="grok-4.7")
    assert text == "- use python -m pytest" and eff and eff > 0
    cmd = seen["cmd"]
    assert cmd[:5] == ["grok", "-p", "propose facts", "--output-format", "json"]
    # An allowlist of one read-only tool (fails closed), the MCP dispatchers removed, and deny
    # rules behind it -- never a deny list of guessed names, which removed nothing on 1.0.70.
    assert cmd[cmd.index("--tools") + 1] == sidecall.GROK_TOOLS == "read_file"
    assert cmd[cmd.index("--disallowed-tools") + 1] == "use_tool,search_tool"
    denied = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--deny"]
    assert {"Bash", "Edit", "Write"} <= set(denied)
    assert "run_terminal_command" not in cmd and "search_replace" not in cmd
    assert cmd[cmd.index("--model") + 1] == "grok-4.7"
    assert "--always-approve" not in cmd and "--yolo" not in cmd
    assert seen["env"]["TE_SIDECALL"] == "1" and seen["stdin"] == subprocess.DEVNULL
    assert seen["cwd"] != os.getcwd()                       # a throwaway directory
    assert sidecall.runner_for("grok", __import__(
        "tokeneconomizer.plugin.config", fromlist=["LiveConfig"]).LiveConfig()
        ).__wrapped__.__code__.co_names.count("grok_call") == 1
    assert sidecall.call_name("grok") == "grok -p"


@pytest.mark.real_sidecalls
def test_a_grok_side_call_with_no_reply_says_so_in_the_error_log(tmp_path, monkeypatch):
    monkeypatch.setattr(sidecall, "program", lambda cli: [cli])
    monkeypatch.setenv("TOKENECONOMIZER_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(sidecall.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 1, stdout="", stderr="Not signed in"))
    assert sidecall.grok_call("p") == ("", None)
    assert "sidecall grok: no reply" in (tmp_path / "home" / "errors.log").read_text()
    assert sidecall.paused("grok") is not None


def test_the_json_reply_reads_the_last_object_and_tolerates_noise():
    text, usage = trace.grok_json_reply('warning: x\n{"text": "hi", "usage": {"input_tokens": 3, '
                                        '"output_tokens": 1}}\n')
    assert text == "hi" and usage["input_tokens"] == 3 and usage["cache_read_input_tokens"] == 0
    assert trace.grok_json_reply("not json") == ("", None)


# ---- packaging ----------------------------------------------------------------------------
def test_the_claude_hook_file_gives_session_start_an_explicit_timeout():
    """grok runs this file as it is, and its default for an observe hook is 5 seconds."""
    root = Path(__file__).resolve().parents[1]
    hooks = json.loads((root / "plugin/hooks/hooks.json").read_text())["hooks"]
    assert all(h.get("timeout", 0) > 5 for entries in hooks.values()
               for group in entries for h in group["hooks"])
    assert all("--host" not in h["command"] for entries in hooks.values()
               for group in entries for h in group["hooks"])   # detection does the work
