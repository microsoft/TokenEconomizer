"""Kimi Code 2.6's wire log, verified with a scripted loopback model.

A prompt is recorded in turn.prompt. Each model step emits usage.record with
usageScope "turn". Tool calls and their results are loop events, paired by the
agent and tool call ID. The Claude projection keeps the tool trajectory and
replaces prompt text with a marker.
"""
import json
from collections import Counter

from .jsonl import records

_USAGE_FIELDS = {
    "inputOther": "input_tokens",
    "inputCacheRead": "cache_read_input_tokens",
    "inputCacheCreation": "cache_creation_input_tokens",
    "output": "output_tokens",
}


def _dict(value):
    return value if isinstance(value, dict) else {}


def _string(value):
    return value if isinstance(value, str) else ""


def kimi_prompt_count(path):
    """Count submitted prompts in the wire log."""
    return sum(row.get("type") == "turn.prompt" for row in records(path))


def kimi_session_usage(path):
    """Sum model steps in the shared ledger's fields."""
    usage = dict.fromkeys(_USAGE_FIELDS.values(), 0)
    models = Counter()
    requests = 0
    for row in records(path):
        if row.get("type") != "usage.record" or row.get("usageScope") != "turn":
            continue
        step = _dict(row.get("usage"))
        counts = {
            target: step[source] for source, target in _USAGE_FIELDS.items()
            if type(step.get(source)) is int and step[source] >= 0
        }
        if not counts:
            continue
        requests += 1
        for name, value in counts.items():
            usage[name] += value
        model = _string(row.get("model"))
        if model:
            models[model] += 1
    return dict(usage, requests=requests, model=models.most_common(1)[0][0] if models else None)


def _projection(rows):
    pending = {}
    next_id = 0
    for row in rows:
        if row.get("type") == "turn.prompt":
            yield {"type": "user", "message": {"role": "user", "content": "[Kimi prompt]"}}
            continue
        if row.get("type") != "context.append_loop_event":
            continue
        event = _dict(row.get("event"))
        call_id = _string(event.get("toolCallId"))
        if not call_id:
            continue
        key = (_string(row.get("agentId")), call_id)
        match event.get("type"):
            case "tool.call":
                name = _string(event.get("name"))
                if not name:
                    continue
                next_id += 1
                uid = f"kimi-{next_id}"
                pending[key] = uid
                args = _dict(event.get("args")).copy()
                if name == "Read" and "path" in args:
                    args["file_path"] = args.pop("path")
                output = {"type": "assistant", "message": {"role": "assistant", "content": [{
                    "type": "tool_use", "id": uid, "name": name, "input": args,
                }]}}
                cwd = _string(_dict(event.get("display")).get("cwd"))
                if cwd:
                    output["cwd"] = cwd
                yield output
            case "tool.result":
                uid = pending.pop(key, None)
                if uid is None:
                    continue
                result = _dict(event.get("result"))
                yield {"type": "user", "message": {"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": uid,
                    "content": _string(result.get("output")),
                    "is_error": result.get("isError") is True,
                }]}}


def kimi_session_to_claude(path, out_path):
    """Project the wire log for load_trajectory, or return None for unrelated input."""
    projection = iter(_projection(records(path)))
    first = next(projection, None)
    if first is None:
        return None
    count = int(first["type"] == "assistant")
    with open(out_path, "w", encoding="utf-8") as out:
        out.write(json.dumps(first) + "\n")
        for row in projection:
            out.write(json.dumps(row) + "\n")
            count += row["type"] == "assistant"
    return count
