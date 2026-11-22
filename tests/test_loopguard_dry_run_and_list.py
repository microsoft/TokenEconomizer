"""A test runner asked only to list tests, show config, or compile/dry-run is no verification.

Commands like `cargo test --no-run`, `go test -list <pattern>`, `ctest --show-only`,
`jest --showConfig`, or `playwright test --dry-run` do not execute any tests.
If counted as a verification, such a run exiting 0 after a failing test would falsely mark
a failed turn as ended green in the envelope tracker.
"""
import pytest

from tokeneconomizer import loopguard as guard
from tokeneconomizer.loopguard import turn

NON_RUNS = [
    "cargo test --no-run",
    "cargo test --no-run tests/test_a.rs",
    "go test -list TestFoo",
    "go test -list=TestFoo ./...",
    "ctest --show-only",
    "ctest --show-only=json-v1",
    "jest --showConfig",
    "vitest --show-config",
    "playwright test --dry-run",
    "npx playwright test --dry-run",
]


@pytest.mark.parametrize("cmd", NON_RUNS)
def test_commands_that_only_list_or_dry_run_are_no_verification(cmd):
    assert turn.status_of_tests(cmd) is None


@pytest.mark.parametrize("cmd", NON_RUNS)
def test_they_do_not_turn_a_failed_turn_green(cmd):
    g = guard.TurnGuard(turn_key="t")
    g.fold("Edit", {"file_path": "/r/src/lib.rs", "old_string": "x", "new_string": "y"}, "ok")
    g.fold("Bash", {"command": "cargo test"}, "test result: FAILED. 1 failed", 101)
    g.fold("Bash", {"command": cmd}, "finished in 0.05s", 0)
    assert g.verifications == 1 and not g.resolved_proxy()


@pytest.mark.parametrize("cmd", [
    "cargo test",
    "cargo test -- --nocapture",
    "go test ./...",
    "ctest",
    "ctest -V",
    "jest",
    "vitest",
    "playwright test",
    "pytest -c setup.cfg",
])
def test_runs_that_execute_tests_still_count(cmd):
    assert turn.status_of_tests(cmd) == "own"
