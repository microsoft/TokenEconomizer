"""A JavaScript package manager running the tests past its own options is a test run.

The loop guard read `npm test`, `pnpm test` and `yarn test` only right after the package manager.
A monorepo names the workspace first (`pnpm --filter web test`, `npm -w web test`, `pnpm -r test`,
`yarn --cwd web test`, `yarn workspace web test`), npm and pnpm also take `t` and `tst` for
`test`, and pnpm and yarn run an installed runner by its name (`pnpm vitest run`, `yarn jest`).
None of these was a verification, so a failing one after the last edit left the turn counted as
ended green. Each manager's options that take a value are read with it: npm's `-w` names a
workspace while pnpm's is `--workspace-root`, a flag."""
import time

import pytest

from tokeneconomizer import loopguard as guard
from tokeneconomizer.loopguard import turn


@pytest.mark.parametrize("cmd", [
    "npm t", "npm tst", "pnpm t", "pnpm tst",
    "pnpm --filter web test", "pnpm -F web test", "pnpm --filter=web test", "pnpm -r test",
    "pnpm --recursive --if-present test", "pnpm -C web test", "pnpm --dir web run test",
    "pnpm -w test", "pnpm --filter $PKG test", "pnpm --filter=${PKG} test",
    "npm --prefix web test", "npm -C web test", "npm -w web test", "npm --workspace=web test",
    "npm -ws test", "npm --workspaces --if-present run test", "npm -s test",
    "npm --loglevel silent test", "npm -w web run-script test:unit",
    "yarn --cwd web test", "yarn --cwd=web run test", "yarn workspace web test",
    "yarn workspace @app/web run test",
    "pnpm vitest", "pnpm vitest run", "pnpm jest --ci", "pnpm --filter web exec vitest run",
    "pnpm playwright test", "yarn jest", "yarn vitest run", "yarn run mocha",
    "yarn workspace web jest"])
def test_a_package_manager_running_the_tests_counts_as_a_verification(cmd):
    assert turn.status_of_tests(cmd) == "own"
    g = guard.TurnGuard(turn_key="t")
    g.fold("Edit", {"file_path": "/r/src/a.ts", "old_string": "x", "new_string": "y"}, "ok")
    g.fold("Bash", {"command": cmd}, "FAIL src/a.test.ts > adds\nTests  1 failed (1)", 1)
    assert g.verifications == 1 and not g.resolved_proxy()


@pytest.mark.parametrize("cmd", [
    "npm token list", "npm team ls org", "npm run t", "npm run tst", "pnpm tsc",
    "npm test-build", "pnpm jest-cli", "yarn avatar",
    "npm -w test", "pnpm -F test", "pnpm -C test build", "yarn --cwd test build",
    "yarn workspace test", "npm -w test run build", "npm --workspace=test run build",
    "npm --prefix test run build", "pnpm --filter test add vitest",
    "yarn workspace web add jest", "pnpm --filter web build", "yarn add -D vitest",
    "pnpm vitest --help", "yarn jest --listTests"])
def test_other_package_manager_commands_are_not_test_runs(cmd):
    """`npm -w test run build` builds the workspace named `test`: `-w` always takes a value."""
    assert turn.status_of_tests(cmd) is None


@pytest.mark.parametrize("cmd", [
    "npm" + " -w a -s --workspace b -ws -C c" * 12 + " run build",
    "pnpm" + " -F a -r --filter=b --filter $(x) -C c" * 12 + " build",
    "yarn" + " --cwd a -s --cwd=b --cwd ${y}" * 12 + " build"], ids=["npm", "pnpm", "yarn"])
def test_a_long_line_of_options_that_runs_no_tests_is_read_quickly(cmd):
    """An option is read one way only. Read two ways, a line of n options that ends in no test
    run would backtrack through 2^n readings before the pattern gave up."""
    start = time.perf_counter()
    assert turn.status_of_tests(cmd) is None
    assert turn.status_of_tests(cmd.rsplit(" ", 1)[0] + " test") == "own"
    assert time.perf_counter() - start < 1
