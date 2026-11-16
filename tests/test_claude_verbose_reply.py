"""Claude Code with `"verbose": true` in the person's settings (#131).

`claude -p --output-format json` then prints the whole event list instead of one result object.
Every place that reads that output takes the `result` event; a side call that still fails skips
the proposer's tick instead of aborting the fold, and leaves a note in errors.log.
"""
import json
import subprocess

import pytest

from tokeneconomizer.microskills import GENERIC
from tokeneconomizer.microskills import distiller as dist
from tokeneconomizer.plugin import config as live_config, sidecall, toolcheck
from fixtures import write_claude_transcript

RESULT = {"type": "result", "subtype": "success", "is_error": False, "result": "OK",
          "usage": {"input_tokens": 12, "output_tokens": 3},
          "modelUsage": {"claude-haiku-4-5": {}}}
# What Claude Code 2.1.318 printed in the report: system, assistant x2, rate_limit_event, result.
VERBOSE = [{"type": "system", "subtype": "init", "tools": []},
           {"type": "assistant", "message": {"content": [{"type": "text", "text": "OK"}]}},
           {"type": "assistant", "message": {"content": []}},
           {"type": "rate_limit_event"},
           RESULT]


@pytest.mark.parametrize("stdout, expected", [
    (json.dumps(RESULT), RESULT),                              # the usual object
    (json.dumps(VERBOSE), RESULT),                             # the verbose event list
    (json.dumps(VERBOSE[:-1]), {}),                            # a list with no result event
    (json.dumps([RESULT, {"type": "result", "result": "later"}]), {"type": "result",
                                                                   "result": "later"}),
    (json.dumps(["text", 3, None]), {}),                       # no dict at all
    (json.dumps("OK"), {}),                                    # a bare JSON string
    ("not json", {}),
    ("", {}),
    (None, {}),
])
def test_claude_json_reads_the_result_object(stdout, expected):
    assert sidecall.claude_json(stdout) == expected


def _fake_claude(monkeypatch, stdout):
    seen = []

    def run(cmd, **kw):
        seen.append(cmd)
        if "--help" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(sidecall.subprocess, "run", run)
    sidecall._HELP.clear()
    return seen


@pytest.mark.real_sidecalls
def test_a_verbose_side_call_returns_its_reply_and_usage(monkeypatch):
    _fake_claude(monkeypatch, json.dumps(VERBOSE))
    text, eff = sidecall.claude_call("Reply with exactly: OK", cli="claude")
    assert text == "OK" and eff and eff > 0


@pytest.mark.real_sidecalls
def test_a_verbose_side_call_without_a_result_is_no_reply(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENECONOMIZER_HOME", str(tmp_path))
    _fake_claude(monkeypatch, json.dumps(VERBOSE[:-1]))
    text, _ = sidecall.claude_call("hi", cli="claude")
    assert text == ""
    assert "sidecall claude: no reply" in (tmp_path / "errors.log").read_text(encoding="utf-8")


def test_the_measure_probe_reads_a_verbose_reply(monkeypatch):
    def run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(
            [{"type": "system"}, dict(RESULT, usage={"input_tokens": 100}, total_cost_usd=0.01)]),
            stderr="")

    monkeypatch.setattr(toolcheck.subprocess, "run", run)
    monkeypatch.setattr(toolcheck, "claude_flags", lambda cli=None: [])
    assert toolcheck._prompt_tokens([], cli="claude") == (100, 0.01)


def _distiller(tmp_path, monkeypatch, runner):
    monkeypatch.setattr(dist, "PROPOSE_EVERY", 1)
    return dist.Distiller.load("acme/tool", str(tmp_path / "state"), runner=runner,
                               vocab=GENERIC)


def _session(tmp_path, n):
    return write_claude_transcript(str(tmp_path / f"s{n}.jsonl"), [
        ("Bash", {"command": "make build"}, "built"),
        ("Bash", {"command": "make test"}, "ok  ./... 0.01s")])


def test_a_failed_proposer_call_skips_the_tick_instead_of_the_fold(tmp_path, monkeypatch):
    calls = []

    def runner(prompt, model):
        calls.append(prompt)
        raise subprocess.TimeoutExpired("claude", 180)

    d = _distiller(tmp_path, monkeypatch, runner)
    d.update(_session(tmp_path, 0), iid="s0")              # does not raise
    d.save(str(tmp_path / "state"))
    assert len(calls) == 1 and d.facts == {} and d.opt_tokens == 0
    # The failure counts as the tick, so a CLI that keeps failing is not called at every fold.
    assert d.last_propose == d.now


def test_the_next_tick_after_a_failure_proposes_as_usual(tmp_path, monkeypatch):
    replies = iter([RuntimeError("boom"), ('{"extractors": [], "facts": []}', 40.0)])

    def runner(prompt, model):
        r = next(replies)
        if isinstance(r, Exception):
            raise r
        return r

    d = _distiller(tmp_path, monkeypatch, runner)
    d.update(_session(tmp_path, 0), iid="s0")
    d.update(_session(tmp_path, 1), iid="s1")
    assert d.opt_tokens == 40.0 and d.last_propose == d.now


@pytest.mark.real_sidecalls
@pytest.mark.parametrize("stdout", [
    json.dumps(dict(RESULT, is_error=True, result="Invalid API key · Please run /login")),
    json.dumps(VERBOSE[:-1] + [dict(RESULT, is_error=True, result="Please run /login")]),
])
def test_an_error_result_is_no_reply_and_pauses_a_signed_out_host(monkeypatch, tmp_path, stdout):
    monkeypatch.setenv("TOKENECONOMIZER_HOME", str(tmp_path))
    _fake_claude(monkeypatch, stdout)
    text, _ = sidecall.claude_call("hi", cli="claude")
    assert text == ""                                   # the error is not the proposer's reply
    assert sidecall.paused("claude", str(tmp_path))


@pytest.mark.parametrize("exc, line", [
    (subprocess.TimeoutExpired(["grok", "-p", "SECRET prompt"], 180), "timed out after 180s"),
    (subprocess.CalledProcessError(2, ["grok", "-p", "SECRET prompt"]), "exit 2"),
    (RuntimeError("boom\nmore"), "RuntimeError: boom more"),
])
def test_a_side_call_that_raises_is_noted_in_errors_log(tmp_path, monkeypatch, exc, line):
    monkeypatch.setenv("TOKENECONOMIZER_HOME", str(tmp_path))

    def boom(*a, **k):
        raise exc

    monkeypatch.setattr(sidecall, "grok_call", boom)
    runner = sidecall.runner_for("grok", live_config.LiveConfig())
    with pytest.raises(type(exc)):
        runner("SECRET prompt")
    log = (tmp_path / "errors.log").read_text(encoding="utf-8")
    assert log.startswith("--- ") and "sidecall grok:" in log and line in log
    # One line, and never the command: the prompt is on grok's argv, and `te doctor` prints this.
    assert log.count("\n") == 1 and "SECRET" not in log


def test_a_side_call_that_answers_is_passed_through(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENECONOMIZER_HOME", str(tmp_path))
    monkeypatch.setattr(sidecall, "codex_call", lambda prompt, model="": ("hi", 7.0))
    assert sidecall.runner_for("codex", live_config.LiveConfig())("q", "agent") == ("hi", 7.0)
    assert not (tmp_path / "errors.log").exists()
