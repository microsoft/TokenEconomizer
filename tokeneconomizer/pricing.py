#!/usr/bin/env python3
"""
Cache-aware effective-cost weighting for token accounting.

`tokens_total` (= prompt_tokens + completion_tokens, summed over every model call)
counts EVERY token at full weight. In an agent loop the conversation is re-sent each
turn, so ~90%+ of `tokens_total` is `cache_read_input_tokens` — re-read context that
the provider bills at a small fraction of fresh input. Reporting raw `tokens_total`
therefore (a) OVERSTATES the cost of cheap cached re-reads and (b) UNDERSTATES the
cost of expensive output tokens. This biases A/B comparisons: a method that mainly
prunes cached re-reads looks better than its real-$ effect, while one that adds output
/ fresh-input (e.g. error-retry loops, negative transfer) looks cheaper than it is.

`effective_tokens()` reweights the per-task token breakdown into fresh-input-token
EQUIVALENTS using real price ratios, so cost A/Bs reflect real spend. Ratios are keyed on
the exact BILLED MODEL ID (weights_for -> MODEL_WEIGHTS), because families are NOT uniform
— e.g. gpt-5.6-sol bills output 6x + cache_write 1.25x, gpt-5.3-codex output 8x +
cache_write 0x, Claude output 5x + cache_write 1.25x. The table is a snapshot of the
providers' published per-model prices; a billed id with no entry HARD-FAILS rather than being
silently mispriced.

    model id (example)              input  cache_read  cache_write  output
    claude-* (Opus/Sonnet/Haiku)     1.00     0.10        1.25        5.00
    gpt-5.6-sol (codex default)      1.00     0.10        1.25        6.00
    gpt-5.3-codex                    1.00     0.097       0.00        8.00
    gpt-5-codex (OpenAI direct)      1.00     0.10        1.00        8.00

Callers may override with an explicit effective_tokens(rec, weights=...).
When the per-task breakdown is absent (older runs), effective_tokens() falls back to
`tokens_total` at full weight so nothing crashes — call has_breakdown() to detect this.
"""

# ---------------------------------------------------------------------------
# Per-model-id price ratios (fresh-input-token equivalents; input := 1.0).
#
# Pricing MUST match the exact model id that was billed — model families are NOT
# uniform (e.g. gpt-5.6-sol bills output 6x + cache_write 1.25x, while gpt-5.3-codex
# bills output 8x + cache_write 0x). So we key on the concrete model id, not a family
# heuristic, and HARD-FAIL if a billed id has no entry (see weights_for).
#
# Source: the providers' published per-model prices, snapshot 2026-07-27; ratios =
# price / input_price. Update the dict when a provider changes its prices.
#
# NB (verified): codex-cli 0.145.0's built-in DEFAULT model is `gpt-5.6-sol` (the resolved id
# is recorded in codex's session rollout file, not in its --json transcript). So an unpinned
# `codex` run bills sol.
MODEL_WEIGHTS = {
    # --- per-model price snapshot (2026-07-27) ------------------------------
    "claude-haiku-4-5":        dict(input=1.0, cache_read=0.10,   cache_write=1.25,  output=5.0),
    "claude-opus-4-6":         dict(input=1.0, cache_read=0.10,   cache_write=1.25,  output=5.0),
    "claude-opus-4-7":         dict(input=1.0, cache_read=0.10,   cache_write=1.25,  output=5.0),
    "claude-opus-4-8":         dict(input=1.0, cache_read=0.10,   cache_write=1.25,  output=5.0),
    "claude-opus-5":           dict(input=1.0, cache_read=0.10,   cache_write=1.25,  output=5.0),
    "claude-sonnet-4-5":       dict(input=1.0, cache_read=0.10,   cache_write=1.25,  output=5.0),
    "claude-sonnet-4-6":       dict(input=1.0, cache_read=0.10,   cache_write=1.25,  output=5.0),
    "claude-sonnet-5":         dict(input=1.0, cache_read=0.10,   cache_write=1.25,  output=5.0),
    "gemini-2.5-pro":          dict(input=1.0, cache_read=0.096,  cache_write=0.0,   output=8.0),
    "gemini-3-flash-preview":  dict(input=1.0, cache_read=0.10,   cache_write=0.0,   output=6.0),
    "gemini-3.1-pro-preview":  dict(input=1.0, cache_read=0.10,   cache_write=0.0,   output=6.0),
    "gemini-3.5-flash":        dict(input=1.0, cache_read=0.10,   cache_write=0.0,   output=6.0),
    "gemini-3.6-flash":        dict(input=1.0, cache_read=0.10,   cache_write=0.0,   output=5.0),
    "gpt-5-mini":              dict(input=1.0, cache_read=0.08,   cache_write=0.0,   output=8.0),
    "gpt-5.3-codex":           dict(input=1.0, cache_read=0.0971, cache_write=0.0,   output=8.0),
    "gpt-5.4":                 dict(input=1.0, cache_read=0.10,   cache_write=0.0,   output=6.0),
    "gpt-5.4-mini":            dict(input=1.0, cache_read=0.0933, cache_write=0.0,   output=6.0),
    "gpt-5.5":                 dict(input=1.0, cache_read=0.10,   cache_write=0.0,   output=6.0),
    "gpt-5.6-luna":            dict(input=1.0, cache_read=0.10,   cache_write=1.25,  output=6.0),
    "gpt-5.6-sol":             dict(input=1.0, cache_read=0.10,   cache_write=1.25,  output=6.0),
    "gpt-5.6-terra":           dict(input=1.0, cache_read=0.10,   cache_write=1.248, output=6.0),
    "mai-code-1-flash-picker": dict(input=1.0, cache_read=0.0933, cache_write=0.0,   output=6.0),
    # --- OpenAI-direct-only ids (public per-1M pricing; not in copilot catalog) ----
    # gpt-5/5.1 & *-codex: $1.25 in / $0.125 cached / $10 out  => cr 0.10, out 8.0
    "gpt-5":                   dict(input=1.0, cache_read=0.10,   cache_write=1.0,   output=8.0),
    "gpt-5.1":                 dict(input=1.0, cache_read=0.10,   cache_write=1.0,   output=8.0),
    "gpt-5-codex":             dict(input=1.0, cache_read=0.10,   cache_write=1.0,   output=8.0),
    "gpt-5.1-codex":           dict(input=1.0, cache_read=0.10,   cache_write=1.0,   output=8.0),
    "gpt-5.1-codex-max":       dict(input=1.0, cache_read=0.10,   cache_write=1.0,   output=8.0),
    "gpt-5.2-codex":           dict(input=1.0, cache_read=0.10,   cache_write=1.0,   output=8.0),
    # codex-mini-latest: $1.50 in / $0.375 cached / $6.00 out  => cr 0.25, out 4.0
    "codex-mini-latest":       dict(input=1.0, cache_read=0.25,   cache_write=1.0,   output=4.0),
}

# Billed model id for an agent when a row does NOT pin one (rec['model'] is None).
# Claude/Copilot agents map to None -> the Claude curve (their ratios are model-uniform
# across Opus/Sonnet/Haiku, so an exact id is unnecessary). Codex maps to codex's built-in
# default, gpt-5.6-sol (verified from codex's session rollout). Probed against the OpenAI
# Platform on 2026-09-09: `gpt-5.6-sol` is both listed by /v1/models AND accepted by
# /v1/responses (HTTP 200).
#
# The trap a catalog check alone will not catch: the `-codex` ALIASES are listed by
# /v1/models and 404 on /v1/responses ("does not exist or you do not have access to it" for
# gpt-5.1-codex, same probe). So pinning one of those fails per task while every catalog gate
# stays green. Pin a base id.
AGENT_DEFAULT_MODEL = {
    "codex": "gpt-5.6-sol",
}

# Claude-family price ratios (Opus/Sonnet/Haiku share them). Used as the default when a
# row has no resolvable model id (legacy rows, claude/copilot agents) so those stay
# byte-identical to historical behavior. Also the value of the back-compat WEIGHTS alias.
WEIGHTS_CLAUDE = {
    "input": 1.0,         # input_tokens = fresh/uncached prompt tokens
    "cache_read": 0.10,   # cache_read_input_tokens
    "cache_write": 1.25,  # cache_creation_input_tokens
    "output": 5.0,        # output_tokens (completion)
}
# Back-compat: modules that read pricing.WEIGHTS directly (figures, costmodel) were
# written against the Claude curve; keep that meaning. Per-row/per-model selection is via
# weights_for(); effective_tokens() applies it automatically.
WEIGHTS = WEIGHTS_CLAUDE


def resolve_model(rec):
    """The billed model id for a row: its explicit `model`, else the agent's default
    billed model (AGENT_DEFAULT_MODEL), else None. run_agent stamps the resolved id into
    `model` for codex runs (read from codex's session rollout), so unpinned codex rows
    still price by their true id."""
    if not isinstance(rec, dict):
        return None
    m = rec.get("model")
    if m:
        # A router's `provider/model` id names a ROUTE, not a price: Copilot's BYOK through
        # a router such as OpenRouter bills `anthropic/claude-haiku-4.5`, whose price is
        # the tail's. Stripping it here is safe because no MODEL_WEIGHTS key contains a
        # slash -- it can only affect an id that would otherwise hard-fail below, and
        # hard-failing a correctly-configured BYOK row at the END of a paid rollout is the
        # outcome this avoids.
        return m.rsplit("/", 1)[-1] if "/" in m else m
    return AGENT_DEFAULT_MODEL.get((rec.get("agent") or "").lower())


def weights_for(rec):
    """Pick the fresh-input-equivalent weight table for a row by its BILLED MODEL ID.

    Resolution (pricing must match the model id):
      * resolve the billed id (resolve_model) — explicit `model`, else agent default;
      * id in MODEL_WEIGHTS       -> that model's exact ratios;
      * id is a claude-* variant  -> Claude curve (covers dotted ids like claude-opus-4.8
                                     not in the catalog snapshot; ratios are uniform);
      * id present but UNPRICED   -> raise ValueError (hard-fail: a new/unknown model must
                                     be added to MODEL_WEIGHTS, never silently mispriced);
      * no resolvable id at all   -> Claude curve (legacy rows / claude / copilot: keeps
                                     historical behavior, never crashes)."""
    if not isinstance(rec, dict):
        return WEIGHTS_CLAUDE
    mid = resolve_model(rec)
    if not mid:
        return WEIGHTS_CLAUDE
    key = mid.lower()
    if key in MODEL_WEIGHTS:
        return MODEL_WEIGHTS[key]
    if "claude" in key:
        return WEIGHTS_CLAUDE
    raise ValueError(
        f"pricing: no weight entry for billed model {mid!r} "
        f"(agent={rec.get('agent')!r}). Add it to pricing.MODEL_WEIGHTS "
        f"(from the vendor's published price list).")


def has_breakdown(rec):
    """True when rec carries the per-task token breakdown needed for cache-aware
    weighting (i.e. input_tokens is populated)."""
    return isinstance(rec, dict) and rec.get("input_tokens") is not None


def effective_tokens(rec, weights=None):
    """Cache-aware effective cost of one task, in fresh-input-token equivalents.

    Uses the token breakdown (input / cache_read / cache_creation / output) when
    present; otherwise falls back to `tokens_total` at full weight (legacy rows).
    The weight table is chosen per row (weights_for) from its agent/model unless an
    explicit `weights=` is passed. Returns 0 when neither breakdown nor total exists.
    """
    w = weights if weights is not None else weights_for(rec)
    if has_breakdown(rec):
        it = rec.get("input_tokens") or 0
        cr = rec.get("cache_read_input_tokens") or 0
        cw = rec.get("cache_creation_input_tokens") or 0
        ot = rec.get("output_tokens") or 0
        return (it * w["input"] + cr * w["cache_read"]
                + cw * w["cache_write"] + ot * w["output"])
    tt = rec.get("tokens_total") if isinstance(rec, dict) else None
    return tt if isinstance(tt, (int, float)) else 0


def approx_tokens(text):
    """A character-count estimate of a text's tokens (~4 characters each)."""
    return max(1, len(text) // 4)


def runner_effective_or_approx(runner, prompt, raw, real):
    """The effective tokens of one side call: the usage its runner recovered, else the runner's
    own model-priced estimate, else an uncached estimate (prompt at x1, reply at x5)."""
    if real is not None:
        return real
    estimator = getattr(runner, "estimate_effective", None)
    if callable(estimator):
        try:
            return estimator(prompt, raw)
        except Exception:
            pass
    return approx_tokens(prompt) + 5 * approx_tokens(raw)
