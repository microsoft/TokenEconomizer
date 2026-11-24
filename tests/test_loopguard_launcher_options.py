"""A test run behind a launcher's own options is a verification.

`uv run --extra test pytest` and `uv run --group dev pytest` put uv's options between `run` and
the command, and take the extra's or the group's name as a word of its own. The micro-skills
observer reads those options before the command, so it learns and injects those runs
(`tests/test_observe_python_launchers.py`, which records that "the loop guard passes over all
six" launchers). A run the loop guard cannot see past the same options is no verification at
all: a failing one leaves its turn counted as ended green, and the envelope learns from a turn
that ended red."""
import pytest

from tokeneconomizer import loopguard as guard
from tokeneconomizer.loopguard import turn

# A run whose command line the options stand in front of, and how it is typed.
BEHIND_OPTIONS = [
    "uv run --extra test pytest -q",
    "uv run --extra=test pytest",
    "uv run --group dev pytest",
    "uv run --only-group dev python -m pytest",
    "uv run --no-group docs pytest",
    "uv run --with pytest-cov pytest",
    "uv run --with pytest pytest tests",
    "uv run --with-editable . pytest",
    "uv run --python 3.13 pytest",
    "uv run --frozen --extra test pytest",
    "uv run --all-extras -- pytest",
    "uv run --extra test python -m unittest",
    "pdm run --group dev pytest",
    "poetry run pytest",
    "hatch run pytest",
    "rye run pytest",
    "npm exec -- vitest run",
    "npx --yes vitest run",
    "npx -y vitest",
    "xvfb-run -a pytest",
    "bundle exec rspec",
]


@pytest.mark.parametrize("cmd", BEHIND_OPTIONS)
def test_a_run_behind_a_launcher_s_own_options_counts_as_a_verification(cmd):
    assert turn.status_of_tests(cmd) == "own"
    g = guard.TurnGuard(turn_key="t")
    g.fold("Edit", {"file_path": "/r/a.py", "old_string": "x", "new_string": "y"}, "ok")
    g.fold("Bash", {"command": cmd}, "1 failed, 1 passed in 0.42s", 1)
    assert g.verifications == 1 and not g.resolved_proxy()


@pytest.mark.parametrize("cmd", ["uv run --extra test python train.py",
                                 "uv run --with pytest --version",
                                 "uv run --extra test pytest --collect-only",
                                 "uv sync --extra test",
                                 "pdm run --group dev python train.py"])
def test_options_alone_still_leave_what_runs_to_decide(cmd):
    assert turn.status_of_tests(cmd) is None


def test_the_turn_a_failing_run_behind_options_ended_is_not_green():
    """The whole turn: an edit, a failing `uv run --extra test pytest`, then a passing run of a
    command that only reads. The failure the run reported is not undone by the later read."""
    g = guard.TurnGuard(turn_key="t")
    g.fold("Edit", {"file_path": "/r/a.py", "old_string": "x", "new_string": "y"}, "ok")
    g.fold("Bash", {"command": "uv run --extra test pytest -q"},
           "FAILED tests/test_a.py::test_x - AssertionError\n1 failed, 1 passed in 0.42s", 1)
    assert g.verifications == 1 and not g.resolved_proxy()
    g.fold("Bash", {"command": "cat tests/test_a.py"}, "def test_x(): ...", 0)
    assert not g.resolved_proxy()
