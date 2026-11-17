"""A command run in a conda environment is observed as that command, in that environment.

`conda run` names its environment by name (`-n ml`) or by path (`-p ./env`, common for an
environment kept in the repository), and micromamba takes the same options. The form keeps the
launcher and the environment, so a learned fact tells the next session the command that works."""
import pytest

from fixtures import write_codex_transcript
from tokeneconomizer.microskills import EnvFactsDistiller, observe


def _seen(cmd):
    return [(o.intent, o.form) for o in observe.classify_command(cmd)]


@pytest.mark.parametrize("cmd, form", [
    ("conda run -n ml pytest -q", "conda run -n ml pytest"),
    ("mamba run -n ml pytest", "conda run -n ml pytest"),
    ("conda run -p ./env pytest", "conda run -p ./env pytest"),
    ("conda run --prefix /opt/envs/ml python -m pytest tests",
     "conda run -p /opt/envs/ml python -m pytest"),
    ("conda run --no-capture-output --prefix=./env pytest", "conda run -p ./env pytest"),
    ("micromamba run -n ml pytest", "micromamba run -n ml pytest"),
    ("micromamba run -p ./env python -m pytest", "micromamba run -p ./env python -m pytest"),
])
def test_a_test_run_in_a_conda_environment_keeps_its_environment(cmd, form):
    assert _seen(cmd) == [("run_tests", form)]


def test_a_prefixed_environment_is_not_a_package_tool_call():
    assert [i for i, _ in _seen("conda run -p ./env pytest")] == ["run_tests"]
    assert _seen("conda install -n ml pytest") == [("package_tool", "conda")]


def test_the_learned_fact_names_the_prefixed_environment(tmp_path):
    """Sessions where a bare run fails and the run in the repository's prefixed environment
    works: the fact injected into the next session names that environment's form."""
    def cmd(command, output, code):
        return {"type": "command_execution", "command": f'/bin/bash -lc "{command}"',
                "aggregated_output": output, "exit_code": code}

    d = EnvFactsDistiller("acme/widget")
    for i in range(4):
        p = str(tmp_path / f"t{i}.jsonl")
        write_codex_transcript(p, [
            {"type": "file_change", "changes": [{"path": f"pkg/mod{i}.py", "kind": "update"}]},
            cmd("python -m pytest tests -q", "/usr/bin/python: No module named pytest", 1),
            cmd("conda run -p ./env python -m pytest tests -q", "1 passed in 0.2s", 0)])
        d.render()
        d.update(p, iid=f"w-{i}", resolved=True)
    assert "conda run -p ./env python -m pytest" in d.render()
