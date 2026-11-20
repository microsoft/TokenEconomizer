"""Every Python test runner the observer knows is a verification.

The micro-skills observer reads `trial`, `nosetests`, `python -m nose` and `python -m django test`
as test runs (its runner heads and modules), and `SpiralState` already counts `trial` among its
test runs, but the loop guard's verification pattern did not: a failing run after the last edit
left the turn counted as ended green."""
import pytest

from tokeneconomizer import loopguard as guard
from tokeneconomizer.loopguard import turn
from tokeneconomizer.microskills import observe

RUNS = ["trial mypkg", "trial -j4 mypkg.test", "nosetests -v", ".venv/bin/nosetests tests",
        "python -m nose", "python3 -m nose -v tests", "python -m django test",
        "python -m django test --settings=app.settings", "python3.12 -W error -m django test app"]


@pytest.mark.parametrize("cmd", RUNS)
def test_a_runner_the_observer_knows_counts_as_a_verification(cmd):
    assert turn.status_of_tests(cmd) == "own"
    g = guard.TurnGuard(turn_key="t")
    g.fold("Edit", {"file_path": "/r/a.py", "old_string": "x", "new_string": "y"}, "ok")
    g.fold("Bash", {"command": cmd}, "FAILED (failures=1)", 1)
    assert g.verifications == 1 and not g.resolved_proxy()


@pytest.mark.parametrize("cmd", ["python -m django runserver", "python -m django startproject x",
                                 "python -m django --version", "trial --help",
                                 "nosetests --version", "python -m nosey", "echo trial"])
def test_the_same_commands_that_run_no_tests_are_not_runs(cmd):
    assert turn.status_of_tests(cmd) is None


@pytest.mark.parametrize("cmd", ["trial mypkg", "nosetests", "python -m nose",
                                 "python -m django test"])
def test_the_observer_and_the_loop_guard_agree(cmd):
    assert [o.intent for o in observe.classify_command(cmd)] == ["run_tests"]
    assert turn.status_of_tests(cmd) == "own"
