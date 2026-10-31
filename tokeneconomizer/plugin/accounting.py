"""The session ledger: what each session cost, on the experiments' basis, and what the plugin
itself spent.

Cost is `pricing.effective_tokens` -- fresh-input-token equivalents with cached re-reads at their
billed fraction -- plus every mechanism-overhead field `ledger.OVERHEAD_KEYS` names, exactly the
amortized definition the results tables use. One live-only allowance: `pricing.weights_for`
hard-fails on a billed id missing from its table (right for an experiment, where a mispriced row
corrupts a comparison). A user's session must still be recorded, so an unpriced id is priced on
a labelled fallback curve and the record says `priced_by: "fallback"`; Claude ids never need it,
because every `claude-*` id already resolves to the Claude curve.
"""
from .. import ledger, pricing

FALLBACK_WEIGHTS = dict(input=1.0, cache_read=0.10, cache_write=1.25, output=6.0)


def effective(usage):
    """(effective_tokens, priced_by) for a usage breakdown; (None, "no_usage") without one."""
    if not usage or usage.get("input_tokens") is None:
        return None, "no_usage"
    rec = {k: usage.get(k) for k in ("input_tokens", "cache_read_input_tokens",
                                     "cache_creation_input_tokens", "output_tokens", "model")}
    try:
        return pricing.effective_tokens(rec), "model"
    except ValueError:
        return pricing.effective_tokens(rec, weights=FALLBACK_WEIGHTS), "fallback"


def overhead_total(overhead):
    return ledger.overhead(overhead or {})


def summarize(records):
    """Aggregate ledger records into the numbers `te status` prints."""
    sessions = [r for r in records if r.get("kind") == "session"]
    priced = [r for r in sessions if isinstance(r.get("effective_tokens"), (int, float))]
    exec_total = sum(r["effective_tokens"] for r in priced)
    # `overhead` records are paid calls charged as they happened (the background stuck judge),
    # so a call that outlived its session's fold is still in the account.
    oh_total = sum(overhead_total(r.get("overhead")) for r in records
                   if r.get("kind") in ("session", "overhead"))
    return {
        "sessions": len(sessions),
        "priced_sessions": len(priced),
        "effective_tokens": exec_total,
        "overhead_tokens": oh_total,
        "overhead_frac": (oh_total / (exec_total + oh_total)) if (exec_total + oh_total) else 0.0,
        "requests": sum(int(r.get("requests") or 0) for r in sessions),
        "nudges": sum(int(r.get("nudges") or 0) for r in sessions),
        "aborts": sum(int(r.get("aborts") or 0) for r in sessions),
        "turns": sum(int(r.get("turns") or 0) for r in sessions),
        "injected_sessions": sum(1 for r in sessions if r.get("injected_chars")),
    }


def sessions_with(records, tools):
    """(sessions, requests) the ledger recorded with exactly `tools` left out."""
    want = sorted(tools)
    sessions = requests = 0
    for r in records:
        if r.get("kind") == "session" and sorted(r.get("untool_applied") or []) == want:
            sessions += 1
            requests += int(r.get("requests") or 0)
    return sessions, requests


def fmt_tokens(n):
    """A token count as a person reads it: 1.2M, 340k, 512."""
    if n is None:
        return "n/a"
    n = float(n)
    for unit, div in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(n) >= div:
            return f"{n / div:.1f}{unit}"
    return f"{n:.0f}"
