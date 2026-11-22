"""Commands executed with Bun, Deno, and Node --test are correctly observed.

`bun test`, `bun run test:unit`, `deno test`, and `node --test` are test runs.
`bun run build`, `bun run lint`, `deno check`, and `deno lint` are builds/checks.
`bun add <pkg>`, `bun install`, and `deno add <pkg>` are package/fetch operations.
"""
import pytest

from tokeneconomizer.microskills import GENERIC, NODE_WEB, observe


def _seen(cmd, vocab=GENERIC):
    return [(o.intent, o.form) for o in observe.classify_command(cmd, vocab=vocab)]


@pytest.mark.parametrize("cmd, form", [
    ("bun test", "bun test"),
    ("bun test tests/auth.test.ts", "bun test"),
    ("bun run test", "bun run test"),
    ("bun run test:unit", "bun run test:unit"),
    ("bun run test:e2e", "bun run test:e2e"),
    ("deno test", "deno test"),
    ("deno test --allow-all tests/", "deno test"),
    ("node --test", "node --test"),
    ("node --test test/*.js", "node --test"),
])
def test_bun_deno_and_node_test_runners_observed_as_tests(cmd, form):
    assert _seen(cmd) == [("run_tests", form)]


@pytest.mark.parametrize("cmd, form", [
    ("bun run build", "bun run build"),
    ("bun run lint", "bun run lint"),
    ("bun run typecheck", "bun run typecheck"),
    ("deno check src/main.ts", "deno check"),
    ("deno lint", "deno lint"),
    ("deno fmt", "deno fmt"),
])
def test_bun_and_deno_builds_and_checks_observed(cmd, form):
    assert _seen(cmd) == [("run_build", form)]


@pytest.mark.parametrize("cmd, form", [
    ("bun install", "bun install"),
    ("bun add lodash", "bun add"),
    ("bun add -d typescript", "bun add"),
    ("deno add @std/fs", "deno add"),
    ("deno install -g esbuild", "deno install"),
])
def test_bun_and_deno_package_fetches_observed(cmd, form):
    assert _seen(cmd) == [("fetch_remote", form)]


def test_bare_scripts_still_observed_as_interpreter():
    assert _seen("bun src/index.ts") == [("run_interpreter", "bun")]
    assert _seen("deno run src/index.ts") == [("run_interpreter", "deno")]
    assert _seen("node server.js") == [("run_interpreter", "node")]
