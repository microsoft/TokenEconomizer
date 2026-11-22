"""A test run spelled the Windows way is a verification.

On Windows a runner is named with backslashes, a drive letter and `.exe`
(`C:\\repo\\.venv\\Scripts\\python.exe -m pytest`, `.venv\\Scripts\\pytest.exe`), as Claude Code's
PowerShell tool and Codex record it. The micro-skills observer already reads these as test runs;
the loop guard did not, so a failing one after the last edit left the turn counted as ended
green."""
import pytest

from tokeneconomizer import loopguard as guard
from tokeneconomizer.loopguard import turn
from tokeneconomizer.microskills import observe

RUNS = [r"C:\repo\.venv\Scripts\python.exe -m pytest tests -q",
        r".venv\Scripts\python.exe -m pytest", ".venv/Scripts/python.exe -m pytest",
        r".venv\Scripts\pytest.exe -q", "D:/work/.venv/Scripts/pytest.exe", "python.exe -m pytest",
        r"C:\Python312\python.exe -m unittest discover", r".venv\Scripts\python.exe manage.py test",
        r"cd C:\repo && .venv\Scripts\python.exe -m pytest"]


@pytest.mark.parametrize("cmd", RUNS)
def test_a_windows_test_run_counts_as_a_verification(cmd):
    assert turn.status_of_tests(cmd) == "own"
    g = guard.TurnGuard(turn_key="t")
    g.fold("Edit", {"file_path": r"C:\repo\a.py", "old_string": "x", "new_string": "y"}, "ok")
    g.fold("PowerShell", {"command": cmd}, "FAILED tests/test_a.py::test_x - AssertionError", 1)
    assert g.verifications == 1 and not g.resolved_proxy()


@pytest.mark.parametrize("cmd", RUNS[:7])
def test_the_observer_and_the_loop_guard_agree(cmd):
    assert "run_tests" in [o.intent for o in observe.classify_command(cmd)]
    assert turn.status_of_tests(cmd) == "own"


@pytest.mark.parametrize("cmd", [r"python.exe repro.py", r".venv\Scripts\python.exe -m pip install x",
                                 r"C:\tools\pytest-runner.exe", r"notepad.exe tests\test_a.py",
                                 r".venv\Scripts\pytest.exe --collect-only"])
def test_a_windows_command_that_runs_no_tests_is_not_one(cmd):
    assert turn.status_of_tests(cmd) is None
