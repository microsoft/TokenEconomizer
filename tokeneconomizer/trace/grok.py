"""Grok Build CLI (`grok`) session logs, normalized to the Claude-shaped trace the mechanisms read.

Verified on grok 1.0.70. A session is a directory `~/.grok/sessions/<url-encoded cwd>/<id>/`,
and its `updates.jsonl` is an ACP JSON-RPC log, one `{"method", "params": {"update": ...}}` per
line (method `session/update` or `_x.ai/session/update`). The updates read here:

  * `user_message_chunk` -- a prompt's text (`content.text`; `_meta.promptIndex` numbers the
    prompt); a prompt can arrive in several chunks;
  * `tool_call` -- `{toolCallId, title, rawInput}`, `title` being grok's own tool name
    (`run_terminal_command`, `read_file`, ...);
  * `tool_call_update` -- `{toolCallId, status, rawOutput}`. A first update carries only the
    input again; the one with `status` (`completed` / `failed`) carries the result.

A tool result, here and in a hook's `tool_response`, is a TAGGED object. A shell result keeps its
output twice, as a list of byte values (`output`) and as the text the model read
(`output_for_prompt`, headed by an `exit: N` line); reading `output` as text gives numbers.

`usage.json` sits beside `updates.jsonl`. Its `inputTokens` INCLUDES the cached reads, so the
fresh input is the difference; `outputTokens` includes the reasoning tokens.
"""
import json
import os
import re

from .calls import strip_rtk_prefix
from .jsonl import records as _records

# grok's tool names, as the mechanisms know them (grok's own alias table maps the same pairs).
_NAMES = {"run_terminal_command": "Bash", "run_terminal_cmd": "Bash", "read_file": "Read",
          "search_replace": "Edit", "grep": "Grep", "list_dir": "Glob",
          "web_fetch": "WebFetch", "web_search": "WebSearch", "spawn_subagent": "Task"}
_EXIT_LINE = re.compile(r"\A\s*exit: (-?\d+)[ \t]*(?:\r?\n|\Z)")
TIMEOUT_CODE = 124          # the conventional status of a command that was killed at its limit


def grok_tool(name, args):
    """(name, args) of a grok tool call in Claude Code's vocabulary: the file and command tools
    renamed with the arguments the mechanisms read; any other tool as it is."""
    args = args if isinstance(args, dict) else {}
    mapped = _NAMES.get(name or "")
    if mapped == "Bash":
        return mapped, {"command": strip_rtk_prefix(str(args.get("command") or ""))}
    if mapped == "Read":
        out = {"file_path": args.get("target_file") or args.get("file_path") or ""}
        out.update({k: args[k] for k in ("offset", "limit") if args.get(k) is not None})
        return mapped, out
    if mapped == "Edit":            # the old and new strings are the file's content, not a signal
        return mapped, {"file_path": args.get("file_path") or args.get("target_file") or ""}
    if mapped == "Grep":
        out = {"pattern": args.get("pattern") or "", "path": args.get("path") or ""}
        if args.get("glob"):
            out["glob"] = args["glob"]
        return mapped, out
    if mapped == "Glob":
        return mapped, {"path": args.get("target_directory") or args.get("path") or ""}
    return mapped or name or "", args


def _bytes_text(value):
    """A list of byte values (grok's raw shell output) as text; a string as it is."""
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(b, int) and 0 <= b < 256 for b in value):
        return bytes(value).decode("utf-8", errors="replace")
    return ""


def grok_result(resp):
    """(text, exit_code_or_None) of a tagged tool result; None for any other value (a result
    past the size limit arrives as a plain string, a failure as an error text), which the caller
    reads as it reads every agent's."""
    kind = resp.get("type") if isinstance(resp, dict) else None
    if not isinstance(kind, str):
        return None
    code = resp.get("exit_code") if isinstance(resp.get("exit_code"), int) else None
    if kind == "Bash":
        text = resp.get("output_for_prompt")
        if not isinstance(text, str):
            text = _bytes_text(resp.get("output"))
        m = _EXIT_LINE.match(text)
        if m:
            text = text[m.end():]
            code = code if code is not None else int(m.group(1))
        if resp.get("timed_out") and not code:
            code = TIMEOUT_CODE
        return text, code
    if kind == "GrepSearch":        # grep's own status: 1 is "no match", which is an answer
        text = "\n".join(t for t in (_bytes_text(resp.get("stdout")),
                                     _bytes_text(resp.get("stderr"))) if t)
        return text, 0 if code in (0, 1) else code
    for body in resp.values():      # ReadFile {FileContent}, ListDir {Content}, SearchReplace ...
        if isinstance(body, dict):
            for key in ("raw_output", "tool_output_for_prompt", "content"):
                if isinstance(body.get(key), str):
                    return body[key], code
    return json.dumps(resp, sort_keys=True), code


def _content_text(content):
    """The text of an update's `content` list ([{type: "content", content: {text}}])."""
    out = []
    for c in content if isinstance(content, list) else ():
        inner = c.get("content") if isinstance(c, dict) else None
        if isinstance(inner, dict) and isinstance(inner.get("text"), str):
            out.append(inner["text"])
    return "\n".join(out)


def _scan(path):
    """(events, results): the prompts and tool calls of an `updates.jsonl` in order, as
    ("user", text) and ("call", id), the calls' details by id, and each result by id."""
    events, calls, results = [], {}, {}
    prompt, prompt_index = [], None

    def flush():
        nonlocal prompt
        if prompt:
            events.append(("user", "".join(prompt)))
        prompt = []

    for rec in _records(path):
        u = (rec.get("params") or {}).get("update")
        kind = u.get("sessionUpdate") if isinstance(u, dict) else None
        if kind == "user_message_chunk":
            index = (u.get("_meta") or {}).get("promptIndex")
            if prompt and index != prompt_index:
                flush()
            prompt_index = index
            content = u.get("content")
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                prompt.append(content["text"])
            continue
        if kind in ("tool_call", "tool_call_update", "agent_message_chunk", "turn_completed"):
            flush()
        tid = u.get("toolCallId") if isinstance(u, dict) else None
        if not tid:
            continue
        if kind == "tool_call":
            calls[tid] = {"name": str(u.get("title") or ""),
                          "args": dict(u["rawInput"]) if isinstance(u.get("rawInput"), dict) else {}}
            events.append(("call", tid))
        elif kind == "tool_call_update":
            if isinstance(u.get("rawInput"), dict) and tid in calls:
                calls[tid]["args"].update({k: v for k, v in u["rawInput"].items()
                                           if k != "variant"})
            if u.get("status"):
                results[tid] = u
    flush()
    return events, calls, results


def grok_session_to_claude(path, out_path):
    """Write a grok session as Claude Code transcript lines.

    Returns the number of tool calls written, or None when the file holds neither a prompt nor
    a call. A call with no result yet is written without one, as a session that ended mid-call."""
    events, calls, results = _scan(path)
    if not events:
        return None
    n, out = 0, []
    for ev in events:
        if ev[0] == "user":
            out.append({"type": "user", "message": {"role": "user", "content": ev[1]}})
            continue
        info, upd = calls[ev[1]], results.get(ev[1])
        name, args = grok_tool(info["name"], info["args"])
        n += 1
        text, code = "", None
        if upd:
            raw = upd.get("rawOutput")
            parsed = grok_result(raw)
            text, code = parsed if parsed else (
                raw if isinstance(raw, str) else _content_text(upd.get("content")), None)
        cwd = upd and isinstance(upd.get("rawOutput"), dict) and upd["rawOutput"].get("current_dir")
        out.append(dict({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": ev[1], "name": name, "input": args}]}},
            **({"cwd": cwd} if isinstance(cwd, str) and cwd else {})))
        if upd:
            failed = upd.get("status") == "failed" or (code is not None and code != 0)
            out.append({"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": ev[1], "content": text,
                 "is_error": failed}]}})
    with open(out_path, "w", encoding="utf-8") as fh:
        for item in out:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")
    return n


def grok_prompt_count(path):
    """How many prompts the session ran: groups of `user_message_chunk` updates that hold text.
    Counted from the prompts themselves, not `turn_completed`, which an interrupted turn lacks."""
    return sum(1 for kind, *rest in _scan(path)[0] if kind == "user" and rest[0].strip())


def grok_session_usage(path):
    """Token breakdown of a session from the `usage.json` beside its `updates.jsonl` ({} when it
    is not there). Input is fresh input: grok's `inputTokens` counts the cached reads too."""
    try:
        with open(os.path.join(os.path.dirname(path or ""), "usage.json"),
                  encoding="utf-8") as fh:
            s = (json.load(fh) or {}).get("session")
    except (OSError, ValueError, AttributeError):
        return {}
    if not isinstance(s, dict) or s.get("inputTokens") is None:
        return {}
    cached = int(s.get("cachedReadTokens") or 0)
    return {"input_tokens": max(0, int(s["inputTokens"]) - cached),
            "cache_read_input_tokens": cached,
            "cache_creation_input_tokens": int(s.get("cacheCreationTokens") or 0),
            "output_tokens": int(s.get("outputTokens") or 0),
            "requests": int(s.get("modelCalls") or 0),
            "model": s.get("primaryModelId"),
            "cost_usd_vendor": round(int(s.get("costUsdTicks") or 0) / 1e10, 6)}


def grok_json_reply(stdout):
    """(text, usage) of a `grok -p --output-format json` run: the reply and its usage in ledger
    fields. Its `input_tokens` already excludes the cache. `usage` is None when it names none."""
    data = None
    for candidate in ((stdout or "").strip(), *reversed((stdout or "").strip().splitlines())):
        try:
            data = json.loads(candidate)
            break
        except ValueError:
            continue
    if not isinstance(data, dict):
        return "", None
    text = data.get("text") if isinstance(data.get("text"), str) else ""
    u = data.get("usage")
    if not isinstance(u, dict) or u.get("input_tokens") is None:
        return text.strip(), None
    models = data.get("modelUsage") if isinstance(data.get("modelUsage"), dict) else {}
    return text.strip(), {
        "input_tokens": int(u["input_tokens"] or 0),
        "cache_read_input_tokens": int(u.get("cache_read_input_tokens") or 0),
        "cache_creation_input_tokens": int(u.get("cache_creation_input_tokens") or 0),
        "output_tokens": int(u.get("output_tokens") or 0),
        "model": next(iter(models), None)}
