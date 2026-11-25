"""A command a Python project launcher runs is observed as that command, launcher included.

`uv run --frozen pytest`, `uv run --extra test pytest` and `uv run --with pytest python -m pytest`
put uv's own options between `run` and the command, and pdm, hatch and rye run commands the way
poetry and pipenv do (the loop guard passes over all six). The form keeps the options as typed,
since an extra or a group is often what makes the command work."""
import pytest

from fixtures import write_codex_transcript
from tokeneconomizer.microskills import EnvFactsDistiller, observe


def _seen(cmd):
    return [(o.intent, o.form) for o in observe.classify_command(cmd)]


@pytest.mark.parametrize("cmd, form", [
    ("uv run pytest -q", "uv run pytest"),
    ("poetry run pytest", "poetry run pytest"),
    ("uv run --frozen pytest -q", "uv run --frozen pytest"),
    ("uv run --extra test pytest tests", "uv run --extra test pytest"),
    ("uv run --group dev --frozen python -m pytest", "uv run --group dev --frozen python -m pytest"),
    ("uv run --with pytest python -m pytest", "uv run --with pytest python -m pytest"),
    ("uv run --python 3.12 pytest", "uv run --python 3.12 pytest"),
    ("uv run --all-extras -- pytest", "uv run --all-extras pytest"),
    ("uv run -- pytest", "uv run pytest"),
    ("pdm run pytest", "pdm run pytest"),
    ("pdm run python -m pytest", "pdm run python -m pytest"),
    ("hatch run pytest", "hatch run pytest"),
    ("rye run pytest", "rye run pytest"),
])
def test_a_test_run_through_a_python_launcher_is_observed_with_it(cmd, form):
    assert _seen(cmd) == [("run_tests", form)]


def test_the_learned_fact_names_the_launcher_with_its_options(tmp_path):
    """Sessions where a bare run fails and `uv run --extra test` passes: the fact injected into
    the next session names the form that worked, extra included."""
    def cmd(command, output, code):
        return {"type": "command_execution", "command": f'/bin/bash -lc "{command}"',
                "aggregated_output": output, "exit_code": code}

    d = EnvFactsDistiller("acme/widget")
    for i in range(4):
        p = str(tmp_path / f"t{i}.jsonl")
        write_codex_transcript(p, [
            {"type": "file_change", "changes": [{"path": f"pkg/mod{i}.py", "kind": "update"}]},
            cmd("python -m pytest tests -q", "/usr/bin/python: No module named pytest", 1),
            cmd("uv run --extra test python -m pytest tests -q", "1 passed in 0.2s", 0)])
        d.render()
        d.update(p, iid=f"w-{i}", resolved=True)
    assert "uv run --extra test python -m pytest" in d.render()
