"""A command run in a pixi environment is observed as that command, launcher included.

`pixi run` runs a command in a pixi (conda-based) environment after its own options:
`-e`/`--environment`, `-p`/`--platform`, `-m`/`--manifest-path` and the other value options in
pixi's CLI reference take a value. The form keeps them as typed, since the environment is often
what makes the command work. `-n`/`--dry-run` only prints the command, so nothing ran."""
import pytest

from fixtures import write_codex_transcript
from tokeneconomizer.microskills import EnvFactsDistiller, observe
from tokeneconomizer.microskills.envfacts import _repairs


def _seen(cmd):
    return [(o.intent, o.form) for o in observe.classify_command(cmd)]


@pytest.mark.parametrize("cmd, form", [
    ("pixi run pytest -q", "pixi run pytest"),
    ("pixi run python -m pytest tests", "pixi run python -m pytest"),
    ("pixi run -e test pytest", "pixi run -e test pytest"),
    ("pixi run --environment=py312 python -m pytest",
     "pixi run --environment=py312 python -m pytest"),
    ("pixi run --frozen -e test pytest", "pixi run --frozen -e test pytest"),
    ("pixi run -m ./pixi.toml pytest", "pixi run -m ./pixi.toml pytest"),
    ("pixi run -e test -- pytest", "pixi run -e test pytest"),
])
def test_a_test_run_in_a_pixi_environment_is_observed_with_it(cmd, form):
    assert _seen(cmd) == [("run_tests", form)]


@pytest.mark.parametrize("cmd", ["pixi run -n pytest", "pixi run --dry-run pytest",
                                 "pixi run -e test --dry-run pytest", "pixi run -xn pytest"])
def test_a_dry_run_ran_nothing(cmd):
    assert _seen(cmd) == []


def test_other_launchers_are_read_as_before():
    assert _seen("uv run --frozen pytest") == [("run_tests", "uv run --frozen pytest")]
    assert _seen("conda run -n ml pytest") == [("run_tests", "conda run -n ml pytest")]


def test_another_command_in_a_pixi_environment_keeps_the_launcher_too():
    assert _seen("pixi run python train.py") == [("run_interpreter", "pixi run python")]


def test_the_learned_fact_names_the_pixi_environment(tmp_path):
    """Sessions where a bare run fails and the run in the pixi `test` environment passes: the fact
    injected into the next session names that form."""
    def cmd(command, output, code):
        return {"type": "command_execution", "command": f'/bin/bash -lc "{command}"',
                "aggregated_output": output, "exit_code": code}

    d = EnvFactsDistiller("acme/widget")
    for i in range(4):
        p = str(tmp_path / f"t{i}.jsonl")
        write_codex_transcript(p, [
            {"type": "file_change", "changes": [{"path": f"pkg/mod{i}.py", "kind": "update"}]},
            cmd("python -m pytest tests -q", "/usr/bin/python: No module named pytest", 1),
            cmd("pixi run -e test python -m pytest tests -q", "1 passed in 0.2s", 0)])
        d.render()
        d.update(p, iid=f"w-{i}", resolved=True)
    assert "pixi run -e test python -m pytest" in d.render()


def test_pixi_is_a_launcher_word_not_the_tool_a_build_runs():
    """A failing build is repaired only by the same tool spelled another way: `pixi` is the
    launcher, as `conda` and `micromamba` are, so `pixi run ninja` does not repair
    `pixi run make`."""
    assert not _repairs("pixi run ninja", "pixi run make", "run_build")
    assert _repairs("pixi run make -j8", "pixi run make", "run_build")
