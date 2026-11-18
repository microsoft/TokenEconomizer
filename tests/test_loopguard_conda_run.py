"""A test run started in a conda environment is a verification.

`conda run -n <env>` (and `mamba run`, `micromamba run`) runs a command in a named or prefixed
environment, as `uv run` and `poetry run` do in theirs. The micro-skills observer already learns
and injects that form (`conda run -n <env> pytest`), so a run the agent types as told must count
as the turn's verification too."""
import pytest

from tokeneconomizer import loopguard as guard
from tokeneconomizer.loopguard import turn


@pytest.mark.parametrize("cmd", ["conda run -n ml pytest -q",
                                 "conda run -n ml python -m pytest tests",
                                 "conda run --no-capture-output -n ml python -m pytest",
                                 "conda run --name ml --live-stream pytest",
                                 "conda run -p ./env pytest", "conda run --prefix=/opt/env pytest",
                                 "conda run --cwd src -n ml python -m unittest",
                                 "mamba run -n ml pytest", "micromamba run -n ml pytest",
                                 "micromamba run -r /opt/mamba -n ml pytest",
                                 "conda run pytest"])
def test_a_runner_in_a_conda_environment_counts_as_a_verification(cmd):
    assert turn.status_of_tests(cmd) == "own"
    g = guard.TurnGuard(turn_key="t")
    g.fold("Edit", {"file_path": "/r/a.py", "old_string": "x", "new_string": "y"}, "ok")
    g.fold("Bash", {"command": cmd}, "FAILED (failures=1)", 1)
    assert g.verifications == 1 and not g.resolved_proxy()


@pytest.mark.parametrize("cmd", ["conda run -n ml python train.py", "conda install -n ml pytest",
                                 "conda run -n ml pytest --collect-only",
                                 "micromamba create -n ml pytest"])
def test_conda_leaves_what_it_runs_to_decide(cmd):
    assert turn.status_of_tests(cmd) is None
