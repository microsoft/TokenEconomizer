"""Antigravity CLI (`agy`) host adapter: one hook call -> several controller events, the transcript
reader, the trace and usage readers, the side call, and the plugin's packaging.

Every transcript here is synthetic (fake ids, `/work/repo`, `/home/u`): rows shaped as agy 1.5.1
writes `transcript_full.jsonl`, built by `Transcript` below. (`*.jsonl` is not tracked in this
repository, so fixtures are written at test time.)
"""
import io
import json
import os
import subprocess
from pathlib import Path

import pytest

from tokeneconomizer import trace
from tokeneconomizer.plugin import hook, hosts, sidecall
from tokeneconomizer.plugin.hosts import Antigravity, ClaudeCode, Cursor, merge_outputs

CID = "4c748b89-0000-4000-8000-000000000001"
ROOT = Path(__file__).resolve().parents[1]
NOTE = ("Created At: 2026-11-05T10:00:00Z\nCompleted At: 2026-11-05T10:00:01Z\n"
        "A pre-tool hook changed the arguments of this tool call before it ran. "
        "Changed: CommandLine.\n\n")


class Transcript:
    """Rows as agy writes them, appended one prompt or model call at a time."""

    def __init__(self, path):
        self.path, self.n = str(path), 0

    def _add(self, **row):
        row.update(step_index=self.n, status=row.get("status", "DONE"),
                   created_at="2026-11-05T10:00:00Z")
        self.n += 1
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")

    def prompt(self, text):
        self._add(source="USER_EXPLICIT", type="USER_INPUT", content=(
            f"<USER_REQUEST>\n{text}\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\nnow\n"
            "</ADDITIONAL_METADATA>"))

    def call(self, calls, results, tokens=True):
        """One model call: its planner row, then one result step per entry of `results`
        (`(content, extra)`; extra overrides the step's fields, `type` included)."""
        row = {"source": "MODEL", "type": "PLANNER_RESPONSE",
               "tool_calls": [{"name": n, "args": a} for n, a in calls]}
        if tokens:
            row.update(input_tokens=100, cache_read_tokens=1000, output_tokens=20)
        self._add(**row)
        for content, extra in results:
            self._add(**dict({"source": "MODEL", "type": "GENERIC", "content": content}, **extra))

    def system(self):
        self._add(source="SYSTEM", type="CHECKPOINT", content="checkpoint")


def sh(cmd):
    return "run_command", {"CommandLine": cmd, "Cwd": "/work/repo", "toolAction": "x",
                           "toolSummary": "y", "WaitMsBeforeAsync": 500}


def out(code, text=""):
    return f"{NOTE}Command exited with code {code}.\n\nCommand output:\n{text}\r\n", {}


def hook_payload(tp, event, ws="/work/repo", **extra):
    return dict({"hook_event_name": event, "conversationId": CID, "workspacePaths": [ws],
                 "transcriptPath": str(tp), "modelName": "m", "lastUserInput": "fix it"}, **extra)


@pytest.fixture
def env(tmp_path):
    return {"TOKENECONOMIZER_HOME": str(tmp_path / "home"), "TE_FOLD_SYNC": "1",
            "TE_MICROSKILLS": "envfacts", "GIT_CEILING_DIRECTORIES": str(tmp_path),
            "PATH": os.environ.get("PATH", "")}


@pytest.fixture
def tp(tmp_path):
    d = tmp_path / "brain" / CID / ".system_generated" / "logs"
    d.mkdir(parents=True)
    return d / "transcript_full.jsonl"


def kinds(events):
    return [(e["hook_event_name"], e.get("tool_name") or e.get("prompt"), e.get("prompt_id"))
            for e in events]


# ---- one agy call -> the controller's events ----------------------------------------------
def test_expand_puts_a_turns_results_before_the_next_prompt_and_counts_turns(tp, env):
    host, t = Antigravity(), Transcript(tp)
    t.prompt("fix it")
    assert kinds(host.expand(hook_payload(tp, "SessionStart"), env)) == [
        ("SessionStart", None, None)]
    # invocationNum 0 is left out of the payload (protojson): the first call of a prompt.
    assert kinds(host.expand(hook_payload(tp, "PreInvocation"), env)) == [
        ("UserPromptSubmit", "fix it", f"{CID}:1")]
    t.call([sh("ls")], [out(0, "a")])
    t.call([("view_file", {"AbsolutePath": "/work/repo/a.py"})], [("print(1)\n", {})])
    t.call([("replace_file_content", {"TargetFile": "/work/repo/a.py", "ReplacementContent": "x",
                                      "TargetContent": "y"})], [("edited", {"type": "CODE_ACTION"})])
    assert kinds(host.expand(hook_payload(tp, "PreInvocation", invocationNum=3), env)) == [
        ("PostToolUse", "Bash", f"{CID}:1"), ("PostToolUse", "Read", f"{CID}:1"),
        ("PostToolUse", "Edit", f"{CID}:1")]
    t.call([sh("pytest")], [out(1, "FAILED")])
    t.prompt("again")                       # a new prompt: its results belong to the old turn
    assert kinds(host.expand(hook_payload(tp, "PreInvocation"), env)) == [
        ("PostToolUse", "Bash", f"{CID}:1"), ("UserPromptSubmit", "fix it", f"{CID}:2")]
    t.call([sh("ls")], [out(0, "b")])
    stop = host.expand(hook_payload(tp, "Stop", terminationReason="NO_TOOL_CALL"), env)
    assert kinds(stop) == [("PostToolUse", "Bash", f"{CID}:2"), ("Stop", None, None)]
    assert stop[-1]["terminationReason"] == "NO_TOOL_CALL"


def test_expand_does_not_emit_a_step_twice(tp, env):
    host, t = Antigravity(), Transcript(tp)
    host.expand(hook_payload(tp, "SessionStart"), env)
    host.expand(hook_payload(tp, "PreInvocation"), env)
    t.call([sh("ls")], [out(0)])
    assert len(host.expand(hook_payload(tp, "PreInvocation", invocationNum=1), env)) == 1
    assert host.expand(hook_payload(tp, "PreInvocation", invocationNum=2), env) == []
    t.call([sh("pwd")], [out(0)])
    again = host.expand(hook_payload(tp, "PreInvocation", invocationNum=3), env)
    assert [e["tool_input"] for e in again] == [{"command": "pwd"}]


def test_a_call_whose_results_are_not_all_in_waits_for_them(tp, env):
    host, t = Antigravity(), Transcript(tp)
    host.expand(hook_payload(tp, "SessionStart"), env)
    host.expand(hook_payload(tp, "PreInvocation"), env)
    t.call([sh("a"), sh("b")], [out(0)])                # the second result is not written yet
    assert host.expand(hook_payload(tp, "PreInvocation", invocationNum=1), env) == []
    t._add(source="MODEL", type="GENERIC", content=out(0)[0])
    got = host.expand(hook_payload(tp, "PreInvocation", invocationNum=2), env)
    assert [e["tool_input"]["command"] for e in got] == ["a", "b"]


def test_a_conversation_this_plugin_did_not_start_is_not_replayed(tp, env):
    t = Transcript(tp)
    t.prompt("old")
    t.call([sh("ls")], [out(0)])
    got = Antigravity().expand(hook_payload(tp, "PreInvocation"), env)
    assert kinds(got) == [("UserPromptSubmit", "fix it", f"{CID}:1")]


def test_a_resumed_conversation_does_not_replay_its_old_calls_at_session_start(tp, env):
    host, t = Antigravity(), Transcript(tp)
    t.prompt("old")
    t.call([sh("ls")], [out(0)])                # before this plugin saw the conversation
    host.expand(hook_payload(tp, "SessionStart"), env)
    assert kinds(host.expand(hook_payload(tp, "PreInvocation"), env)) == [
        ("UserPromptSubmit", "fix it", f"{CID}:1")]
    t.call([sh("pwd")], [out(0)])
    got = host.expand(hook_payload(tp, "PreInvocation", invocationNum=1), env)
    assert [e["tool_input"]["command"] for e in got] == ["pwd"]


@pytest.mark.skipif(os.name == "nt",
                    reason="POSIX permission bits: Windows keeps the state private by ACL")
def test_the_cursor_file_is_private(tp, env):
    Antigravity().expand(hook_payload(tp, "SessionStart"), env)
    state = Path(env["TOKENECONOMIZER_HOME"]) / "antigravity-sessions"
    assert (state.stat().st_mode & 0o777, (state / f"{CID}.json").stat().st_mode & 0o777) == (
        0o700, 0o600)


def test_a_missing_or_unreadable_transcript_gives_no_tool_events(tmp_path, env):
    host = Antigravity()
    gone = tmp_path / "nope.jsonl"
    host.expand(hook_payload(gone, "SessionStart"), env)
    assert kinds(host.expand(hook_payload(gone, "PreInvocation"), env)) == [
        ("UserPromptSubmit", "fix it", f"{CID}:1")]
    assert kinds(host.expand(hook_payload(gone, "Stop"), env)) == [("Stop", None, None)]
    assert host.expand(hook_payload(gone, "PreInvocation", conversationId="../x"), env) == []
    assert host.expand({"hook_event_name": "PostToolUse", "conversationId": CID}, env) == []


# ---- what a result step becomes ------------------------------------------------------------
def test_generic_and_typed_steps_become_claude_shaped_calls(tp):
    t = Transcript(tp)
    t.prompt("go")
    t.call([sh("pytest -q"), ("grep_search", {"Query": "foo", "SearchPath": "/work/repo",
                                              "toolAction": "z"}),
            ("list_dir", {"DirectoryPath": "/work/repo"}), ("call_mcp_tool", {
                "ServerName": "s", "ToolName": "t", "toolSummary": "x"})],
           [out(1, "ls: nope"), ("hit\n", {"type": "GREP_SEARCH"}), ("a\nb\n", {"type": "LIST_DIRECTORY"}),
            ("ok", {"type": "MCP_TOOL"})])
    t.call([("run_command", {"CommandLine": "make"})],
           [("done", {"type": "RUN_COMMAND", "exit_code": 2})])
    items = [i for i in trace.antigravity_read(str(tp))[0] if i["kind"] == "tool"]
    assert [(i["name"], i["args"]) for i in items] == [
        ("Bash", {"command": "pytest -q"}), ("Grep", {"pattern": "foo", "path": "/work/repo"}),
        ("Glob", {"path": "/work/repo"}), ("call_mcp_tool", {"ServerName": "s", "ToolName": "t"}),
        ("Bash", {"command": "make"})]
    assert items[0]["cwd"] == "/work/repo"
    # the rtk notice and the timestamps are gone; the exit code is read from the text, or the
    # typed step's own field
    assert items[0]["content"].startswith("Command exited with code 1.")
    assert [i["exit_code"] for i in items] == [1, None, None, None, 2]
    assert [i["is_error"] for i in items] == [True, False, False, False, True]


def test_a_step_with_status_error_is_a_failure_with_its_error_text(tp):
    t = Transcript(tp)
    t.call([("view_file", {"AbsolutePath": "/work/repo/x"})],
           [("", {"status": "ERROR", "error": "tool call denied by pre-tool hook: "})])
    (item,) = trace.antigravity_read(str(tp))[0]
    assert item["is_error"] and item["exit_code"] is None
    assert "denied by pre-tool hook" in item["content"]
    host = Antigravity()
    name, args, text, code = host.tool_call({
        "tool_name": "Read", "tool_input": {}, "tool_response": {
            "content": item["content"], "exit_code": None, "isError": True}})
    assert (name, code) == ("Read", 1) and "denied" in text
    assert host.tool_call({"tool_name": "Bash", "tool_input": {}, "tool_response": {
        "content": "ok", "exit_code": None, "isError": False}})[3] is None


def test_running_steps_system_rows_and_finish_do_not_shift_the_pairing(tp):
    t = Transcript(tp)
    t.call([sh("sleep 9"), sh("ls")], [("", {"status": "RUNNING"}), out(0, "x")])
    t.system()
    t.call([("finish", {"summary": "s"})], [("", {"type": "FINISH"})])
    items = trace.antigravity_read(str(tp))[0]
    assert [(i["args"]["command"], i["exit_code"]) for i in items] == [("ls", 0)]


# ---- agy's answer --------------------------------------------------------------------------
def test_render_answers_each_event_in_agys_terms():
    host = Antigravity()
    ctx = merge_outputs(host.context("SessionStart", "facts"), host.notice("heads up"))
    start = host.render({"hook_event_name": "SessionStart"}, ctx)
    (step,) = start["injectSteps"]
    assert step["systemMessage"]["systemMessage"].startswith("facts\n\n")
    assert "heads up" in step["systemMessage"]["systemMessage"]      # told through the model
    assert host.render({"hook_event_name": "PreInvocation"}, {"hookSpecificOutput": {
        "hookEventName": "PostToolUse", "additionalContext": "redirect"}, "systemMessage": "n"}) == {
        "injectSteps": [{"ephemeralMessage": "redirect\n\nn"}]}
    assert host.render({"hook_event_name": "Stop"}, ctx) == {}
    assert host.render({"hook_event_name": "PreInvocation"}, {}) == {} == host.render(
        {"hook_event_name": "SessionStart"}, {})


@pytest.mark.parametrize("reason,normal", [("NO_TOOL_CALL", True), ("USER_CANCELED", False),
                                           ("ERROR", False), (None, False)])
def test_only_a_turn_that_ran_to_its_end_stopped_normally(reason, normal):
    assert Antigravity().stopped_normally({"terminationReason": reason}) is normal


def test_session_id_cwd_and_transcript_come_from_the_payload():
    host, p = Antigravity(), hook_payload("/h/.gemini/antigravity-cli/brain/x/t.jsonl", "Stop")
    assert (host.session_id(p), host.cwd(p, {}), host.transcript(p)) == (
        CID, "/work/repo", "/h/.gemini/antigravity-cli/brain/x/t.jsonl")
    assert host.cwd({"workspacePaths": []}, {}) == os.getcwd()


def test_detect_finds_agy_by_its_payload_and_leaves_the_others_alone():
    p = {"conversationId": CID, "transcriptPath": "/home/u/.gemini/antigravity-cli/brain/x/t"}
    assert isinstance(hosts.detect(p, {}), Antigravity)
    assert isinstance(hosts.detect(p, {}, hint="antigravity"), Antigravity)
    assert isinstance(hosts.detect(dict(p, transcriptPath="/x/other.jsonl"), {}), ClaudeCode)
    assert isinstance(hosts.detect({"transcript_path": "/x/y"}, {}), ClaudeCode)
    assert hosts.HOSTS["antigravity"] is Antigravity


# ---- the finished session ------------------------------------------------------------------
def test_usage_sums_the_rows_that_report_it_and_skips_the_rest(tp):
    t = Transcript(tp)
    t.prompt("go")
    t.call([sh("a")], [out(0)])
    t.call([sh("b")], [out(0)], tokens=False)           # an older agy: no token fields
    t.call([], [])
    t._add(source="MODEL", type="PLANNER_RESPONSE", input_tokens=5, output_tokens=7)
    assert trace.antigravity_session_usage(str(tp)) == {
        "input_tokens": 205, "cache_read_input_tokens": 2000, "cache_creation_input_tokens": 0,
        "output_tokens": 47, "requests": 3, "model": "antigravity"}
    assert trace.antigravity_session_usage(str(tp) + ".none") == {}
    from tokeneconomizer.plugin import accounting
    assert accounting.effective(Antigravity().session_usage(str(tp)))[1] == "fallback"
    assert accounting.effective(Antigravity().session_usage(str(tp) + ".none")) == (
        None, "no_usage")


def test_the_trace_conversion_feeds_the_mechanisms_and_prompts_are_counted(tp, tmp_path):
    t = Transcript(tp)
    t.prompt("first")
    t.call([sh("pytest")], [out(1, "FAILED x")])
    t.call([("replace_file_content", {"TargetFile": "/work/repo/a.py"})],
           [("ok", {"type": "CODE_ACTION"})])
    t.prompt("second")
    t.call([("view_file", {"AbsolutePath": "/work/repo/a.py"})], [("body", {"type": "VIEW_FILE"})])
    host, work = Antigravity(), tmp_path / "w"
    work.mkdir()
    path = host.mechanism_trace(str(tp), str(work))
    assert trace.load_trajectory(path) == [
        ("Bash", {"command": "pytest"}, "Command exited with code 1.\n\nCommand output:\nFAILED x",
         False),
        ("Edit", {"file_path": "/work/repo/a.py"}, "ok", True),
        ("Read", {"file_path": "/work/repo/a.py"}, "body", True)]
    assert trace.call_cwds(path)[0] == "/work/repo"
    assert host.prompt_count(str(tp)) == 2
    assert host.mechanism_trace(str(tp) + ".none", str(work)) is None


# ---- the hook entry point ------------------------------------------------------------------
def test_main_reads_the_event_from_the_command_line(tp, env):
    out_ = io.StringIO()
    Transcript(tp).prompt("x")
    p = hook_payload(tp, "ignored")
    del p["hook_event_name"]
    hook.main(["--host", "antigravity", "--event", "SessionStart"], io.StringIO(json.dumps(p)),
              out_, dict(env, TE_MICROSKILLS="off"))
    assert json.loads(out_.getvalue()) == {}
    assert (Path(env["TOKENECONOMIZER_HOME"]) / "antigravity-sessions" / f"{CID}.json").exists()


def test_a_sidecall_marker_makes_the_hook_do_nothing(tp, env):
    out_ = io.StringIO()
    hook.main(["--host", "antigravity", "--event", "SessionStart"],
              io.StringIO(json.dumps(hook_payload(tp, "SessionStart"))), out_,
              dict(env, TE_SIDECALL="1"))
    assert json.loads(out_.getvalue()) == {}
    assert not (Path(env["TOKENECONOMIZER_HOME"]) / "antigravity-sessions").exists()


def test_the_expand_render_seam_leaves_other_hosts_as_they_were(tmp_path, env):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    p = {"session_id": "s", "cwd": str(repo), "transcript_path": "", "hook_event_name":
         "SessionStart", "source": "startup"}
    assert hook.run(p, env, host_hint="claude") == {}
    for cls in (ClaudeCode, Cursor):
        assert not hasattr(cls, "expand") and not hasattr(cls, "render")


def test_the_seam_runs_each_expanded_event_in_order_and_renders_the_merge(monkeypatch, tmp_path,
                                                                           env):
    seen = []

    class Fake(ClaudeCode):
        name = "claude"

        def expand(self, payload, env):
            return [dict(payload, hook_event_name="SessionStart", n=1),
                    dict(payload, hook_event_name="SessionStart", n=2)]

        def render(self, payload, merged, ctx):
            return {"event": payload["hook_event_name"], "merged": merged}

    from tokeneconomizer.plugin import controller
    monkeypatch.setitem(hosts.HOSTS, "fake", Fake)
    monkeypatch.setattr(controller.LiveController, "handle", lambda self: (
        seen.append(self.p["n"]) or {"additionalContext": f"c{self.p['n']}"}))
    got = hook.run({"session_id": "s", "cwd": str(tmp_path), "hook_event_name": "PreInvocation"},
                   env, host_hint="fake")
    assert seen == [1, 2]
    assert got == {"event": "PreInvocation", "merged": {"additionalContext": "c1\n\nc2"}}


# ---- end to end: a stuck turn becomes an ephemeral message ---------------------------------
def _run_turn(env, tp, t, prompt, batches, ws):
    """One prompt through the hook, as agy calls it: PreInvocation per model call, then Stop."""
    outs = []

    def call(event, **extra):
        buf = io.StringIO()
        hook.main(["--host", "antigravity", "--event", event],
                  io.StringIO(json.dumps(hook_payload(tp, event, ws, lastUserInput=prompt, **extra))),
                  buf, env)
        return json.loads(buf.getvalue())

    t.prompt(prompt)
    outs.append(call("PreInvocation"))
    for n, (calls, results) in enumerate(batches, 1):
        t.call(calls, results)
        outs.append(call("PreInvocation", invocationNum=n))
    call("Stop", terminationReason="NO_TOOL_CALL", executionNum=1)
    return outs


def test_a_stuck_turn_is_redirected_through_an_ephemeral_message(tp, tmp_path, env):
    repo = tmp_path / "repo"
    (repo / ".git" / "info").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (repo / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://github.com/acme/widget.git\n')
    env = dict(env, TE_LOOPGUARD="on")
    t, ws = Transcript(tp), str(repo)
    hook.main(["--host", "antigravity", "--event", "SessionStart"], io.StringIO(
        json.dumps(hook_payload(tp, "SessionStart", ws))), io.StringIO(), env)
    green = [([("view_file", {"AbsolutePath": f"{repo}/a.py"})] * 8, [("ok", {})] * 8)]
    for k in range(5):
        _run_turn(env, tp, t, f"green {k}", green, ws)
    fail = "FAILED tests/test_a.py::test_x - AssertionError: assert 1 == 2"
    stuck = [([("replace_file_content", {"TargetFile": f"{repo}/pkg/a.py"}), sh("pytest -q")],
              [("edited", {"type": "CODE_ACTION"}), out(1, fail)]) for _ in range(20)]
    outs = _run_turn(env, tp, t, "stuck", stuck, ws)
    steps = [s for o in outs for s in o.get("injectSteps", [])]
    assert steps and all(set(s) == {"ephemeralMessage"} for s in steps)
    assert any("Process check (automated)" in s["ephemeralMessage"] for s in steps)


# ---- the side call -------------------------------------------------------------------------
def test_antigravity_makes_no_side_call(monkeypatch):
    # agy has no option that runs a turn with every tool off, and a side call's prompt holds
    # text a repository controls: the proposer and the judge get no runner.
    started = []
    monkeypatch.setattr(sidecall.subprocess, "run", lambda *a, **k: started.append(a))
    assert sidecall.runner_for("antigravity", type("C", (), {"paid_model": "m"})()) is None
    assert not hasattr(sidecall, "agy_call") and started == []


def test_a_stuck_looking_agy_turn_never_starts_a_judge(tp, env, monkeypatch):
    from tokeneconomizer.plugin import controller, judge
    spawned = []
    monkeypatch.setattr(judge, "spawn_job", lambda *a, **k: spawned.append(a))
    ctl = type("C", (), {"host": Antigravity(), "ctx": None, "sid": CID})()
    verdict = controller.LiveController._judge(ctl, {"judge_confidence": 0.5}, envelope={})
    assert isinstance(verdict, judge.NoVerdict) and spawned == []


# ---- packaging -----------------------------------------------------------------------------
def test_the_plugin_declares_the_three_events_and_never_pretooluse():
    manifest = json.loads((ROOT / "plugin/.antigravity-plugin/plugin.json").read_text())
    claude = json.loads((ROOT / "plugin/.claude-plugin/plugin.json").read_text())
    assert (manifest["name"], manifest["version"]) == ("tokeneconomizer", claude["version"])
    hooks = json.loads((ROOT / "plugin/hooks.json").read_text())
    assert set(hooks) == {"tokeneconomizer"}
    events = hooks["tokeneconomizer"]
    # PreToolUse would make agy deny every tool whose hook prints `{}`; PostToolUse carries no
    # result; neither is registered.
    assert set(events) == {"SessionStart", "PreInvocation", "Stop"}
    assert {e: h[0]["timeout"] for e, h in events.items()} == {
        "SessionStart": 15, "PreInvocation": 10, "Stop": 10}
    for event, (handler,) in events.items():
        assert f"--host antigravity --event {event}" in handler["command"]
        assert "./scripts/te-hook" in handler["command"] and "te-hook.cmd" in handler["command"]
    assert os.access(ROOT / "plugin/scripts/te-hook", os.X_OK)
    assert (ROOT / "plugin/scripts/te-hook.cmd").is_file()
    assert (ROOT / "plugin/scripts/te.cmd").is_file()
    # no other agent reads the root hook file: Claude Code's is `hooks/hooks.json`.
    assert "hooks" not in claude


def test_antigravity_recognizes_modern_antigravity_tools(tp, tmp_path):
    t = Transcript(tp)
    t.prompt("modern tools")
    t.call([("write_file", {"Path": "/work/repo/b.py"}),
            ("edit_file", {"Path": "/work/repo/b.py"})],
           [("created", {"type": "GENERIC"}), ("updated", {"type": "GENERIC"})])
    host, work = Antigravity(), tmp_path / "w2"
    work.mkdir()
    path = host.mechanism_trace(str(tp), str(work))
    traj = trace.load_trajectory(path)
    assert ("Write", {"file_path": "/work/repo/b.py"}, "created", True) in traj
    assert ("Edit", {"file_path": "/work/repo/b.py"}, "updated", True) in traj