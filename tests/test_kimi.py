"""Keep unrelated host payloads out of Kimi Code's adapter."""
import pytest

from tokeneconomizer.plugin import hosts

WIRE = "/home/me/.kimi-code/sessions/wd_repo_1a2b/session_0f0f/agents/main/wire.jsonl"


@pytest.mark.parametrize("payload", [
    {"session_id": "s", "hook_event_name": "UserPromptSubmit", "transcript_path": WIRE},
    {"transcript_path": WIRE.replace("/", "\\")},
    {},
])
def test_an_unknown_hook_is_not_kimi(payload):
    assert type(hosts.detect(payload, {})).__name__ == "ClaudeCode"


def test_a_camel_case_payload_with_a_kimi_log_is_not_zcode():
    payload = {"sessionId": "s", "hookEventName": "PostToolUse", "transcriptPath": WIRE,
               "transcript_path": WIRE}
    assert not isinstance(hosts.detect(payload, {}), (hosts.ZCode, hosts.Kimi))
