"""Claude Code's session transcript: tool calls, token usage, subagents, deferred tools.

The interactive transcript uses the same `message.content[]` envelope as `stream-json`, so one
reader serves both. Usage is the difference: the transcript writes ONE ENTRY PER CONTENT BLOCK,
each repeating its message's `usage`, so summing entries over-counts by the number of blocks;
`claude_session_usage` keys by message id. Subagents write their own files under
`<session>/subagents/`, and their tool calls and tokens belong to the session that spawned them.

Only the conversation's own entries (`assistant`, `user`) carry its calls. Claude Code 2.1.300
also writes `api-request-blob` entries that repeat each request's messages, earlier tool calls
and results included; read as calls, every call counted twice.
"""
import glob
import json
import os
from datetime import datetime

from .jsonl import lines as _lines

_CLAUDE_KEYS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
                "output_tokens")
CONVERSATION = ("assistant", "user")


def claude_calls(lines):
    """[(name, args, result_text, success, cwd)] for the lines of a Claude transcript: each
    `tool_use` block paired with its `tool_result` by id, with the directory the shell was in."""
    out, order, seen = [], [], {}
    results = {}
    for line in lines:
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("type", "assistant") not in CONVERSATION:
            continue
        msg = ev.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                tid = b.get("id")
                if tid in seen:
                    continue
                seen[tid] = {"name": b.get("name"), "args": b.get("input", {}),
                             "cwd": ev.get("cwd") if isinstance(ev.get("cwd"), str) else None}
                order.append(tid)
            elif b.get("type") == "tool_result":
                tid = b.get("tool_use_id")
                c = b.get("content")
                if isinstance(c, list):
                    c = " ".join(x.get("text", "") for x in c
                                 if isinstance(x, dict))
                results[tid] = (c if isinstance(c, str) else "",
                                not b.get("is_error", False))
    for tid in order:
        info = seen[tid]
        res_text, ok = results.get(tid, ("", True))
        out.append((info["name"], info["args"], res_text, ok, info["cwd"]))
    return out


def _is_prompt(ev):
    """Is this entry a prompt the person gave, typed or as a slash command? Its own `user`
    entries, less tool results, a local command's output, an interrupt marker, a compaction
    summary, the meta text Claude Code adds, and a subagent's."""
    if (ev.get("type") != "user" or ev.get("isMeta") or ev.get("isSidechain")
            or ev.get("isCompactSummary")):
        return False
    content = (ev.get("message") or {}).get("content")
    if isinstance(content, list):
        blocks = [b for b in content if isinstance(b, dict)]
        if any(b.get("type") == "tool_result" for b in blocks):
            return False
        content = " ".join(b.get("text") or "" for b in blocks if b.get("type") == "text")
    return isinstance(content, str) and not content.startswith(("<local-command",
                                                                "[Request interrupted"))


def claude_prompt_count(transcript_path):
    """How many prompts the person gave a Claude Code session (`_is_prompt`)."""
    n = 0
    for line in _lines(transcript_path):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        n += isinstance(ev, dict) and _is_prompt(ev)
    return n


def claude_projects_dir(env=None):
    """Where Claude Code files its session logs: `$CLAUDE_CONFIG_DIR/projects`, else
    `~/.claude/projects`."""
    env = os.environ if env is None else env
    base = env.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(base, "projects")


def claude_entrypoint(transcript_path, limit=20):
    """The front end a Claude Code session named in its first records: `sdk-cli` for an
    automated SDK / `claude -p` run, `cli` for the terminal UI, "" when it names none."""
    for n, line in enumerate(_lines(transcript_path)):
        if n >= limit:
            break
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        ep = ev.get("entrypoint")
        if not isinstance(ep, str):
            ep = (ev.get("message") or {}).get("entrypoint")
        if isinstance(ep, str) and ep.strip():
            return ep.strip()
    return ""


def claude_history(projects_dir):
    """(session_id, path, mtime) for every finished TOP-LEVEL session log under
    `projects_dir`: `<project>/<session-id>.jsonl`.

    A session's subagents are not sessions of their own -- their calls and tokens already count
    with the session that spawned them (`claude_session_usage`, `claude_tool_usage`) -- so the
    `<session-id>/subagents/` files are never listed."""
    try:
        projects = sorted(os.listdir(projects_dir))
    except OSError:
        return
    for project in projects:
        d = os.path.join(projects_dir, project)
        if not os.path.isdir(d):
            continue
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(d, name)
            if not os.path.isfile(path):
                continue
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            yield os.path.splitext(name)[0], path, mtime


def claude_session_span(transcript_path):
    """The first and last `timestamp` of a Claude Code transcript, as epoch seconds; None for
    either when the log has none. Used when a session was never recorded live (see the
    plugin's history backfill)."""
    first = last = None
    for line in _lines(transcript_path):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        ts = ev.get("timestamp") if isinstance(ev, dict) else None
        if not isinstance(ts, str):
            continue
        try:
            stamp = datetime.fromisoformat(ts[:-1] + "+00:00" if ts.endswith("Z") else ts)
            stamp = stamp.timestamp()
        except (ValueError, OverflowError, OSError):
            continue
        if first is None:
            first = stamp
        last = stamp
    return first, last


def claude_subagent_files(transcript_path):
    """Subagent transcripts of a Claude Code session: `<dir>/<session-id>/subagents/*.jsonl`."""
    if not transcript_path:
        return []
    stem = os.path.splitext(transcript_path)[0]
    return sorted(glob.glob(os.path.join(stem, "subagents", "*.jsonl")))


def claude_session_usage(transcript_path, include_subagents=True):
    """Token breakdown of a Claude Code session, de-duplicated by assistant message id.

    Returns the four breakdown fields `pricing.effective_tokens` reads, plus `model` (the most
    used one), `requests` (distinct assistant messages carrying usage) and `files` read."""
    files = [transcript_path] + (claude_subagent_files(transcript_path)
                                 if include_subagents else [])
    by_id, models, anon = {}, {}, 0
    for path in files:
        for line in _lines(path):
            if '"usage"' not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") != "assistant":
                continue
            msg = ev.get("message") or {}
            usage = msg.get("usage")
            if not isinstance(usage, dict):
                continue
            mid = msg.get("id")
            if not mid:
                anon += 1
                mid = f"anon-{path}-{anon}"
            # Later entries of the same message carry the final counts; keep the last.
            by_id[mid] = {k: int(usage.get(k) or 0) for k in _CLAUDE_KEYS}
            model = msg.get("model")
            if model:
                models[model] = models.get(model, 0) + 1
    out = {k: sum(u[k] for u in by_id.values()) for k in _CLAUDE_KEYS}
    out["requests"] = len(by_id)
    out["model"] = max(models, key=models.get) if models else None
    out["files"] = len([f for f in files if os.path.exists(f)])
    return out


def claude_tool_usage(transcript_path, include_subagents=True):
    """{tool: calls} over a session and, by default, its subagents -- a tool a subagent used is
    in use, and denying it would take it from the subagent too."""
    from .calls import tool_usage
    counts = {}
    files = [transcript_path] + (claude_subagent_files(transcript_path)
                                 if include_subagents else [])
    for path in files:
        for name, n in tool_usage(path).items():
            counts[name] = counts.get(name, 0) + n
    return counts


def claude_deferred_tools(transcript_path):
    """Tool names the session listed as deferred (`deferred_tools_delta` attachments).

    Deferred tools cost one listing line each, not a schema, so they matter little to the bill;
    they are still part of what the session could call, so the pruning catalog includes them."""
    names = set()
    for line in _lines(transcript_path):
        if '"deferred_tools_delta"' not in line:
            continue
        try:
            att = json.loads(line).get("attachment") or {}
        except ValueError:
            continue
        names.update(n for n in (att.get("addedNames") or []) if isinstance(n, str))
        names.difference_update(n for n in (att.get("removedNames") or []) if isinstance(n, str))
    return names
