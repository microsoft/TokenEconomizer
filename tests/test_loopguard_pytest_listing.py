"""A pytest run asked only to show something runs no tests, so it is no verification.

pytest's own help says `--fixtures` (`--funcargs`), `--fixtures-per-test` and `--markers` show
what is available, `--setup-plan` shows what would run "but don't execute anything",
`--setup-only` sets up fixtures and does "not execute tests", and `--cache-show` shows the cache
and does not "perform collection or tests". Counted as a verification, such a run that exits 0
after a failing test run made the turn count as ended green."""
import pytest

from tokeneconomizer import loopguard as guard
from tokeneconomizer.loopguard import turn

LISTINGS = ["pytest --fixtures", "python -m pytest --funcargs", "pytest --fixtures-per-test tests",
            "pytest --markers", "python3 -m pytest --setup-plan tests/test_a.py",
            "pytest --setup-only", "pytest --cache-show", "pytest --cache-show=cache/lastfailed",
            "uv run pytest --fixtures"]


@pytest.mark.parametrize("cmd", LISTINGS)
def test_a_pytest_run_that_only_shows_something_is_no_verification(cmd):
    assert turn.status_of_tests(cmd) is None


@pytest.mark.parametrize("cmd", LISTINGS)
def test_it_does_not_turn_a_failed_turn_green(cmd):
    g = guard.TurnGuard(turn_key="t")
    g.fold("Edit", {"file_path": "/r/a.py", "old_string": "x", "new_string": "y"}, "ok")
    g.fold("Bash", {"command": "pytest -q"}, "FAILED tests/test_a.py::test_x - AssertionError", 1)
    g.fold("Bash", {"command": cmd}, "cache -- _pytest/cacheprovider.py\n    Return a cache", 0)
    assert g.verifications == 1 and not g.resolved_proxy()


@pytest.mark.parametrize("cmd", ["pytest --setup-show", "pytest -q --fixtures-dir x",
                                 "pytest tests/test_markers.py", "pytest -m slow"])
def test_runs_that_execute_tests_still_count(cmd):
    assert turn.status_of_tests(cmd) == "own"
