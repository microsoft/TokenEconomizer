"""opencode's own database, read to recover the sessions its plugin never watched live.

opencode 2.x keeps every session in `<data>/opencode.db` (SQLite): `session_v2`, one row per
session, and `session_message`, one row per message with the message JSON in `data`. That schema
belongs to opencode; it is read-only here, and only to rebuild the shape `trace.opencode` already
understands -- the export document `{info, messages}` of `{info, parts}` messages the plugin
writes at a session's end.

Why it exists: the plugin learns from the sessions it sees end. A repository with a history -- a
clone that ran opencode for weeks before the plugin was installed, or a session whose end no
hook saw -- teaches it nothing until new sessions pile up. `import` (see
`plugin/backfill.py`) folds what the database already holds, and the live path stays the same.

What is reconstructed, and from where:

  * a user message: `data.text` and `data.files`, as one `text` part (`files` become a second
    part, so a prompt that was only an attachment still counts as a prompt);
  * an assistant message: `data.content`, whose parts are `text`, `reasoning` and `tool`; a
    `tool` part becomes `{callID, tool, state}` with its input, its output and its status, which
    is what `trace.calls` reads the calls out of. `reasoning` is dropped: no mechanism reads it,
    and it is most of the bytes;
  * the session's tokens and cost: opencode totals them on the session row, not per message, so
    they are attached to the first assistant message -- `trace.opencode_session_usage` sums the
    assistant messages, and the sum is then the session's own total.

Subagent sessions (`parent_id` set) are left out: their calls belong to the parent's trajectory,
and counting both would confirm every fact twice.
"""
import json
import os
import sqlite3

DEFAULT_DIR = (".local", "share", "opencode", "opencode.db")
_SKIP_DIR_PREFIXES = ("te-side-",)      # the plugin's own throwaway side-call directories


def db_path(env=None):
    """opencode's database: `TE_OPENCODE_DB`, else the one opencode keeps for every session.
    The home directory is read from `env` too, so a caller that hands over an environment gets
    the database that environment would name."""
    env = os.environ if env is None else env
    named = env.get("TE_OPENCODE_DB")
    if named:
        return os.path.abspath(named)
    home = env.get("HOME") or env.get("USERPROFILE") or os.path.expanduser("~")
    return os.path.join(home, *DEFAULT_DIR)


def connect(path):
    """A read-only connection: opencode writes this database while it runs (`mode=ro` keeps us
    from taking its write lock or leaving a journal of our own). Rows are keyed by column:
    every reader below names its columns."""
    conn = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True, timeout=2.0)
    conn.row_factory = sqlite3.Row
    return conn


def has_schema(conn):
    """Whether this is a 2.x database: `session_v2` and `session_message` both exist."""
    try:
        names = {r[0] for r in conn.execute(
            "select name from sqlite_master where type='table'")}
    except sqlite3.Error:
        return False
    return {"session_v2", "session_message"} <= names


def _rows(conn, sql, params=()):
    """Every row of a query as a dict keyed by column. Built from the cursor's own description,
    so a caller's connection (which may or may not have `sqlite3.Row` as its row factory) makes
    no difference."""
    cur = conn.execute(sql, params)
    names = [c[0] for c in cur.description]
    return [dict(zip(names, row)) for row in cur]


def norm_dir(path):
    """A directory as the database and the command line both spell it: `C:/a/b`."""
    return (path or "").replace("\\", "/").rstrip("/")


def _skipped_dir(directory):
    return os.path.basename(norm_dir(directory).lower()).startswith(_SKIP_DIR_PREFIXES)


def sessions(conn, since_ms, directories=None, exclude=(), include_subagents=False):
    """Sessions updated at or after `since_ms`, newest first, as plain dicts keyed by column.

    `directories` limits them to those directories (normalized, case-insensitive on Windows),
    `exclude` drops session ids the caller already knows -- a session still running, whose own
    end will fold it. Sessions with no message, of a throwaway directory, or of a subagent run
    are left out: they teach nothing, or teach it twice. Times are epoch seconds (`started`,
    `updated`), the way the session records the hooks write keep them."""
    wanted = None
    if directories is not None:
        wanted = {norm_dir(d).lower() for d in directories}
    out = []
    for d in _rows(conn,
                   """select s.id, s.directory, s.parent_id, s.time_created, s.time_updated,
                             s.tokens_input, s.tokens_output, s.tokens_reasoning,
                             s.tokens_cache_read, s.tokens_cache_write, s.cost,
                             (select count(*) from session_message m
                               where m.session_id = s.id) as n
                      from session_v2 s where s.time_updated >= ?
                      order by s.time_updated desc""",
                   (int(since_ms),)):
        d["directory"] = directory = norm_dir(d["directory"])
        d["messages"] = int(d["n"] or 0)
        d["started"] = (d["time_created"] or 0) / 1000.0
        d["updated"] = (d["time_updated"] or 0) / 1000.0
        if _skipped_dir(directory) or d["messages"] == 0:
            continue
        if wanted is not None and directory.lower() not in wanted:
            continue
        if d["id"] in exclude:
            continue
        if d["parent_id"] and not include_subagents:
            continue
        out.append(d)
    return out


def directories(conn, since_ms, include_subagents=False):
    """The project directories that ran a session since `since_ms`, each once, most recent
    first -- what `import --all` walks."""
    seen, out = set(), []
    for s in sessions(conn, since_ms, include_subagents=include_subagents):
        key = s["directory"].lower()
        if key not in seen:
            seen.add(key)
            out.append(s["directory"])
    return out


def _text(content):
    """The text of a part list, as `trace.opencode` joins it."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(str(p.get("text") or "") for p in content
                     if isinstance(p, dict) and p.get("type") == "text")


def _state(status):
    """opencode's statuses in the words `trace.opencode` reads: a call is `completed` or
    `error`, anything else still running when the session ended."""
    status = str(status or "").lower()
    if status in ("completed", "success", "succeeded", "done"):
        return "completed"
    if status in ("error", "failed", "failure", "denied"):
        return "error"
    return "running"


def _tool_part(part):
    st = part.get("state") if isinstance(part.get("state"), dict) else {}
    out = {"callID": part.get("id"), "tool": part.get("name"), "state": {
        "status": _state(st.get("status")), "input": st.get("input") or {}}}
    if out["state"]["status"] == "completed":
        out["state"]["output"] = _text(st.get("content")) or str(st.get("output") or "")
        if isinstance(st.get("metadata"), dict):
            out["state"]["metadata"] = st["metadata"]
    elif out["state"]["status"] == "error":
        out["state"]["error"] = str(st.get("error") or _text(st.get("content")) or "")
    return out


def assistant_parts(data):
    """An assistant message's `content` as parts `trace.opencode` reads: text and tool calls,
    with the reasoning dropped."""
    parts = []
    for p in data.get("content") or ():
        if not isinstance(p, dict):
            continue
        if p.get("type") == "text" and p.get("text"):
            parts.append({"type": "text", "text": p["text"]})
        elif p.get("type") == "tool":
            parts.append(dict(_tool_part(p), type="tool"))
    return parts


def _user_parts(data):
    parts = []
    if str(data.get("text") or "").strip():
        parts.append({"type": "text", "text": data["text"]})
    files = [f for f in data.get("files") or () if isinstance(f, str) and f]
    if files:
        parts.append({"type": "text", "text": "attached: " + ", ".join(files)})
    return parts


def _session_tokens(session):
    """The token totals opencode keeps on the session row, in the export's `info.tokens`."""
    total = {"input": int(session.get("tokens_input") or 0),
             "output": int(session.get("tokens_output") or 0),
             "reasoning": int(session.get("tokens_reasoning") or 0),
             "cache": {"read": int(session.get("tokens_cache_read") or 0),
                       "write": int(session.get("tokens_cache_write") or 0)}}
    return total if any(total[k] for k in ("input", "output", "reasoning")) else None


def document(conn, session):
    """The export document of one session: `{info, messages}`, or None when it holds no message.

    The messages are joined by `seq`, the order opencode ran them, and the first assistant
    message carries the session's tokens and cost -- the readers sum the assistant messages, so
    the sum is the session's total."""
    rows = _rows(conn,
                 "select type, data from session_message where session_id = ? order by seq",
                 (session["id"],))
    messages, tokens_used = [], False
    for row in rows:
        try:
            data = json.loads(row["data"] or "{}")
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        if row["type"] == "user":
            parts = _user_parts(data)
            if parts:
                messages.append({"info": {"role": "user"}, "parts": parts})
            continue
        if row["type"] != "assistant":
            continue
        info = {"role": "assistant", "path": {"cwd": session["directory"]}}
        model = data.get("model") if isinstance(data.get("model"), dict) else {}
        if model.get("id"):
            info["modelID"] = model["id"]
        if not tokens_used:
            tokens = _session_tokens(session)
            if tokens:
                info["tokens"] = tokens
                info["cost"] = float(session.get("cost") or 0)
                tokens_used = True
        parts = assistant_parts(data)
        if parts:
            messages.append({"info": info, "parts": parts})
    if not messages:
        return None
    return {"info": {"id": session["id"], "directory": session["directory"]},
            "messages": messages}


def write_export(conn, session, out_path):
    """Write one session's export; returns the number of messages, or None when it has none."""
    doc = document(conn, session)
    if doc is None:
        return None
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    tmp = f"{out_path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False)
        os.replace(tmp, out_path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return len(doc["messages"])
