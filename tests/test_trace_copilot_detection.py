"""Copilot metadata must not make its event stream look like a Claude transcript."""
import json

from tokeneconomizer.trace import call_cwds, load_trajectory, tool_usage


def test_copilot_tool_use_finish_reason_preserves_calls(tmp_path):
    path = tmp_path / "events.jsonl"
    events = [
        {"type": "session.start", "data": {"context": {"cwd": str(tmp_path)}}},
        {"type": "assistant.turn_end", "data": {"finishReason": "tool_use"}},
        {"type": "tool.execution_start", "data": {
            "toolCallId": "read-1", "toolName": "view", "arguments": {"path": "fixture.txt"}}},
        {"type": "tool.execution_complete", "data": {
            "toolCallId": "read-1", "success": True, "result": {"content": "amber"}}},
        {"type": "tool.execution_start", "data": {
            "toolCallId": "read-2", "toolName": "view", "arguments": {"path": "missing.txt"}}},
        {"type": "tool.execution_complete", "data": {
            "toolCallId": "read-2", "success": False, "result": {"content": "Path does not exist"}}},
    ]
    path.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
    assert load_trajectory(path) == [
        ("view", {"path": "fixture.txt"}, "amber", True),
        ("view", {"path": "missing.txt"}, "Path does not exist", False),
    ]
    assert tool_usage(path) == {"view": 2}
    assert call_cwds(path) == [str(tmp_path), str(tmp_path)]
