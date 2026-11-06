"""The GitHub Copilot CLI's session log: tool calls, usage, prompt-turns, MCP servers.

Copilot writes each session to `<COPILOT_HOME>/session-state/<id>/events.jsonl`: the same
`tool.execution_start` / `tool.execution_complete` event stream `copilot --output-format json`
prints, so a session log and a harness stream read alike. Usage is in the `session.shutdown`
event, per model:

    modelMetrics[model].usage = {inputTokens, outputTokens, cacheReadTokens,
                                 cacheWriteTokens, reasoningTokens}

`inputTokens` is the GRAND input total: against a scripted endpoint reporting prompt_tokens=1000
with cached_tokens=600 on each of 4 requests, it recorded inputTokens=4000 and
cacheReadTokens=2400 (1.0.83). Fresh input is therefore input less both cache fields.
`outputTokens` is the provider's completion count, which already includes reasoning. A session
killed before it shut down has no usage; it is reported as unpriced.
"""
import json
import os
import re

from .jsonl import records as _copilot_events

_COPILOT_EXIT_RE = re.compile(r"<shellId: [^>]*?completed with exit code (-?\d+)>")


def copilot_event_calls(lines):
    """[(name, args, result_text, success)] from Copilot `tool.execution_*` events, a start
    paired with its completion by call id."""
    out, pend = [], {}
    for line in lines:
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if not isinstance(ev, dict):
            continue
        t = ev.get("type")
        d = ev.get("data", {}) if isinstance(ev.get("data"), dict) else {}
        if t == "tool.execution_start":
            pend[d.get("toolCallId")] = {"name": d.get("toolName"),
                                         "args": d.get("arguments", {})}
        elif t == "tool.execution_complete":
            info = pend.get(d.get("toolCallId"), {"name": "?", "args": {}})
            res = d.get("result") or {}
            content = res.get("content") if isinstance(res, dict) else ""
            out.append((info["name"], info["args"],
                        content if isinstance(content, str) else "",
                        d.get("success")))
    return out


def copilot_session_cwd(lines):
    """The directory a Copilot session started in (`session.start` -> `context.cwd`), or None."""
    for line in lines:
        if '"session.start"' not in line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        d = ev.get("data") or {}
        cwd = (d.get("context") or {}).get("cwd")
        return cwd if isinstance(cwd, str) else None
    return None


def copilot_session_meta(path):
    """{cwd, model, cli_version, session_id} from a Copilot session's `session.start`."""
    for ev in _copilot_events(path):
        if ev.get("type") == "session.start":
            d = ev.get("data") or {}
            return {"cwd": (d.get("context") or {}).get("cwd"), "model": d.get("selectedModel"),
                    "cli_version": d.get("copilotVersion"), "session_id": d.get("sessionId")}
    return {}


def copilot_session_usage(path):
    """Token breakdown of a Copilot session from its `session.shutdown` events."""
    totals = {"input_tokens": 0, "cache_read_input_tokens": 0,
              "cache_creation_input_tokens": 0, "output_tokens": 0}
    requests, models, seen, tool_defs = 0, {}, False, None
    for ev in _copilot_events(path):
        if ev.get("type") != "session.shutdown":
            continue
        d = ev.get("data") or {}
        for model, m in (d.get("modelMetrics") or {}).items():
            u = (m or {}).get("usage") or {}
            inp = int(u.get("inputTokens") or 0)
            cr, cw = int(u.get("cacheReadTokens") or 0), int(u.get("cacheWriteTokens") or 0)
            totals["input_tokens"] += max(0, inp - cr - cw)
            totals["cache_read_input_tokens"] += cr
            totals["cache_creation_input_tokens"] += cw
            totals["output_tokens"] += int(u.get("outputTokens") or 0)
            n = int(((m or {}).get("requests") or {}).get("count") or 0)
            requests += n
            models[model] = models.get(model, 0) + n
            seen = True
        if d.get("toolDefinitionsTokens") is not None:
            tool_defs = int(d["toolDefinitionsTokens"])
    if not seen:
        out = {k: None for k in totals}
        out.update(requests=0, model=copilot_session_meta(path).get("model"))
        return out
    totals.update(requests=requests, model=max(models, key=models.get) if models else None,
                  tool_definitions_tokens=tool_defs)
    return totals


def copilot_turns(path):
    """Prompt-turns of a Copilot session: [[(name, args, result, exit_code)]], one list per
    user message, tool calls paired start-to-complete by call id."""
    turns, cur, pend = [], None, {}
    for ev in _copilot_events(path):
        t, d = ev.get("type"), ev.get("data") or {}
        if t == "user.message":
            if cur:
                turns.append(cur)
            cur, pend = [], {}
        elif t == "tool.execution_start" and cur is not None:
            pend[d.get("toolCallId")] = (str(d.get("toolName") or ""),
                                         d.get("arguments") if isinstance(d.get("arguments"),
                                                                          dict) else {})
        elif t == "tool.execution_complete" and cur is not None:
            name, args = pend.pop(d.get("toolCallId"), (None, None))
            if name is None:
                continue
            res = d.get("result") or {}
            text = res.get("content") if isinstance(res, dict) else str(res)
            text = text if isinstance(text, str) else json.dumps(text)
            if not d.get("success", True) and isinstance(d.get("error"), (str, dict)):
                text = (text or "") + "\n" + json.dumps(d.get("error"))
            m = _COPILOT_EXIT_RE.search((text or "")[-400:])
            cur.append((name, args, text or "", int(m.group(1)) if m else None))
    if cur:
        turns.append(cur)
    return turns


def copilot_prompt_count(path):
    """How many prompts a Copilot session ran: one `user.message` event each."""
    return sum(1 for ev in _copilot_events(path) if ev.get("type") == "user.message")



def copilot_mcp_servers(env=None):
    """MCP server names a Copilot session can load: the built-in GitHub server plus every
    server in the user's `mcp-config.json`. Tool names are `<server>-<tool>`."""
    env = os.environ if env is None else env
    home = env.get("COPILOT_HOME") or os.path.join(os.path.expanduser("~"), ".copilot")
    names = {"github-mcp-server"}
    try:
        with open(os.path.join(home, "mcp-config.json"), encoding="utf-8") as fh:
            names.update((json.load(fh).get("mcpServers") or {}).keys())
    except (OSError, ValueError, AttributeError):
        pass
    return names


def mcp_server_of(tool, servers):
    """The server a Copilot tool name belongs to (longest matching `<server>-` prefix)."""
    best = None
    for s in servers:
        if tool.startswith(s + "-") and (best is None or len(s) > len(best)):
            best = s
    return best
