"""ZCode (Z.ai): a session read from its SQLite store.

ZCode keeps every session in one database, `<ZCODE_STORAGE_DIR or ~/.zcode>/cli/db/db.sqlite`
(read off 0.17.1; the schema is opencode's, with usage tables of ZCode's own):

    session      (id, directory, parent_id, ...)           one row per conversation
    message      (id, session_id, time_created, data)      `data` JSON: role, modelID, tokens, ...
    part         (id, message_id, session_id, data)        `data` JSON: type text|reasoning|tool|...
    tool_usage   (session_id, tool_call_id, exit_code, ...)
    model_usage  (session_id, query_source, model_id, input_tokens, ...)

A hook's own `transcript_path` is no log: ZCode writes one line to a temporary file and deletes it
when the hook returns. So a fold copies a session's rows into one JSON file beside the other state
(`zcode_export`), and this module reads that file:

    {"info": {"id", "directory"},
     "messages": [{"prompt": true} | {"role": "assistant", "model", "cwd", "tools": [...]}],
     "usage": [<model_usage rows>]}

The copy leaves out the words: a prompt is counted and not kept, and what the model said or
reasoned is not read at all. A tool's input and output stay, since they are what is learned
(a file written by a tool is cut to `CAP` characters, where the guard folds a result too).

What the store says, checked on a real database (aggregates only, no conversation read):

  * A person's prompt is a `user` message with a non-empty `text` part. ZCode also writes user
    messages itself: a reminder, a goal's continuation, a background task's notice. They carry
    `source` (`todo_reminder`, `background_task`, `goal-continuation`, `goal_state_change`,
    `agent_listing_delta`) or `synthetic`, and are no prompts.
  * Tool names are Claude Code's (`Bash`, `Read`, `Write`, `Edit`, `Agent`), so no mapping is
    needed; `tool_usage.exit_code` has a shell call's real exit code.
  * `model_usage.input_tokens` COUNTS the cached reads (every row's total is input + output, and
    the cached part is never larger than the input), where the ledger's input does not, so the
    cache is taken out of it. `cache_creation_input_tokens` was 0 in every row. A subagent's
    calls are rows of its own session (`parent_id` set) with `query_source` `subagent`; the
    session that ran it paid for them, so they are added. A session forked from another has a
    parent too, but its rows say `main_turn`: it is its own session and not added.

Stdlib only. The store is opened read-only: ZCode is the only writer.
"""
import json
import os
import pathlib
import re
import sqlite3

CAP = 20000
# A session's id as ZCode writes it (`sess_...`); it names a file in the state and binds into a
# query, so anything else is refused.
_ID_RE = re.compile(r"\w[\w.-]*")
_PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File:\s*(.+?)\s*$", re.M)
# The `source` of a user message ZCode wrote itself. A source not listed counts as a prompt:
# losing a prompt costs less than a reminder counted as one.
MACHINE_SOURCES = frozenset({"todo_reminder", "background_task", "goal-continuation",
                             "goal_state_change", "agent_listing_delta"})
PROMPT = "[ZCode prompt]"
_USAGE_COLUMNS = ("model_id", "query_source", "input_tokens", "output_tokens", "reasoning_tokens",
                  "cache_creation_input_tokens", "cache_read_input_tokens")


def zcode_db(env=None):
    """Where ZCode keeps its sessions: `ZCODE_STORAGE_DIR` (ZCode's own override; its beta build
    sets `~/.zcode-beta`) or `~/.zcode`."""
    env = os.environ if env is None else env
    root = str(env.get("ZCODE_STORAGE_DIR") or "").strip() or os.path.join(
        os.path.expanduser("~"), ".zcode")
    return os.path.join(root, "cli", "db", "db.sqlite")


def zcode_tool(name, args):
    """(name, args) of a call as the mechanisms read it: ZCode's names are Claude Code's, but a
    GPT model on ZCode edits with a whole patch, the format of Codex's `apply_patch`."""
    args = args if isinstance(args, dict) else {}
    if str(name).lower() in ("applypatch", "apply_patch"):
        patch = next((v for v in (args.get("input"), args.get("patch"), args.get("command"))
                      if isinstance(v, str)), "")
        paths = _PATCH_FILE_RE.findall(patch)
        return "apply_patch", {"path": paths[0] if paths else "", "paths": paths}
    return name, args


# -- the live database, for a fold ----------------------------------------------------------
def _connect(db_path):
    con = sqlite3.connect(pathlib.Path(db_path).as_uri() + "?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _json(text):
    try:
        v = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def _cut(v):
    return v[:CAP] if isinstance(v, str) else v


def _is_prompt(message, parts):
    """Whether a `user` message is something a person typed."""
    if message.get("synthetic") or message.get("source") in MACHINE_SOURCES:
        return False
    return any(p.get("type") == "text" and not p.get("synthetic")
               and str(p.get("text") or "").strip() for p in parts)


def _tool(part, exit_codes):
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    call_id = part.get("callID")
    code = exit_codes.get(call_id)
    return {"id": call_id, "tool": part.get("tool"),
            "input": {k: _cut(v) for k, v in (state.get("input") or {}).items()}
            if isinstance(state.get("input"), dict) else {},
            "output": _cut(state.get("output")) if isinstance(state.get("output"), str) else "",
            "error": _cut(state.get("error")) if isinstance(state.get("error"), str) else "",
            "status": state.get("status"),
            "exit_code": code if isinstance(code, int) and not isinstance(code, bool) else None}


def zcode_export(db_path, session_id, path):
    """Write one session's rows as the JSON this module reads; whether it was written. False for
    a store that is missing, locked, not ZCode's or lacking the session, and then nothing is
    written (nor is a store created: it is opened read-only)."""
    if not isinstance(session_id, str) or not _ID_RE.fullmatch(session_id):
        return False
    try:
        con = _connect(db_path)
        try:
            row = con.execute("SELECT id, directory FROM session WHERE id = ?",
                              (session_id,)).fetchone()
            if not row:
                return False
            messages = con.execute("SELECT id, data FROM message WHERE session_id = ? "
                                   "ORDER BY time_created, id", (session_id,)).fetchall()
            parts = {}
            for r in con.execute("SELECT message_id, data FROM part WHERE session_id = ? "
                                 "ORDER BY id", (session_id,)):
                parts.setdefault(r["message_id"], []).append(_json(r["data"]))
            exit_codes = {r["tool_call_id"]: r["exit_code"] for r in con.execute(
                "SELECT tool_call_id, exit_code FROM tool_usage WHERE session_id = ?",
                (session_id,))}
            usage = con.execute(
                "WITH RECURSIVE sub(id) AS (SELECT id FROM session WHERE parent_id = ? "
                "UNION SELECT s.id FROM session s JOIN sub ON s.parent_id = sub.id) "
                f"SELECT {', '.join(_USAGE_COLUMNS)} FROM model_usage WHERE session_id = ? "
                "OR (session_id IN (SELECT id FROM sub) AND query_source = 'subagent')",
                (session_id, session_id)).fetchall()
        finally:
            con.close()
        out = []
        for m in messages:
            data, mparts = _json(m["data"]), parts.get(m["id"], [])
            if data.get("role") == "user":
                if _is_prompt(data, mparts):
                    out.append({"role": "user", "prompt": True})
            elif data.get("role") == "assistant":
                tools = [_tool(p, exit_codes) for p in mparts if p.get("type") == "tool"]
                if tools:
                    cwd = (data.get("path") or {}).get("cwd") if isinstance(
                        data.get("path"), dict) else None
                    out.append({"role": "assistant", "model": data.get("modelID"),
                                "cwd": cwd if isinstance(cwd, str) else None, "tools": tools})
        doc = {"info": {"id": row["id"], "directory": row["directory"]}, "messages": out,
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


# -- the export file ------------------------------------------------------------------------
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


def zcode_session_info(path):
    """The session's row ({id, directory}), {} if the export does not carry it."""
    return _load(path)[0]


def zcode_prompt_count(path):
    """How many prompts the conversation ran."""
    return sum(1 for m in _load(path)[1] if m.get("prompt"))


def _failed(tool):
    code = tool.get("exit_code")
    return tool.get("status") == "error" or (isinstance(code, int) and code != 0)


def zcode_session_to_claude(path, out_path):
    """Write an export as Claude Code transcript lines. Returns the number of tool calls written,
    or None when the file is missing or holds no messages. A call that never finished is written
    without a result, as a session that ended mid-call."""
    if not path or not os.path.exists(path):
        return None
    info, messages, _ = _load(path)
    if not messages:
        return None
    n = 0
    with open(out_path, "w", encoding="utf-8") as out:
        for m in messages:
            if m.get("prompt"):
                out.write(json.dumps({"type": "user", "message": {
                    "role": "user", "content": PROMPT}}) + "\n")
                continue
            tools = [t for t in m.get("tools") or [] if isinstance(t, dict)]
            cwd = m.get("cwd") or info.get("directory")
            blocks = []
            for t in tools:
                name, args = zcode_tool(str(t.get("tool") or ""), t.get("input"))
                blocks.append({"type": "tool_use", "id": t.get("id"), "name": name, "input": args})
            n += len(blocks)
            if blocks:
                out.write(json.dumps(dict({"type": "assistant", "message": {
                    "role": "assistant", "content": blocks, "model": m.get("model")}},
                    **({"cwd": cwd} if isinstance(cwd, str) and cwd else {}))) + "\n")
            for t in tools:
                if t.get("status") in ("running", None):
                    continue
                out.write(json.dumps({"type": "user", "message": {"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": t.get("id"),
                    "content": t.get("output") or t.get("error") or "",
                    "is_error": _failed(t)}]}}) + "\n")
    return n


def _int(row, key):
    v = row.get(key)
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


def zcode_session_usage(path):
    """Token breakdown of a session and its subagents, in the ledger's fields. `model` is the one
    that served the most calls; the title call a session makes of its own counts as one."""
    totals = {"input_tokens": 0, "cache_read_input_tokens": 0,
              "cache_creation_input_tokens": 0, "output_tokens": 0}
    models, requests = {}, 0
    for row in _load(path)[2]:
        cached = _int(row, "cache_read_input_tokens")
        totals["input_tokens"] += max(0, _int(row, "input_tokens") - cached)
        totals["cache_read_input_tokens"] += cached
        totals["cache_creation_input_tokens"] += _int(row, "cache_creation_input_tokens")
        # Reasoning is billed as output; the store reported 0 of it in every row seen.
        totals["output_tokens"] += _int(row, "output_tokens") + _int(row, "reasoning_tokens")
        requests += 1
        if row.get("model_id"):
            models[row["model_id"]] = models.get(row["model_id"], 0) + 1
    totals.update(requests=requests, model=max(models, key=models.get) if models else None)
    return totals
