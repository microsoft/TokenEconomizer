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
import re
from datetime import datetime

from .jsonl import lines as _lines

_CLAUDE_KEYS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
                "output_tokens")
# The part of `cache_creation_input_tokens` written to the 1-hour cache, which Anthropic bills
# at 2x input where the 5-minute cache is 1.25x. A message's usage splits its cache writes in
# `cache_creation: {ephemeral_5m_input_tokens, ephemeral_1h_input_tokens}`.
_CLAUDE_1H = "cache_creation_1h_input_tokens"
CONVERSATION = ("assistant", "user")
SYNTHETIC_MODEL = "<synthetic>"     # the model of an entry Claude Code wrote, not a model's reply


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
        if not isinstance(ev, dict):
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
                    c = "\n".join(x.get("text", "") for x in c
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


# ---- the session logs Claude Code keeps -----------------------------------------------------
# A session id names files (`folded.json` keys, `sessions/<id>.json`), so a log's id is taken
# only when it is a plain token: Claude Code's are UUIDs.
SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
HEAD_BYTES = 8 << 20     # how much of a log's head is read, at most, for its metadata
LINE_BYTES = 1 << 20     # an entry longer than this is skipped, not parsed
HEAD_LINES = 200


def claude_safe_session_id(sid):
    """`sid` when it is safe to put in a file name (`SESSION_ID_RE`), else None."""
    return sid if isinstance(sid, str) and SESSION_ID_RE.fullmatch(sid) else None


def claude_projects_dir(env=None):
    """Where Claude Code keeps its session logs: `$CLAUDE_CONFIG_DIR/projects`, else
    `~/.claude/projects`."""
    env = os.environ if env is None else env
    base = env.get("CLAUDE_CONFIG_DIR")
    if not base:
        home = env.get("HOME") if os.name != "nt" else env.get("USERPROFILE")
        base = os.path.join(home or os.path.expanduser("~"), ".claude")
    return os.path.join(base, "projects")


def claude_project_folder(path):
    """The folder name Claude Code files a directory's sessions under: every character that is
    not a letter or a digit becomes `-` (`/home/u/widget` -> `-home-u-widget`)."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path or ""))


def claude_history_logs(projects_dir):
    """[(folder, path, mtime, size)] for every top-level session log under `projects_dir`:
    `<folder>/<session id>.jsonl`. A session's subagents write under `<folder>/<session id>/
    subagents/` (earlier versions: `<folder>/agent-*.jsonl`); their calls and tokens belong to
    the session that spawned them, so they are never listed as sessions of their own."""
    out = []
    try:
        folders = sorted(os.listdir(projects_dir))
    except OSError:
        return out
    for folder in folders:
        d = os.path.join(projects_dir, folder)
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for name in names:
            if not name.endswith(".jsonl") or name.startswith("agent-"):
                continue
            path = os.path.join(d, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            if not os.path.isfile(path):
                continue
            out.append((folder, path, st.st_mtime, st.st_size))
    return out


def _epoch(ts):
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.fromisoformat(ts[:-1] + "+00:00" if ts.endswith("Z") else ts).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def claude_log_head(path):
    """What the head of a session log says about the session: {"session_id", "cwd", "started"
    (its first timestamp, epoch seconds), "entrypoint"} -- each None (or "") where the first
    lines do not say. `entrypoint` is the front end: `cli` for the terminal UI, `sdk-*` for
    `claude -p` and other SDK runs. Reads at most `HEAD_BYTES`, and stops at the first lines that
    say all four."""
    out = {"session_id": None, "cwd": None, "started": None, "entrypoint": ""}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            left = HEAD_BYTES
            for _ in range(HEAD_LINES):
                line = fh.readline(min(left, LINE_BYTES)) if left > 0 else ""
                if not line:
                    break
                left -= len(line)
                if not line.endswith("\n") and len(line) >= LINE_BYTES:
                    while left > 0:             # a long entry (a snapshot): skip the rest
                        rest = fh.readline(min(left, LINE_BYTES))
                        left -= len(rest)
                        if not rest or rest.endswith("\n"):
                            break
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(ev, dict):
                    continue
                if out["session_id"] is None and isinstance(ev.get("sessionId"), str):
                    out["session_id"] = ev["sessionId"]
                if out["cwd"] is None and isinstance(ev.get("cwd"), str) and ev["cwd"]:
                    out["cwd"] = ev["cwd"]
                if out["started"] is None:
                    out["started"] = _epoch(ev.get("timestamp"))
                if not out["entrypoint"] and isinstance(ev.get("entrypoint"), str):
                    out["entrypoint"] = ev["entrypoint"].strip()
                if out["session_id"] and out["cwd"] and out["started"] and out["entrypoint"]:
                    break
    except OSError:
        pass
    return out


def claude_prompt_turns(transcript_path):
    """Each prompt-turn of a Claude Code session, as the log lines from its prompt to the next
    (`_is_prompt`), one turn at a time. Lines before the first prompt (file snapshots, setup) are
    no turn, and a subagent's sidechain entries are not the main conversation's."""
    current = None
    for line in _lines(transcript_path):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        if _is_prompt(ev):
            if current is not None:
                yield current
            current = [line]
        elif current is not None and not ev.get("isSidechain"):
            current.append(line)
    if current is not None:
        yield current


def claude_turn_interrupted(turn_lines):
    """Did the person interrupt this turn? Claude Code then writes a user entry starting
    `[Request interrupted by user`."""
    for line in turn_lines:
        if "[Request interrupted" not in line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict) or ev.get("type") != "user":
            continue
        content = (ev.get("message") or {}).get("content")
        if isinstance(content, list):
            content = " ".join(b.get("text") or "" for b in content
                               if isinstance(b, dict) and b.get("type") == "text")
        if isinstance(content, str) and content.startswith("[Request interrupted"):
            return True
    return False


def claude_turn_answered(turn_lines):
    """Does the turn end with text the assistant wrote, rather than a tool call or result left
    without an answer (a session killed mid-turn)?"""
    for line in reversed(turn_lines):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict) or ev.get("type") not in CONVERSATION:
            continue
        if ev.get("type") != "assistant":
            return False
        content = (ev.get("message") or {}).get("content")
        if isinstance(content, str):
            return bool(content.strip())
        return any(isinstance(b, dict) and b.get("type") == "text"
                   and (b.get("text") or "").strip() for b in content or [])
    return False


def claude_subagent_files(transcript_path):
    """Subagent transcripts of a Claude Code session: `<dir>/<session-id>/subagents/*.jsonl`."""
    if not transcript_path:
        return []
    stem = os.path.splitext(transcript_path)[0]
    return sorted(glob.glob(os.path.join(stem, "subagents", "*.jsonl")))


def _cache_write_1h(usage, counts):
    """A message's 1-hour cache-write tokens, never more than the cache writes it reports: the
    split is a breakdown of that total, and a notice with no usage of its own can carry one."""
    split = usage.get("cache_creation")
    if not isinstance(split, dict):
        return 0
    try:
        n = int(split.get("ephemeral_1h_input_tokens") or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, min(n, counts["cache_creation_input_tokens"]))


def claude_session_usage(transcript_path, include_subagents=True):
    """Token breakdown of a Claude Code session, de-duplicated by assistant message id.

    Returns the four breakdown fields `pricing.effective_tokens` reads and the 1-hour share of
    the cache writes (`cache_creation_1h_input_tokens`), plus `model` (the most used one),
    `requests` (distinct assistant messages carrying usage, less the `<synthetic>` ones Claude
    Code writes itself) and `files` read."""
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
            if not isinstance(ev, dict) or ev.get("type") != "assistant":
                continue
            msg = ev.get("message") or {}
            usage = msg.get("usage")
            if not isinstance(usage, dict):
                continue
            # Claude Code writes its own notices (an API error, "No response requested.") as
            # assistant entries of model `<synthetic>` with zero usage: no request was made.
            if msg.get("model") == SYNTHETIC_MODEL:
                continue
            mid = msg.get("id")
            if not mid:
                anon += 1
                mid = f"anon-{path}-{anon}"
            # Later entries of the same message carry the final counts; keep the last.
            by_id[mid] = {k: int(usage.get(k) or 0) for k in _CLAUDE_KEYS}
            by_id[mid][_CLAUDE_1H] = _cache_write_1h(usage, by_id[mid])
            model = msg.get("model")
            if model:
                models[model] = models.get(model, 0) + 1
    out = {k: sum(u[k] for u in by_id.values()) for k in (*_CLAUDE_KEYS, _CLAUDE_1H)}
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
