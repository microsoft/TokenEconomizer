"""History backfill end to end, through the real hook entry point.

A synthetic `CLAUDE_CONFIG_DIR` holds three old Claude Code transcripts across two git
repositories (each with an origin remote) and one fresh one. The first SessionStart folds
exactly the three old sessions, oldest first; a second start folds nothing and shows the
one-time notice; a third says nothing.
"""
import io
import json
import os
import time

from tests.test_backfill import git_repo, history_file, old

from tokeneconomizer.plugin import hook
from tokeneconomizer.plugin.paths import repo_identity, repo_state_dir
from tokeneconomizer.plugin.store import Store


def make_env(tmp_path):
    return {"TOKENECONOMIZER_HOME": str(tmp_path / "te"),
            "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"),
            "HOME": str(tmp_path / "home"),
            "GIT_CEILING_DIRECTORIES": str(tmp_path),
            "TE_FOLD_SYNC": "1",
            # A deny list from a LIVE fold would be written at once; a backfill fold must not.
            "TE_TOOLPRUNE_QUIET_MINUTES": "0",
            "PATH": os.environ.get("PATH", "")}


def call(env, payload):
    out = io.StringIO()
    hook.main(["--host", "claude"], io.StringIO(json.dumps(payload)), out, env)
    return json.loads(out.getvalue().splitlines()[-1])


def state(env, repo):
    return Store(repo_state_dir(repo_identity(repo, env), "claude", env))


def session(env, repo, sid, *, calls, first):
    return history_file(env, sid, project="project", cwd=repo, first=first, calls=calls)


def calls_for(repo):
    return [("Edit", {"file_path": os.path.join(repo, "pkg", "a.py"), "old_string": "x",
                      "new_string": "y"}, "ok", False),
            ("Bash", {"command": "pytest -q"}, "1 passed in 0.1s", False)]


def start(env, sid, repo):
    return call(env, {"hook_event_name": "SessionStart", "session_id": sid,
                      "source": "startup", "transcript_path": "", "cwd": repo})


def test_first_session_start_folds_history_then_says_so_once(tmp_path):
    alpha = git_repo(tmp_path / "alpha", "https://github.com/o/alpha")
    beta = git_repo(tmp_path / "beta", "https://github.com/o/beta")
    env = make_env(tmp_path)
    now = time.time()
    base = now - 10_000

    s1 = session(env, alpha, "s1", calls=calls_for(alpha), first=base)
    s2 = session(env, alpha, "s2", calls=calls_for(alpha), first=base + 1000)
    s3 = session(env, beta, "s3", calls=calls_for(beta), first=base + 2000)
    fresh = session(env, alpha, "fresh", calls=calls_for(alpha), first=base + 3000)
    old(s1, now, 4000)
    old(s2, now, 3000)
    old(s3, now, 2000)
    old(fresh, now, 30)                     # a live or just-ended session

    first = start(env, "live1", alpha)
    assert "learned from" not in first.get("systemMessage", "")
    a, b = state(env, alpha), state(env, beta)
    assert set(a.read("folded.json")) == {"s1", "s2"}       # the fresh one is not history
    assert set(b.read("folded.json")) == {"s3"}
    assert "fresh" not in a.read("folded.json")

    # Exactly the three old sessions, each marked as a backfill, oldest first per repository.
    led_a = a.read_lines("ledger.jsonl")
    led_b = b.read_lines("ledger.jsonl")
    assert [r["session_id"] for r in led_a] == ["s1", "s2"]
    assert [r["session_id"] for r in led_b] == ["s3"]
    assert all(r.get("backfill") is True for r in led_a + led_b)
    assert led_a[0]["started"] < led_a[1]["started"]
    assert len(led_a) + len(led_b) == 3

    # History fed tool-pruning evidence and the loop guard's envelope...
    assert a.read("untool.json")["sessions"] == 2
    assert b.read("untool.json")["sessions"] == 1
    assert a.read("envelope.json")["resolved_turns"] == [2, 2]
    assert b.read("envelope.json")["resolved_turns"] == [2]
    # ...and applied nothing: the free fold left no deny list behind.
    assert not os.path.exists(os.path.join(alpha, ".claude", "settings.local.json"))
    assert not os.path.exists(os.path.join(beta, ".claude", "settings.local.json"))

    doc = json.load(open(os.path.join(env["TOKENECONOMIZER_HOME"], "backfill.json")))
    assert doc["claude"]["state"] == "done"
    assert len(doc["claude"]["repos"]) == 2
    assert sum(r["sessions"] for r in doc["claude"]["repos"].values()) == 3

    # The next start shows the notice once, folds nothing, and never says it again.
    second = start(env, "live2", beta)
    assert "learned from 3 earlier Claude Code session(s) in 2 repositories" in \
        second["systemMessage"]
    assert "/tokeneconomizer:status" in second["systemMessage"]
    assert len(a.read_lines("ledger.jsonl")) + len(b.read_lines("ledger.jsonl")) == 3
    third = start(env, "live3", alpha)
    assert "systemMessage" not in third
    assert not os.path.exists(os.path.join(env["TOKENECONOMIZER_HOME"],
                                           ".backfilling-claude"))


def test_shadow_mode_does_not_use_up_the_notice(tmp_path):
    repo = git_repo(tmp_path / "repo", "https://github.com/o/r")
    env = make_env(tmp_path)
    path = os.path.join(env["TOKENECONOMIZER_HOME"], "backfill.json")
    Store(env["TOKENECONOMIZER_HOME"]).write("backfill.json", {"claude": {
        "state": "done", "noticed": False,
        "repos": {"r": {"label": "o/r", "sessions": 2, "skipped": {}}}}})

    out = start(dict(env, TE_SHADOW="1"), "s1", repo)
    assert "systemMessage" not in out
    assert json.load(open(path))["claude"]["noticed"] is False

    out = start(env, "s2", repo)
    assert "learned from 2 earlier Claude Code session(s)" in out["systemMessage"]
    assert json.load(open(path))["claude"]["noticed"] is True
