"""Hermes-native integration contract for TokenEconomizer."""
import json


def test_hermes_host_exposes_context_and_tool_calls():
    from tokeneconomizer.plugin import hosts

    host = hosts.HOSTS["hermes"]()
    assert host.name == "hermes"
    assert host.context("pre_llm_call", "learned fact") == {"context": "learned fact"}
    assert host.tool_call({"tool_name": "terminal", "args": {"command": "pytest"},
                           "result": "1 passed"}) == ("terminal", {"command": "pytest"},
                                                          "1 passed", None)


def test_hermes_plugin_registers_native_lifecycle_hooks():
    from tokeneconomizer.plugin.hermes import register

    class Context:
        def __init__(self):
            self.hooks = {}

        def register_hook(self, name, callback):
            self.hooks[name] = callback

    ctx = Context()
    register(ctx)
    assert {"pre_llm_call", "pre_tool_call", "post_tool_call", "pre_api_request",
            "post_api_request", "on_session_end"} <= set(ctx.hooks)


def test_hermes_usage_normalization_splits_cached_prompt_tokens():
    from tokeneconomizer.plugin.hermes import normalize_usage

    usage = normalize_usage({"prompt_tokens": 120, "completion_tokens": 20,
                             "prompt_tokens_details": {"cached_tokens": 80}})
    assert usage == {"input_tokens": 40, "cache_read_input_tokens": 80,
                     "cache_creation_input_tokens": 0, "output_tokens": 20}


def test_hermes_session_trace_persists_across_prompts(tmp_path):
    from tokeneconomizer import trace
    from tokeneconomizer.plugin.hermes import HermesHooks

    hooks = HermesHooks({"TOKENECONOMIZER_HOME": str(tmp_path / "te"),
                        "TE_HERMES_CWD": str(tmp_path)})
    events = []
    hooks._dispatch = lambda state, event, **fields: events.append(event) or {}
    hooks.pre_llm_call(session_id="session-1", user_message="private prompt 1", platform="cli",
                        turn_id="turn-1")
    first_path = hooks.sessions["session-1"]["transcript"]
    hooks.pre_api_request(task_id="task-1", session_id="session-1", turn_id="turn-1")
    args = {"command": "pytest -q"}
    hooks.pre_tool_call(tool_name="terminal", args=args, task_id="task-1")
    hooks.post_tool_call(tool_name="terminal", args=args, result="1 passed", task_id="task-1")

    hooks.pre_llm_call(session_id="session-1", user_message="same turn tool follow-up", platform="cli",
                        turn_id="turn-1")
    hooks.pre_api_request(task_id="task-1-followup", session_id="session-1", turn_id="turn-1")
    hooks.post_api_request(task_id="task-1-followup", session_id="session-1", api_request_id="req-1",
                           model="test-model", usage={"prompt_tokens": 120,
                                                         "completion_tokens": 20,
                                                         "prompt_tokens_details": {
                                                             "cached_tokens": 80}})
    hooks.pre_llm_call(session_id="session-1", user_message="private prompt 2", platform="cli",
                        turn_id="turn-2")

    state = hooks.sessions["session-1"]
    usage = trace.claude_session_usage(first_path, include_subagents=False)
    assert state["transcript"] == first_path
    assert events.count("SessionStart") == 1
    assert events.count("UserPromptSubmit") == 2
    assert trace.claude_prompt_count(first_path) == 2
    assert trace.load_trajectory(first_path) == [("terminal", args, "1 passed", True)]
    assert usage["input_tokens"] == 40 and usage["cache_read_input_tokens"] == 80
    with open(first_path, encoding="utf-8") as stream:
        transcript = stream.read()
    assert "private prompt 1" not in transcript and "private prompt 2" not in transcript
