"""Importing a repository's sessions from before the plugin, from the agent's own logs.

The first session start in a repository folds that repository's earlier Claude Code logs, oldest
first and each once; learning then carries on from the sessions the plugin sees end. Logs are
laid out as Claude Code keeps them: `<home>/projects/<folder>/<session id>.jsonl`.
"""
import json
import os
import time

from test_live_hooks import (_catalog, call, env, repo, run_session, store_for,  # noqa: F401
                             trap_session, write_session)
from tokeneconomizer.plugin import accounting, cli, denylist, fold, history, hosts, sidecall
from tokeneconomizer.plugin.config import load_config
from tokeneconomizer.plugin.context import LiveContext
from tokeneconomizer.plugin.history import default_dirs     # before conftest stubs it

CLAIM = "`pytest …` fails here with command not found; `python -m pytest …` works."


def earlier_log(home, cwd, sid, i, age_s=3600, prompts=None):
    """A finished session's log where Claude Code keeps it, last written `age_s` ago: the
    `pytest` trap, or `prompts` as `write_session` takes them."""
    folder = os.path.join(str(home), "projects", "-" + os.path.basename(cwd))
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{sid}.jsonl")
    path = write_session(path, prompts) if prompts else trap_session(path, cwd, i)
    with open(path) as fh:
        lines = [json.loads(ln) for ln in fh]
    lines[0]["cwd"] = cwd                   # Claude Code names the directory on every entry
    with open(path, "w") as fh:
        fh.write("\n".join(json.dumps(x) for x in lines) + "\n")
    t = time.time() - age_s
    os.utime(path, (t, t))
    return path


def start(env, repo, sid, tmp_path):
    t = str(tmp_path / f"{sid}.jsonl")
    open(t, "w").close()
    return call(env, {"hook_event_name": "SessionStart", "session_id": sid,
                      "source": "startup", "transcript_path": t, "cwd": repo})


def ctx_for(env, repo):
    return LiveContext.build(hosts.HOSTS["claude"](), repo, env)


def test_the_first_session_start_learns_from_the_repositorys_earlier_sessions(env, repo,
                                                                               tmp_path):
    home = tmp_path / "claude"
    env = dict(env, TE_HISTORY_DIRS=str(home))
    for i in range(6):
        earlier_log(home, repo, f"old{i}", i, age_s=3600 * (10 - i))
    other = tmp_path / "other"
    other.mkdir()
    earlier_log(home, str(other), "elsewhere", 0)               # another repository's
    earlier_log(home, repo, "running", 9, age_s=5)              # a session still writing
    sub = os.path.join(str(home), "projects", "-repo", "old0", "subagents")
    os.makedirs(sub)
    earlier_log(os.path.join(str(home), "projects", "-repo", "old0"), repo, "agent-x", 0)

    first = start(env, repo, "new1", tmp_path)
    assert "hookSpecificOutput" not in first     # the import runs after this start's injection
    st = store_for(env, repo)
    folded = st.read("folded.json")
    assert sorted(folded) == [f"old{i}" for i in range(6)]
    assert all(e["imported"] for e in folded.values())
    ledger = st.read_lines("ledger.jsonl")
    assert [r["session_id"] for r in ledger] == [f"old{i}" for i in range(6)]   # oldest first
    assert all(r["imported"] for r in ledger)
    assert st.read("history.json")["imported"] == 6

    second = start(env, repo, "new2", tmp_path)
    assert CLAIM in second["hookSpecificOutput"]["additionalContext"]
    assert "learned from 6 earlier session(s)" in second["systemMessage"]
    third = start(env, repo, "new3", tmp_path)
    assert "earlier session" not in third.get("systemMessage", "")    # said once


def test_history_is_imported_once_and_an_import_run_again_takes_only_new_logs(
        env, repo, tmp_path, capsys, monkeypatch):
    home = tmp_path / "claude"
    env = dict(env, TE_HISTORY_DIRS=str(home))
    for k, v in env.items():                    # `te` reads the process environment
        monkeypatch.setenv(k, v)
    earlier_log(home, repo, "old0", 0)
    seen = earlier_log(home, repo, "seen", 1)
    run_session(env, repo, "seen", seen)        # a session the plugin saw: its own fold took it
    st = store_for(env, repo)
    assert "imported" not in st.read("folded.json")["seen"]
    assert sorted(st.read("folded.json")) == ["old0", "seen"]

    earlier_log(home, repo, "old1", 2)
    start(env, repo, "new", tmp_path)           # imported already: nothing runs by itself
    assert "old1" not in st.read("folded.json")
    cli.main(["--cwd", repo, "--host", "claude", "history", "show"])
    assert "1 earlier session(s) of this repository" in capsys.readouterr().out

    cli.main(["--cwd", repo, "--host", "claude", "history", "import", "--dir", str(home)])
    assert "imported 1 earlier session(s)" in capsys.readouterr().out
    cli.main(["--cwd", repo, "--host", "claude", "history", "import", "--dir", str(home)])
    assert "imported 0 earlier session(s)" in capsys.readouterr().out
    assert len([r for r in st.read_lines("ledger.jsonl") if r.get("imported")]) == 2
    assert len(st.read_lines("ledger.jsonl")) == 3            # each session once


def test_history_import_off_imports_nothing_and_the_limit_keeps_the_most_recent(
        env, repo, tmp_path):
    home = tmp_path / "claude"
    for i in range(4):
        earlier_log(home, repo, f"old{i}", i, age_s=3600 * (10 - i))
    off = dict(env, TE_HISTORY_DIRS=str(home), TE_HISTORY_IMPORT="off")
    start(off, repo, "a", tmp_path)
    st = store_for(env, repo)
    assert st.read("folded.json") is None and st.read("history.json") is None

    capped = dict(env, TE_HISTORY_DIRS=str(home), TE_HISTORY_MAX_SESSIONS="2")
    start(capped, repo, "b", tmp_path)
    assert sorted(st.read("folded.json")) == ["old2", "old3"]
    assert st.read("history.json")["last"]["over_limit"] == 2


TOOLS = ["Bash", "Read", "Edit", "Write", "AskUserQuestion", "Workflow", "CronCreate",
         "ShareOnboardingGuide", "WebFetch"]
UNUSED = ["CronCreate", "ShareOnboardingGuide", "Workflow"]


def looked(repo):
    return [("look", [("Read", {"file_path": f"{repo}/a.py"}, "x", False),
                      ("Bash", {"command": "ls"}, "a.py", False)]), ("and again", [])]


def test_a_deny_list_an_import_proposes_waits_for_apply_even_under_auto(env, repo, tmp_path,
                                                                        capsys, monkeypatch):
    """`toolprune=auto` writes a deny list by itself only from sessions the plugin saw: one
    that an import of earlier sessions proposed is announced and waits for `toolprune apply`."""
    home = tmp_path / "claude"
    env = dict(env, TE_HISTORY_DIRS=str(home))
    _catalog(env, TOOLS)
    for i in range(5):
        earlier_log(home, repo, f"old{i}", i, age_s=3600 * (10 - i), prompts=looked(repo))
    start(env, repo, "first", tmp_path)
    st = store_for(env, repo)
    unt = st.read("untool.json")
    assert sorted(unt["proposal"]) == UNUSED and unt["from_history"] == UNUSED
    assert "auto_pending" not in unt
    assert not os.path.exists(denylist.settings_path(repo))

    notice = start(env, repo, "second", tmp_path)["systemMessage"]
    assert "3 tool(s) went unused" in notice
    assert "nothing changes until you run `/tokeneconomizer:toolprune apply`" in notice
    run_session(env, repo, "live", write_session(str(tmp_path / "live.jsonl"), looked(repo)))
    assert not os.path.exists(denylist.settings_path(repo))     # a live fold does not take it
    assert st.read("untool.json")["from_history"] == UNUSED

    for k, v in env.items():
        monkeypatch.setenv(k, v)
    cli.main(["--cwd", repo, "--host", "claude", "toolprune", "show"])
    assert "written only when you run `toolprune apply`" in capsys.readouterr().out
    cli.main(["--cwd", repo, "--host", "claude", "toolprune", "apply"])
    deny = json.load(open(denylist.settings_path(repo)))["permissions"]["deny"]
    assert sorted(deny) == UNUSED


def test_what_the_plugins_own_sessions_add_to_the_list_is_still_written_by_auto(
        env, repo, tmp_path):
    """Three imported sessions are not enough evidence; two more the plugin sees end are, and
    the list they form is the plugin's own: `auto` writes it."""
    home = tmp_path / "claude"
    env = dict(env, TE_HISTORY_DIRS=str(home))
    _catalog(env, TOOLS)
    for i in range(3):
        earlier_log(home, repo, f"old{i}", i, age_s=3600 * (10 - i), prompts=looked(repo))
    start(env, repo, "first", tmp_path)
    st = store_for(env, repo)
    assert st.read("untool.json")["proposal"] == []
    for i in range(2):
        run_session(env, repo, f"live{i}",
                    write_session(str(tmp_path / f"live{i}.jsonl"), looked(repo)))
    deny = json.load(open(denylist.settings_path(repo)))["permissions"]["deny"]
    assert sorted(deny) == UNUSED
    assert "from_history" not in st.read("untool.json")


def test_an_import_never_calls_a_model(env, repo, tmp_path, monkeypatch):
    """Micro-skills' proposer runs over sessions the plugin sees end, never over a history."""
    home = tmp_path / "claude"
    for i in range(3):
        earlier_log(home, repo, f"old{i}", i)
    asked = []
    monkeypatch.setattr(sidecall, "runner_for", lambda *a, **k: asked.append(a) or None)
    env = dict(env, TE_HISTORY_DIRS=str(home), TE_MICROSKILLS="on",
               TE_MICROSKILLS_PROPOSE_EVERY="1")
    start(env, repo, "new", tmp_path)
    assert len(store_for(env, repo).read("folded.json")) == 3 and asked == []
    fold.fold_session(ctx_for(env, repo), "seen",
                      transcript_path=trap_session(str(tmp_path / "seen.jsonl"), repo, 7))
    assert asked                                # a session the plugin saw end still may


def test_an_import_waits_until_a_mechanism_learns_and_is_retried_after_a_crash(env, repo,
                                                                               tmp_path):
    home = tmp_path / "claude"
    earlier_log(home, repo, "old0", 0)
    env = dict(env, TE_HISTORY_DIRS=str(home))
    ctx = ctx_for(dict(env, TE_MICROSKILLS="off", TE_TOOLPRUNE="off"), repo)
    assert not history.start(ctx)               # nothing would learn from it
    ctx = ctx_for(env, repo)
    ctx.store.write(history.STATE, {"started": time.time()})    # an import still running
    assert not history.start(ctx)
    ctx.store.write(history.STATE, {"started": time.time() - history.RESPAWN_S - 1})
    assert history.start(ctx)                   # the one before it died: try again
    assert "old0" in ctx.store.read("folded.json")


def test_where_the_logs_are(tmp_path):
    assert default_dirs("claude", {"HOME": str(tmp_path)}) == [
        os.path.join(os.path.realpath(str(tmp_path)), ".claude")]
    assert default_dirs("claude", {"HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": "/c"}) == ["/c"]
    assert default_dirs("codex", {"HOME": str(tmp_path)}) == []
    proj = tmp_path / "projects" / "-w"
    (proj / "s1" / "subagents").mkdir(parents=True)
    for p in (proj / "s1.jsonl", proj / "agent-a.jsonl", proj / "s1" / "subagents" / "x.jsonl"):
        p.write_text("{}\n")
    want = [(str(proj / "s1.jsonl"), "s1")]
    assert history.logs(str(tmp_path)) == want                    # an agent home
    assert history.logs(str(tmp_path / "projects")) == want       # its projects directory
    assert history.logs(str(proj)) == want                        # one project's folder


def test_the_history_settings(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "config.json").write_text(json.dumps(
        {"history_dirs": ["/a", "/b"], "history_import": "no", "history_max_sessions": 0}))
    cfg = load_config(str(root), env={})
    assert cfg.history_dirs == os.pathsep.join(["/a", "/b"])
    assert cfg.history_import == "off" and cfg.history_max_sessions == 0
    cfg = load_config(str(root), env={"TE_HISTORY_IMPORT": "maybe"})
    assert cfg.history_import == "off" and any("history_import" in w for w in cfg.warnings)


def test_imported_sessions_are_counted_apart_from_the_plugins_own():
    s = accounting.summarize([
        {"kind": "session", "effective_tokens": 100.0},
        {"kind": "session", "effective_tokens": 900.0, "imported": True}])
    assert s["sessions"] == 1 and s["imported_sessions"] == 1
    assert s["effective_tokens"] == 100.0
