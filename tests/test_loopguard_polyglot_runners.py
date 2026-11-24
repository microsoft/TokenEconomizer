"""Test runners for polyglot ecosystems (Zig, Meson, Dart, Flutter, Forge, Ninja) count as verifications.

A test run in a Zig, Meson, Dart/Flutter, Foundry, or CMake/Ninja repository runs the project's
checks and exits with their status, so a failing one after the last edit must leave the turn
not green.
"""
import pytest

from tokeneconomizer import loopguard as guard
from tokeneconomizer.loopguard import turn


@pytest.mark.parametrize("cmd, fail_out", [
    ("zig test src/main.zig", "1/5 test.a... FAIL (TestUnexpectedResult)"),
    ("zig build test", "1/3 test.unit... FAIL"),
    ("meson test", "Fail: 1"),
    ("meson test -C build --verbose", "Fail: 2"),
    ("dart test", "Some tests failed."),
    ("flutter test", "Some tests failed."),
    ("forge test", "[FAIL. Reason: assertion failed] test_transfer()"),
    ("ninja test", "FAILED: test\nninja: build stopped: subcommand failed."),
    ("ninja check", "FAILED: check\nninja: build stopped: subcommand failed."),
    ("ninja -C build test", "FAILED: test\nninja: build stopped: subcommand failed."),
    ("samu check", "FAILED: check\nsamu: subcommand failed."),
])
def test_polyglot_test_runners_count_as_verifications(cmd, fail_out):
    assert turn.status_of_tests(cmd) == "own"
    g = guard.TurnGuard(turn_key="t")
    g.fold("Edit", {"file_path": "/r/src/code.ext", "old_string": "x", "new_string": "y"}, "ok")
    g.fold("Bash", {"command": cmd}, fail_out, 1)
    assert g.verifications == 1 and not g.resolved_proxy()


@pytest.mark.parametrize("cmd", [
    ("zig build"),
    ("zig fmt src/main.zig"),
    ("meson setup build"),
    ("meson compile -C build"),
    ("dart format lib/"),
    ("dart compile exe bin/main.dart"),
    ("forge build"),
    ("ninja"),
    ("ninja all"),
    ("ninja -C build"),
])
def test_non_test_commands_are_not_verifications(cmd):
    assert turn.status_of_tests(cmd) is None
