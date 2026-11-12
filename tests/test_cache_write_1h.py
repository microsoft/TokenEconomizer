"""A cache write is billed by its lifetime: Anthropic charges 1.25x input for the 5-minute cache
and 2x for the 1-hour one, and a message's usage says how its cache writes split between them
(`cache_creation: {ephemeral_5m_input_tokens, ephemeral_1h_input_tokens}`). Claude Code on a
subscription writes to the 1-hour cache, so pricing every cache write at 1.25x understates such a
session. A cache hit is billed at 0.1x input except on a few models (0.05x on Opus 5.5 and
Sonnet 5.5, 0.025x on Fable and Mythos 5.1 and 5.2), and hits are most of a session's tokens."""
import json

import pytest

from tokeneconomizer import pricing, trace
from tokeneconomizer.plugin import accounting


def _row(cache_write=1000, one_hour=None, model="claude-opus-4-8"):
    row = {"input_tokens": 100, "cache_read_input_tokens": 10_000,
           "cache_creation_input_tokens": cache_write, "output_tokens": 10, "model": model}
    if one_hour is not None:
        row["cache_creation_1h_input_tokens"] = one_hour
    return row


BASE = 100 * 1.0 + 10_000 * 0.10 + 10 * 5.0          # everything but the cache writes


# ---- pricing ---------------------------------------------------------------------------------
def test_one_hour_cache_writes_are_priced_at_twice_input():
    assert pricing.effective_tokens(_row(1000, one_hour=1000)) == BASE + 1000 * 2.0


def test_a_mixed_row_prices_each_share_at_its_own_rate():
    assert pricing.effective_tokens(_row(1000, one_hour=400)) == BASE + 600 * 1.25 + 400 * 2.0


def test_a_row_without_the_split_is_priced_as_before():
    assert pricing.effective_tokens(_row(1000)) == BASE + 1000 * 1.25
    assert pricing.effective_tokens(_row(1000, one_hour=0)) == BASE + 1000 * 1.25


def test_a_claude_id_outside_the_table_gets_the_one_hour_rate_too():
    row = _row(1000, one_hour=1000, model="claude-opus-4-5")
    assert pricing.effective_tokens(row) == BASE + 1000 * 2.0


def test_the_one_hour_share_is_never_more_than_the_cache_writes():
    assert pricing.cache_write_1h_tokens(_row(1000, one_hour=5000)) == 1000
    assert pricing.cache_write_1h_tokens(_row(1000, one_hour=-5)) == 0
    assert pricing.cache_write_1h_tokens(_row(1000, one_hour="x")) == 0
    assert pricing.cache_write_1h_tokens(None) == 0


def test_a_weight_table_without_a_one_hour_rate_uses_its_cache_write_rate():
    # gpt-5.6-sol has no 1-hour entry: its cache writes stay at 1.25x whatever the row says.
    row = _row(1000, one_hour=1000, model="gpt-5.6-sol")
    assert pricing.effective_tokens(row) == 100 + 10_000 * 0.10 + 1000 * 1.25 + 10 * 6.0
    explicit = dict(input=1.0, cache_read=0.0, cache_write=1.0, output=1.0)
    assert pricing.effective_tokens(_row(1000, one_hour=1000), weights=explicit) == 100 + 1000 + 10


def test_the_split_is_read_from_an_anthropic_usage_object():
    usage = {"cache_creation_input_tokens": 248,
             "cache_creation": {"ephemeral_5m_input_tokens": 148, "ephemeral_1h_input_tokens": 100}}
    assert pricing.cache_write_1h_tokens(usage) == 100
    assert pricing.cache_write_1h_tokens({"cache_creation_input_tokens": 248,
                                          "cache_creation": "not an object"}) == 0


# ---- Claude Code transcripts -----------------------------------------------------------------
def _message(mid, cache_write, split=None, blocks=2):
    usage = {"input_tokens": 10, "cache_read_input_tokens": 1000,
             "cache_creation_input_tokens": cache_write, "output_tokens": 50}
    if split is not None:
        usage["cache_creation"] = split
    return [{"type": "assistant", "message": {
        "id": mid, "model": "claude-opus-4-8", "role": "assistant", "usage": usage,
        "content": [{"type": "text", "text": f"block {b}"}]}} for b in range(blocks)]


def _transcript(path, entries):
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return str(path)


def test_a_claude_session_reports_its_one_hour_cache_writes(tmp_path):
    p = _transcript(tmp_path / "s.jsonl", [
        *_message("m1", 300, {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 300}),
        *_message("m2", 200, {"ephemeral_5m_input_tokens": 150, "ephemeral_1h_input_tokens": 50}),
        *_message("m3", 100),                                   # an older entry: no split
    ])
    u = trace.claude_session_usage(p)
    assert u["cache_creation_input_tokens"] == 600              # each message once, as before
    assert u["cache_creation_1h_input_tokens"] == 350


def test_a_split_on_a_message_that_reports_no_cache_writes_adds_nothing(tmp_path):
    # Seen in real transcripts: every count 0, yet a `cache_creation` split that is not.
    p = _transcript(tmp_path / "s.jsonl", [
        *_message("m1", 0, {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 2948}),
        *_message("m2", 80, {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": "x"}),
    ])
    u = trace.claude_session_usage(p)
    assert u["cache_creation_input_tokens"] == 80
    assert u["cache_creation_1h_input_tokens"] == 0


# ---- the ledger ------------------------------------------------------------------------------
def test_a_session_is_priced_with_its_one_hour_cache_writes(tmp_path):
    p = _transcript(tmp_path / "s.jsonl", _message(
        "m1", 1000, {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 1000}))
    eff, priced_by = accounting.effective(trace.claude_session_usage(p))
    assert priced_by == "model"
    assert eff == 10 + 1000 * 0.10 + 1000 * 2.0 + 50 * 5.0


def test_a_claude_side_call_is_priced_from_the_usage_object_it_returns():
    usage = {"input_tokens": 10, "cache_read_input_tokens": 0, "output_tokens": 0,
             "cache_creation_input_tokens": 1000, "model": "claude-haiku-4-5",
             "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 1000}}
    assert accounting.effective(usage) == (10 + 1000 * 2.0, "model")


def test_an_unpriced_model_keeps_the_fallback_cache_write_rate():
    usage = {"input_tokens": 10, "cache_read_input_tokens": 0, "output_tokens": 0,
             "cache_creation_input_tokens": 1000, "cache_creation_1h_input_tokens": 1000,
             "model": "some-new-model"}
    assert accounting.effective(usage) == (10 + 1000 * 1.25, "fallback")


# ---- cache hits ------------------------------------------------------------------------------
@pytest.mark.parametrize("model, ratio", [
    ("claude-opus-5-5", 0.05), ("claude-sonnet-5-5", 0.05),
    ("claude-fable-5-1", 0.025), ("claude-fable-5-2", 0.025),
    ("claude-mythos-5-1", 0.025), ("claude-mythos-5-2", 0.025),
    ("claude-sonnet-5.5", 0.05),                                # a router's dotted spelling
    ("anthropic/claude-opus-5.5", 0.05),                        # and its provider prefix
    ("claude-opus-5-5-20261001", 0.05),                         # a dated snapshot
])
def test_a_cache_hit_is_billed_at_the_models_own_fraction(model, ratio):
    assert pricing.weights_for(_row(model=model))["cache_read"] == ratio
    cache_hits = 10_000 * ratio
    assert pricing.effective_tokens(_row(0, model=model)) == 100 + cache_hits + 10 * 5.0


@pytest.mark.parametrize("model", [
    "claude-opus-5", "claude-opus-4-8", "claude-sonnet-5", "claude-sonnet-4-6",
    "claude-haiku-5-5", "claude-haiku-4-5", "claude-fable-5", "claude-mythos-5",
    "claude-opus-4.5",
])
def test_the_other_claude_models_keep_the_usual_cache_hit_fraction(model):
    assert pricing.weights_for(_row(model=model))["cache_read"] == 0.10
    assert pricing.effective_tokens(_row(0, model=model)) == BASE


def test_a_session_on_opus_5_5_is_priced_with_its_cheaper_cache_hits(tmp_path):
    entries = [{"type": "assistant", "message": {
        "id": "m1", "model": "claude-opus-5-5", "role": "assistant",
        "usage": {"input_tokens": 10, "cache_read_input_tokens": 1000,
                  "cache_creation_input_tokens": 0, "output_tokens": 50},
        "content": [{"type": "text", "text": "x"}]}}]
    eff, priced_by = accounting.effective(trace.claude_session_usage(_transcript(
        tmp_path / "s.jsonl", entries)))
    assert priced_by == "model"
    assert eff == 10 + 1000 * 0.05 + 50 * 5.0
