"""Backfill: a repository's first session folds the Claude Code sessions it ran before the plugin
was installed, oldest first, once; later sessions keep learning as before."""
import json
import os
import time

from tokeneconomizer.plugin import backfill
from tokeneconomizer.plugin.context import LiveContext
from tokeneconomizer.plugin import hosts

from test_live_hooks import call, env, repo, run_session, store_for, trap_session  # noqa: F401


def _history(tmp_path, repo, sids, cwd=None, age_s=3600):
    """Claude Code logs under `<config>/projects/<encoded>/<sid>.jsonl`, with the cwd and
    timestamps a real log's records carry."""
    proj = tmp_path / "claude" / "projects" / "-encoded-dir"
    proj.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, sid in enumerate(sids):
        p = str(proj / f"{sid}.jsonl")
        trap_session(p, repo, i)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(1_700_000_000 + i * 60))
        with open(p) as fh:
            recs = [json.loads(ln) for ln in fh]
        for r in recs:
            r.update(sessionId=sid, cwd=cwd or repo, timestamp=stamp)
        with open(p, "w") as fh:
            fh.write("\n".join(json.dumps(r) for r in recs) + "\n")
        old = time.time() - age_s
        os.utime(p, (old, old))
        paths.append(p)
    return paths


def test_first_session_folds_earlier_history_once(env, repo, tmp_path):
    env["CLAUDE_CONFIG_DIR"] = str(tmp_path / "claude")
    _history(tmp_path, repo, ["old1", "old2", "old3"])
    _history(tmp_path, str(tmp_path / "elsewhere"), ["other"], cwd=str(tmp_path / "elsewhere"))
    run_session(env, repo, "now", trap_session(str(tmp_path / "now.jsonl"), repo, 9))
    st = store_for(env, repo)
    folded = st.read("folded.json")
    assert {"old1", "old2", "old3", "now"} <= set(folded) and "other" not in folded
    ids = [r["session_id"] for r in st.read_lines("ledger.jsonl")]
    assert ids.index("old1") < ids.index("old2") < ids.index("old3")
    assert st.read("backfill.json")["folded"] == 3
    # The facts learned from history reach the next session.
    start = run_session(env, repo, "next", trap_session(str(tmp_path / "n.jsonl"), repo, 8))
    assert "python -m pytest" in json.dumps(start)
    assert len(st.read_lines("ledger.jsonl")) == 5         # nothing folded twice


def test_backfill_skips_a_live_log_and_respects_the_cap(env, repo, tmp_path):
    env["CLAUDE_CONFIG_DIR"] = str(tmp_path / "claude")
    env["TE_BACKFILL_SESSIONS"] = "2"
    _history(tmp_path, repo, ["a", "b", "c"])
    _history(tmp_path, repo, ["live"], age_s=0)
    ctx = LiveContext.build(hosts.HOSTS["claude"](), repo, env)
    assert [sid for _, sid, _, _ in backfill.history(ctx)] == ["b", "c"]


def test_backfill_off_and_other_agents_read_nothing(env, repo, tmp_path):
    env["CLAUDE_CONFIG_DIR"] = str(tmp_path / "claude")
    _history(tmp_path, repo, ["a"])
    off = dict(env, TE_BACKFILL="0")
    assert not backfill.due(LiveContext.build(hosts.HOSTS["claude"](), repo, off))
    assert backfill.history(LiveContext.build(hosts.HOSTS["codex"](), repo, env)) == []
