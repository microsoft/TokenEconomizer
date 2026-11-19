"""A make target run past make's own options is a verification.

`make -j8 check`, `make -C build test` and `make V=1 test` run the project's checks and exit with
their status, so a failing one after the last edit must leave the turn not green. Before, only a
bare `make test` or `make check` counted, and a C or C++ repository's failing turns fed the
envelope as ended green."""
import pytest

from tokeneconomizer import loopguard as guard
from tokeneconomizer.loopguard import turn


@pytest.mark.parametrize("cmd", ["make test", "make -j4 test", "make -j 4 test", "make -j test",
                                 "make -C build test", "make --directory=build check",
                                 "make -k -j8 check", "make V=1 test",
                                 "make -f Makefile.ci test"])
def test_a_make_target_past_its_options_counts_as_a_verification(cmd):
    assert turn.status_of_tests(cmd) == "own"
    g = guard.TurnGuard(turn_key="t")
    g.fold("Edit", {"file_path": "/r/a.c", "old_string": "x", "new_string": "y"}, "ok")
    g.fold("Bash", {"command": cmd}, "make: *** [Makefile:12: test] Error 1", 2)
    assert g.verifications == 1 and not g.resolved_proxy()


@pytest.mark.parametrize("cmd", ["make", "make install", "make -C build install", "make -j4",
                                 "make testdata", "make -j8 all"])
def test_other_make_targets_are_not_test_runs(cmd):
    assert turn.status_of_tests(cmd) is None
