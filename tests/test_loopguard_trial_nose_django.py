"""trial, nosetests, python -m nose and python -m django test are test runs.

The micro-skills observer already reads Twisted's `trial`, nose's `nosetests` and `python -m nose`,
and `python -m django test` as test runs, and `SpiralState` already counts `trial`. `VERIFY_RE` now
reads them too, so turns verified with them do not count as ended green after a failure."""
import pytest

from tokeneconomizer import loopguard as guard
from tokeneconomizer.loopguard import turn


@pytest.mark.parametrize("cmd", [
    "trial mypkg",
    "trial mypkg.test_foo",
    ".venv/bin/trial mypkg",
    "nosetests -v",
    "nosetests tests/",
    ".venv/bin/nosetests",
    "python -m nose",
    "python3 -m nose -v",
    "python -m django test",
    "python3.12 -m django test myapp",
    "uv run trial mypkg",
    "uv run nosetests",
    "uv run python -m django test",
    "poetry run python -m nose",
])
def test_trial_nose_and_django_test_count_as_verifications(cmd):
    assert turn.status_of_tests(cmd) == "own"
    g = guard.TurnGuard(turn_key="t")
    g.fold("Edit", {"file_path": "/r/a.py", "old_string": "x", "new_string": "y"}, "ok")
    g.fold("Bash", {"command": cmd}, "FAILED (failures=1)", 1)
    assert g.verifications == 1 and not g.resolved_proxy()


@pytest.mark.parametrize("cmd", [
    "trial --help",
    "trial --version",
    "nosetests --help",
    "nosetests -h",
    "nosetests --version",
    "python -m django runserver",
    "python -m django migrate",
    "python -m django",
    "python -m nose --help",
    "trials",
    "nosetestspro",
])
def test_commands_that_run_no_tests_are_not_verifications(cmd):
    assert turn.status_of_tests(cmd) is None


def test_the_observer_and_the_loop_guard_agree():
    from tokeneconomizer.microskills import observe
    for cmd in ["trial mypkg", "nosetests -v", "python -m nose", "python -m django test"]:
        assert [o.intent for o in observe.classify_command(cmd)] == ["run_tests"]
        assert turn.status_of_tests(cmd) == "own"
