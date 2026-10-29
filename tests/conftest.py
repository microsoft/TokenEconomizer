"""pytest fixtures shared across the offline suite."""
import os
import sys

import pytest

_TESTS = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_TESTS)
for p in (_REPO, _TESTS):
    if p not in sys.path:
        sys.path.insert(0, p)


def symlink_or_skip(src, dst):
    """`os.symlink`, or skip where this system makes none (Windows without Developer Mode)."""
    try:
        os.symlink(src, dst)
    except OSError as e:
        pytest.skip(f"this system makes no symlinks: {e}")


def pytest_configure(config):
    config.addinivalue_line("markers", "real_sidecalls: test the side-call functions themselves "
                                       "(their subprocess is faked inside the test)")


@pytest.fixture(autouse=True)
def _no_model_calls(request, monkeypatch):
    """No test reaches a real agent CLI. With the loop guard on, a stuck-looking turn wakes its
    judge: the plugin's side calls answer nothing here, and a judge job runs inline instead of
    as a detached process. A test that wants a verdict fakes the judge (`stuck_judge`)."""
    from tokeneconomizer.plugin import judge, sidecall
    monkeypatch.setattr(judge, "spawn_job", judge.run_job)
    if request.node.get_closest_marker("real_sidecalls"):
        return
    for name in ("claude_call", "codex_call", "copilot_call", "pi_call"):
        monkeypatch.setattr(sidecall, name, lambda *a, **k: ("", None))


@pytest.fixture(autouse=True)
def _no_real_history(tmp_path, monkeypatch):
    """No test reads the machine's own Claude Code history: a context without
    `CLAUDE_CONFIG_DIR` finds an empty one under the test's directory."""
    from tokeneconomizer.plugin import backfill
    real = backfill.claude_projects_dir
    empty = str(tmp_path / "no-claude-history")
    monkeypatch.setattr(backfill, "claude_projects_dir",
                        lambda env: real(env) if env.get("CLAUDE_CONFIG_DIR") else empty)
