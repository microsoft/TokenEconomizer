"""`te savings`: what tool pruning leaves out, the measurement of that exact list, and the
sessions recorded with it. It prints no estimate of what sessions would have cost, and never
measures by itself."""
import pytest

from tokeneconomizer.plugin import accounting, cli, denylist, toolcheck


def _session(requests, applied):
    return {"kind": "session", "requests": requests, "untool_applied": applied}


def test_only_sessions_recorded_with_exactly_this_list_count():
    records = [_session(100, ["B", "A"]), _session(50, ["A"]), _session(7, []),
               {"kind": "overhead"}]
    assert accounting.sessions_with(records, ["A", "B"]) == (1, 100)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENECONOMIZER_HOME", str(tmp_path / "home"))
    work = tmp_path / "work"
    work.mkdir()
    ctx = cli._ctx(cli.build_parser().parse_args(["--cwd", str(work), "--host", "claude", "savings"]))
    return work, ctx.store


@pytest.fixture
def no_model_calls(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("savings must not measure")
    monkeypatch.setattr(toolcheck, "measure", refuse)


def _run(work, capsys, *argv):
    code = cli.main(["--cwd", str(work), "--host", "claude", *(argv or ["savings"])])
    return code, capsys.readouterr().out


def test_nothing_applied_says_nothing_is_left_out(repo, no_model_calls, capsys):
    work, _ = repo
    code, out = _run(work, capsys)
    assert code == 0 and out.startswith("Tool pruning has not left anything out")


def test_unmeasured_list_names_the_command_and_its_cost_and_does_not_run_it(
        repo, no_model_calls, monkeypatch, capsys):
    work, _ = repo
    monkeypatch.setattr(denylist, "applied_for", lambda *a: ["A", "B"])
    code, out = _run(work, capsys)
    assert code == 0
    assert "te toolprune measure --applied" in out and "paid `claude -p`" in out


def test_measured_list_prints_three_lines_of_fact_and_no_saving(
        repo, no_model_calls, monkeypatch, capsys):
    work, store = repo
    monkeypatch.setattr(denylist, "applied_for", lambda *a: ["A", "B"])
    store.write(cli.MEASURED, {"claude:A,B": {"per_request": 4000, "estimated": False,
                                              "measured_on": "2026-10-30"}})
    store.append("ledger.jsonl", _session(1000, ["A", "B"]))
    code, out = _run(work, capsys)
    lines = out.strip().splitlines()
    assert code == 0 and lines == [
        "Tool pruning leaves 2 tools out of this repository's sessions.",
        "Measured on 2026-10-30: one request without them is 4.0k prompt tokens smaller.",
        "Sessions recorded with this list: 1 (1,000 requests).",
    ]
    assert "saved" not in out.lower()


def test_a_changed_deny_list_is_not_read_with_the_old_measurement(
        repo, no_model_calls, monkeypatch, capsys):
    work, store = repo
    store.write(cli.MEASURED, {"claude:A": {"per_request": 4000, "estimated": False,
                                            "measured_on": "2026-10-30"}})
    monkeypatch.setattr(denylist, "applied_for", lambda *a: ["A", "B"])
    code, out = _run(work, capsys)
    assert "has not been measured" in out


def test_measure_applied_measures_the_applied_list_even_with_a_proposal_open(
        repo, monkeypatch, capsys):
    work, store = repo
    monkeypatch.setattr(denylist, "applied_for", lambda *a: ["A", "B"])
    monkeypatch.setattr(cli, "_proposal", lambda *a: ["C"])
    seen = []

    def fake_measure(tools, host="claude"):
        seen.append(sorted(tools))
        return {"saved_per_request": 1000 * len(tools)}
    monkeypatch.setattr(toolcheck, "measure", fake_measure)

    _run(work, capsys, "toolprune", "measure")
    _run(work, capsys, "toolprune", "measure", "--applied")
    assert seen == [["C"], ["A", "B"]]
    # Measuring the proposal did not replace the applied list's measurement.
    assert set(store.read(cli.MEASURED)) == {"claude:C", "claude:A,B"}
    code, out = _run(work, capsys)
    assert "2.0k prompt tokens smaller" in out


def test_an_estimate_from_characters_says_so(repo, monkeypatch, capsys):
    work, store = repo
    monkeypatch.setattr(denylist, "applied_for", lambda *a: ["A"])
    monkeypatch.setattr(toolcheck, "measure",
                        lambda tools, host="claude": {"saved_tokens_per_request_est": 500})
    _run(work, capsys, "toolprune", "measure", "--applied")
    code, out = _run(work, capsys)
    assert "about 500 tokens smaller (estimated from its size in characters)" in out
