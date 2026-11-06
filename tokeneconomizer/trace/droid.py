"""Factory Droid: a session stored as one JSONL file under `~/.factory/sessions/`.

Each line is a record with a `type`:

  * `session_start` -- the header: `id`, `cwd`, `version`, `title`, `owner`. This is the only
    place a session says which repository it belongs to.
  * `message`       -- one message under `message`: `role`, `content` (the Claude Code block
    list: `text`, `thinking`, `tool_use`, `tool_result`), `modelId`. Tool calls carry
    `{type, id, name, input}`; results carry `{type, tool_use_id, is_error, content}`.
  * `agent_turn_outcome`, `todo_state`, `compaction_state` -- bookkeeping, ignored here.

The format is close enough to a Claude Code transcript that a session is read by unwrapping
each `message` record into a `{"type": role, "message": ...}` line, which the readers already
handle; `droid_session_to_claude` writes that file.

Usage is not in the transcript. Droid keeps it beside the log, in `<session id>.settings.json`:
`tokenUsage` is this session's own breakdown (`inputTokens`, `outputTokens`,
`cacheCreationTokens`, `cacheReadTokens`, `thinkingTokens`, `factoryCredits`), and
`inclusiveTokenUsage` adds child sessions, which are folded separately -- so only `tokenUsage`
is read here, never the inclusive figure. A prompt is a `user` message that is neither an
injected system reminder nor a tool result; Droid marks messages it never shows the person with
`visibility: "user_only"` or `"llm_only"`, and those are not prompts either.
"""
import json
import os
import re
from datetime import datetime

SYSTEM_PREFIXES = ("<system-reminder", "<local-command", "[Request interrupted")
# Droid's own markers for messages that are not the person's prompt.
NOT_PROMPTS = ("llm_only", "user_only")

# Droid's tool names, as the names the mechanism readers already key on (Claude Code's). The
# inputs are the same shape: `command`, `file_path`, `old_string`/`new_string`.
_TOOL_NAMES = {
    "execute": "Bash", "script": "Bash",
    "create": "Write", "write": "Write",
    "edit": "Edit", "multiedit": "MultiEdit", "str_replace": "Edit",
    "applypatch": "Edit", "apply_patch": "Edit",
    "read": "Read", "ls": "LS", "grep": "Grep", "glob": "Glob",
}

# A session id names files (`folded.json` keys, `sessions/<id>.json`), so a log's id is taken
# only when it is a plain token: Droid's are UUIDs.
SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


def claude_tool_name(name):
    """A Droid tool name as the equivalent Claude Code one, or unchanged."""
    return _TOOL_NAMES.get((name or "").lower(), name)


def droid_records(path):
    """The parsed records of a Droid session, in file order (which is `seq` order)."""
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if isinstance(ev, dict):
                    out.append(ev)
    except OSError:
        return []
    return out


def _epoch(ts):
    """Droid's ISO timestamp as epoch seconds, or None."""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.fromisoformat(ts[:-1] + "+00:00" if ts.endswith("Z") else ts).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def droid_session_header(path):
    """The `session_start` record ({id, cwd, version, title, ...}), {} if absent."""
    for ev in droid_records(path):
        if ev.get("type") == "session_start":
            return ev
    return {}


def droid_session_cwd(path):
    """The directory the session started in, or None."""
    cwd = droid_session_header(path).get("cwd")
    return cwd if isinstance(cwd, str) and cwd else None


def droid_log_head(path):
    """A session's `{cwd, started}` from its header and first message, {} if unreadable.

    `started` is its first timestamp in epoch seconds, as every other host's head reports it."""
    head, started = {}, None
    for ev in droid_records(path):
        if ev.get("type") == "session_start" and not head:
            head = ev
        elif ev.get("type") == "message" and started is None:
            started = _epoch(ev.get("timestamp"))
        if head and started is not None:
            break
    out = {}
    if isinstance(head.get("cwd"), str) and head["cwd"]:
        out["cwd"] = head["cwd"]
    if started:
        out["started"] = started
    return out


def droid_safe_session_id(sid):
    """A log's id when it is a plain token (safe as a file name), else None."""
    return sid if isinstance(sid, str) and SESSION_ID_RE.fullmatch(sid) else None


def _droid_is_prompt(msg):
    """A message the person (or the agent that launched a task) typed, not injected text."""
    if (msg.get("role") or "") != "user":
        return False
    if msg.get("visibility") in NOT_PROMPTS:
        return False
    content = msg.get("content")
    if isinstance(content, str):
        return bool(content.strip()) and not content.lstrip().startswith(SYSTEM_PREFIXES)
    if not isinstance(content, list):
        return False
    texts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            return False                 # a tool result returned to the model
        if block.get("type") == "text":
            texts.append(block.get("text") or "")
    return any(t.strip() and not t.lstrip().startswith(SYSTEM_PREFIXES) for t in texts)


def droid_prompt_count(path):
    """How many prompts the session ran."""
    return sum(1 for ev in droid_records(path)
               if ev.get("type") == "message"
               and _droid_is_prompt(ev.get("message") or {}))


def _droid_content(content):
    """A message's content as blocks: strings become one text block."""
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    if isinstance(content, str) and content:
        return [{"type": "text", "text": content}]
    return []


def _claude_line(ev, cwd=None):
    """One Droid `message` record as the line `load_trajectory` reads, or None."""
    msg = ev.get("message") or {}
    role = msg.get("role")
    if role not in ("user", "assistant"):
        return None
    content = _droid_content(msg.get("content"))
    if not content:
        return None
    content = [dict(b, name=claude_tool_name(b.get("name")))
               if b.get("type") == "tool_use" else b for b in content]
    record = {"type": role, "message": {"role": role, "content": content}}
    if msg.get("modelId"):
        record["message"]["model"] = msg["modelId"]
    if cwd:
        record["cwd"] = cwd
    return record


def droid_session_to_claude(path, out_path):
    """Write a Droid session as Claude Code transcript lines, for `load_trajectory`.

    Every line carries the session's `cwd` (Droid records no per-call directory): a command
    typed as `cd sub && make` is still read from where the session started. Returns the number
    of tool calls written, or None when the file is missing."""
    if not path or not os.path.exists(path):
        return None
    cwd = droid_session_cwd(path)
    n = 0
    with open(out_path, "w", encoding="utf-8") as out:
        for ev in droid_records(path):
            if ev.get("type") != "message":
                continue
            record = _claude_line(ev, cwd)
            if record is None:
                continue
            n += sum(1 for b in record["message"]["content"] if b.get("type") == "tool_use")
            out.write(json.dumps(record) + "\n")
    return n


def droid_prompt_turns(path):
    """Each prompt-turn of a Droid session, as Claude Code log lines from its prompt to the
    next, one turn at a time. Used to seed the loop guard's envelope from imported sessions."""
    cwd = droid_session_cwd(path)
    current = None
    for ev in droid_records(path):
        if ev.get("type") != "message":
            continue
        record = _claude_line(ev, cwd)
        if record is None:
            continue
        line = json.dumps(record)
        if _droid_is_prompt(ev.get("message") or {}):
            if current is not None:
                yield current
            current = [line]
        elif current is not None:
            current.append(line)
    if current is not None:
        yield current


def droid_settings_path(transcript_path):
    """The settings file Droid keeps beside a session log, or None."""
    if not transcript_path:
        return None
    base = transcript_path[:-len(".jsonl")] if transcript_path.endswith(".jsonl") else \
        transcript_path
    return base + ".settings.json"


def droid_session_usage(transcript_path):
    """Token breakdown of a Droid session from the settings file beside its log.

    Returns the four fields `pricing.effective_tokens` reads, plus `model`, `requests` (assistant
    messages in the log), and Droid's own `factory_credits`. Only this session's `tokenUsage` is
    read: `inclusiveTokenUsage` also counts child sessions, which are folded on their own.
    Without a breakdown every count is None, so the ledger says `no_usage` rather than guessing.
    """
    out = {"input_tokens": None, "cache_read_input_tokens": None,
           "cache_creation_input_tokens": None, "output_tokens": None,
           "requests": 0, "model": None, "factory_credits": None}
    settings = droid_settings_path(transcript_path)
    data = {}
    if settings:
        try:
            with open(settings, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = {}
    if not isinstance(data, dict):
        data = {}
    out["model"] = data.get("model") if isinstance(data.get("model"), str) else None

    models, assistant = {}, 0
    for ev in droid_records(transcript_path):
        if ev.get("type") != "message":
            continue
        msg = ev.get("message") or {}
        if msg.get("role") != "assistant":
            continue
        assistant += 1
        mid = msg.get("modelId")
        if isinstance(mid, str) and mid:
            models[mid] = models.get(mid, 0) + 1
    out["requests"] = assistant
    if not out["model"] and models:
        out["model"] = max(models, key=models.get)

    usage = data.get("tokenUsage")
    if not isinstance(usage, dict):
        return out

    def _int(key):
        v = usage.get(key)
        return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0

    counts = {"input_tokens": _int("inputTokens"),
              "cache_read_input_tokens": _int("cacheReadTokens"),
              "cache_creation_input_tokens": _int("cacheCreationTokens"),
              "output_tokens": _int("outputTokens")}
    if not any(counts.values()):
        return out
    out.update(counts)
    credits = usage.get("factoryCredits")
    if isinstance(credits, (int, float)) and not isinstance(credits, bool):
        out["factory_credits"] = credits
    return out


def droid_sessions(sessions_root=None, env=None):
    """Every `*.jsonl` session under the Droid sessions directory, newest mtime first.

    Each row is `{path, id, cwd, version, title, started, mtime, size}`; `started` is the first
    message's timestamp in epoch seconds when it has one, else the file's mtime.
    `$DROID_SESSIONS_DIR` overrides
    the location (tests, or a non-default install)."""
    env = os.environ if env is None else env
    root = sessions_root or env.get("DROID_SESSIONS_DIR") or os.path.join(
        os.path.expanduser("~"), ".factory", "sessions")
    if not os.path.isdir(root):
        return []
    rows = []
    for dirpath, dirnames, filenames in os.walk(root):
        depth = os.path.relpath(dirpath, root).count(os.sep)
        if depth > 1:                    # `<workspace>/<session>.jsonl`, no deeper
            dirnames[:] = []
            continue
        for fname in filenames:
            if not fname.endswith(".jsonl"):
                continue
            path = os.path.join(dirpath, fname)
            header, started = {}, None
            try:
                mtime = os.path.getmtime(path)
                size = os.path.getsize(path)
                with open(path, encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        try:
                            ev = json.loads(line)
                        except ValueError:
                            continue
                        if ev.get("type") == "session_start" and not header:
                            header = ev
                        elif ev.get("type") == "message" and started is None:
                            started = _epoch(ev.get("timestamp"))
                        if header and started is not None:
                            break
            except OSError:
                continue
            rows.append({"path": path, "id": header.get("id") or os.path.splitext(fname)[0],
                         "cwd": header.get("cwd") if isinstance(header.get("cwd"), str) else None,
                         "version": header.get("version"), "title": header.get("title"),
                         # A subagent session names the session that launched it; it is part
                         # of that session's work, not a person's session of its own.
                         "parent": header.get("callingSessionId"),
                         "started": started, "mtime": mtime, "size": size,
                         "folder": os.path.basename(dirpath)})
    rows.sort(key=lambda r: (r["mtime"], r["path"]))
    return rows
