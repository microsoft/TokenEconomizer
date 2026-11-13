"""Models that came after the 2026-07-27 price snapshot are in the table with their vendors' own
ratios. A model missing from the table is priced on the live ledger's fallback curve (cache hit
0.10x input, output 6x), which overstates a session on a model whose cache hit costs 0.05x input
(gpt-6.1-sol, gpt-6.1-terra, gemini-4-*) or whose output costs 5x (every GPT-6 model)."""
import pytest

from tokeneconomizer import pricing
from tokeneconomizer.plugin import accounting

# id, input, cached input, cache write (30-minute, or None), output: USD per 1M tokens, Standard
# tier, short context, as OpenAI's and Google's price pages listed them on 2026-11-13.
PRICES = [
    ("gpt-6-astra",           10.00, 1.00,  12.50, 50.00),
    ("gpt-6-luna",             0.10, 0.01,  0.125,  0.50),
    ("gpt-6-sol",              2.00, 0.20,   2.50, 10.00),
    ("gpt-6.1-sol",            2.00, 0.10,   2.50, 10.00),
    ("gpt-6.1-terra",          1.00, 0.05,   1.25,  5.00),
    ("gemini-3.1-flash-lite",  0.25, 0.025, None,   1.50),
    ("gemini-3.5-flash-lite",  0.30, 0.03,  None,   2.50),
    ("gemini-3.8-flash",       0.75, 0.075, None,   3.75),
    ("gemini-4-argon",         2.00, 0.10,  None,  10.00),
    ("gemini-4-carbon",        2.00, 0.10,  None,  10.00),
    ("gemini-4-hydrogen",      1.50, 0.075, None,   7.50),
]
IDS = [p[0] for p in PRICES]

# A Codex-sized session: fresh input, cache hits, cache writes, output.
USAGE = {"input_tokens": 800, "cache_read_input_tokens": 2000,
         "cache_creation_input_tokens": 200, "output_tokens": 120}


@pytest.mark.parametrize("model, price_in, cached, write, price_out", PRICES)
def test_a_models_weights_are_its_prices_over_its_input_price(model, price_in, cached, write,
                                                              price_out):
    w = pricing.weights_for({"model": model})
    assert w["input"] == 1.0
    assert w["cache_read"] == pytest.approx(cached / price_in, rel=1e-3)
    assert w["cache_write"] == pytest.approx(write / price_in if write else 0.0, rel=1e-3)
    assert w["output"] == pytest.approx(price_out / price_in, rel=1e-3)


@pytest.mark.parametrize("model", IDS)
def test_a_session_on_one_of_these_models_is_priced_by_the_model_not_the_fallback(model):
    _, priced_by = accounting.effective(dict(USAGE, model=model))
    assert priced_by == "model"


def test_a_cache_hit_on_gpt_6_1_terra_is_billed_at_half_the_usual_fraction():
    eff, priced_by = accounting.effective(dict(USAGE, model="gpt-6.1-terra"))
    assert priced_by == "model"
    assert eff == pytest.approx(800 + 2000 * 0.05 + 200 * 1.25 + 120 * 5.0)
    fallback, _ = accounting.effective(dict(USAGE, model="some-new-model"))
    assert eff < fallback


def test_an_unlisted_model_is_still_priced_on_the_labelled_fallback():
    assert accounting.effective(dict(USAGE, model="some-new-model"))[1] == "fallback"
