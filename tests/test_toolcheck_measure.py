"""`te toolprune measure` for Claude Code: what the two `claude -p` probes are started with."""
import json
import subprocess

import pytest

from tokeneconomizer.plugin import toolcheck


def _fake_claude(monkeypatch, seen, stdout=None):
    stdout = json.dumps({"usage": {"input_tokens": 100}}) if stdout is None else stdout

    def run(cmd, **kw):
        rec = {"cmd": cmd}
        if "--settings" in cmd:
            with open(cmd[cmd.index("--settings") + 1], encoding="utf-8") as fh:
                rec["settings"] = json.load(fh)
        seen.append(rec)
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(toolcheck.subprocess, "run", run)
    monkeypatch.setattr(toolcheck, "claude_flags", lambda cli=None: [])


def test_the_lean_probe_takes_the_deny_list_from_a_settings_file(monkeypatch):
    # Hundreds of tool names on argv overflow cmd.exe's 8191-character line when `claude` is the
    # npm `claude.cmd` shim, and the probe then reads no usage at all.
    tools = [f"mcp__server_{i}__some_long_tool_name_{i}" for i in range(400)]
    seen = []
    _fake_claude(monkeypatch, seen)
    toolcheck.measure(tools, cli="claude")
    lean = seen[1]
    assert sum(len(a) for a in lean["cmd"]) < 2000
    assert lean["settings"] == {"permissions": {"deny": tools}}


@pytest.mark.parametrize("stdout", [
    "",                                                     # nothing started
    "The command line is too long.",                        # what cmd.exe printed
    json.dumps({"usage": {}}),
    json.dumps({"is_error": True, "usage": {"input_tokens": 0}}),
])
def test_a_failed_probe_raises_instead_of_counting_zero(monkeypatch, stdout):
    _fake_claude(monkeypatch, [], stdout=stdout)
    with pytest.raises(RuntimeError):
        toolcheck.measure(["mcp__x__y"], cli="claude")


def test_a_probe_that_exits_non_zero_raises_even_with_usage(monkeypatch):
    # A crashed lean probe can still print usage; counting it would report 80 tokens saved.
    replies = iter([(0, 100, ""), (1, 20, "boom")])

    def run(cmd, **kw):
        code, tokens, err = next(replies)
        return subprocess.CompletedProcess(
            cmd, code, stdout=json.dumps({"is_error": False, "usage": {"input_tokens": tokens}}),
            stderr=err)

    monkeypatch.setattr(toolcheck.subprocess, "run", run)
    monkeypatch.setattr(toolcheck, "claude_flags", lambda cli=None: [])
    with pytest.raises(RuntimeError, match="exited 1: boom"):
        toolcheck.measure(["mcp__x__y"], cli="claude")


@pytest.mark.parametrize("lean_stdout", ["", None])
def test_a_codex_probe_that_prints_nothing_raises(monkeypatch, lean_stdout):
    # An empty lean render would otherwise count as 0 characters: the whole request "saved".
    replies = iter(["x" * 1000, lean_stdout])
    monkeypatch.setattr(toolcheck.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0, stdout=next(replies), stderr=""))
    with pytest.raises(RuntimeError):
        toolcheck.measure(["web_search"], cli="codex", host="codex")
