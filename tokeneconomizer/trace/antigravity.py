"""Antigravity CLI (`agy`) sessions, read from the conversation's `transcript_full.jsonl`.

The transcript is one JSON object per line (snake_case), written under
`~/.gemini/antigravity-cli/brain/<conversation id>/.system_generated/logs/`. Verified on 1.5.1:

  * `USER_INPUT` (source `USER_EXPLICIT`) is a prompt, its text wrapped in `<USER_REQUEST>`.
  * `PLANNER_RESPONSE` (source `MODEL`) is one model call: `tool_calls` [{name, args}] and, on
    current versions, `input_tokens` (fresh input only), `cache_read_tokens` and `output_tokens`
    (thinking included). Older versions leave the token fields off most rows: missing is unknown,
    not zero.
  * The tool results follow their planner row, one `MODEL` step per call, in the call order.
    Current agy writes them all as `GENERIC` steps whose `content` reads `Created At: ...`,
    `Completed At: ...`, perhaps a line saying a pre-tool hook changed the arguments (rtk does),
    then `Command exited with code N.` and `Command output:` for a shell call; older versions
    write typed steps (`RUN_COMMAND` with an `exit_code`, `VIEW_FILE`, `GREP_SEARCH`, ...). A
    failed call has `status: "ERROR"` and an `error` string. `SYSTEM` rows (`CHECKPOINT`,
    `EPHEMERAL_MESSAGE`, ...) are agy's own and are skipped.

`antigravity_read` pairs the results with their calls and is what both the live adapter (a few
steps at a time, from where it stopped) and the fold (the whole file) use; its tool names and
arguments are the ones the mechanisms know (`Bash {command}`, `Read {file_path}`, ...), so no
mechanism needs an agy branch. Nothing is read from a tool's arguments but what that mapping
keeps: a file write's content, a prompt sent to a subagent, and agy's display-only keys
(`toolAction`, `toolSummary`) stay in agy's log.
"""
import json
import os
import re

from .jsonl import records as _records

_USER_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.S)
_EXIT_RE = re.compile(r"Command exited with code (-?\d+)")
# The lines agy puts ahead of a result's text: when it ran, and that a hook rewrote the call.
_PREFIX_RE = re.compile(r"(?:Created At|Completed At):|A pre-tool hook changed the arguments")
_TOKENS = (("input_tokens", "input_tokens"), ("cache_read_tokens", "cache_read_input_tokens"),
           ("output_tokens", "output_tokens"))
_ARG_CAP = 2000          # characters of an unmapped tool's string argument that are kept

# agy tool -> (name the mechanisms read, {their argument: agy's argument names, first found})
_FILE = ("TargetFile", "AbsolutePath", "Path")
_TOOLS = {
    "run_command": ("Bash", {"command": ("CommandLine",)}),
    "view_file": ("Read", {"file_path": _FILE}),
    "read_file": ("Read", {"file_path": _FILE}),
    "write_file": ("Write", {"file_path": _FILE}),
    "write_to_file": ("Write", {"file_path": _FILE}),
    "edit_file": ("Edit", {"file_path": _FILE}),
    "replace_file_content": ("Edit", {"file_path": _FILE}),
    "multi_replace_file_content": ("Edit", {"file_path": _FILE}),
    "sed_file": ("Edit", {"file_path": _FILE}),
    "grep_search": ("Grep", {"pattern": ("Query",), "path": ("SearchPath",)}),
    "find_by_name": ("Glob", {"pattern": ("Pattern",), "path": ("SearchDirectory",)}),
    "list_dir": ("Glob", {"path": ("DirectoryPath",)}),
}
_UI_KEYS = ("toolAction", "toolSummary", "WaitMsBeforeAsync")


def antigravity_tool(name, args):
    """(name, args) of a call as the mechanisms read it; a tool without a mapping keeps its name
    and its arguments less the UI-only ones."""
    args = args if isinstance(args, dict) else {}
    if name in _TOOLS:
        mapped, keys = _TOOLS[name]
        return mapped, {k: next((args[a] for a in names if isinstance(args.get(a), str)), "")
                        for k, names in keys.items()}
    return str(name or "?"), {k: v[:_ARG_CAP] if isinstance(v, str) else v
                              for k, v in args.items() if k not in _UI_KEYS}


def antigravity_result(row):
    """(text, exit_code or None, failed) of one tool-result step."""
    content = row.get("content") if isinstance(row.get("content"), str) else ""
    lines = content.replace("\r\n", "\n").split("\n")
    while lines and _PREFIX_RE.match(lines[0]):
        lines.pop(0)
    text = "\n".join(lines).strip("\n")
    code = row.get("exit_code")
    if not isinstance(code, int) or isinstance(code, bool):
        m = _EXIT_RE.search(text[:400])
        code = int(m.group(1)) if m else None
    failed = row.get("status") in ("ERROR", "INVALID") or bool(code)
    if isinstance(row.get("error"), str) and row["error"]:
        text = (row["error"] + "\n" + text).strip("\n")
    return text, code, failed


def _prompt_text(row):
    content = row.get("content") if isinstance(row.get("content"), str) else ""
    m = _USER_RE.search(content)
    return (m.group(1) if m else content.split("<ADDITIONAL_METADATA>")[0]).strip()


def _read_rows(path, offset):
    """[(offset after the line, row)] for the complete lines from `offset` on. A last line still
    being written is left for the next read; an offset past the end starts over."""
    try:
        with open(path, "rb") as fh:
            if offset > os.fstat(fh.fileno()).st_size:
                offset = 0
            fh.seek(offset)
            data = fh.read()
    except OSError:
        return []
    out, pos = [], offset
    for raw in data.splitlines(keepends=True):
        try:
            row = json.loads(raw)
        except ValueError:
            if not raw.endswith(b"\n"):
                break
            row = None
        pos += len(raw)
        if isinstance(row, dict):
            out.append((pos, row))
    return out


def _is_result(row):
    # A `finish` call may leave a FINISH step; it is agy's way of ending a turn, no tool run.
    return row.get("source") == "MODEL" and row.get("type") not in ("PLANNER_RESPONSE", "FINISH")


def antigravity_read(path, offset=0, after=-1, final=True):
    """(items, offset, step_index): what the transcript at `path` holds from byte `offset` on,
    skipping steps up to `after`, and where to read from next.

    An item is {"kind": "prompt", "text"} or {"kind": "tool", "name", "args", "cwd", "content",
    "exit_code", "is_error"}, in transcript order. A planner row's calls are paired with the
    result steps after it, in order, once the next planner row or prompt closes them. The last
    planner row is closed too if all its results are in or `final` is set (a read at the end of
    a turn); otherwise it is left for the next read, and the returned offset stays before it. A
    call with no result is left out."""
    rows = [(pos, r) for pos, r in _read_rows(path, offset)
            if not isinstance(r.get("step_index"), int) or r["step_index"] > after]
    items, done, calls, results = [], 0, None, []

    def close():
        nonlocal calls, results
        for spec, row in zip(calls or (), results):
            if row.get("status") == "RUNNING":
                continue
            text, code, failed = antigravity_result(row)
            name, args = antigravity_tool(spec.get("name"), spec.get("args"))
            cwd = (spec.get("args") or {}).get("Cwd")
            items.append({"kind": "tool", "name": name, "args": args,
                          "cwd": cwd if isinstance(cwd, str) else None,
                          "content": text, "exit_code": code, "is_error": failed})
        calls, results = None, []

    for i, (_pos, row) in enumerate(rows):
        if row.get("type") == "PLANNER_RESPONSE" or row.get("type") == "USER_INPUT":
            close()
            done = i + 1
            if row["type"] == "USER_INPUT":
                items.append({"kind": "prompt", "text": _prompt_text(row)})
            elif isinstance(row.get("tool_calls"), list):
                calls = [c for c in row["tool_calls"]
                         if isinstance(c, dict) and isinstance(c.get("args") or {}, dict)
                         and c.get("name") != "finish"]
                if calls:
                    done = i              # not consumed until its results are
        elif calls:
            if _is_result(row):
                results.append(row)
        else:
            done = i + 1
    if calls and (final or len(results) >= len(calls)):
        close()
        done = len(rows)
    if not done:
        return items, offset, after
    last = rows[done - 1][1].get("step_index")
    return items, rows[done - 1][0], last if isinstance(last, int) else after


def antigravity_session_to_claude(path, out_path):
    """Write the transcript as Claude Code transcript lines (the shape `cursor_events_to_claude`
    writes). Returns the number of records, or None if the file is unreadable or empty."""
    items = antigravity_read(path)[0]
    if not items:
        return None
    out = []
    for n, item in enumerate(items):
        if item["kind"] == "prompt":
            if item["text"]:
                out.append({"type": "user", "message": {"content": [
                    {"type": "text", "text": item["text"]}]}})
            continue
        tool_id = f"agy-{n}"
        out.append({"type": "assistant", "cwd": item["cwd"], "message": {"content": [
            {"type": "tool_use", "id": tool_id, "name": item["name"], "input": item["args"]}]}})
        out.append({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": tool_id, "content": item["content"],
             "is_error": item["is_error"]}]}})
    try:
        with open(out_path, "w", encoding="utf-8") as fh:
            for rec in out:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        return None
    return len(out)


def antigravity_session_usage(path):
    """Token breakdown over the planner rows that report usage; {} when none does. `input_tokens`
    is fresh input only, as agy writes it, so cached re-reads are `cache_read_input_tokens`.
    The transcript names no model, so `model` is "antigravity": an id no price table has, which
    the ledger prices on its labelled fallback curve (with none it would be the Claude curve)."""
    totals = {"input_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
              "output_tokens": 0}
    requests = 0
    for row in _records(path):
        if row.get("type") != "PLANNER_RESPONSE":
            continue
        seen = False
        for src, dst in _TOKENS:
            v = row.get(src)
            if isinstance(v, int) and not isinstance(v, bool):
                totals[dst] += v
                seen = True
        requests += seen
    if not requests:
        return {}
    return dict(totals, requests=requests, model="antigravity")


def antigravity_prompt_count(path):
    """How many prompts the person gave the conversation."""
    return sum(1 for row in _records(path) if row.get("type") == "USER_INPUT"
               and row.get("source") == "USER_EXPLICIT")
