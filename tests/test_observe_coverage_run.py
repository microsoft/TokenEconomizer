"""A test run measured by coverage.py is observed as a test run.

`coverage run -m pytest`, `python -m coverage run -m pytest` and Django's documented
`coverage run manage.py test` run the tests under coverage measurement. The loop guard counts them
as verifications; the observer records them as test runs too, in a form that keeps coverage, so
a learned fact names the command that works."""
import pytest

from fixtures import write_codex_transcript
from tokeneconomizer.microskills import EnvFactsDistiller, observe


def _seen(cmd):
    return [(o.intent, o.form) for o in observe.classify_command(cmd)]


@pytest.mark.parametrize("cmd, form", [
    ("coverage run -m pytest", "coverage run -m pytest"),
    ("coverage run -m pytest -q tests", "coverage run -m pytest"),
    ("coverage run --branch --source=pkg -m pytest", "coverage run -m pytest"),
    ("coverage run --rcfile .coveragerc -m unittest discover", "coverage run -m unittest"),
    ("python -m coverage run -m pytest", "python -m coverage run -m pytest"),
    ("python3 -m coverage run -p -m pytest", "python3 -m coverage run -m pytest"),
    ("coverage run manage.py test", "coverage run manage.py"),
    ("coverage run tests/runtests.py", "coverage run tests/runtests.py"),
    (".venv/bin/coverage run -m pytest", ".venv/bin/coverage run -m pytest"),
    ("uv run coverage run -m pytest", "uv run coverage run -m pytest"),
])
def test_a_test_run_under_coverage_is_observed_as_a_test_run(cmd, form):
    assert _seen(cmd) == [("run_tests", form)]


@pytest.mark.parametrize("cmd, seen", [
    ("coverage run repro.py", []),
    ("coverage report -m", []),
    ("coverage run manage.py migrate", []),
    ("python -m coverage report", [("run_interpreter", "python")]),
])
def test_coverage_of_anything_else_is_read_as_before(cmd, seen):
    assert _seen(cmd) == seen


def test_the_learned_fact_names_the_coverage_run(tmp_path):
    """Sessions where `pytest` is not on PATH and the run under coverage passes: the fact injected
    into the next session names the coverage form."""
    def cmd(command, output, code):
        return {"type": "command_execution", "command": f'/bin/bash -lc "{command}"',
                "aggregated_output": output, "exit_code": code}

    d = EnvFactsDistiller("acme/widget")
    for i in range(4):
        p = str(tmp_path / f"t{i}.jsonl")
        write_codex_transcript(p, [
            {"type": "file_change", "changes": [{"path": f"pkg/mod{i}.py", "kind": "update"}]},
            cmd("pytest tests -q", "bash: pytest: command not found", 127),
            cmd("python -m coverage run -m pytest tests -q", "1 passed in 0.2s", 0)])
        d.render()
        d.update(p, iid=f"w-{i}", resolved=True)
    assert "python -m coverage run -m pytest" in d.render()
