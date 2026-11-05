"""Issue #131: with `"verbose": true` in the user's Claude Code settings, every side call
fails. Under `-p --output-format json`, a verbose Claude Code prints a JSON ARRAY of stream
events (`system`, `assistant`, ..., `result`) instead of the bare result object, so
`claude_call`'s `data.get("result")` raises `AttributeError: 'list' object has no attribute
'get'`. That escapes through the micro-skills proposer (`Distiller._maybe_propose` calls its
runner unguarded, unlike the loop guard's judge) and aborts the whole fold."""
import json

import pytest


def _verbose_array(result="OK", usage=None, model_usage=None):
    """The shape `claude -p --output-format json` prints under the user's `verbose: true`
    (Claude Code 2.1.318): one JSON array of events, the result its last element."""
    events = [{"type": "system", "subtype": "init", "session_id": "s1"},
              {"type": "assistant", "message": {"content": [{"type": "text", "text": result}]}}]
    if usage is not None:
        events.append({"type": "result", "subtype": "success", "result": result,
                       "usage": usage, "modelUsage": model_usage or {}})
    return json.dumps(events)


def _fake_run(events_stdout, helps="  --no-session-persistence\n  -p"):
    """A `claude` whose side-call stdout is `events_stdout`, as the tests in test_live_core
    fake it (the subprocess is faked, not the CLI)."""
    import subprocess
    from tokeneconomizer.plugin import sidecall

    def fake_run(cmd, **kw):
        if cmd[1:] == ["--help"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=helps, stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout=events_stdout, stderr="")
    return fake_run


@pytest.mark.real_sidecalls
def test_a_claude_side_call_reads_the_result_event_from_a_verbose_array(monkeypatch):
    import subprocess
    from tokeneconomizer.plugin import sidecall
    monkeypatch.setattr(sidecall.subprocess, "run",
                        _fake_run(_verbose_array("OK", {"input_tokens": 10, "output_tokens": 1},
                                                 {"claude-haiku-4-5": {}})))
    monkeypatch.setattr(sidecall, "_HELP", {})
    text, eff = sidecall.claude_call("Reply with exactly: OK", cli="new")
    assert text == "OK"
    assert eff > 0


@pytest.mark.real_sidecalls
def test_a_claude_side_call_array_without_a_result_event_comes_back_empty(monkeypatch):
    import subprocess
    from tokeneconomizer.plugin import sidecall
    monkeypatch.setattr(sidecall.subprocess, "run",
                        _fake_run(json.dumps([{"type": "system", "subtype": "init"}])))
    monkeypatch.setattr(sidecall, "_HELP", {})
    text, _eff = sidecall.claude_call("Reply with exactly: OK", cli="new")
    assert text == ""


def test_a_failed_side_call_skips_the_proposal_instead_of_aborting_the_fold():
    """`_maybe_propose` calls its runner unguarded: one failed side call (a `verbose` array,
    a timeout, a CLI that would not run) escaped and aborted the whole fold, so the session
    stayed pending and was re-folded -- with another paid call -- on a later tick."""
    from tokeneconomizer.microskills.distiller import Distiller

    def broken(prompt, model):
        raise AttributeError("'list' object has no attribute 'get'")

    d = Distiller("repo", broken, "haiku")
    d.raw_window = [[("git status", "")]]
    d.window = [{"iid": 1, "edited_files": [], "features": {}}]
    d.now = 10
    d._maybe_propose()                       # must not raise
    # ... and must not be retried on every tick: a failed paid call backs off one cadence
    assert d.last_propose == 10
