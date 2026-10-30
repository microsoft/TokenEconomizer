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
def _no_real_claude_history(tmp_path_factory, monkeypatch):
    """No test reads the developer's own `~/.claude` history: the plugin now folds earlier
    Claude Code sessions in at session start, and the tests use synthetic ones only."""
    config = tmp_path_factory.mktemp("claude-config")
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    # The commands build their environment from explicit dicts that may omit the variable;
    # an empty home keeps `~/.claude` out of reach for those too.
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))


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
