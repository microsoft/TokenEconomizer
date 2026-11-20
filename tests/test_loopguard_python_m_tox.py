"""tox and nox run through the interpreter are test runs.

`python -m tox` and `python -m nox` run the same test sessions as `tox` and `nox`, which the loop
guard already counts, and the micro-skills observer already records all four as test runs."""
import pytest

from tokeneconomizer import loopguard as guard
from tokeneconomizer.loopguard import turn


@pytest.mark.parametrize("cmd", ["python -m tox", "python3 -m tox -e py311",
                                 "python -m nox -s tests", "python3.12 -m nox",
                                 ".venv/bin/python -m tox -p", "uv run python -m nox -s tests"])
def test_tox_or_nox_run_through_python_counts_as_a_verification(cmd):
    assert turn.status_of_tests(cmd) == "own"
    g = guard.TurnGuard(turn_key="t")
    g.fold("Edit", {"file_path": "/r/a.py", "old_string": "x", "new_string": "y"}, "ok")
    g.fold("Bash", {"command": cmd}, "py311: FAIL code 1", 1)
    assert g.verifications == 1 and not g.resolved_proxy()


@pytest.mark.parametrize("cmd", ["python -m tox --version", "python -m tox --help",
                                 "python -m nox --list", "python -m toxiproxy", "python -m noxfile"])
def test_a_tox_or_nox_command_that_runs_no_tests_is_not_one(cmd):
    assert turn.status_of_tests(cmd) is None


def test_the_observer_and_the_loop_guard_agree():
    from tokeneconomizer.microskills import observe
    for cmd in ("python -m tox", "python -m nox -s tests"):
        assert [o.intent for o in observe.classify_command(cmd)] == ["run_tests"]
        assert turn.status_of_tests(cmd) == "own"
