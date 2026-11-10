"""Pi (earendil-works/pi): a session stored as a TREE of JSONL entries.

Entries link by `id` / `parentId`; the conversation is the path from the current leaf back to
the root, and an abandoned branch stays in the file. Assistant messages carry `toolCall` blocks
({id, name, arguments}) and their own usage; tool results are `toolResult` messages with
`isError`. `pi_session_to_claude` writes the ACTIVE branch as the Claude Code envelope the other
readers already handle, so no mechanism needs a Pi branch.

Usage, per pi-ai's own type: `input` excludes the cache fields, `output` already includes
reasoning, and `cacheWrite1h` is a subset of `cacheWrite` -- so the mapping to the ledger's four
fields is direct.
"""
import json
import os

from .jsonl import records


def _pi_entries(path):
    return list(records(path))


def pi_session_header(path):
    """The `session` header line ({id, cwd, version, ...}), {} if absent.

    omp (a Pi fork) starts its log with a `{"type":"title", ...}` line before the `session`
    one, so the first few entries are scanned, not just the first."""
    for ev in _pi_entries(path)[:8]:
        if ev.get("type") == "session":
            return ev
    return {}


def pi_active_branch(entries):
    """Entries on the path from the last entry (the leaf) back to the root, in order."""
    by_id = {e.get("id"): e for e in entries if e.get("id")}
    tree = [e for e in entries if e.get("id")]
    if not tree:
        return []
    path, cur, seen = [], tree[-1], set()
    while cur is not None and cur.get("id") not in seen:
        seen.add(cur.get("id"))
        path.append(cur)
        cur = by_id.get(cur.get("parentId"))
    return list(reversed(path))


def pi_prompt_count(path):
    """How many prompts the conversation ran: `user` messages on its active branch."""
    return sum(1 for e in pi_active_branch(_pi_entries(path))
               if e.get("type") == "message" and (e.get("message") or {}).get("role") == "user")


def _pi_usage(u):
    if not isinstance(u, dict):
        return None
    return {"input_tokens": int(u.get("input") or 0),
            "cache_read_input_tokens": int(u.get("cacheRead") or 0),
            "cache_creation_input_tokens": int(u.get("cacheWrite") or 0),
            "output_tokens": int(u.get("output") or 0),
            "cost_usd": float((u.get("cost") or {}).get("total") or 0)}


def _pi_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content
                         if isinstance(c, dict) and c.get("type") == "text")
    return ""


def pi_session_to_claude(path, out_path):
    """Write a Pi session's active branch as Claude Code transcript lines.

    Pi starts every shell command afresh in the session's directory, so each call is written
    with the header's `cwd`: an agent that types `cd /abs/repo && tox` into that directory ran
    plain `tox`, and is read that way. Returns the number of tool calls written, or None when
    the file is missing."""
    if not path or not os.path.exists(path):
        return None
    n = 0
    entries = _pi_entries(path)
    head = pi_session_header(path)
    cwd = head.get("cwd") if isinstance(head.get("cwd"), str) else None
    started = head.get("timestamp") if isinstance(head.get("timestamp"), str) else None
    with open(out_path, "w", encoding="utf-8") as out:
        for e in pi_active_branch(entries):
            if e.get("type") != "message":
                continue
            m = e.get("message") or {}
            role = m.get("role")
            if role == "user":
                user = {"role": "user", "content": _pi_text(m.get("content"))}
                if started:
                    user["timestamp"] = started
                if cwd:
                    user["cwd"] = cwd
                out.write(json.dumps({"type": "user", "message": user}) + "\n")
            elif role == "assistant":
                blocks = []
                for b in m.get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "toolCall":
                        blocks.append({"type": "tool_use", "id": b.get("id"),
                                       "name": b.get("name"), "input": b.get("arguments") or {}})
                        n += 1
                    elif isinstance(b, dict) and b.get("type") == "text":
                        blocks.append({"type": "text", "text": b.get("text", "")})
                msg = {"id": e.get("id"), "role": "assistant", "content": blocks,
                       "model": m.get("model")}
                usage = _pi_usage(m.get("usage"))
                if usage:
                    msg["usage"] = {k: v for k, v in usage.items() if k != "cost_usd"}
                out.write(json.dumps(dict({"type": "assistant", "message": msg},
                                          **({"cwd": cwd} if cwd else {}))) + "\n")
            elif role == "toolResult":
                out.write(json.dumps({"type": "user", "message": {"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": m.get("toolCallId"),
                    "content": _pi_text(m.get("content")),
                    "is_error": bool(m.get("isError"))}]}}) + "\n")
    return n


def pi_json_reply(stdout):
    """(text, usage) of a `pi --print --mode json` run: the last assistant message's text, and
    the usage of every assistant message in ledger fields, with Pi's model id and its own cost
    estimate (`cost_usd`). `usage` is None when the run produced no assistant message."""
    text, usage = "", None
    for line in (stdout or "").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        m = ev.get("message") if isinstance(ev, dict) and ev.get("type") == "message_end" else None
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        text = _pi_text(m.get("content")) or text
        u = _pi_usage(m.get("usage"))
        if u:
            usage = usage or dict.fromkeys(u, 0)
            for k, v in u.items():
                usage[k] += v
            usage["model"] = m.get("model") or usage.get("model")
    return text, usage


def pi_session_usage(path):
    """Token breakdown of a Pi session's active branch, plus Pi's own cost estimate."""
    totals = {"input_tokens": 0, "cache_read_input_tokens": 0,
              "cache_creation_input_tokens": 0, "output_tokens": 0}
    cost, requests, models = 0.0, 0, {}
    for e in pi_active_branch(_pi_entries(path)):
        u = None
        if e.get("type") == "message" and (e.get("message") or {}).get("role") == "assistant":
            m = e["message"]
            u = _pi_usage(m.get("usage"))
            if m.get("model"):
                models[m["model"]] = models.get(m["model"], 0) + 1
        elif e.get("type") in ("usage", "compaction", "branch_summary"):
            u = _pi_usage(e.get("usage"))
        if u:
            requests += 1
            cost += u.pop("cost_usd")
            for k in totals:
                totals[k] += u[k]
    totals.update(requests=requests, cost_usd_vendor=round(cost, 6),
                  model=max(models, key=models.get) if models else None)
    return totals
