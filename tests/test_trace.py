"""Trace seam: load_trajectory / tool_usage / parse_catalog over synthetic transcripts."""
import os

from tokeneconomizer import trace
from fixtures import write_claude_transcript, write_codex_transcript


def test_load_trajectory_claude(tmp_path):
    p = str(tmp_path / "t.jsonl")
    write_claude_transcript(p, [
        ("bash", {"command": "python -m pytest tests/test_x.py"}, "2 passed"),
        ("edit", {"file_path": "pkg/mod.py"}, "ok")])
    traj = trace.load_trajectory(p)
    names = [name for name, _a, _r, _ok in traj]
    assert "bash" in names and "edit" in names


def test_a_claude_request_blob_repeating_the_conversation_adds_no_call(tmp_path):
    """Claude Code 2.1.300 writes `api-request-blob` entries that repeat a request's messages,
    earlier tool calls and results included. Read as calls, each one counted twice."""
    import json
    use = {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "pytest -q"}}
    res = {"type": "tool_result", "tool_use_id": "toolu_1",
           "content": "/bin/bash: line 1: pytest: command not found", "is_error": True}
    lines = [{"type": "assistant", "cwd": "/w/repo", "message": {"role": "assistant",
                                                                "content": [use]}},
             {"type": "user", "message": {"role": "user", "content": [res]}},
             {"type": "api-request-blob", "message": {"role": "assistant", "content": [use]}},
             {"type": "api-request-blob", "message": {"role": "user", "content": [res]}},
             {"type": "assistant", "message": {"role": "assistant", "content": [use]}}]
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(json.dumps(l) for l in lines))
    calls = trace.load_trajectory(str(p))
    assert [(c[0], c[1]["command"], c[3]) for c in calls] == [("Bash", "pytest -q", False)]
    assert trace.call_cwds(str(p)) == ["/w/repo"]
    assert trace.tool_usage(str(p)) == {"Bash": 1}
    from tokeneconomizer.microskills import distiller
    assert len(distiller.load_calls(str(p))) == 1


def test_tool_usage_counts(tmp_path):
    p = str(tmp_path / "t.jsonl")
    write_claude_transcript(p, [
        ("bash", {"command": "ls"}, ""),
        ("bash", {"command": "pwd"}, ""),
        ("edit", {"file_path": "a.py"}, "")])
    usage = trace.tool_usage(p)
    assert usage.get("bash", 0) >= 2


def test_parse_catalog_codex_default():
    cat = trace.parse_catalog("/nonexistent", "codex")
    assert "shell" in cat and "apply_patch" in cat


def test_parse_catalog_missing_returns_empty(tmp_path):
    # Claude path with no transcript file -> empty catalog, no crash.
    assert trace.parse_catalog(str(tmp_path), "claude") == {}


# ---- Copilot tool-catalog sources: one pipeline across CLI builds -------------------
#
# The CLI has moved the loaded tool catalog twice, and each move made the then-current
# reader return {} -- which untooling cannot tell apart from "no tools loaded", so it
# degraded to a silent no-op for a whole sweep (measured: CLI 1.0.82 dropped 8 of 17 tools
# on 30/31 tasks; 1.0.83, same harness and repo, dropped nothing on 31/31). These tests
# pin all three source formats and the auto-selection between them.
import json as _json

from tokeneconomizer import trace as _trace
from tokeneconomizer.trace import catalog as _catalog

_SCHEMA_LOG = (
    '{"body":"{\\"tools\\":[{\\"name\\":\\"bash\\",\\"title\\":null,'
    '\\"description\\":\\"Run a shell command\\"},'
    '{\\"name\\":\\"view\\",\\"description\\":\\"Read a file\\"}]}"}'
)
_FLAT_LOG = '{\n  "tools": [\n    "bash:66921ccc",\n    "view:0963ceac",\n    "edit:9e640c64"\n  ]\n}'


def _checkpoint_transcript(*names):
    return _json.dumps({
        "type": "session.usage_checkpoint",
        "data": {"promptCacheBreakState": [
            {"models": {"m": {"tools": [{"name": n} for n in names]}}}]},
    })


def test_flat_catalog_parses_name_fingerprint_form():
    """CLI >= 1.0.82 publishes `"tools": ["bash:66921ccc", ...]` and no schemas."""
    cat = _catalog.parse_tool_catalog_flat(_FLAT_LOG)
    assert set(cat) == {"bash", "view", "edit"}
    assert all(v == "" for v in cat.values())      # names only, no descriptions
    assert _catalog.parse_tool_catalog(_FLAT_LOG) == {}   # schema reader stays schema-only


def test_schema_catalog_still_wins_and_keeps_descriptions(tmp_path):
    (tmp_path / "copilot_debug.log").write_text(_SCHEMA_LOG + "\n" + _FLAT_LOG)
    cat, src = _trace.parse_catalog_with_source(str(tmp_path), "copilot")
    assert src == "schema_debug_log"               # richest: it has descriptions
    assert cat["bash"].startswith("Run a shell command")


def test_flat_used_when_schemas_and_checkpoint_are_gone(tmp_path):
    """The 1.0.83 shape: no schemas, no usage_checkpoint -- must NOT parse empty."""
    (tmp_path / "copilot_debug.log").write_text(_FLAT_LOG)
    (tmp_path / "transcript.jsonl").write_text('{"type":"assistant.message","data":{}}')
    cat, src = _trace.parse_catalog_with_source(str(tmp_path), "copilot")
    assert src == "flat_debug_log"
    assert set(cat) == {"bash", "view", "edit"}


def test_usage_checkpoint_source_is_still_read(tmp_path):
    """The 1.0.82 shape must keep working -- supporting the new build is not a swap."""
    (tmp_path / "transcript.jsonl").write_text(_checkpoint_transcript("bash", "view"))
    cat, src = _trace.parse_catalog_with_source(str(tmp_path), "copilot")
    assert src == "usage_checkpoint"
    assert set(cat) == {"bash", "view"}


def test_no_source_reports_none_not_empty_catalog(tmp_path):
    """No source must be distinguishable from an empty catalog: {} is not 'no tools'."""
    (tmp_path / "copilot_debug.log").write_text("nothing useful here")
    cat, src = _trace.parse_catalog_with_source(str(tmp_path), "copilot")
    assert cat == {}
    assert src is None                              # defect signal, not an empty catalog


# ---- Web-Bench pass@1 vs pass@N ------------------------------------------------------
#
# Web-Bench grades each task with up to `retry` attempts, so a task has TWO defensible
# resolve metrics, and they differ materially (measured on prisma: 1-4 tasks per arm).
# Web-Bench's own headline is pass@1. The runner breaks out of the attempt loop on the
# first pass, which makes pass@1 recoverable from older rows that predate the explicit
# fields -- these pin that derivation, since getting it wrong reports pass@N under the
# pass@1 column.
def _p1p2(row):
    """The aggregator's per-row pass@1/pass@N resolution, mirrored for test."""
    p1, p2 = row.get("resolved_p1", ""), row.get("resolved_p2", "")
    att = row.get("webbench_attempts", "")
    if p1 == "" and p2 == "":
        if att != "":
            att = int(att or 0)
            if att == 0:
                return None, None
            p2 = row["resolved"] is True
            p1 = p2 and att == 1
        else:
            p1 = p2 = row["resolved"]
    return p1, p2


def test_explicit_attempt_fields_win():
    assert _p1p2({"resolved": True, "resolved_p1": False,
                  "resolved_p2": True, "webbench_attempts": 2}) == (False, True)


def test_legacy_row_second_attempt_pass_is_not_pass_at_1():
    """resolved=True after 2 attempts means attempt 1 FAILED."""
    assert _p1p2({"resolved": True, "webbench_attempts": 2}) == (False, True)


def test_legacy_row_first_attempt_pass():
    assert _p1p2({"resolved": True, "webbench_attempts": 1}) == (True, True)


def test_legacy_row_failed_every_attempt():
    assert _p1p2({"resolved": False, "webbench_attempts": 2}) == (False, False)


def test_unreached_task_counts_in_neither_metric():
    """Never attempted is not a miss -- it must not enter numerator or denominator."""
    assert _p1p2({"resolved": False, "webbench_attempts": 0}) == (None, None)


def test_single_attempt_domain_collapses_to_resolved():
    """SWE-bench rows carry no attempt fields; pass@1 and pass@N are the same number."""
    assert _p1p2({"resolved": True}) == (True, True)
    assert _p1p2({"resolved": False}) == (False, False)


def _jsonl(path, records):
    import json
    with open(path, "w") as fh:
        fh.write("\n".join(json.dumps(r) for r in records) + "\n")
    return str(path)


def test_a_claude_session_counts_the_prompts_the_person_gave(tmp_path):
    """Typed prompts and slash commands; not tool results, a local command's output, an
    interrupt marker, a compaction summary, meta text, or a subagent's prompts."""
    def user(content, **flags):
        return dict({"type": "user", "message": {"role": "user", "content": content}}, **flags)
    p = _jsonl(tmp_path / "c.jsonl", [
        user("fix the test"),
        user([{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]),
        user([{"type": "text", "text": "and the docs"}, {"type": "image", "source": {}}]),
        user("<command-name>/tokeneconomizer:status</command-name>"),
        user("<local-command-stdout>done</local-command-stdout>"),
        user("[Request interrupted by user]"),
        user("Caveat: the messages below were generated by the user", isMeta=True),
        user("This session is being continued...", isCompactSummary=True),
        user("subagent task", isSidechain=True),
        {"type": "assistant", "message": {"role": "assistant", "content": "done"}}])
    assert trace.claude_prompt_count(p) == 3


def test_a_claude_background_task_notification_is_no_prompt(tmp_path):
    """Claude Code tells the model that a background task finished in a `user` entry of its
    own, `origin.kind` "task-notification" (2.1.317). The person typed one prompt here. The
    notification still starts a turn of its own: the model answers it."""
    note = {"type": "user", "promptSource": "system", "turnOrigin": "task_notification",
            "origin": {"kind": "task-notification", "producer": "session-task"},
            "message": {"role": "user", "content": (
                "<task-notification>\n<task-id>b1</task-id>\n<status>completed</status>\n"
                "<summary>Background command \"pytest\" completed (exit code 0)</summary>\n"
                "</task-notification>")}}
    p = _jsonl(tmp_path / "c.jsonl", [
        {"type": "user", "message": {"role": "user", "content": "run the tests in the background"}},
        {"type": "assistant", "message": {"role": "assistant", "content": "started"}},
        note,
        {"type": "assistant", "message": {"role": "assistant", "content": "they pass"}}])
    assert trace.claude_prompt_count(p) == 1
    assert len(list(trace.claude_prompt_turns(p))) == 2


def test_a_codex_session_counts_one_prompt_per_turn(tmp_path):
    turn = [{"type": "turn_context", "payload": {"turn_id": "t"}},
            {"type": "event_msg", "payload": {"type": "task_started"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user"}}]
    assert trace.codex_prompt_count(_jsonl(tmp_path / "r.jsonl", turn * 3)) == 3
    # a rollout without task_started events counts its turn contexts
    older = [r for r in turn if r["type"] != "event_msg"]
    assert trace.codex_prompt_count(_jsonl(tmp_path / "o.jsonl", older * 2)) == 2


def test_a_copilot_session_counts_its_user_messages(tmp_path):
    p = _jsonl(tmp_path / "events.jsonl", [
        {"type": "session.start", "data": {}}, {"type": "user.message", "data": {}},
        {"type": "tool.execution_start", "data": {}}, {"type": "user.message", "data": {}}])
    assert trace.copilot_prompt_count(p) == 2



def test_a_pi_session_counts_the_prompts_on_its_active_branch(tmp_path):
    """Pi keeps a conversation as a tree; a prompt on a branch the person left is not counted."""
    def msg(eid, parent, role):
        return {"type": "message", "id": eid, "parentId": parent, "message": {"role": role}}
    p = _jsonl(tmp_path / "pi.jsonl", [
        {"type": "session", "id": "s"}, msg("a", None, "user"), msg("b", "a", "assistant"),
        msg("x", "b", "user"), msg("y", "x", "assistant"),        # abandoned
        msg("c", "b", "user"), msg("d", "c", "toolResult"), msg("e", "d", "assistant")])
    assert trace.pi_prompt_count(p) == 2


# Valid JSON that is not an object. A reader that takes every line for a record calls `.get` on
# it. The array holds each marker a reader looks for before it parses a line.
NON_OBJECT_LINES = [
    "null", "42", '"text"',
    '["usage", "session_meta", "turn_context", "token_count", "response_item", '
    '"session.start", "type", "system"]']


def _around_non_objects(records):
    import json
    return NON_OBJECT_LINES + [json.dumps(r) for r in records] + NON_OBJECT_LINES


def test_a_non_object_line_does_not_discard_the_calls_around_it():
    claude = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": "ls"}}]}}
    codex = {"type": "item.completed",
             "item": {"type": "command_execution", "command": "ls", "exit_code": 0}}
    start = {"type": "tool.execution_start",
             "data": {"toolCallId": "c", "toolName": "bash", "arguments": {"command": "ls"}}}
    done = {"type": "tool.execution_complete", "data": {"toolCallId": "c", "success": True}}
    assert [c[:2] for c in trace.claude_calls(_around_non_objects([claude]))] == [
        ("Bash", {"command": "ls"})]
    assert [c[:2] for c in trace.codex_exec_calls(_around_non_objects([codex]))] == [
        ("shell", {"command": "ls"})]
    assert [c[:2] for c in trace.copilot_event_calls(_around_non_objects([start, done]))] == [
        ("bash", {"command": "ls"})]


def test_a_non_object_line_does_not_stop_a_session_from_being_read(tmp_path):
    from tokeneconomizer.trace.copilot import copilot_session_cwd
    claude = {"type": "assistant", "message": {"id": "m1", "usage": {"input_tokens": 5}}}
    meta = {"type": "session_meta", "payload": {"cwd": "/w", "cli_version": "1.0"}}
    count = {"type": "event_msg", "payload": {"type": "token_count", "info": {
        "total_token_usage": {"input_tokens": 10, "cached_input_tokens": 4,
                              "output_tokens": 3}}}}
    start = {"type": "session.start", "data": {"context": {"cwd": "/repo"}}}
    p = tmp_path / "claude.jsonl"
    p.write_text("\n".join(_around_non_objects([claude])))
    assert trace.claude_session_usage(str(p))["input_tokens"] == 5
    r = tmp_path / "rollout.jsonl"
    r.write_text("\n".join(_around_non_objects([meta, count])))
    assert trace.codex_session_meta(str(r)) == {"cwd": "/w", "cli_version": "1.0"}
    usage = trace.codex_session_usage(str(r))
    assert (usage["input_tokens"], usage["output_tokens"], usage["requests"]) == (6, 3, 1)
    assert copilot_session_cwd(_around_non_objects([start])) == "/repo"


def test_a_non_object_line_does_not_stop_a_rollout_from_being_converted(tmp_path):
    done = {"type": "event_msg", "payload": {"type": "item_completed", "item": {
        "type": "CommandExecution", "command": ["ls"], "exit_code": 0}}}
    call = {"type": "response_item", "payload": {
        "type": "function_call", "name": "x", "call_id": "c", "arguments": "{}"}}
    r, out = tmp_path / "rollout.jsonl", tmp_path / "exec.jsonl"
    r.write_text("\n".join(_around_non_objects([done, call])))
    assert trace.codex_rollout_to_exec(str(r), str(out)) == 1
    assert trace.codex_capability_trace(str(r), str(out)) == 2


def test_a_non_object_line_does_not_empty_a_tool_catalog():
    system = {"type": "system", "tools": ["Bash"]}
    checkpoint = {"type": "session.usage_checkpoint", "data": {"promptCacheBreakState": [
        {"models": {"m": {"tools": [{"name": "view"}]}}}]}}
    text = "\n".join(_around_non_objects([system]))
    assert trace.parse_tool_catalog_claude(text, snapshot={}) == {"Bash": ""}
    text = "\n".join(_around_non_objects([checkpoint]))
    assert trace.parse_tool_names_copilot(text) == {"view": ""}


def test_the_loop_guard_and_the_capability_audit_skip_a_non_object_line(tmp_path):
    from tokeneconomizer.loopguard.state import completed_calls
    from tokeneconomizer.toolprune.codex import inspect_usage
    assert [completed_calls(line, {}) for line in NON_OBJECT_LINES] == [[]] * 4
    reconnect = {"type": "error", "message": "Reconnecting... 1/5"}
    p = tmp_path / "exec.jsonl"
    p.write_text("\n".join(_around_non_objects([reconnect])))
    assert inspect_usage(str(p))["reconnects"] == 1
