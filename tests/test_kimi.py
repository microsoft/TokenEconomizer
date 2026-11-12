"""Kimi Code 2.x: observe-only, and what must be learned before a reader is written.

Kimi Code keeps a session at `~/.kimi-code/sessions/wd_<name>_<hash>/session_<uuid>/agents/main/
wire.jsonl`, one JSON record per line with a `type` key. The only record types seen so far are
`metadata`, `config.update` and `tools.set_active_tools`; the record that carries a prompt, a tool
call or a tool result has not been seen, so no reader exists: a reader written from a guess would
count the wrong things and still pass its own tests. Its hooks are `[[hooks]]` entries in
`~/.kimi-code/config.toml`; the docs say only that stdout of an exit-0 hook "may be appended to
context", with no `additionalContext` field, and that has not been tried.

The two skipped tests below are the work that is missing. Each names the evidence it needs and
the check it will run once there is one (docs/KIMI.md has the steps for the person who has Kimi
Code installed). The other two tests pin what holds until then.

Proves TC-980-2 (TASK-980): an unknown payload is not taken for Kimi (nor for ZCode).
"""
import pytest

from tokeneconomizer import trace
from tokeneconomizer.plugin import hosts

WIRE = "/home/me/.kimi-code/sessions/wd_repo_1a2b/session_0f0f/agents/main/wire.jsonl"


def test_kimi_is_not_a_host_yet():
    assert "kimi" not in hosts.HOSTS
    assert not any(name.startswith("kimi") for name in dir(trace))


@pytest.mark.parametrize("payload", [
    {"session_id": "s", "hook_event_name": "UserPromptSubmit", "transcript_path": WIRE},
    {"transcript_path": WIRE.replace("/", "\\")},
    {},
])
def test_a_payload_that_names_a_kimi_session_log_is_claimed_by_no_other_agent(payload):
    """Until a Kimi adapter exists a Kimi payload is Claude-shaped at best, and must not be
    claimed by an adapter for another agent: not ZCode, not Grok, not Codex."""
    assert type(hosts.detect(payload, {}, hint="kimi")).__name__ == "ClaudeCode"
    assert type(hosts.detect(payload, {})).__name__ == "ClaudeCode"


def test_a_camel_case_payload_with_a_kimi_log_is_not_zcode():
    """The Grok adapter still takes any camelCase `hookEventName`, as it did before ZCode; what
    this pins is that ZCode's marker is the throwaway transcript, not the field names."""
    payload = {"sessionId": "s", "hookEventName": "PostToolUse", "transcriptPath": WIRE,
               "transcript_path": WIRE}
    assert not isinstance(hosts.detect(payload, {}), hosts.ZCode)


@pytest.mark.skip(reason="NEEDS EVIDENCE: a multi-turn wire.jsonl. Run two prompts in one Kimi "
                         "Code session, one of which makes the agent run a shell command and "
                         "read a file, then save the record `type` of every line of "
                         "~/.kimi-code/sessions/.../agents/main/wire.jsonl (types and key "
                         "names only, no text) as tests/fixtures/kimi_wire_types.json.")
def test_the_wire_log_names_a_prompt_a_tool_call_and_a_tool_result():
    """Once the sample exists: assert that the fixture holds a record type for each of the three,
    then write `trace/kimi.py` (prompt count, tool calls with exit codes, usage) and replace this
    test with the reader's tests, as `test_zcode.py` has."""
    raise AssertionError("no fixture")


@pytest.mark.skip(reason="NEEDS EVIDENCE: an injection test. In a scratch directory put a "
                         "[[hooks]] entry for UserPromptSubmit in ~/.kimi-code/config.toml whose "
                         "command prints a made-up sentence and exits 0, ask Kimi Code 'repeat "
                         "the sentence the hook printed', and record whether it can.")
def test_stdout_of_an_exit_zero_hook_reaches_the_model():
    """If it does, the Kimi adapter returns context as plain stdout from SessionStart and
    UserPromptSubmit and sets `prompt_context`; if it does not, Kimi stays observe-only with the
    loop guard's judge and the micro-skills block off (nothing could be injected)."""
    raise AssertionError("no result")
