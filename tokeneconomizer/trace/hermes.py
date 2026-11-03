"""Hermes Agent (NousResearch/hermes-agent): a session read from its `state.db`.

Hermes keeps sessions in SQLite, in `<HERMES_HOME>/state.db`; its schema is documented in the
Hermes developer guide (session storage). A fold copies a session's rows into one JSON file beside
the other state (`hermes_export`), from whatever the session left behind, a session its gateway
lost in mid-turn included, and this module reads that file:

    {"info": {<columns of the sessions row>}, "messages": [<columns of its rows>],
     "usage": [<usage rows>]}

The copy leaves out the words of the conversation: a prompt is `[Hermes prompt]`, which is enough
to count it, and the rest of a message's `content` is empty unless a tool returned it.

`messages` is the session's whole lineage, oldest first: compression can end a session and go on
in a child (`parent_session_id`, the parent's `end_reason` "compression"), and it keeps earlier
rows, so a call can appear twice and is read once (by `tool_call_id`). `usage` is every
`session_model_usage` row of that lineage and of its subagents: the main loop's (`task` empty)
and each auxiliary call's (title generation, compression, ...), which the session paid for too.

Usage, read off a scripted provider on 0.22.5: six calls of 1,000 prompt tokens, 600 of them
cached, were stored as `input_tokens` 2,400 and `cache_read_tokens` 3,600, so `input_tokens`
excludes the cache. `output_tokens` is the provider's completion count, reasoning included. The
top-level counters of `hermes --usage-file` do not have this shape (6,000 here): they are not read.

A tool result is the JSON string its tool returned, perhaps inside an `<untrusted_tool_result>`
wrapper and perhaps with a Hermes note after it. `hermes_result` reads it, and `hermes_tool` gives
a call the names the mechanisms know, so no mechanism needs a Hermes branch.
"""
import json
import os
import pathlib
import re
import sqlite3

# Hermes's tool -> the name the mechanisms read. `patch` also has a mode that takes a whole V4A
# patch, the format of Codex's `apply_patch` (below).
_TOOL_NAMES = {"terminal": "bash", "read_file": "read", "write_file": "write", "patch": "edit",
               "search_files": "grep"}
_PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File:\s*(.+?)\s*$", re.M)
_WRAPPER = "<untrusted_tool_result"
# The export names what it keeps: a session's row also holds its title, a description of its last
# activity and its errors, and its messages their reasoning, all in the person's or the model's words,
# and a later Hermes can add a column.
INFO_FIELDS = ("id", "source", "created_source", "cwd", "model", "parent_session_id", "started_at",
               "ended_at", "end_reason")
MESSAGE_FIELDS = ("role", "content", "tool_call_id", "tool_calls", "tool_name", "timestamp",
                  "message_uid", "_compressed_summary")
PROMPT = "[Hermes prompt]"


def hermes_tool(name, args):
    """(name, args) of a call as the mechanisms read it. A deferred tool arrives through the
    `tool_call` bridge, as {name, arguments}: it is that tool that ran."""
    args = args if isinstance(args, dict) else {}
    if name == "tool_call" and isinstance(args.get("name"), str):
        inner = args.get("arguments")
        name, args = args["name"], inner if isinstance(inner, dict) else {}
    if name == "patch" and isinstance(args.get("patch"), str):
        paths = _PATCH_FILE_RE.findall(args["patch"])
        return "apply_patch", {"path": paths[0] if paths else "", "paths": paths}
    return _TOOL_NAMES.get(name, name), args


def _json_object(text):
    """The JSON object a tool result holds, whatever Hermes put around it; None if there is none."""
    if not isinstance(text, str):
        return None
    start = text.find("{") if text.lstrip().startswith(_WRAPPER) else len(text) - len(text.lstrip())
    if start < 0 or text[start:start + 1] != "{":
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def hermes_result(text):
    """(text, exit_code or None, failed) of one tool result. A terminal call reports a real exit
    code; other tools report `error` or `success: false`."""
    obj = _json_object(text)
    if obj is None:
        return (text if isinstance(text, str) else str(text or "")), None, False
    code = obj.get("exit_code")
    code = code if isinstance(code, int) and not isinstance(code, bool) else None
    failed = bool(obj.get("error")) or bool(code) or obj.get("success") is False
    parts = [obj[k] for k in ("output", "content", "error") if isinstance(obj.get(k), str) and obj[k]]
    return ("\n".join(parts) or text), code, failed


def _load(path):
    """(info, messages, usage) of an export file; ({}, [], []) when it cannot be read."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, TypeError):
        return {}, [], []
    if not isinstance(data, dict):
        return {}, [], []
    rows = [[r for r in data.get(k) or [] if isinstance(r, dict)] if isinstance(
        data.get(k) or [], list) else [] for k in ("messages", "usage")]
    return (data.get("info") if isinstance(data.get("info"), dict) else {}), rows[0], rows[1]


def _prompts(messages):
    """The user's own messages, each once: compression adds its summary as a message, and a
    rotated session carries rows its parent already had."""
    seen, out = set(), []
    for m in messages:
        if m.get("role") != "user" or m.get("_compressed_summary"):
            continue
        key = m.get("message_uid") or (m.get("timestamp"), m.get("content"))
        if key not in seen and str(m.get("content") or "").strip():
            seen.add(key)
            out.append(m)
    return out


def hermes_session_info(path):
    """The session's row ({id, source, cwd, ...}), {} if the export does not carry it."""
    return _load(path)[0]


def hermes_prompt_count(path):
    """How many prompts the conversation ran."""
    return len(_prompts(_load(path)[1]))


def _tool_calls(msg):
    try:
        calls = json.loads(msg.get("tool_calls") or "[]")
    except (TypeError, ValueError):
        return []
    return [c for c in calls if isinstance(c, dict)] if isinstance(calls, list) else []


def hermes_session_to_claude(path, out_path):
    """Write an export as Claude Code transcript lines. Returns the number of tool calls written,
    or None when the file is missing or holds no messages. A call with no result yet is written
    without one, as a session that ended mid-call."""
    if not path or not os.path.exists(path):
        return None
    info, messages, _ = _load(path)
    if not messages:
        return None
    prompts = {id(m) for m in _prompts(messages)}
    cwd = info.get("cwd") if isinstance(info.get("cwd"), str) else ""
    seen_calls, seen_results, n = set(), set(), 0
    with open(out_path, "w", encoding="utf-8") as out:
        for m in messages:
            role = m.get("role")
            if role == "user" and id(m) in prompts:
                out.write(json.dumps({"type": "user", "message": {
                    "role": "user", "content": str(m.get("content"))}}) + "\n")
            elif role == "assistant":
                blocks = [{"type": "text", "text": m["content"]}] if m.get("content") else []
                for c in _tool_calls(m):
                    cid = c.get("id") or c.get("call_id")
                    if cid in seen_calls:
                        continue
                    seen_calls.add(cid)
                    fn = c.get("function") if isinstance(c.get("function"), dict) else {}
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except (TypeError, ValueError):
                        args = {}
                    name, args = hermes_tool(fn.get("name"), args)
                    blocks.append({"type": "tool_use", "id": cid, "name": name, "input": args})
                    n += 1
                if blocks:
                    out.write(json.dumps(dict({"type": "assistant", "message": {
                        "role": "assistant", "content": blocks, "model": info.get("model")}},
                        **({"cwd": cwd} if cwd else {}))) + "\n")
            elif role == "tool" and m.get("tool_call_id") not in seen_results:
                seen_results.add(m.get("tool_call_id"))
                text, _, failed = hermes_result(m.get("content"))
                out.write(json.dumps({"type": "user", "message": {"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": m.get("tool_call_id"), "content": text,
                    "is_error": failed}]}}) + "\n")
    return n


def _int(row, key):
    v = row.get(key)
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


def hermes_row_usage(row):
    """One `session_model_usage` row in ledger fields."""
    return {"input_tokens": _int(row, "input_tokens"),
            "cache_read_input_tokens": _int(row, "cache_read_tokens"),
            "cache_creation_input_tokens": _int(row, "cache_write_tokens"),
            "output_tokens": _int(row, "output_tokens"), "model": row.get("model")}


def hermes_session_usage(path):
    """Token breakdown of a session and its subagents, auxiliary calls included, plus Hermes's
    own cost estimate. `model` is the one that served the most calls."""
    totals = {"input_tokens": 0, "cache_read_input_tokens": 0,
              "cache_creation_input_tokens": 0, "output_tokens": 0}
    cost, requests, models = 0.0, 0, {}
    for row in _load(path)[2]:
        usage = hermes_row_usage(row)
        calls = _int(row, "api_call_count")
        requests += calls
        cost += float(row.get("estimated_cost_usd") or 0)
        for k in totals:
            totals[k] += usage[k]
        if usage["model"]:
            models[usage["model"]] = models.get(usage["model"], 0) + max(calls, 1)
    totals.update(requests=requests, cost_usd_vendor=round(cost, 6),
                  model=max(models, key=models.get) if models else None)
    return totals


# -- the live database, for the plugin's own side calls --------------------------------------
def _connect(db_path):
    con = sqlite3.connect(pathlib.Path(db_path).as_uri() + "?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _row(con, session_id):
    row = con.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return dict(row) if row else None


def _lineage(con, session_id):
    """The session's rows, oldest first, from any id of it. Compression can end a session and go
    on in a child, with a new id, and the plugin names a session by the first."""
    tip, seen = _row(con, session_id), set()
    while tip and tip.get("end_reason") == "compression" and tip["id"] not in seen:
        seen.add(tip["id"])
        child = con.execute("SELECT id FROM sessions WHERE parent_session_id = ? AND source IS NOT "
                            "'subagent' ORDER BY started_at DESC LIMIT 1", (tip["id"],)).fetchone()
        if not child:
            break
        tip = _row(con, child["id"])
    rows, seen, row = [], set(), tip
    while row and row["id"] not in seen:
        seen.add(row["id"])
        rows.insert(0, row)
        parent = _row(con, row["parent_session_id"]) if row.get("parent_session_id") else None
        row = parent if parent and parent.get("end_reason") == "compression" else None
    return rows


def _kept(row, fields):
    return {k: (row[k] if not isinstance(row[k], bytes) else None) for k in fields if k in row}


def _private(message):
    """A message as the export holds it. A person's prompt is counted, never kept, and what the
    model said is not read at all. A tool's arguments and result stay: they are what is learned."""
    row = _kept(message, MESSAGE_FIELDS)
    if row.get("role") == "tool":
        return row
    typed = (row.get("role") == "user" and not row.get("_compressed_summary")
             and str(row.get("content") or "").strip())
    return dict(row, content=PROMPT if typed else "")


def hermes_export(db_path, session_id, path):
    """Write a session's rows as the JSON this module reads; whether it was written."""
    try:
        con = _connect(db_path)
        try:
            chain = _lineage(con, session_id)
            if not chain:
                return False
            ids = [r["id"] for r in chain]
            marks = ",".join("?" * len(ids))
            messages = con.execute(f"SELECT * FROM messages WHERE session_id IN ({marks}) "
                                   "ORDER BY id", ids).fetchall()
            # What a subagent spent is the session's; its work is its own session.
            usage = con.execute(
                f"SELECT * FROM session_model_usage WHERE session_id IN ({marks}) OR session_id "
                f"IN (SELECT id FROM sessions WHERE parent_session_id IN ({marks}) "
                "AND source = 'subagent')", ids + ids).fetchall()
        finally:
            con.close()
        doc = {"info": _kept(chain[-1], INFO_FIELDS),
               "messages": [_private(dict(m)) for m in messages],
               "usage": [dict(u) for u in usage]}
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w",
                  encoding="utf-8") as fh:
            json.dump(doc, fh)
        os.replace(tmp, path)
        return True
    except (OSError, ValueError, TypeError, sqlite3.Error):
        return False


def hermes_session_row(db_path, session_id):
    """(row or None, its usage rows) for one session; (None, []) when the database cannot be
    read. `row` is None too for a session that is not there."""
    try:
        con = _connect(db_path)
        try:
            row = con.execute("SELECT id, title, ended_at FROM sessions WHERE id = ?",
                              (session_id,)).fetchone()
            usage = con.execute("SELECT * FROM session_model_usage WHERE session_id = ?",
                                (session_id,)).fetchall()
        finally:
            con.close()
    except (OSError, sqlite3.Error):
        return None, []
    return (dict(row) if row else None), [dict(r) for r in usage]


def hermes_find_session(db_path, source, prompt, since):
    """The id of the session of `source` started after `since` whose first message is `prompt`:
    a side call killed at its timeout never printed its id. None when there is none."""
    try:
        con = _connect(db_path)
        try:
            row = con.execute(
                "SELECT s.id FROM sessions s JOIN messages m ON m.session_id = s.id "
                "WHERE (s.source = ? OR s.created_source = ?) AND s.started_at >= ? "
                "AND m.role = 'user' AND m.content = ? ORDER BY s.started_at DESC LIMIT 1",
                (source, source, since, prompt)).fetchone()
        finally:
            con.close()
    except (OSError, sqlite3.Error):
        return None
    return row["id"] if row else None
