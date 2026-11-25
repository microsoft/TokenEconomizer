"""A test run measured by coverage.py is a verification.

`coverage run -m pytest` (and `python -m coverage run -m pytest`, or Django's documented
`coverage run manage.py test`) runs the tests under coverage measurement and exits with the test
run's status, so a failing one after the last edit must leave the turn not green."""
import pytest

from tokeneconomizer import loopguard as guard
from tokeneconomizer.loopguard import turn


@pytest.mark.parametrize("cmd", ["coverage run -m pytest", "coverage run -m pytest -q tests",
                                 "python -m coverage run -m pytest",
                                 "python3 -m coverage run --branch -m pytest",
                                 "coverage run --source=pkg -m unittest discover",
                                 "coverage run --rcfile .coveragerc -m pytest",
                                 "coverage run -p --branch -m pytest",
                                 "coverage run manage.py test", "coverage run tests/runtests.py",
                                 "uv run coverage run -m pytest",
                                 ".venv/bin/coverage run -m pytest"])
def test_a_runner_under_coverage_counts_as_a_verification(cmd):
    assert turn.status_of_tests(cmd) == "own"
    g = guard.TurnGuard(turn_key="t")
    g.fold("Edit", {"file_path": "/r/a.py", "old_string": "x", "new_string": "y"}, "ok")
    g.fold("Bash", {"command": cmd}, "FAILED (failures=1)", 1)
    assert g.verifications == 1 and not g.resolved_proxy()


@pytest.mark.parametrize("cmd, status", [("coverage run repro.py", None),
                                         ("coverage report -m", None),
                                         ("coverage html", None),
                                         ("coverage run -m pytest --collect-only", None),
                                         ("coverage run -m pytest && coverage report", "own")])
def test_coverage_leaves_what_it_runs_to_decide(cmd, status):
    assert turn.status_of_tests(cmd) == status
