"""opencode (anomalyco/opencode): a session exported as JSON.

opencode keeps its sessions in its own database, which is not a stable format to read. What it
publishes is the export -- `opencode export <session>`, or `client.session.messages` from a
plugin -- a list of `{info, parts}` messages, or `{info, messages}` around that list. The plugin
writes that export beside the other state when a session ends, and this module reads the file.

A user message's `parts` carry its `text` parts. An assistant message carries its `modelID`,
`path.cwd`, `cost` and `tokens` ({input, output, reasoning, cache: {read, write}}), and a `tool`
part per call: `{callID, tool, state}`, where `state` is `completed` (`input`, `output`,
`metadata`) or `error` (`input`, `error`). `opencode_session_to_claude` writes the conversation as
the Claude Code envelope the other readers already handle, so no mechanism needs an opencode
branch.

Usage, per opencode's own type: `input` excludes the cache fields and `output` excludes
`reasoning`, so output tokens are the two together.

opencode 2.x exports with `opencode session export <session>`, and its messages are flat (2.2.5):
`{id, type: "user", text}` for a prompt, `{id, type: "assistant", model: {id}, content, cost,
tokens}` for an answer, whose `content` holds `text` parts and `{type: "tool", id, name, state:
{status, input, content}}` calls. They are read as the `{info, parts}` above (`_nested`).
"""
import json
import os


def _content_text(content):
    if isinstance(content, list):
        return "\n".join(c.get("text") or "" for c in content if isinstance(c, dict))
    return str(content or "")


def _nested(msg):
    """A flat 2.x message as `{info, parts}`; any other message as it is."""
    kind = msg.get("type")
    if "info" in msg or kind not in ("user", "assistant"):
        return msg
    info = {"id": msg.get("id"), "role": kind}
    if kind == "user":
        return {"info": info, "parts": [{"type": "text", "text": str(msg.get("text") or "")}]}
    model = msg.get("model") if isinstance(msg.get("model"), dict) else {}
    info.update(modelID=model.get("id"), cost=msg.get("cost"), tokens=msg.get("tokens"))
    parts = []
    for c in msg.get("content") or []:
        if not isinstance(c, dict):
            continue
        if c.get("type") == "text":
            parts.append({"type": "text", "text": str(c.get("text") or "")})
        elif c.get("type") == "tool":
            st = c.get("state") if isinstance(c.get("state"), dict) else {}
            state = {"status": st.get("status"), "input": st.get("input") or {}}
            if st.get("status") == "error":
                err = st.get("error")
                state["error"] = (str(err.get("message") or "") if isinstance(err, dict)
                                  else str(err or "")) or _content_text(st.get("content"))
            else:
                state["output"] = _content_text(st.get("content"))
            parts.append({"type": "tool", "callID": c.get("id"), "tool": c.get("name"),
                          "state": state})
    return {"info": info, "parts": parts}


def _load(path):
    """(session info, messages) of an export file; ({}, []) when it cannot be read."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}, []
    if isinstance(data, list):
        return {}, [_nested(m) for m in data if isinstance(m, dict)]
    if isinstance(data, dict):
        msgs = data.get("messages")
        return (data.get("info") if isinstance(data.get("info"), dict) else {},
                [_nested(m) for m in msgs if isinstance(m, dict)] if isinstance(msgs, list)
                else [])
    return {}, []


def _role(msg):
    return (msg.get("info") or {}).get("role")


def _text(msg):
    """The text a user typed: `text` parts that opencode did not add itself."""
    return "\n".join(p.get("text", "") for p in msg.get("parts") or []
                     if isinstance(p, dict) and p.get("type") == "text"
                     and not p.get("synthetic") and not p.get("ignored"))


def opencode_session_info(path):
    """The session's `info` ({id, directory, ...}), {} if the export does not carry it."""
    return _load(path)[0]


def opencode_prompt_count(path):
    """How many prompts the conversation ran: user messages that hold text."""
    return sum(1 for m in _load(path)[1] if _role(m) == "user" and _text(m).strip())


def _usage(info):
    t = info.get("tokens")
    if not isinstance(t, dict):
        return None
    cache = t.get("cache") if isinstance(t.get("cache"), dict) else {}
    return {"input_tokens": int(t.get("input") or 0),
            "cache_read_input_tokens": int(cache.get("read") or 0),
            "cache_creation_input_tokens": int(cache.get("write") or 0),
            "output_tokens": int(t.get("output") or 0) + int(t.get("reasoning") or 0),
            "cost_usd": float(info.get("cost") or 0)}


def opencode_session_to_claude(path, out_path):
    """Write an opencode export as Claude Code transcript lines.

    Returns the number of tool calls written, or None when the file is missing or holds no
    messages. A call still pending or running when the export was taken is written without a
    result, as a session that ended mid-call."""
    if not path or not os.path.exists(path):
        return None
    _, messages = _load(path)
    if not messages:
        return None
    n = 0
    with open(out_path, "w", encoding="utf-8") as out:
        for m in messages:
            info = m.get("info") or {}
            if info.get("role") == "user":
                text = _text(m)
                if text.strip():
                    out.write(json.dumps({"type": "user", "message": {
                        "role": "user", "content": text}}) + "\n")
                continue
            if info.get("role") != "assistant":
                continue
            blocks, results = [], []
            for p in m.get("parts") or []:
                if not isinstance(p, dict):
                    continue
                if p.get("type") == "text" and p.get("text"):
                    blocks.append({"type": "text", "text": p["text"]})
                elif p.get("type") == "tool":
                    st = p.get("state") if isinstance(p.get("state"), dict) else {}
                    blocks.append({"type": "tool_use", "id": p.get("callID"),
                                   "name": p.get("tool"), "input": st.get("input") or {}})
                    n += 1
                    if st.get("status") == "completed":
                        results.append((p.get("callID"), str(st.get("output") or ""), False))
                    elif st.get("status") == "error":
                        results.append((p.get("callID"), str(st.get("error") or ""), True))
            cwd = (info.get("path") or {}).get("cwd")
            msg = {"id": info.get("id"), "role": "assistant", "content": blocks,
                   "model": info.get("modelID")}
            usage = _usage(info)
            if usage:
                msg["usage"] = {k: v for k, v in usage.items() if k != "cost_usd"}
            out.write(json.dumps(dict({"type": "assistant", "message": msg},
                                      **({"cwd": cwd} if isinstance(cwd, str) and cwd else {})))
                      + "\n")
            for call_id, text, is_error in results:
                out.write(json.dumps({"type": "user", "message": {"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": call_id, "content": text,
                    "is_error": is_error}]}}) + "\n")
    return n


def opencode_session_usage(path):
    """Token breakdown of an opencode session, plus opencode's own cost estimate."""
    totals = {"input_tokens": 0, "cache_read_input_tokens": 0,
              "cache_creation_input_tokens": 0, "output_tokens": 0}
    cost, requests, models = 0.0, 0, {}
    for m in _load(path)[1]:
        info = m.get("info") or {}
        if info.get("role") != "assistant":
            continue
        u = _usage(info)
        if not u:
            continue
        requests += 1
        cost += u.pop("cost_usd")
        for k in totals:
            totals[k] += u[k]
        if info.get("modelID"):
            models[info["modelID"]] = models.get(info["modelID"], 0) + 1
    totals.update(requests=requests, cost_usd_vendor=round(cost, 6),
                  model=max(models, key=models.get) if models else None)
    return totals


def opencode_run_reply(stdout):
    """(text, usage, session_id) of an `opencode run --format json` run: the reply text, the
    usage of every finished step in ledger fields (with opencode's own cost as `cost_usd`), and
    the session it created. `usage` is None when no step finished."""
    text, usage, sid = [], None, ""
    for line in (stdout or "").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        sid = sid or str(ev.get("sessionID") or "")
        part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
        if ev.get("type") == "text" and part.get("text"):
            text.append(part["text"])
        elif ev.get("type") == "step_finish":
            u = _usage({"tokens": part.get("tokens"), "cost": part.get("cost")})
            if u:
                usage = usage or dict.fromkeys(u, 0)
                for k, v in u.items():
                    usage[k] += v
    return "".join(text).strip(), usage, sid
