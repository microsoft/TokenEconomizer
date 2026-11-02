"""Tiny synthetic fixtures for the offline test suite (no Docker, no datasets)."""
import json
import os


def write_claude_transcript(path, calls):
    """Write a minimal Claude-schema transcript.jsonl.
    `calls` is a list of (tool_name, input_dict, result_text[, cwd]); a cwd is recorded on the
    call's entry, as Claude Code records the directory its shell is in."""
    lines = []
    for i, (name, inp, res, *cwd) in enumerate(calls):
        tid = f"t{i}"
        lines.append(dict({"message": {"content": [
            {"type": "tool_use", "id": tid, "name": name, "input": inp}]}},
            **({"cwd": cwd[0]} if cwd else {})))
        lines.append({"message": {"content": [
            {"type": "tool_result", "tool_use_id": tid, "content": res}]}})
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(x) for x in lines))
    return path


def write_codex_transcript(path, items):
    """Write a minimal Codex-schema transcript.jsonl.
    `items` is a list of dicts merged into an `item.completed` event's `item`."""
    lines = []
    for i, item in enumerate(items):
        it = {"id": f"i{i}", "status": "completed"}
        it.update(item)
        lines.append({"type": "item.completed", "item": it})
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(x) for x in lines))
    return path


def run_dir_with(tmp_path, transcript_lines_writer):
    """Make a run dir containing a transcript.jsonl written by the given writer."""
    d = os.path.join(str(tmp_path), "run")
    os.makedirs(d, exist_ok=True)
    transcript_lines_writer(os.path.join(d, "transcript.jsonl"))
    return d


def write_claude_transcript_with_catalog(path, tools, calls):
    """Claude transcript that also carries the `system/init` line naming the LOADED tools.

    The tool catalog is only recoverable from that init line (trace.parse_catalog ->
    reflect.parse_tool_catalog_claude), so a fixture without it cannot exercise the
    evidence-untooling path at all: the stage would see an empty catalog forever and
    correctly drop nothing.
    """
    import json as _json
    lines = [{"type": "system", "subtype": "init", "tools": list(tools)}]
    for i, (name, inp, res) in enumerate(calls):
        tid = f"t{i}"
        lines.append({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": tid, "name": name, "input": inp}]}})
        lines.append({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": tid, "content": res}]}})
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(_json.dumps(x) for x in lines))
    return path


def opencode_session_export(sid="ses_f"):
    """`opencode session export` on 2.2.5, as #118 shows it: flat messages -- the prompt, an
    answer with a bash call that fails and one that passes, and the turn's end."""
    tokens = {"input": 18348, "output": 68, "reasoning": 2, "cache": {"read": 896, "write": 0}}
    msgs = [{"id": "msg_u", "time": {}, "type": "user", "text": "Run the tests", "files": []},
            {"id": "msg_a", "time": {}, "type": "assistant", "agent": "build",
             "model": {"id": "gpt-5.6-sol", "providerID": "openai"}, "content": [
                 {"type": "text", "text": "I'll run them."},
                 {"type": "tool", "id": "c1", "name": "bash", "executed": True,
                  "state": {"status": "error", "input": {"command": "pytest -q"},
                            "error": {"message": "bash: pytest: command not found"}}},
                 {"type": "tool", "id": "c2", "name": "bash", "executed": True,
                  "state": {"status": "completed", "input": {"command": "python -m pytest -q"},
                            "content": [{"type": "text", "text": "1 passed in 0.1s"}]}}],
             "cost": 0.0056, "tokens": tokens},
            {"id": "msg_i", "time": {}, "type": "idle", "outcome": "completed"}]
    return json.dumps({"info": {"id": sid, "projectID": "p", "cost": 0.0056},
                       "messages": msgs})
