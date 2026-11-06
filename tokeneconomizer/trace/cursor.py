"""Cursor Agent hook events, normalized to the Claude-shaped trace the mechanisms read.

Cursor's JSONL transcript format is private and omits tool results. Its documented hooks provide
the information needed for micro-skills and loop detection, so the host adapter records those
events locally and this reader pairs each tool request with its observed result.
"""
import json
import os

from .jsonl import lines as _lines


def _response_text(response):
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        return "\n".join(str(response[k]) for k in
                         ("content", "stdout", "stderr", "output", "error")
                         if response.get(k) is not None)
    return str(response or "")


def _exit_code(response):
    if not isinstance(response, dict):
        return None
    for key in ("exit_code", "exitCode", "returncode", "status_code"):
        code = response.get(key)
        if isinstance(code, int) and not isinstance(code, bool):
            return code
    return None


def cursor_events_to_claude(path, out_path):
    """Write the normalized trajectory. Return the number of records, or None if unreadable."""
    try:
        lines = _lines(path)
    except OSError:
        return None
    if not lines:
        return None
    out = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        name = event.get("hook_event_name")
        if name == "UserPromptSubmit":
            prompt = event.get("prompt")
            if isinstance(prompt, str) and prompt.strip():
                out.append({"type": "user", "message": {"content": [
                    {"type": "text", "text": prompt}]}})
        elif name in ("PostToolUse", "PostToolUseFailure"):
            tool_id = str(event.get("tool_use_id") or event.get("generation_id") or len(out))
            tool_name = str(event.get("tool_name") or "unknown")
            tool_input = event.get("tool_input")
            if not isinstance(tool_input, dict):
                tool_input = {}
            if tool_name.lower() == "shell":
                tool_name = "Bash"
            out.append({"type": "assistant", "cwd": event.get("cwd"), "message": {
                "content": [{"type": "tool_use", "id": tool_id, "name": tool_name,
                             "input": tool_input}]}})
            response = event.get("tool_response")
            if name == "PostToolUseFailure":
                response = {"content": event.get("error") or "tool failed", "isError": True}
            result = _response_text(response)
            code = _exit_code(response)
            failed = name == "PostToolUseFailure" or (code is not None and code != 0)
            out.append({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": tool_id, "content": result,
                 "is_error": failed}]}})
    try:
        with open(out_path, "w", encoding="utf-8") as fh:
            for item in out:
                fh.write(json.dumps(item, ensure_ascii=False) + "\n")
    except OSError:
        return None
    return len(out)


def cursor_prompt_count(path):
    """Count the session's prompts.

    The adapter's own spool counts the `beforeSubmitPrompt` events it recorded. Headless
    `cursor-agent -p` never delivers that event, so with none recorded the count falls back to
    the user rows of Cursor's own transcript, whose path the spool carries as `agent_transcript`.
    Only each row's role is read; no prompt text is kept.
    """
    count, agent = 0, None
    for line in _lines(path):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        count += event.get("hook_event_name") == "UserPromptSubmit"
        if isinstance(event.get("agent_transcript"), str):
            agent = event["agent_transcript"]
    if count or not agent or os.path.realpath(agent) == os.path.realpath(path):
        return count
    for line in _lines(agent):
        if '"role"' not in line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        count += isinstance(row, dict) and row.get("role") == "user"
    return count
