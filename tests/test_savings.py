"""`te savings`: what tool pruning kept out of requests, read from the ledger and the last measure."""
import json

import pytest

from tokeneconomizer.plugin import accounting, cli, denylist


def _session(requests, applied, effective=1_000_000.0):
    return {"kind": "session", "requests": requests, "untool_applied": applied,
            "effective_tokens": effective, "model": "claude-sonnet-5-5", "overhead": {}}


def test_only_sessions_that_ran_with_the_deny_list_count_and_cached_reads_are_priced_as_reads():
    records = [_session(100, ["A", "B"]), _session(50, []), {"kind": "overhead"}]
    won = accounting.tool_pruning_savings(records, per_request=1000)
    assert won["requests"] == 100
    assert won["tokens_kept_out"] == 100_000
    assert won["effective_saved"] == pytest.approx(100_000 * 0.10)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENECONOMIZER_HOME", str(tmp_path / "home"))
    work = tmp_path / "work"
    work.mkdir()
    ctx = cli._ctx(cli.build_parser().parse_args(["--cwd", str(work), "--host", "claude", "savings"]))
    return work, ctx.store


def _run(work, capsys):
    code = cli.main(["--cwd", str(work), "--host", "claude", "savings"])
    return code, capsys.readouterr().out


def test_nothing_applied_says_nothing_saved(repo, capsys):
    work, _ = repo
    code, out = _run(work, capsys)
    assert code == 0 and out.startswith("Nothing saved yet")


def test_applied_but_unmeasured_asks_for_one_measure(repo, monkeypatch, capsys):
    work, store = repo
    monkeypatch.setattr(denylist, "applied_for", lambda *a: ["A", "B"])
    code, out = _run(work, capsys)
    assert code == 1 and "te toolprune measure" in out


def test_measured_prints_three_plain_lines_net_of_overhead(repo, monkeypatch, capsys):
    work, store = repo
    monkeypatch.setattr(denylist, "applied_for", lambda *a: ["A", "B"])
    store.write(cli.MEASURED, {"tools": ["A", "B"], "saved_per_request": 4000})
    for r in (_session(1000, ["A", "B"], effective=9_000_000.0),
              {"kind": "overhead", "overhead": {"spiral_judge_tokens": 100_000.0}}):
        store.append("ledger.jsonl", r)
    code, out = _run(work, capsys)
    lines = out.strip().splitlines()
    assert code == 0 and len(lines) == 3
    # 1000 requests * 4000 tokens * 0.10 = 400k effective, less 100k overhead = 300k
    assert lines[0].startswith("TokenEconomizer saved about 300.0k tokens")
    assert "after its own 100.0k cost" in lines[0]
    assert "4.0k tokens out of each of 1,000 requests (2 unused tools hidden)" in lines[1]
    assert "micro-skills" in lines[2]


def test_a_changed_deny_list_invalidates_the_measurement(repo, monkeypatch, capsys):
    work, store = repo
    store.write(cli.MEASURED, {"tools": ["A"], "saved_per_request": 4000})
    monkeypatch.setattr(denylist, "applied_for", lambda *a: ["A", "B"])
    code, out = _run(work, capsys)
    assert code == 1 and "Not measured yet" in out
