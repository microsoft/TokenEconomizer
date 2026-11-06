"""Verbose Claude Code streams an event array, not one result object (#131).

With "verbose": true in the user's settings, `claude -p --output-format json`
prints a JSON array of events, and claude_call's data.get("result") raised
AttributeError on the list. The micro-skills proposer then let the exception
escape, aborting the whole fold. Both halves are pinned here.
"""
import json
from types import SimpleNamespace

import pytest

from tokeneconomizer.microskills import GENERIC
from tokeneconomizer.microskills import distiller as dist
from tokeneconomizer.plugin import sidecall
from fixtures import write_claude_transcript


def _verbose_stdout(result_text):
    events = [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {}},
        {"type": "result", "result": result_text,
         "usage": {"input_tokens": 10, "output_tokens": 5}},
    ]
    return json.dumps(events)


@pytest.mark.real_sidecalls
def test_claude_call_accepts_verbose_event_array(monkeypatch):
    def fake_run(cmd, **kw):
        if "--help" in cmd:
            return SimpleNamespace(stdout="")
        return SimpleNamespace(stdout=_verbose_stdout("hello"), stderr="")

    monkeypatch.setattr(sidecall.subprocess, "run", fake_run)
    text, eff = sidecall.claude_call("hi")
    assert text == "hello"
    assert eff is not None


@pytest.mark.real_sidecalls
def test_claude_call_ignores_events_without_a_result(monkeypatch):
    # No result event at all: no reply, not a crash.
    def fake_run(cmd, **kw):
        if "--help" in cmd:
            return SimpleNamespace(stdout="")
        return SimpleNamespace(
            stdout=json.dumps([{"type": "system", "subtype": "init"}]), stderr="")

    monkeypatch.setattr(sidecall.subprocess, "run", fake_run)
    text, eff = sidecall.claude_call("hi")
    assert text == ""
    assert eff is None


def test_failed_proposer_call_skips_proposal_instead_of_aborting_fold(tmp_path, monkeypatch):
    monkeypatch.setattr(dist, "PROPOSE_EVERY", 1)

    calls = []

    def runner(prompt, model):
        calls.append(prompt)
        raise RuntimeError("side call failed")

    root = str(tmp_path / "state")
    d = dist.Distiller.load("acme/tool", root, runner=runner, vocab=GENERIC)
    path = write_claude_transcript(str(tmp_path / "s0.jsonl"), [
        ("Bash", {"command": "make build"}, "built"),
        ("Bash", {"command": "make test"}, "ok  ./... 0.01s")])
    d.update(path, iid="s0")  # must not raise
    d.save(root)
    assert d.facts == {}
    # The failed call was made once and counts as the tick: the next waits its sessions.
    assert len(calls) == 1 and d.last_propose == d.now


def test_a_failed_proposer_call_is_noted_in_the_errors_log(tmp_path):
    from tokeneconomizer.plugin import fold

    def runner(prompt, model):
        raise RuntimeError("side call failed")

    call = fold._noted(str(tmp_path), runner)
    with pytest.raises(RuntimeError):
        call("a prompt", "agent")
    log = (tmp_path / "errors.log").read_text(encoding="utf-8")
    assert "(proposer side call)" in log and "RuntimeError: side call failed" in log
