"""Learning from the sessions an agent logged before the plugin was installed (`backfill`)."""
import json
import os
import time

import pytest

from tests.test_live_hooks import call, env, repo, store_for, trap_session  # noqa: F401
from tokeneconomizer.plugin import backfill, cli, fold, hosts
from tokeneconomizer.plugin.context import LiveContext

pytestmark = pytest.mark.real_history


@pytest.fixture
def history(env, tmp_path):
    """The agent's own projects directory, which `CLAUDE_CONFIG_DIR` points the plugin at."""
    env["CLAUDE_CONFIG_DIR"] = str(tmp_path / "claude")
    return os.path.join(env["CLAUDE_CONFIG_DIR"], "projects")


def old_log(history, project, sid, repo_dir, i, age=3600):
    """One earlier session's log, `age` seconds old, filed the way Claude Code files it."""
    d = os.path.join(history, project)
    os.makedirs(d, exist_ok=True)
    path = trap_session(os.path.join(d, f"{sid}.jsonl"), repo_dir, i)
    lines = [json.loads(x) for x in open(path).read().splitlines()]
    lines[0]["cwd"] = repo_dir          # a real log names where the session started
    with open(path, "w") as fh:
        fh.write("\n".join(json.dumps(x) for x in lines) + "\n")
    t = time.time() - age
    os.utime(path, (t, t))
    return path


def start(env, repo, sid="now"):
    return call(env, {"hook_event_name": "SessionStart", "session_id": sid,
                      "source": "startup", "transcript_path": "", "cwd": repo})


def test_the_first_start_learns_from_the_repositorys_earlier_sessions(env, repo, history, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    for i in range(4):
        old_log(history, "proj-a", f"old{i}", repo, i, age=7200 - i)
    old_log(history, "proj-b", "elsewhere", str(other), 9)       # another directory's session
    start(env, repo)
    st = store_for(env, repo)
    folded = st.read("folded.json")
    assert set(folded) == {"old0", "old1", "old2", "old3"}
    assert [r["session_id"] for r in st.read_lines("ledger.jsonl")] == [
        "old0", "old1", "old2", "old3"]                         # oldest first
    assert all(r.get("history") for r in st.read_lines("ledger.jsonl"))
    # The facts the history taught are in the next session's context.
    assert "pytest" in st.read("injection.json")["rendered"]
    assert st.read("backfill.json")["folded"] == 4


def test_a_second_start_does_not_read_the_history_again(env, repo, history):
    old_log(history, "proj", "old0", repo, 0)
    start(env, repo, "a")
    old_log(history, "proj", "late", repo, 1)
    start(env, repo, "b")
    assert "late" not in store_for(env, repo).read("folded.json")
    ctx = LiveContext.build(hosts.ClaudeCode(), repo, env)
    assert backfill.run(ctx)["folded"] == 1                      # `te backfill` adds it
    assert backfill.run(ctx)["folded"] == 0                      # and never twice


def test_a_log_still_being_written_and_the_current_session_are_left_to_the_hooks(
        env, repo, history):
    old_log(history, "proj", "live", repo, 0, age=5)
    old_log(history, "proj", "now", repo, 1)
    start(env, repo, "now")
    assert not store_for(env, repo).read("folded.json")


def test_only_the_newest_sessions_up_to_the_limit_are_read(env, repo, history):
    env["TE_BACKFILL_SESSIONS"] = "2"
    for i in range(5):
        old_log(history, "proj", f"s{i}", repo, i, age=9000 - 100 * i)
    start(env, repo)
    assert set(store_for(env, repo).read("folded.json")) == {"s3", "s4"}


def test_zero_turns_it_off(env, repo, history):
    env["TE_BACKFILL_SESSIONS"] = "0"
    old_log(history, "proj", "s0", repo, 0)
    start(env, repo)
    assert not store_for(env, repo).read("folded.json")
    assert store_for(env, repo).read("backfill.json") is None


def test_history_makes_no_model_call(env, repo, history, monkeypatch):
    env["TE_MICROSKILLS"] = "on"                    # the proposer is on for live folds
    calls = []
    monkeypatch.setattr(fold.sidecall, "runner_for",
                        lambda *a, **k: calls.append(1) or (lambda *x, **y: ("", None)))
    for i in range(4):
        old_log(history, "proj", f"s{i}", repo, i, age=5000 - i)
    start(env, repo)
    assert len(store_for(env, repo).read("folded.json")) == 4 and calls == []


def test_the_cli_reads_the_history_by_hand(env, repo, history, capsys):
    env["TE_BACKFILL_SESSIONS"] = "0"
    old_log(history, "proj", "s0", repo, 0)
    start(env, repo)
    os.environ.update({k: env[k] for k in ("TOKENECONOMIZER_HOME", "TE_FOLD_SYNC",
                                           "CLAUDE_CONFIG_DIR", "GIT_CEILING_DIRECTORIES")})
    try:
        assert cli.main(["--host", "claude", "--cwd", repo, "backfill", "--limit", "5"]) == 0
    finally:
        for k in ("TOKENECONOMIZER_HOME", "TE_FOLD_SYNC", "CLAUDE_CONFIG_DIR",
                  "GIT_CEILING_DIRECTORIES"):
            os.environ.pop(k, None)
    assert "folded 1" in capsys.readouterr().out


def test_other_agents_have_no_history_to_read():
    assert backfill.history_dir("codex", {}) is None
    assert backfill.history_dir("claude", {"CLAUDE_CONFIG_DIR": "/x"}) == os.path.join(
        "/x", "projects")
