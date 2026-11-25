"""The numbers `te status` prints, and what a paid side call is charged when its CLI reports no
usage.

`accounting.summarize` turns the session ledger into the cost summary: priced and unpriced
sessions, the agent's own cost, the plugin's overhead and its share of the total, with sessions
imported from before the plugin kept apart. `pricing.runner_effective_or_approx` is what the loop
guard's judge charges for one call: the usage its runner recovered, else the runner's own
estimate, else the prompt at 1x and the reply at 5x (uncached input and output). Values are
worked out by hand."""
import pytest

from tokeneconomizer import pricing
from tokeneconomizer.loopguard import judge
from tokeneconomizer.plugin import accounting

LEDGER = [
    {"kind": "session", "effective_tokens": 1000.0, "requests": 3, "turns": 2, "nudges": 1,
     "aborts": 0, "injected_chars": 120,
     "overhead": {"spiral_judge_tokens": 50, "optimizer_tokens": 30}},
    # unpriced: a session whose agent named a model no table prices and reported no usage
    {"kind": "session", "effective_tokens": None, "requests": 2, "turns": 1, "aborts": 1,
     "overhead": {"made_up_tokens": 500}},
    # a background judge call charged after its session was folded
    {"kind": "overhead", "overhead": {"spiral_judge_tokens": 20}},
    # a session from before the plugin: counted on its own, in nothing else
    {"kind": "session", "imported": True, "effective_tokens": 5000.0, "requests": 9,
     "overhead": {"optimizer_tokens": 999}},
    {"kind": "note", "overhead": {"spiral_judge_tokens": 7}},
]


def test_the_summary_adds_up_the_ledger():
    s = accounting.summarize(LEDGER)
    assert (s["sessions"], s["imported_sessions"], s["priced_sessions"]) == (2, 1, 1)
    assert s["effective_tokens"] == 1000.0
    assert s["overhead_tokens"] == 50 + 30 + 20         # only known overhead fields count
    assert s["overhead_frac"] == pytest.approx(100 / 1100)
    assert (s["requests"], s["turns"], s["nudges"], s["aborts"]) == (5, 3, 1, 1)
    assert s["injected_sessions"] == 1


def test_a_session_priced_at_zero_is_priced():
    s = accounting.summarize([{"kind": "session", "effective_tokens": 0}])
    assert (s["sessions"], s["priced_sessions"], s["effective_tokens"]) == (1, 1, 0)


def test_an_empty_ledger_has_no_overhead_share():
    s = accounting.summarize([])
    assert (s["sessions"], s["effective_tokens"], s["overhead_tokens"]) == (0, 0, 0)
    assert s["overhead_frac"] == 0.0


def test_overhead_alone_is_the_whole_share():
    s = accounting.summarize([{"kind": "overhead", "overhead": {"spiral_judge_tokens": 40}}])
    assert (s["overhead_tokens"], s["overhead_frac"]) == (40, 1.0)


# ---- a side call with no usage --------------------------------------------------------------
class _Runner:
    def __init__(self, estimate=None):
        if estimate is not None:
            self.estimate_effective = estimate


def test_approx_tokens_is_four_characters_each_and_never_zero():
    assert (pricing.approx_tokens(""), pricing.approx_tokens("abc")) == (1, 1)
    assert pricing.approx_tokens("x" * 4000) == 1000


def test_the_usage_a_runner_recovered_wins_even_when_it_is_zero():
    runner = _Runner(estimate=lambda p, r: 999)
    assert pricing.runner_effective_or_approx(runner, "x" * 400, "y" * 40, 321) == 321
    assert pricing.runner_effective_or_approx(runner, "x" * 400, "y" * 40, 0) == 0


def test_without_usage_the_runners_own_estimate_is_used():
    runner = _Runner(estimate=lambda p, r: len(p) + 2 * len(r))
    assert pricing.runner_effective_or_approx(runner, "x" * 400, "y" * 40, None) == 480


def test_without_usage_or_estimate_the_prompt_is_fresh_input_and_the_reply_output():
    want = 100 + 5 * 10                                  # 400 chars at 1x, 40 chars at 5x
    assert pricing.runner_effective_or_approx(_Runner(), "x" * 400, "y" * 40, None) == want

    def broken(p, r):
        raise RuntimeError("no price for this model")
    assert pricing.runner_effective_or_approx(_Runner(broken), "x" * 400, "y" * 40, None) == want


def test_the_judge_charges_the_estimate_when_its_runner_recovers_no_usage():
    reply = '{"stuck": true, "confidence": 0.9, "reason": "same failure"}'
    seen = {}

    def runner(prompt, model):
        seen["prompt"] = prompt
        return reply, None
    v = judge.llm_stuck_judge("turns=30 repeated_identical_failures=4", runner)
    assert v["stuck"] is True
    assert v["_tokens"] == pricing.approx_tokens(seen["prompt"]) + 5 * pricing.approx_tokens(reply)
    assert judge.llm_stuck_judge("turns=30", lambda p, m: (reply, 77))["_tokens"] == 77
