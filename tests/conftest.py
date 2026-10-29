"""pytest fixtures shared across the offline suite."""
import os
import sys

import pytest

_TESTS = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_TESTS)
for p in (_REPO, _TESTS):
    if p not in sys.path:
        sys.path.insert(0, p)


def pytest_configure(config):
    config.addinivalue_line("markers", "real_history: read the agent's session history (backfill); "
                                       "every other test sees none, so it never reads the "
                                       "developer's own logs")
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
def _no_session_history(request, monkeypatch):
    """A hook's first session start would read the machine's own ~/.claude logs (`backfill`)."""
    if request.node.get_closest_marker("real_history"):
        return
    from tokeneconomizer.plugin import backfill
    monkeypatch.setattr(backfill, "history_dir", lambda host, env=None: None)
