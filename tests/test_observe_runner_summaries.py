"""A Go run replayed from the test cache, and a minitest or test-unit run, are tests that ran.

`go test` prints `ok  \t<pkg>\t(cached)` in place of the time for a package whose result it
replays from its test cache (cmd/go/internal/test). minitest sums a run up as `3 runs, 3
assertions, 0 failures, 0 errors, 0 skips`, test-unit as `3 tests, 4 assertions, 0 failures, ...`.
The loop guard reads all three as a passing run (`loopguard.turn.runner_verdict`); the observer
read none of them as tests having run, so such a run never showed that its form works."""
import pytest

from fixtures import write_codex_transcript
from tokeneconomizer.loopguard import turn
from tokeneconomizer.microskills import EnvFactsDistiller, observe

GENERIC = observe.GENERIC

PASSING = {
    "go, cached": "ok  \texample.com/widget/pkg\t(cached)\n",
    "go, cached with coverage":
        "ok  \texample.com/widget/pkg\t(cached)\tcoverage: 81.2% of statements\n",
    "go, cached and run": ("ok  \texample.com/widget/a\t(cached)\n"
                           "ok  \texample.com/widget/b\t0.012s\n"),
    "minitest": ("Run options: --seed 4242\n\n# Running:\n\n...\n\n"
                 "Finished in 0.001s, 2000 runs/s, 2000 assertions/s.\n\n"
                 "3 runs, 3 assertions, 0 failures, 0 errors, 0 skips\n"),
    "test-unit": ("Loaded suite test/a_test\nStarted\n...\n"
                  "Finished in 0.002 seconds.\n---\n"
                  "3 tests, 4 assertions, 0 failures, 0 errors, 0 pendings, 0 omissions, "
                  "0 notifications\n100% passed\n"),
}


def _observed(tmp_path, command, output):
    p = str(tmp_path / "t.jsonl")
    write_codex_transcript(p, [{"type": "command_execution", "command": command,
                                "aggregated_output": output, "exit_code": 0}])
    return [o for o in observe.observe_task(p, GENERIC)["obs"] if o.intent == "run_tests"]


@pytest.mark.parametrize("name", PASSING)
def test_a_passing_summary_shows_the_tests_ran(tmp_path, name):
    command = "go test ./..." if name.startswith("go") else "bundle exec rake test"
    [o] = _observed(tmp_path, command, PASSING[name])
    assert o.tests_ran
    assert observe.form_worked(o)


@pytest.mark.parametrize("name", PASSING)
def test_the_observer_and_the_loop_guard_read_the_same_summaries(name):
    assert turn.runner_verdict(PASSING[name]) is False


@pytest.mark.parametrize("output", [
    "0 runs, 0 assertions, 0 failures, 0 errors, 0 skips\n",
    "0 tests, 0 assertions, 0 failures, 0 errors, 0 pendings, 0 omissions, 0 notifications\n",
    "?   \texample.com/widget/cmd\t[no test files]\n",
    "Finished in 0.001s, 2000 runs/s, 2000 assertions/s.\n",
])
def test_a_run_that_ran_no_test_shows_none(tmp_path, output):
    [o] = _observed(tmp_path, "go test ./...", output)
    assert not o.tests_ran


@pytest.mark.parametrize("name", ["minitest", "test-unit"])
def test_a_ruby_run_teaches_the_working_form(tmp_path, name):
    """Sessions where `rake` is not on PATH and the bundle's runs the tests: the fact injected
    into the next session names the form that works."""
    def cmd(command, output, code):
        return {"type": "command_execution", "command": command,
                "aggregated_output": output, "exit_code": code}

    d = EnvFactsDistiller("acme/widget", vocab=GENERIC)
    for i in range(4):
        p = str(tmp_path / f"t{i}.jsonl")
        write_codex_transcript(p, [
            {"type": "file_change", "changes": [{"path": f"lib/m{i}.rb", "kind": "update"}]},
            cmd("rake test", "bash: rake: command not found", 127),
            cmd("bundle exec rake test", PASSING[name], 0)])
        d.render()
        d.update(p, iid=f"w-{i}", resolved=True)
    [fact] = [f for f in d.facts.values() if f.family == "form_repair"]
    assert fact.params["a"] == "rake test"
    assert fact.working_form().startswith("bundle ")
    assert "`rake test …` fails here with command not found; `bundle " in d.render()
