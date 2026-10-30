"""Learning from a repository's earlier Claude Code sessions: the history seam, the plan, the
model-free fold, the one-time notice, and the loop guard's envelope seeded from old transcripts.

Offline and synthetic: every transcript is built here, and the tests' history is a tmp
`CLAUDE_CONFIG_DIR`, never the developer's own `~/.claude`.
"""
import json
import os
import time
from datetime import datetime, timezone

import pytest

from tokeneconomizer import loopguard, trace
from tokeneconomizer.plugin import accounting, backfill, fold, hosts, paths, repos, sidecall
from tokeneconomizer.plugin.context import LiveContext
from tokeneconomizer.plugin.paths import repo_identity, repo_state_dir
from tokeneconomizer.plugin.store import Store


# ---- fixtures ---------------------------------------------------------------------------------
def git_repo(root, remote=None):
    root = str(root)
    os.makedirs(os.path.join(root, ".git", "info"), exist_ok=True)
    with open(os.path.join(root, ".git", "HEAD"), "w") as fh:
        fh.write("ref: refs/heads/main\n")
    cfg = "[core]\n\trepositoryformatversion = 0\n"
    if remote:
        cfg += f'[remote "origin"]\n\turl = {remote}\n'
    with open(os.path.join(root, ".git", "config"), "w") as fh:
        fh.write(cfg)
    return root


def worktree(main, path, name="feature"):
    wt_git = os.path.join(main, ".git", "worktrees", name)
    os.makedirs(wt_git)
    with open(os.path.join(wt_git, "commondir"), "w") as fh:
        fh.write("../..")
    os.makedirs(path)
    with open(os.path.join(path, ".git"), "w") as fh:
        fh.write(f"gitdir: {wt_git}\n")
    return str(path)


def stamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def claude_log(path, cwd, calls=(), prompt="fix the bug", first=1_700_000_000.0,
               final_text="Done.", interrupted=False):
    """A Claude Code transcript: one prompt, its tool calls and results, and (unless asked
    otherwise) a final assistant text. Every entry names the directory and a timestamp."""
    tag = os.path.splitext(os.path.basename(path))[0]
    lines = []

    def add(ev):
        ev["cwd"] = cwd
        ev["timestamp"] = stamp(first + len(lines))
        lines.append(ev)

    usage = {"input_tokens": 10, "cache_read_input_tokens": 1000,
             "cache_creation_input_tokens": 100, "output_tokens": 50}
    add({"type": "user", "message": {"role": "user", "content": prompt}})
    for n, (name, inp, result, is_error) in enumerate(calls, 1):
        add({"type": "assistant", "message": {
            "id": f"msg_{tag}_{n}", "model": "claude-opus-5-5", "role": "assistant",
            "usage": usage,
            "content": [{"type": "tool_use", "id": f"tu{tag}{n}", "name": name, "input": inp}]}})
        add({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"tu{tag}{n}", "content": result,
             "is_error": is_error}]}})
    if final_text:
        add({"type": "assistant", "message": {
            "id": f"msg_{tag}_text", "model": "claude-opus-5-5", "role": "assistant",
            "usage": usage, "content": [{"type": "text", "text": final_text}]}})
    if interrupted:
        add({"type": "user", "message": {"role": "user",
                                         "content": "[Request interrupted by user]"}})
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write("\n".join(json.dumps(x) for x in lines) + "\n")
    return path


def make_env(tmp_path):
    return {"TOKENECONOMIZER_HOME": str(tmp_path / "te"),
            "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"),
            "HOME": str(tmp_path / "home"),
            "GIT_CEILING_DIRECTORIES": str(tmp_path),
            "PATH": os.environ.get("PATH", "")}


def history_file(env, sid, project="project", **kw):
    path = os.path.join(env["CLAUDE_CONFIG_DIR"], "projects", project, f"{sid}.jsonl")
    return claude_log(path, **kw)


def old(path, now, age=3600):
    t = now - age
    os.utime(path, (t, t))
    return t


def state(env, repo, host="claude"):
    return Store(repo_state_dir(repo_identity(repo, env), host, env))


# ---- the history seam ------------------------------------------------------------------------
def test_claude_history_honours_config_dir_and_ignores_subagents(tmp_path, monkeypatch):
    config = tmp_path / "cfg"
    project = config / "projects" / "-w-repo"
    claude_log(str(project / "a.jsonl"), "/w/repo")
    claude_log(str(project / "b.jsonl"), "/w/repo")
    claude_log(str(project / "a" / "subagents" / "agent-1.jsonl"), "/w/repo")
    assert trace.claude_projects_dir({"CLAUDE_CONFIG_DIR": str(config)}) == str(
        config / "projects")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    assert trace.claude_projects_dir() == str(config / "projects")
    assert trace.claude_projects_dir({}) == str(
        os.path.join(os.path.expanduser("~"), ".claude", "projects"))
    got = sorted(trace.claude_history(str(config / "projects")))
    assert [sid for sid, _p, _m in got] == ["a", "b"]
    assert all(os.path.dirname(p) == str(project) for _s, p, _m in got)


def test_host_history_reads_extra_dirs_and_dedupes_by_session_id(tmp_path):
    repo = git_repo(tmp_path / "repo", "https://github.com/o/r")
    env = make_env(tmp_path)
    primary = history_file(env, "same", project="p", cwd=repo)
    history_file(env, "only-primary", project="p", cwd=repo)
    extra = tmp_path / "other-projects"
    claude_log(str(extra / "p" / "same.jsonl"), repo)
    extra_only = claude_log(str(extra / "p" / "extra.jsonl"), repo)
    env = dict(env, TE_BACKFILL_DIRS=f"{extra};{tmp_path / 'missing'}")
    got = {sid: (path, source) for sid, path, _mtime, source in
           hosts.ClaudeCode().history(env)}
    assert set(got) == {"same", "only-primary", "extra"}
    # The default projects directory is read first, so its copy of a shared id wins.
    assert got["same"] == (primary, trace.claude_projects_dir(env))
    assert got["extra"] == (extra_only, str(extra))


def test_translate_cwd_for_wsl_history_dirs():
    localhost = r"\\wsl.localhost\Ubuntu-24.04\home\me\.claude\projects"
    legacy = r"\\wsl$\Ubuntu-24.04\home\me\.claude\projects"
    assert backfill.translate_cwd("/mnt/c/x", localhost) == r"C:\x"
    assert backfill.translate_cwd("/home/u/r", localhost) == (
        r"\\wsl.localhost\Ubuntu-24.04\home\u\r")
    assert backfill.translate_cwd("/home/u/r", legacy) == (
        r"\\wsl$\Ubuntu-24.04\home\u\r")
    assert backfill.translate_cwd("/mnt/d/work/repo", localhost) == r"D:\work\repo"
    # A local history folder, or a cwd that is already a Windows path, is left alone.
    assert backfill.translate_cwd("/mnt/c/x", r"C:\Users\me\.claude\projects") == "/mnt/c/x"
    assert backfill.translate_cwd(r"C:\work", localhost) == r"C:\work"


# ---- the plan --------------------------------------------------------------------------------
def test_plan_skips_what_is_not_backfillable(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo", "https://github.com/o/r")
    env = make_env(tmp_path)
    now = time.time()

    def make(sid, age=3600, **kw):
        kw.setdefault("cwd", repo)
        p = history_file(env, sid, project="p", **kw)
        old(p, now, age)
        return p

    make("kept-old", 7200)
    make("kept", 3600)
    make("fresh", 30)
    make("too-old", 400 * 86400)
    make("excluded", 3600)
    no_prompts = make("no-prompts", 3600)
    make("missing-cwd", 3600, cwd=str(tmp_path / "gone"))
    make("recorded", 3600)
    folded = make("folded", 3600)
    too_large = make("too-large", 3600)

    st = state(env, repo)
    st.write("sessions/recorded.json", {"session_id": "recorded"})
    st.write("folded.json", {"folded": {"at": 1.0, "transcript": folded}})
    # A zero-prompt log is a side call or an empty log, not something to learn from.
    with open(no_prompts, "w") as fh:
        fh.write(json.dumps({"type": "assistant", "cwd": repo,
                             "message": {"role": "assistant",
                                         "content": [{"type": "text", "text": "hi"}]}}) + "\n")
    old(no_prompts, now)
    # Make one log look larger than the cap without writing 256 MB.
    real = os.path.getsize
    monkeypatch.setattr(os.path, "getsize",
                        lambda p: (backfill.MAX_BYTES + 1 if p == too_large else real(p)))
    groups = backfill.plan(hosts.ClaudeCode(), env, now=now, exclude={"excluded"})

    assert len(groups) == 1
    group = groups[0]
    assert [sid for sid, _p, _m in group["sessions"]] == ["kept-old", "kept"]
    assert group["skipped"] == {"fresh": 1, "too_old": 1, "excluded": 1, "no_prompts": 1,
                                "already_folded": 2, "too_large": 1}
    assert all(sid not in [s[0] for s in group["sessions"]] for sid in ("fresh", "missing-cwd"))


def test_plan_keeps_the_most_recent_sessions_per_repository_oldest_first(tmp_path):
    repo = git_repo(tmp_path / "repo", "https://github.com/o/r")
    env = make_env(tmp_path)
    now = time.time()
    for sid, age in (("one", 7200), ("two", 3600), ("three", 1800)):
        p = history_file(env, sid, project="p", cwd=repo)
        old(p, now, age)
    (group,) = backfill.plan(hosts.ClaudeCode(), env, now=now, max_per_repo=2)
    assert [sid for sid, _p, _m in group["sessions"]] == ["two", "three"]
    assert group["skipped"] == {"max_per_repo": 1}


def test_two_worktrees_of_one_repository_share_a_key(tmp_path):
    main = git_repo(tmp_path / "main", "https://github.com/o/r")
    wt = worktree(main, tmp_path / "feature")
    env = make_env(tmp_path)
    now = time.time()
    a = history_file(env, "main", project="p", cwd=main)
    b = history_file(env, "wt", project="p", cwd=wt)
    old(a, now, 7200)
    old(b, now, 3600)
    (group,) = backfill.plan(hosts.ClaudeCode(), env, now=now)
    assert group["key"] == repo_identity(main, env).key == repo_identity(wt, env).key
    assert [sid for sid, _p, _m in group["sessions"]] == ["main", "wt"]


def test_plan_reports_progress_every_25_history_files(tmp_path):
    repo = git_repo(tmp_path / "repo", "https://github.com/o/r")
    env = make_env(tmp_path)
    now = time.time()
    p = history_file(env, "s", project="p", cwd=repo, calls=[
        ("Bash", {"command": "pytest -q"}, "1 passed in 0.1s", False)])
    old(p, now)
    entries = [(f"s{i}", p, os.path.getmtime(p), trace.claude_projects_dir(env))
               for i in range(26)]
    host = type("HistoryHost", (), {
        "name": "claude",
        "history": staticmethod(lambda _env: list(entries))})()
    calls = []
    backfill.plan(host, env, now=now, on_progress=lambda: calls.append(1))
    assert len(calls) == 1


def test_backfill_days_and_max_per_repo_follow_global_config_and_env(tmp_path):
    repo = git_repo(tmp_path / "repo", "https://github.com/o/r")
    env = make_env(tmp_path)
    now = time.time()
    for sid, age in (("recent", 10 * 86400), ("mid", 100 * 86400),
                     ("old", 400 * 86400)):
        p = history_file(env, sid, project="p", cwd=repo, calls=[
            ("Bash", {"command": "pytest -q"}, "1 passed in 0.1s", False)])
        old(p, now, age)
    Store(env["TOKENECONOMIZER_HOME"]).write(
        "config.json", {"backfill_days": 200, "backfill_max_per_repo": 1})
    (group,) = backfill.plan(hosts.ClaudeCode(), env, now=now)
    assert [sid for sid, _p, _m in group["sessions"]] == ["recent"]
    assert group["skipped"] == {"too_old": 1, "max_per_repo": 1}

    env = dict(env, TE_BACKFILL_DAYS="50", TE_BACKFILL_MAX_PER_REPO="3")
    (group,) = backfill.plan(hosts.ClaudeCode(), env, now=now)
    assert [sid for sid, _p, _m in group["sessions"]] == ["recent"]
    assert group["skipped"] == {"too_old": 2}


def test_headless_sessions_are_skipped_unless_backfill_headless_is_on(tmp_path):
    repo = git_repo(tmp_path / "repo", "https://github.com/o/r")
    env = make_env(tmp_path)
    now = time.time()
    p = history_file(env, "sdk", project="p", cwd=repo, calls=[
        ("Bash", {"command": "pytest -q"}, "1 passed in 0.1s", False)])
    lines = [json.loads(line) for line in open(p)]
    lines[0]["entrypoint"] = "sdk-cli"
    with open(p, "w") as fh:
        fh.write("\n".join(json.dumps(x) for x in lines) + "\n")
    old(p, now)

    (group,) = backfill.plan(hosts.ClaudeCode(), env, now=now)
    assert group["sessions"] == [] and group["skipped"] == {"headless": 1}
    (group,) = backfill.plan(hosts.ClaudeCode(), dict(env, TE_BACKFILL_HEADLESS="on"),
                             now=now)
    assert [sid for sid, _p, _m in group["sessions"]] == ["sdk"]
    assert group["skipped"] == {}


def test_an_sdk_session_a_person_talked_through_is_kept(tmp_path, monkeypatch):
    # A chat front end drives Claude Code through the SDK, one prompt per message: several
    # prompts mean a person, not an automated `claude -p` run.
    repo = git_repo(tmp_path / "repo", "https://github.com/o/r")
    env = make_env(tmp_path)
    now = time.time()
    p = history_file(env, "chat", project="p", cwd=repo, calls=[
        ("Bash", {"command": "pytest -q"}, "1 passed in 0.1s", False)])
    lines = [json.loads(line) for line in open(p)]
    lines[0]["entrypoint"] = "sdk-cli"
    with open(p, "w") as fh:
        fh.write("\n".join(json.dumps(x) for x in lines) + "\n")
    old(p, now)
    monkeypatch.setattr(backfill.trace, "claude_prompt_count", lambda path: 3)

    (group,) = backfill.plan(hosts.ClaudeCode(), env, now=now)
    assert [sid for sid, _p, _m in group["sessions"]] == ["chat"]
    assert group["skipped"] == {}


# ---- the model-free fold ---------------------------------------------------------------------
def test_a_backfill_fold_makes_no_model_call_and_applies_nothing(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo", "https://github.com/o/r")
    env = dict(make_env(tmp_path), TE_MICROSKILLS="on", TE_TOOLPRUNE_QUIET_MINUTES="0")
    now = time.time()
    p = history_file(env, "s1", project="p", cwd=repo, calls=[
        ("Edit", {"file_path": f"{repo}/pkg/a.py", "old_string": "x", "new_string": "y"},
         "ok", False),
        ("Bash", {"command": "pytest -q"}, "1 passed in 0.1s", False)])
    old(p, now)

    def no_model(*a, **kw):
        raise AssertionError("a backfill fold called a model")
    monkeypatch.setattr(sidecall, "runner_for", no_model)
    monkeypatch.setattr(hosts.ClaudeCode, "settle",
                        lambda self, transcript_path: (_ for _ in ()).throw(
                            AssertionError("settle in a backfill fold")))

    ctx = LiveContext.build(hosts.ClaudeCode(), repo, env)
    record = fold.fold_session(ctx, "s1", p, backfill=True)

    assert record["backfill"] is True
    assert record["started"] is not None and record["ended"] is not None
    assert record["started"] < record["ended"]
    assert record["effective_tokens"] > 0
    assert ctx.store.read("folded.json")["s1"]["backfill"] is True
    # The free fact layer still ran; the proposer did not.
    assert ctx.store.read("injection.json")["profile"] == "envfacts+distiller"
    unt = ctx.store.read("untool.json")
    assert unt["sessions"] == 1 and unt["prompts"] == 1
    assert "auto_pending" not in unt
    # Nothing was applied to the repository or scheduled.
    from tokeneconomizer.plugin import denylist
    assert not os.path.exists(denylist.settings_path(repo))
    assert not os.path.exists(ctx.store.path("toolprune-waiter"))

    summary = accounting.summarize([record])
    assert summary["sessions"] == 0 and summary["backfilled"] == 1
    assert summary["effective_tokens"] == 0 and summary["requests"] == 0


# ---- run, budget and settings ----------------------------------------------------------------
def test_run_stops_at_its_budget_and_the_next_run_resumes(tmp_path):
    repo = git_repo(tmp_path / "repo", "https://github.com/o/r")
    env = make_env(tmp_path)
    now = time.time()
    p = history_file(env, "s1", project="p", cwd=repo, calls=[
        ("Bash", {"command": "pytest -q"}, "1 passed in 0.1s", False)])
    old(p, now)
    host = hosts.ClaudeCode()
    first = backfill.run(host, env, budget_s=0)
    assert first["state"] == "partial" and first["sessions"] == 0
    assert state(env, repo).read("folded.json") is None
    doc = json.load(open(os.path.join(env["TOKENECONOMIZER_HOME"], backfill.STATE)))
    assert doc["claude"]["state"] == "partial"
    assert not os.path.exists(os.path.join(env["TOKENECONOMIZER_HOME"],
                                           ".backfilling-claude"))
    second = backfill.run(host, env, budget_s=60)
    assert second["state"] == "done" and second["sessions"] == 1
    assert "s1" in state(env, repo).read("folded.json")
    assert json.load(open(os.path.join(env["TOKENECONOMIZER_HOME"], backfill.STATE)))[
        "claude"]["state"] == "done"


def test_sessions_that_go_live_after_planning_are_not_folded(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo", "https://github.com/o/r")
    env = make_env(tmp_path)
    now = time.time()
    recorded = history_file(env, "went-recorded", project="p", cwd=repo, calls=[
        ("Bash", {"command": "pytest -q"}, "1 passed in 0.1s", False)])
    moved = history_file(env, "went-fresh", project="p", cwd=repo, calls=[
        ("Bash", {"command": "pytest -q"}, "1 passed in 0.1s", False)])
    stable = history_file(env, "stable", project="p", cwd=repo, calls=[
        ("Bash", {"command": "pytest -q"}, "1 passed in 0.1s", False)])
    for p, age in ((recorded, 7200), (moved, 5400), (stable, 3600)):
        old(p, now, age)
    real_plan = backfill.plan

    def plan_then_go_live(*args, **kwargs):
        groups = real_plan(*args, **kwargs)
        state(env, repo).write("sessions/went-recorded.json",
                               {"session_id": "went-recorded"})
        os.utime(moved, None)               # its transcript moved inside the quiet window
        return groups
    monkeypatch.setattr(backfill, "plan", plan_then_go_live)

    res = backfill.run(hosts.ClaudeCode(), env, budget_s=60)
    assert res["state"] == "done" and res["sessions"] == 1
    folded = state(env, repo).read("folded.json")
    assert "stable" in folded and "went-recorded" not in folded and "went-fresh" not in folded
    entry = res["repos"][paths.repo_identity(repo, env).key]
    assert entry["sessions"] == 1 and entry["skipped"] == {"fresh": 2}


def test_run_refreshes_the_marker_while_planning(tmp_path, monkeypatch):
    git_repo(tmp_path / "repo", "https://github.com/o/r")
    env = make_env(tmp_path)
    refreshed = []

    def fake_plan(*args, on_progress=None, **kwargs):
        assert on_progress is not None
        on_progress()
        return []
    monkeypatch.setattr(backfill, "plan", fake_plan)
    monkeypatch.setattr(backfill, "_refresh_marker",
                        lambda root, host: refreshed.append(host))
    res = backfill.run(hosts.ClaudeCode(), env)
    assert res["state"] == "done" and refreshed == ["claude"]


def test_a_fold_error_is_recorded_and_does_not_make_the_run_partial(tmp_path, monkeypatch):
    """One transcript that always throws must not make every later session start rescan the
    history: it is counted as an error and the run finishes."""
    repo = git_repo(tmp_path / "repo", "https://github.com/o/r")
    env = make_env(tmp_path)
    now = time.time()
    good = history_file(env, "good", project="p", cwd=repo, calls=[
        ("Bash", {"command": "pytest -q"}, "1 passed in 0.1s", False)])
    bad = history_file(env, "bad", project="p", cwd=repo, calls=[
        ("Bash", {"command": "pytest -q"}, "1 passed in 0.1s", False)])
    old(good, now, 7200)
    old(bad, now, 3600)
    real = fold.fold_session

    def fake(ctx, sid, transcript_path=None, backfill=False):
        if sid == "bad":
            raise RuntimeError("this transcript always throws")
        return real(ctx, sid, transcript_path, backfill=backfill)
    monkeypatch.setattr(backfill.fold, "fold_session", fake)

    res = backfill.run(hosts.ClaudeCode(), env, budget_s=60)
    assert res["state"] == "done" and res["sessions"] == 1
    folded = state(env, repo).read("folded.json")
    assert "good" in folded and "bad" not in folded
    entry = res["repos"][paths.repo_identity(repo, env).key]
    assert entry["sessions"] == 1 and entry["skipped"] == {"error": 1}
    doc = json.load(open(os.path.join(env["TOKENECONOMIZER_HOME"], backfill.STATE)))
    assert doc["claude"]["state"] == "done"
    assert "this transcript always throws" in open(
        os.path.join(env["TOKENECONOMIZER_HOME"], "errors.log")).read()


def test_the_run_marker_is_refreshed_before_each_fold(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo", "https://github.com/o/r")
    env = make_env(tmp_path)
    now = time.time()
    for sid, age in (("one", 7200), ("two", 3600)):
        p = history_file(env, sid, project="p", cwd=repo, calls=[
            ("Bash", {"command": "pytest -q"}, "1 passed in 0.1s", False)])
        old(p, now, age)
    marker = os.path.join(env["TOKENECONOMIZER_HOME"], ".backfilling-claude")
    os.makedirs(env["TOKENECONOMIZER_HOME"], exist_ok=True)
    open(marker, "w").close()
    stale = now - backfill.MARKER_S - 100
    os.utime(marker, (stale, stale))

    seen = []
    real = fold.fold_session

    def fake(ctx, sid, transcript_path=None, backfill=False):
        seen.append(os.path.getmtime(marker))
        return real(ctx, sid, transcript_path, backfill=backfill)
    monkeypatch.setattr(backfill.fold, "fold_session", fake)

    res = backfill.run(hosts.ClaudeCode(), env, budget_s=60)
    assert res["state"] == "done" and len(seen) == 2
    assert all(mtime > stale + 1 for mtime in seen)     # refreshed before each fold
    assert not os.path.exists(marker)                   # and removed when the run ends


def test_backfill_is_a_config_setting_like_its_neighbours(tmp_path):
    from tokeneconomizer.plugin import config
    path = str(tmp_path / "config.json")
    assert config.LiveConfig().backfill == "on"
    config.write_config(path, {"backfill": "off"})
    assert json.load(open(path)) == {"backfill": "off"}
    assert config.load_config(str(tmp_path), env={}).backfill == "off"
    assert config.load_config(str(tmp_path), env={"TE_BACKFILL": "on"}).backfill == "on"
    with pytest.raises(ValueError):
        config.write_config(path, {"backfill": "sometimes"})


def test_the_other_backfill_settings_are_settable(tmp_path):
    from tokeneconomizer.plugin import config
    path = str(tmp_path / "config.json")
    config.write_config(path, {"backfill_days": "30", "backfill_max_per_repo": "7",
                               "backfill_dirs": r"C:\a;C:\b", "backfill_headless": "on"})
    assert json.load(open(path)) == {"backfill_days": 30, "backfill_max_per_repo": 7,
                                     "backfill_dirs": r"C:\a;C:\b",
                                     "backfill_headless": "on"}
    cfg = config.load_config(str(tmp_path), env={})
    assert (cfg.backfill_days, cfg.backfill_max_per_repo, cfg.backfill_dirs,
            cfg.backfill_headless) == (30, 7, r"C:\a;C:\b", "on")
    cfg = config.load_config(str(tmp_path), env={
        "TE_BACKFILL_DAYS": "2", "TE_BACKFILL_MAX_PER_REPO": "3",
        "TE_BACKFILL_DIRS": "/a;/b", "TE_BACKFILL_HEADLESS": "off"})
    assert (cfg.backfill_days, cfg.backfill_max_per_repo, cfg.backfill_dirs,
            cfg.backfill_headless) == (2, 3, "/a;/b", "off")


def test_repos_does_not_count_backfilled_folds(tmp_path):
    st = Store(str(tmp_path / "st"))
    st.write("folded.json", {"live": {"at": 1.0},
                             "old": {"at": 2.0, "backfill": True}})
    st.write("sessions/live.json", {"session_id": "live"})
    assert set(repos._sessions(st)) == {"live"}


# ---- start and the notice --------------------------------------------------------------------
def test_start_respects_off_done_and_a_fresh_marker(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo", "https://github.com/o/r")
    env = make_env(tmp_path)
    spawned = []
    monkeypatch.setattr(LiveContext, "spawn",
                        lambda self, argv, log: spawned.append((argv, log)) or True)
    ctx = LiveContext.build(hosts.ClaudeCode(), repo, dict(env, TE_BACKFILL="off"))
    assert backfill.start(ctx, exclude=("s",)) is None
    assert spawned == [] and not os.path.exists(os.path.join(ctx.root, ".backfilling-claude"))

    ctx = LiveContext.build(hosts.ClaudeCode(), repo, env)
    Store(ctx.root).write(backfill.STATE, {"claude": {"state": "done"}})
    assert backfill.start(ctx) is None and spawned == []

    Store(ctx.root).write(backfill.STATE, {"claude": {"state": "partial"}})
    marker = os.path.join(ctx.root, ".backfilling-claude")
    open(marker, "w").close()
    assert backfill.start(ctx) is None and spawned == []

    os.utime(marker, (time.time() - backfill.MARKER_S - 1,) * 2)
    assert backfill.start(ctx, exclude=("cur",)) is True
    assert spawned == [(["backfill", "--quiet", "--exclude", "cur"], backfill.LOG)]


def test_the_notice_fires_once_after_done(tmp_path):
    root = str(tmp_path / "te")
    Store(root).write(backfill.STATE, {"claude": {
        "state": "done", "noticed": False,
        "repos": {"a": {"label": "o/a", "sessions": 12, "skipped": {}},
                  "b": {"label": "o/b", "sessions": 28, "skipped": {}}}}})
    line = backfill.notice(root, "claude", "te backfill")
    assert "40 earlier Claude Code session(s)" in line
    assert "2 repositories" in line and "`te backfill`" in line
    assert "`backfill=off` turns this off" in line
    assert backfill.notice(root, "claude", "te backfill") is None
    Store(root).write(backfill.STATE, {"claude": {"state": "partial", "noticed": False}})
    assert backfill.notice(root, "claude", "te backfill") is None


# ---- the envelope seed -----------------------------------------------------------------------
def test_envelope_replay_records_green_turns_and_never_aborts(tmp_path):
    repo = git_repo(tmp_path / "repo", "https://github.com/o/r")
    env = make_env(tmp_path)
    now = time.time()
    green = history_file(env, "green", project="p", cwd=repo, calls=[
        ("Edit", {"file_path": f"{repo}/a.py", "old_string": "x", "new_string": "y"},
         "ok", False),
        ("Bash", {"command": "pytest -q"}, "1 passed in 0.1s", False)])
    old(green, now)
    interrupted = history_file(env, "interrupted", project="p", cwd=repo, calls=[
        ("Edit", {"file_path": f"{repo}/a.py", "old_string": "x", "new_string": "y"},
         "ok", False),
        ("Bash", {"command": "pytest -q"}, "1 failed in 0.1s", True)],
        final_text="", interrupted=True)
    old(interrupted, now)
    unfinished = history_file(env, "unfinished", project="p", cwd=repo, calls=[
        ("Bash", {"command": "pytest -q"}, "1 passed in 0.1s", False)], final_text="")
    old(unfinished, now)

    ctx = LiveContext.build(hosts.ClaudeCode(), repo, env)
    assert backfill.replay_envelope(ctx, green) == 1
    assert ctx.store.read("envelope.json")["resolved_turns"] == [2]
    assert backfill.replay_envelope(ctx, interrupted) == 1
    assert backfill.replay_envelope(ctx, unfinished) == 1
    assert ctx.store.read("envelope.json")["resolved_turns"] == [2]

    # With no envelope and no judge, the guard has nothing to act on: a pathological turn
    # still runs to its end and is only counted.
    guard = loopguard.TurnGuard()
    for k in range(40):
        call = (("Edit", {"file_path": "/r/a.py", "old_string": "x", "new_string": "y"}, "ok")
                if k % 2 == 0 else
                ("Bash", {"command": "pytest -q"},
                 "FAILED tests/test_a.py::t - AssertionError"))
        decision = guard.fold(*call, envelope=None,
                              thr=loopguard.base_thresholds("default"), judge=None)
        assert decision is None
    assert guard.aborted is False and guard.cum_turns == 40


def test_envelope_replay_leaves_out_sidechains_and_large_logs(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo", "https://github.com/o/r")
    env = make_env(tmp_path)
    now = time.time()
    p = history_file(env, "main", project="p", cwd=repo, calls=[
        ("Bash", {"command": "pytest -q"}, "1 passed in 0.1s", False)])
    old(p, now)
    lines = [json.loads(line) for line in open(p)]
    lines[1:1] = [
        {"type": "assistant", "isSidechain": True, "cwd": repo, "message": {
            "id": "side", "role": "assistant",
            "content": [{"type": "tool_use", "id": "tu-side", "name": "Bash",
                         "input": {"command": "pytest -q"}}]}},
        {"type": "user", "isSidechain": True, "cwd": repo, "message": {
            "role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu-side",
                 "content": "1 passed in 0.1s", "is_error": False}]}}]
    with open(p, "w") as fh:
        fh.write("\n".join(json.dumps(x) for x in lines) + "\n")

    ctx = LiveContext.build(hosts.ClaudeCode(), repo, env)
    assert backfill.replay_envelope(ctx, p) == 1
    assert ctx.store.read("envelope.json")["resolved_turns"] == [1]

    real = os.path.getsize
    monkeypatch.setattr(os.path, "getsize",
                        lambda path: (backfill.REPLAY_MAX_BYTES + 1 if path == p
                                      else real(path)))
    assert backfill.replay_envelope(ctx, p) == 0
    assert ctx.store.read("envelope.json")["resolved_turns"] == [1]
