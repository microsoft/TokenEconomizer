"""`hatch test` is a test run.

Hatch 1.10 and later run the project's tests with `hatch test`, pytest in Hatch's own test
environment (`src/hatch/cli/test/__init__.py`). The loop guard read `hatch run pytest` as a test
run but not `hatch test`. Its `-s`/`--show` only shows the environments and runs nothing; click
lets short flags combine (`-ps`), and what follows `--` goes to pytest (`-- -s` is pytest's)."""
import pytest

from tokeneconomizer import loopguard as guard
from tokeneconomizer.loopguard import turn


@pytest.mark.parametrize("cmd", ["hatch test", "hatch test tests/test_a.py", "hatch test -c",
                                 "hatch test --cover -p", "hatch test -py 3.12",
                                 "hatch test -i py=3.11 -x feature=slow", "hatch test --all",
                                 "hatch test -- -s -k slow", "hatch test --retries 2 -r"])
def test_hatch_test_counts_as_a_verification(cmd):
    assert turn.status_of_tests(cmd) == "own"
    g = guard.TurnGuard(turn_key="t")
    g.fold("Edit", {"file_path": "/r/a.py", "old_string": "x", "new_string": "y"}, "ok")
    g.fold("Bash", {"command": cmd}, "FAILED tests/test_a.py::test_x - AssertionError", 1)
    assert g.verifications == 1 and not g.resolved_proxy()


@pytest.mark.parametrize("cmd", ["hatch test --show", "hatch test -s", "hatch test -ps",
                                 "hatch test --all --show", "hatch test --help",
                                 "hatch tests", "hatch build", "hatch env show"])
def test_a_hatch_command_that_runs_no_tests_is_not_one(cmd):
    assert turn.status_of_tests(cmd) is None
