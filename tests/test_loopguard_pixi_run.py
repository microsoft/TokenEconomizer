"""A test run in a pixi environment is a verification.

`pixi run` runs a command in a pixi (conda-based) environment, as `conda run` and `uv run` do in
theirs, after its own options: `-e`/`--environment`, `-p`/`--platform`, `-m`/`--manifest-path`
and the other value options in pixi's CLI reference take a value. `-n`/`--dry-run` only prints the
command, so it runs no tests."""
import pytest

from tokeneconomizer import loopguard as guard
from tokeneconomizer.loopguard import turn


@pytest.mark.parametrize("cmd", ["pixi run pytest -q", "pixi run python -m pytest tests",
                                 "pixi run -e test pytest", "pixi run --environment=py312 pytest",
                                 "pixi run --frozen -e test python -m pytest",
                                 "pixi run --manifest-path ./pixi.toml pytest",
                                 "pixi run -m pyproject.toml --locked pytest",
                                 "pixi run -x --clean-env pytest"])
def test_a_runner_in_a_pixi_environment_counts_as_a_verification(cmd):
    assert turn.status_of_tests(cmd) == "own"
    g = guard.TurnGuard(turn_key="t")
    g.fold("Edit", {"file_path": "/r/a.py", "old_string": "x", "new_string": "y"}, "ok")
    g.fold("Bash", {"command": cmd}, "FAILED (failures=1)", 1)
    assert g.verifications == 1 and not g.resolved_proxy()


@pytest.mark.parametrize("cmd", ["pixi run -n pytest", "pixi run --dry-run pytest",
                                 "pixi run -xn pytest", "pixi run -nx pytest",
                                 "pixi run python train.py", "pixi run -e test python train.py",
                                 "pixi install", "pixi add pytest",
                                 "pixi run pytest --collect-only"])
def test_pixi_leaves_what_it_runs_to_decide(cmd):
    assert turn.status_of_tests(cmd) is None
