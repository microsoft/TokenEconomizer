"""Ledger: overhead() and effective_exec()."""
from tokeneconomizer import ledger
from tokeneconomizer.ledger import OVERHEAD_KEYS


def test_overhead_sums_only_known_keys():
    row = {"optimizer_tokens": 10, "spiral_judge_tokens": 5,
           "untool_judge_tokens": 3, "not_an_overhead_key": 999}
    assert ledger.overhead(row) == 18


def test_overhead_ignores_non_numeric():
    assert ledger.overhead({"optimizer_tokens": None, "spiral_judge_tokens": "x"}) == 0


def test_all_overhead_keys_counted():
    row = {k: 1 for k in OVERHEAD_KEYS}
    assert ledger.overhead(row) == len(OVERHEAD_KEYS)


def test_effective_exec_uses_breakdown():
    # cache_read weighted at 0.10, output at 5.0 on the Claude curve.
    row = {"input_tokens": 100, "cache_read_input_tokens": 1000,
           "cache_creation_input_tokens": 0, "output_tokens": 10}
    assert ledger.effective_exec(row) == 100 * 1.0 + 1000 * 0.10 + 10 * 5.0


def test_effective_exec_falls_back_to_total():
    assert ledger.effective_exec({"tokens_total": 777}) == 777

