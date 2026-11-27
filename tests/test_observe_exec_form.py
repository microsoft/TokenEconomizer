"""A command run through `bundle exec`, `pnpm exec` or `yarn exec` keeps `exec` in its form.

The form is what a learned fact tells the next session to type. The observer wrote every
launcher's verb as `run`, so `bundle exec rspec` became `bundle run rspec`, which Bundler has no
command for, and `pnpm exec jest` became `pnpm run jest`, which runs a `package.json` script named
`jest` rather than the binary (pnpm's `run` docs: "Runs a script defined in the package's
manifest file")."""
import pytest

from fixtures import write_codex_transcript
from tokeneconomizer.microskills import EnvFactsDistiller, envfacts, observe

GENERIC = observe.GENERIC


def _seen(cmd):
    return [(o.intent, o.form) for o in observe.classify_command(cmd, vocab=GENERIC)]


@pytest.mark.parametrize("cmd, seen", [
    ("bundle exec rspec", [("run_tests", "bundle exec rspec")]),
    ("bundle exec rspec spec/models/user_spec.rb", [("run_tests", "bundle exec rspec")]),
    ("bundle exec rake test", [("run_tests", "bundle exec rake test")]),
    ("bundle exec ruby -Itest test/a_test.rb", [("run_interpreter", "bundle exec ruby")]),
    ("pnpm exec jest", [("run_tests", "pnpm exec jest")]),
    ("pnpm exec vitest run", [("run_tests", "pnpm exec vitest")]),
    ("yarn exec jest", [("run_tests", "yarn exec jest")]),
])
def test_an_exec_launcher_keeps_exec_in_the_form(cmd, seen):
    assert _seen(cmd) == seen
    assert not any(observe.is_bare(form) for _, form in seen)


@pytest.mark.parametrize("cmd, seen", [
    ("uv run pytest", [("run_tests", "uv run pytest")]),
    ("poetry run pytest -q", [("run_tests", "poetry run pytest")]),
    ("pdm run pytest", [("run_tests", "pdm run pytest")]),
    ("yarn run jest", [("run_tests", "yarn run jest")]),
    ("rspec", [("run_tests", "rspec")]),
])
def test_a_run_launcher_and_a_bare_runner_are_read_as_before(cmd, seen):
    assert _seen(cmd) == seen


def test_exec_alone_does_not_make_two_builds_one_tool():
    """A build or check is repaired only by the same tool spelled another way; sharing the
    launcher's verb, as sharing `run` before, is not sharing the tool."""
    assert not envfacts._repairs("pnpm exec eslint", "yarn exec tsc", "run_build")
    assert envfacts._repairs("bundle exec rubocop", "rubocop", "run_build")


def test_the_learned_fact_names_bundle_exec(tmp_path):
    """Sessions where `rspec` is not on PATH and `bundle exec rspec` passes: the fact injected
    into the next session names the command Bundler runs."""
    def cmd(command, output, code):
        return {"type": "command_execution", "command": command,
                "aggregated_output": output, "exit_code": code}

    d = EnvFactsDistiller("acme/widget", vocab=GENERIC)
    for i in range(4):
        p = str(tmp_path / f"t{i}.jsonl")
        write_codex_transcript(p, [
            {"type": "file_change", "changes": [{"path": f"app/models/m{i}.rb", "kind": "update"}]},
            cmd("rspec", "bash: rspec: command not found", 127),
            cmd("bundle exec rspec", "Finished in 0.02 seconds\n3 examples, 0 failures\n", 0)])
        d.render()
        d.update(p, iid=f"w-{i}", resolved=True)
    block = d.render()
    assert "`bundle exec rspec …` works" in block
    assert "bundle run" not in block
