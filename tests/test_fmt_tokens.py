"""`te status` prints token figures as a person reads them (`accounting.fmt_tokens`): 512, 340.0k,
1.2M. A figure that rounds up to 1000 of its unit is one of the next unit: 999,950 tokens is
1.0M, not 1000.0k. Effective tokens are weighted, so they need not be whole: 999.6 is 1.0k."""
import pytest

from tokeneconomizer.plugin.accounting import fmt_tokens


@pytest.mark.parametrize("n, shown", [
    (None, "n/a"), (0, "0"), (512, "512"), (999, "999"), (999.4, "999"),
    (1000, "1.0k"), (1049, "1.0k"), (340_000, "340.0k"), (999_949, "999.9k"),
    (1_000_000, "1.0M"), (1_250_000, "1.2M"), (999_949_999, "999.9M"),
    (1_000_000_000, "1.0B"), (2.5e12, "2500.0B"), (-1500, "-1.5k"),
])
def test_a_token_figure_is_shown_in_its_unit(n, shown):
    assert fmt_tokens(n) == shown


@pytest.mark.parametrize("n, shown", [
    (999.6, "1.0k"), (999_950, "1.0M"), (999_999, "1.0M"),
    (999_950_000, "1.0B"), (999_999_999, "1.0B"), (-999_950, "-1.0M"),
])
def test_a_figure_that_rounds_up_to_a_thousand_is_one_of_the_next_unit(n, shown):
    assert fmt_tokens(n) == shown
