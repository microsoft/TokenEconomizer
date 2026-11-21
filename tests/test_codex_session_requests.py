"""A Codex session's requests are its model calls, not its `token_count` events.

Codex writes a `token_count` event after each model response, and also re-sends the totals it
already has when nothing new was used: a rate-limit snapshot during compaction
(`update_rate_limits`), the local estimate after it (`recompute_token_usage`, which replaces only
`last_token_usage`), and a usage-limit error. Those events carry an unchanged
`total_token_usage`; a model call always adds to it."""
import json

from tokeneconomizer import trace


def _total(inp, cached, out):
    return {"input_tokens": inp, "cached_input_tokens": cached, "cache_write_input_tokens": 0,
            "output_tokens": out, "reasoning_output_tokens": 0, "total_tokens": inp + out}


def _token_count(total, last_total_tokens):
    return {"type": "event_msg", "payload": {"type": "token_count", "info": {
        "total_token_usage": total,
        "last_token_usage": {"input_tokens": 0, "cached_input_tokens": 0,
                             "output_tokens": 0, "total_tokens": last_total_tokens},
        "model_context_window": 272000}, "rate_limits": {"primary": {"used_percent": 4.0}}}}


def _rollout(path, events):
    recs = [{"type": "session_meta", "payload": {"id": "thread-1", "cwd": "/work/r",
                                                 "cli_version": "0.171.0"}},
            {"type": "turn_context", "payload": {"turn_id": "t1", "model": "gpt-6.1-sol"}},
            *events]
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    return str(path)


def test_a_resent_total_is_not_another_request(tmp_path):
    first, second, third = _total(1000, 600, 40), _total(3000, 2000, 90), _total(5200, 3900, 130)
    p = _rollout(tmp_path / "rollout.jsonl", [
        _token_count(first, 1040),
        _token_count(second, 2090),
        _token_count(second, 2090),      # compaction's rate-limit snapshot: nothing new used
        _token_count(second, 1800),      # the local estimate after compaction: last usage only
        _token_count(third, 2200),
        {"type": "event_msg", "payload": {"type": "token_count", "info": None,
                                          "rate_limits": {"primary": {"used_percent": 5.0}}}},
    ])
    u = trace.codex_session_usage(p)
    assert u["requests"] == 3
    # the breakdown is still the last cumulative total
    assert (u["input_tokens"], u["cache_read_input_tokens"], u["output_tokens"]) == (
        5200 - 3900, 3900, 130)


def test_each_call_that_adds_to_the_total_is_a_request(tmp_path):
    totals = [_total(1000 * k, 500 * k, 10 * k) for k in range(1, 5)]
    p = _rollout(tmp_path / "rollout.jsonl", [_token_count(t, 0) for t in totals])
    assert trace.codex_session_usage(p)["requests"] == 4
