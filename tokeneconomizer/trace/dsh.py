"""DeepSeek Harness (dsh): a session as the plugin exports it.

dsh writes its own log Zstandard-compressed by default (`~/.dsh/sessions/--<cwd>--/<id>/
session.v4.jsonl.zstd`), which the standard library cannot read. The plugin in `plugin/dsh/`
therefore writes `agent.session.snapshotEvents()` -- the same records, uncompressed -- to
`<state root>/dsh-sessions/<id>.json` at the end of every turn, as `{header, events}`, and this
module reads that file.

`header` is the session header (`{id, cwd, createdAt, origin?, delegationDepth, ...}`); each
event is `{type, seq, time, data}` (dsh-session `SessionEvent`, verified on 0.2.2-rc.5):

  * `user/message`: `data` is the message `{content, source}`; only `source.kind == "user"` is a
    person's prompt -- injected context carries the injecting package's kind;
  * `assistant/message`: `data.message.content` holds `text` blocks and `tool-call` blocks
    `{id, name, arguments}`, `arguments` being the JSON text the model wrote; `data.usage` is
    the step's `TokenUsage`;
  * `tool/result`: `data.message` is `{toolCallId, content, isError}`;
  * `request/context` and `request/header` name the provider's model id for the next request.

Usage: dsh's counts are disjoint (`inputTokens` is uncached input only; `cacheReadTokens` and
`cacheWriteTokens` are separate, dsh-llm `TokenUsage`), and `outputTokens` is the provider's
completion count, which includes reasoning on DeepSeek; `reasoningTokens`, where an adapter
sets it, is part of it and is not added again.

`dsh_session_to_claude` writes the conversation as the Claude Code envelope the other readers
already handle, with dsh's tool names mapped to Claude's (`TOOL_NAMES`).
"""
import json
import os

# dsh's built-in tools by the Claude Code names the readers key on. Their arguments already
# use Claude's names (bash `command`; read/write/edit `file_path`, edit `old_string` and
# `new_string`); `apply_patch` takes `input`, the patch text.
TOOL_NAMES = {"bash": "Bash", "read": "Read", "write": "Write", "edit": "Edit",
              "apply_patch": "Edit", "glob": "Glob", "grep": "Grep", "web_fetch": "WebFetch",
              "web_search": "WebSearch", "subagent": "Task", "todo_write": "TodoWrite"}


def claude_tool_name(name):
    return TOOL_NAMES.get(str(name or ""), str(name or ""))


def _load(path):
    """(header, events) of an export file; ({}, []) when it cannot be read."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, TypeError):
        return {}, []
    if not isinstance(data, dict):
        return {}, []
    header = data.get("header") if isinstance(data.get("header"), dict) else {}
    events = data.get("events") if isinstance(data.get("events"), list) else []
    return header, [e for e in events if isinstance(e, dict)]


def _data(ev):
    return ev.get("data") if isinstance(ev.get("data"), dict) else {}


def _blocks_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(str(b.get("text") or "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text")


def _prompt_text(ev):
    """The text of a person's prompt, "" for any other event."""
    if ev.get("type") != "user/message":
        return ""
    msg = _data(ev)
    source = msg.get("source") if isinstance(msg.get("source"), dict) else {}
    if source.get("kind") != "user":
        return ""
    return _blocks_text(msg.get("content"))


def dsh_session_header(path):
    """The session's header ({id, cwd, ...}), {} if the export does not carry it."""
    return _load(path)[0]


def dsh_prompt_count(path):
    """How many prompts the conversation ran: a person's messages that hold text."""
    return sum(1 for e in _load(path)[1] if _prompt_text(e).strip())


def _usage(u):
    if not isinstance(u, dict) or u.get("inputTokens") is None:
        return None
    return {"input_tokens": int(u.get("inputTokens") or 0),
            "cache_read_input_tokens": int(u.get("cacheReadTokens") or 0),
            "cache_creation_input_tokens": int(u.get("cacheWriteTokens") or 0),
            "output_tokens": int(u.get("outputTokens") or 0)}


def _model_of(ev, current):
    """The model id an event names for the requests after it, else `current`."""
    d = _data(ev)
    if ev.get("type") == "request/context" and d.get("model"):
        return str(d["model"])
    if ev.get("type") == "request/header":
        cfg = ((d.get("header") or {}).get("config") or {}) if isinstance(d.get("header"),
                                                                          dict) else {}
        if isinstance(cfg, dict) and cfg.get("model"):
            return str(cfg["model"])
    return current


def _arguments(raw):
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(raw) if isinstance(raw, str) and raw else {}
    except ValueError:
        return {"_raw": raw}
    return v if isinstance(v, dict) else {"_raw": raw}


def dsh_session_to_claude(path, out_path):
    """Write a dsh export as Claude Code transcript lines.

    Returns the number of tool calls written, or None when the file is missing or holds no
    events. A call with no `tool/result` (the session ended mid-call) is written without one."""
    if not path or not os.path.exists(path):
        return None
    header, events = _load(path)
    if not events:
        return None
    cwd = header.get("cwd") if isinstance(header.get("cwd"), str) else ""
    model, n = None, 0
    with open(out_path, "w", encoding="utf-8") as out:
        for ev in events:
            model = _model_of(ev, model)
            kind = ev.get("type")
            if kind == "user/message":
                text = _prompt_text(ev)
                if text.strip():
                    out.write(json.dumps({"type": "user", "message": {
                        "role": "user", "content": text}}) + "\n")
            elif kind == "assistant/message":
                d = _data(ev)
                msg = d.get("message") if isinstance(d.get("message"), dict) else {}
                blocks = []
                for b in msg.get("content") or []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text" and b.get("text"):
                        blocks.append({"type": "text", "text": str(b["text"])})
                    elif b.get("type") == "tool-call":
                        blocks.append({"type": "tool_use", "id": b.get("id"),
                                       "name": claude_tool_name(b.get("name")),
                                       "input": _arguments(b.get("arguments"))})
                        n += 1
                rec = {"id": msg.get("id"), "role": "assistant", "content": blocks,
                       "model": model}
                usage = _usage(d.get("usage"))
                if usage:
                    rec["usage"] = usage
                line = {"type": "assistant", "message": rec}
                if cwd:
                    line["cwd"] = cwd
                out.write(json.dumps(line) + "\n")
            elif kind == "tool/result":
                msg = _data(ev).get("message")
                if not isinstance(msg, dict):
                    continue
                out.write(json.dumps({"type": "user", "message": {"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": msg.get("toolCallId"),
                    "content": _blocks_text(msg.get("content")),
                    "is_error": bool(msg.get("isError"))}]}}) + "\n")
    return n


def dsh_session_usage(path):
    """Token breakdown of a dsh session: the sum of every step's usage, the step count, and
    the model most of them ran on. dsh records no cost of its own."""
    totals = {"input_tokens": 0, "cache_read_input_tokens": 0,
              "cache_creation_input_tokens": 0, "output_tokens": 0}
    requests, models, model = 0, {}, None
    for ev in _load(path)[1]:
        model = _model_of(ev, model)
        if ev.get("type") != "assistant/message":
            continue
        u = _usage(_data(ev).get("usage"))
        if not u:
            continue
        requests += 1
        for k in totals:
            totals[k] += u[k]
        if model:
            models[model] = models.get(model, 0) + 1
    totals.update(requests=requests, model=max(models, key=models.get) if models else None)
    return totals


def dsh_headless_reply(stdout):
    """(text, usage, session_id, reason) of a `dsh --profile headless --json` run: the final
    reply, the usage of every finished step in ledger fields (None when no step reported one),
    the session it created, and the turn's end reason (`completed`, `error`, ...; "" if the
    run ended before its `final` event)."""
    texts, final, usage, sid, reason = [], None, None, "", ""
    for line in (stdout or "").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        kind = ev.get("type")
        sid = sid or str(ev.get("sessionId") or "")
        if kind == "text" and isinstance(ev.get("text"), str):
            texts.append(ev["text"])
        elif kind == "status" and ev.get("phase") == "step_end":
            u = _usage(ev.get("usage"))
            if u:
                usage = usage or dict.fromkeys(u, 0)
                for k, v in u.items():
                    usage[k] += v
        elif kind == "final":
            final = ev.get("text") if isinstance(ev.get("text"), str) else None
            r = ev.get("reason")
            reason = str(r.get("kind") or "") if isinstance(r, dict) else ""
    text = final if final is not None and final.strip() else "".join(texts)
    return text.strip(), usage, sid, reason
