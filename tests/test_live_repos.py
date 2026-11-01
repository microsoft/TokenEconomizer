"""Which repository a session's state belongs to: what counts as a checkout, one key per
repository, session pins, carrying state over from the keys 0.1.0 used, per-checkout deny lists,
and `te repos`.

The cases: an empty `~/.git` is no checkout of the projects below it; a clone and a worktree of
one repository share its key, whatever their folder names; a session that `cd`s stays under the
repository it started in; and a session is folded once, under one key.
"""
import io
import json
import os

import pytest

from tokeneconomizer.plugin import cli, denylist, fold, hook, paths, repos
from tokeneconomizer.plugin.store import Store


def git_repo(root, remote=None):
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
    return path


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return {"TOKENECONOMIZER_HOME": str(tmp_path / "te"), "TE_FOLD_SYNC": "1",
            # A deny list is written at the fold here; the wait before it has its own tests.
            "TE_TOOLPRUNE_QUIET_MINUTES": "0",
            # Facts are learned here without a model call. Micro-skills is off by default, and
            # its `on` adds the proposer; both have their own tests.
            "TE_MICROSKILLS": "envfacts",
            "GIT_CEILING_DIRECTORIES": str(tmp_path), "HOME": str(home),
            "PATH": os.environ.get("PATH", "")}


def call(env, payload, host):
    """One hook call's final JSON object, with Copilot's notice lines as `progress`."""
    out = io.StringIO()
    hook.main(["--host", host], io.StringIO(json.dumps(payload)), out, env)
    lines = out.getvalue().splitlines()
    res = json.loads(lines[-1])
    if lines[:-1]:
        res["progress"] = [json.loads(ln)["message"] for ln in lines[:-1]]
    return res


def state(env, key, host="claude"):
    return Store(os.path.join(env["TOKENECONOMIZER_HOME"], "repos", key, host))


# ---- what counts as a checkout -----------------------------------------------------------------
def test_an_empty_or_dangling_dot_git_is_no_checkout(tmp_path, env):
    empty = tmp_path / "empty"
    (empty / ".git").mkdir(parents=True)
    dangling = tmp_path / "dangling"
    dangling.mkdir()
    (dangling / ".git").write_text(f"gitdir: {tmp_path / 'gone'}\n")
    for d in (empty, dangling):
        assert not paths.is_git_entry(str(d / ".git"))
        r = paths.repo_identity(str(d), env)
        assert r.how == paths.BY_DIR and r.root == str(d)
    assert repos.ignored_git_entries(str(empty))[:1] == [str(empty / ".git")]


def test_a_versioned_home_directory_keeps_only_its_own_sessions(tmp_path, env):
    """The case that filed every unversioned project under `~`: the walk stops at the home
    directory for anything below it, whether `~/.git` is a real repository or an empty one."""
    home = env["HOME"]
    proj = os.path.join(home, "proposal", "draft")
    os.makedirs(proj)
    for make in (lambda: os.makedirs(os.path.join(home, ".git")),       # empty, as seen
                 lambda: git_repo(home, "https://github.com/me/dotfiles")):
        make()
        r = paths.repo_identity(proj, env)
        assert r.how == paths.BY_DIR and r.root == proj and r.label == "draft"
    assert paths.repo_identity(home, env).label == "me/dotfiles"     # started in ~ itself
    assert "is a git repository" in repos.home_git_note(env)


def test_clones_and_worktrees_share_one_key_whatever_their_folder_names(tmp_path, env):
    a = git_repo(str(tmp_path / "TE_MSR"), "git@github.com:Microsoft/TokenEconomizer.git")
    b = git_repo(str(tmp_path / "TokenEconomizer"), "https://github.com/microsoft/TokenEconomizer")
    wt = worktree(a, str(tmp_path / "wt" / "agent-af30"))
    keys = {paths.repo_identity(d, env).key for d in (a, b, wt)}
    assert keys == {f"tokeneconomizer-{paths.digest('github.com/microsoft/tokeneconomizer')}"}
    # A repository with no remote is keyed by its main checkout, and so is its worktree.
    local = git_repo(str(tmp_path / "local"))
    lwt = worktree(local, str(tmp_path / "local-wt"))
    rl, rw = paths.repo_identity(local, env), paths.repo_identity(lwt, env)
    assert rl.key == rw.key and rl.label == rw.label == "local" and rw.root == lwt
    assert rl.how == paths.BY_PATH
    # ... unchanged from 0.1.0 for a main checkout, so nothing of it needs carrying over.
    assert rl.key == repos.legacy_key(local, env)


# ---- one session, one repository, on every agent ------------------------------------------------
def _payloads(host, sid, cwd, n):
    base = {"session_id": sid, "cwd": cwd}
    turn = {"claude": {"prompt_id": f"p{n}"}, "codex": {"turn_id": f"t{n}"},
            "pi": {"prompt_id": f"{sid}:{n}"}, "copilot": {}}[host]
    result = ({"tool_result": {"text_result_for_llm": "ok"}} if host == "copilot"
              else {"tool_response": {"stdout": "ok"}})
    return [dict(base, **turn, hook_event_name="UserPromptSubmit", prompt="go"),
            dict(base, **turn, **result, hook_event_name="PostToolUse", tool_name="Bash",
                 tool_input={"command": "ls"}),
            dict(base, **turn, hook_event_name="Stop", stop_hook_active=False)]


@pytest.mark.parametrize("host", ["claude", "codex", "copilot", "pi"])
def test_a_session_that_moves_into_another_repository_stays_with_its_own(tmp_path, env, host):
    """Claude Code's hook `cwd` follows the agent's `cd`; every agent's hooks name a cwd. The
    first hook pins the session, and nothing is recorded under the repository it moved to."""
    a = git_repo(str(tmp_path / "a"), "https://github.com/o/a")
    b = git_repo(str(tmp_path / "b"), "https://github.com/o/b")
    ka, kb = paths.repo_identity(a, env).key, paths.repo_identity(b, env).key
    call(env, {"hook_event_name": "SessionStart", "session_id": "s1", "cwd": a,
               "source": "startup"}, host)
    for p in _payloads(host, "s1", b, 1):
        call(env, p, host)
    st = state(env, ka, host)
    rec = st.read(os.path.join("sessions", "s1.json"))
    assert rec["cwd"] == a and len(rec["turns"]) == 1
    assert not os.path.exists(os.path.join(env["TOKENECONOMIZER_HOME"], "repos", kb))


def test_te_fold_for_a_session_uses_its_pin(tmp_path, env, monkeypatch):
    a = git_repo(str(tmp_path / "a"), "https://github.com/o/a")
    b = git_repo(str(tmp_path / "b"), "https://github.com/o/b")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    call(env, {"hook_event_name": "SessionStart", "session_id": "s1", "cwd": a}, "claude")
    call(env, {"hook_event_name": "SessionEnd", "session_id": "s1", "cwd": b}, "claude")
    ka = paths.repo_identity(a, env).key
    assert "s1" in (state(env, ka).read("folded.json") or {})
    assert cli.main(["--host", "claude", "--cwd", b, "fold", "--session", "s1"]) == 0
    assert not os.path.exists(os.path.join(env["TOKENECONOMIZER_HOME"], "repos",
                                           paths.repo_identity(b, env).key, "claude",
                                           "folded.json"))


def test_status_says_when_this_session_is_recorded_elsewhere(tmp_path, env, monkeypatch, capsys):
    a = git_repo(str(tmp_path / "a"), "https://github.com/o/a")
    b = git_repo(str(tmp_path / "b"), "https://github.com/o/b")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    call(env, {"hook_event_name": "SessionStart", "session_id": "s9", "cwd": a}, "claude")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s9")
    assert cli.main(["--host", "claude", "--cwd", b, "status"]) == 0
    out = capsys.readouterr().out
    assert "identified by its origin remote (github.com/o/b)" in out
    assert f"this claude session started in {a} and is recorded under o/a" in out
    assert cli.main(["--host", "claude", "--cwd", a, "status"]) == 0
    assert "note " not in capsys.readouterr().out


# ---- carrying state over from 0.1.0's keys ------------------------------------------------------
def transcript(path, cwd):
    with open(path, "w") as fh:
        fh.write(json.dumps({"type": "user", "cwd": cwd, "message": {"content": "hi"}}) + "\n")
    os.utime(path, (1, 1))           # long quiet: a finished session
    return str(path)


def legacy_session(st, sid, tp, cwd=None, folded=True, tokens=100.0, folded_at=1.0):
    rec = {"session_id": sid, "host": "claude", "started": folded_at, "transcript": tp,
           "turns": [], "updated": folded_at}
    if cwd:
        rec["cwd"] = cwd
    st.write(os.path.join("sessions", f"{sid}.json"), rec)
    if folded:
        f = st.read("folded.json") or {}
        f[sid] = {"at": folded_at, "transcript": tp}
        st.write("folded.json", f)
        st.append("ledger.jsonl", {"kind": "session", "session_id": sid, "folded_at": folded_at,
                                   "effective_tokens": tokens})


def learned(st, label, facts=1):
    st.write("injection.json", {"rendered": "- a fact", "sessions": facts})
    st.write(os.path.join("mechanisms", "envfacts_state", label.replace("/", "__") + ".json"),
             {"repo": label, "facts": {}})


def test_a_clone_under_another_folder_name_is_carried_over_whole(tmp_path, env):
    clone = git_repo(str(tmp_path / "TE_MSR"), "https://github.com/acme/widget")
    old = repos.legacy_key(clone, env)
    assert old.startswith("te-msr-")
    st = state(env, old)
    legacy_session(st, "s1", transcript(tmp_path / "s1.jsonl", clone))
    learned(st, "acme/widget")
    st.write("envelope.json", {"resolved_turns": [5, 6]})
    root = env["TOKENECONOMIZER_HOME"]
    repo = paths.repo_identity(clone, env)
    repos.adopt(root, repo, env)
    new = state(env, repo.key)
    assert repo.key.startswith("widget-") and not os.path.isdir(os.path.dirname(st.base))
    assert new.read("injection.json")["rendered"] == "- a fact"
    assert new.read("envelope.json")["resolved_turns"] == [5, 6]
    assert "s1" in new.read("folded.json") and len(new.read_lines("ledger.jsonl")) == 1
    assert os.listdir(os.path.join(root, "merged"))[0].startswith(old)
    # Seen once, the checkout is not migrated again.
    assert repos.adopt(root, repo, env) == []


def test_a_directory_windows_will_not_move_is_retried_and_the_merge_stands(tmp_path, env,
                                                                         monkeypatch):
    """`os.replace` of a directory fails on Windows while a handle is open inside it (a scanner,
    an agent's watcher): the error in `errors.log`. The retry moves it, and the merge -- which
    already happened -- is not repeated."""
    clone = git_repo(str(tmp_path / "TE_MSR"), "https://github.com/acme/widget")
    old = repos.legacy_key(clone, env)
    st = state(env, old)
    legacy_session(st, "s1", transcript(tmp_path / "s1.jsonl", clone))
    learned(st, "acme/widget")
    root = env["TOKENECONOMIZER_HOME"]
    repo = paths.repo_identity(clone, env)
    real_replace = os.replace
    attempts = []

    def flaky(src, dst):
        if os.path.isdir(src) and os.path.basename(src) == old:
            attempts.append(src)
            if len(attempts) == 1:
                raise PermissionError(13, "Access is denied")
        return real_replace(src, dst)

    monkeypatch.setattr(repos.os, "replace", flaky)
    repos.adopt(root, repo, env)
    assert len(attempts) == 2
    assert not os.path.isdir(os.path.join(root, "repos", old))
    assert os.listdir(os.path.join(root, "merged"))[0].startswith(old)
    assert state(env, repo.key).read("injection.json")["rendered"] == "- a fact"


def test_a_directory_that_never_moves_aborts_nothing_and_writes_no_redirect(tmp_path, env,
                                                                          monkeypatch):
    """The merged directory stays where it is, the settle still succeeds, and no redirect sends
    the sessions pinned to the old key to a key whose directory is still there."""
    clone = git_repo(str(tmp_path / "TE_MSR"), "https://github.com/acme/widget")
    old = repos.legacy_key(clone, env)
    st = state(env, old)
    legacy_session(st, "s1", transcript(tmp_path / "s1.jsonl", clone))
    learned(st, "acme/widget")
    root = env["TOKENECONOMIZER_HOME"]
    repo = paths.repo_identity(clone, env)
    real_replace = os.replace

    def refuse(src, dst):
        if os.path.isdir(src) and os.path.basename(src) == old:
            raise PermissionError(13, "Access is denied")
        return real_replace(src, dst)

    monkeypatch.setattr(repos.os, "replace", refuse)
    repos.adopt(root, repo, env)                     # no exception
    idx = Store(root).read(repos.INDEX) or {}
    assert old not in (idx.get("redirects") or {})
    assert os.path.isdir(os.path.join(root, "repos", old))


def test_an_old_remote_carries_its_learned_state_under_the_new_name(tmp_path, env):
    """`aaron-jx-li/tokeneconomizer` moved to `microsoft/tokeneconomizer`: the micro-skill state
    is filed by repository name, so it is renamed with the move."""
    clone = git_repo(str(tmp_path / "tokeneconomizer"), "https://github.com/microsoft/te")
    old = f"tokeneconomizer-{paths.digest('github.com/aaron-jx-li/te')}"
    st = state(env, old)
    legacy_session(st, "s1", transcript(tmp_path / "s1.jsonl", clone))
    learned(st, "aaron-jx-li/te")
    root = env["TOKENECONOMIZER_HOME"]
    assert repos.needs_migration(root)
    repos.run_migration(root, env)
    new = state(env, paths.repo_identity(clone, env).key)
    path = new.path("mechanisms", "envfacts_state", "microsoft__te.json")
    assert json.load(open(path))["repo"] == "microsoft/te"
    assert not repos.needs_migration(root)


def test_sessions_of_other_directories_leave_the_home_directory(tmp_path, env):
    """The home directory's state keeps the sessions started in ~; one started in a project
    below it moves there, and is folded again so that project learns from it."""
    home = env["HOME"]
    os.makedirs(os.path.join(home, ".git"))                  # the empty one
    proj = os.path.join(home, "agentcache")
    os.makedirs(proj)
    old = repos.legacy_key(proj, env)
    assert old == repos.legacy_key(home, env)                # 0.1.0 filed both under ~
    st = state(env, old)
    legacy_session(st, "mine", transcript(tmp_path / "mine.jsonl", home), cwd=home)
    legacy_session(st, "theirs", transcript(tmp_path / "theirs.jsonl", proj), cwd=proj)
    learned(st, "home")
    root = env["TOKENECONOMIZER_HOME"]
    repos.migrate_all(root, env)
    assert set(st.read("folded.json")) == {"mine"}
    assert [r["session_id"] for r in st.read_lines("ledger.jsonl")] == ["mine"]
    assert st.read("injection.json")                        # what ~ learned stays
    dst = state(env, paths.repo_identity(proj, env).key)
    assert dst.read(os.path.join("sessions", "theirs.json"))["folded"] is False
    assert "theirs" not in (dst.read("folded.json") or {})
    assert dst.read_lines("ledger.jsonl") == []              # its fold writes the record anew


def test_a_session_split_over_two_keys_counts_once(tmp_path, env):
    """0.1.0 recorded a session wherever the agent was, and could fold it under two keys; the
    first directory its log names says where it started, and the later fold's cost is kept."""
    home = env["HOME"]
    os.makedirs(os.path.join(home, ".git"))
    elsewhere = os.path.join(home, "bench")
    os.makedirs(elsewhere)
    tp = transcript(tmp_path / "s.jsonl", home)
    a = state(env, repos.legacy_key(home, env))
    b = state(env, "bench-0123456789")
    # The record at home names where a compaction's SessionStart found the agent: not the start.
    legacy_session(a, "s", tp, cwd=elsewhere, tokens=10.0, folded_at=1.0)
    legacy_session(b, "s", tp, cwd=None, tokens=50.0, folded_at=2.0)
    learned(a, "home")
    learned(b, "bench")
    root = env["TOKENECONOMIZER_HOME"]
    repos.migrate_all(root, env)
    assert not os.path.isdir(os.path.dirname(b.base))
    led = a.read_lines("ledger.jsonl")
    assert [(r["session_id"], r["effective_tokens"]) for r in led] == [("s", 50.0)]
    assert set(a.read("folded.json")) == {"s"}
    assert a.read(os.path.join("sessions", "s.json")).get("folded") is not False


def test_where_a_compaction_found_the_agent_does_not_move_its_session(tmp_path, env):
    home = env["HOME"]
    os.makedirs(os.path.join(home, ".git"))
    later = os.path.join(home, "slides")
    os.makedirs(later)
    st = state(env, repos.legacy_key(home, env))
    legacy_session(st, "s", transcript(tmp_path / "s.jsonl", home), cwd=later)
    repos.migrate_all(env["TOKENECONOMIZER_HOME"], env)
    assert set(st.read("folded.json")) == {"s"}
    assert not os.path.isdir(os.path.dirname(state(env, paths.repo_identity(later, env).key).base))


def test_a_repositorys_own_state_is_not_merged_away_by_a_visiting_session(tmp_path, env):
    """A repository's only session started in ~ and `cd`-ed in: it moves to ~, and the
    repository's state directory stays."""
    home = env["HOME"]
    repo_dir = git_repo(os.path.join(home, "widget"), "https://github.com/acme/widget")
    repo = paths.repo_identity(repo_dir, env)
    st = state(env, repo.key)
    legacy_session(st, "visit", transcript(tmp_path / "v.jsonl", home), cwd=repo_dir)
    learned(st, "acme/widget")
    repos.adopt(env["TOKENECONOMIZER_HOME"], repo, env)
    assert st.read("injection.json") and not os.path.exists(st.path("sessions", "visit.json"))
    at_home = state(env, paths.repo_identity(home, env).key)
    assert at_home.read(os.path.join("sessions", "visit.json"))["folded"] is False
    assert not os.path.isdir(os.path.join(env["TOKENECONOMIZER_HOME"], "merged"))


def test_a_checkout_only_ever_visited_keeps_its_state_directory(tmp_path, env):
    """Its sessions all started in ~, but a hook fired in it: it is a live repository, so the
    migration moves the sessions out and leaves its state directory where it is."""
    home = env["HOME"]
    repo_dir = git_repo(os.path.join(home, "bench"), "https://github.com/acme/bench")
    key = paths.repo_identity(repo_dir, env).key
    st = state(env, key)
    legacy_session(st, "v", transcript(tmp_path / "v.jsonl", home), cwd=repo_dir)
    learned(st, "acme/bench")
    repos.migrate_all(env["TOKENECONOMIZER_HOME"], env)
    assert st.read("injection.json") and "v" not in (st.read("folded.json") or {})
    assert not os.path.isdir(os.path.join(env["TOKENECONOMIZER_HOME"], "merged"))


def test_learned_state_moves_into_an_empty_mechanisms_directory(tmp_path, env):
    clone = git_repo(str(tmp_path / "other-name"), "https://github.com/acme/widget")
    old = state(env, repos.legacy_key(clone, env))
    legacy_session(old, "s1", transcript(tmp_path / "s1.jsonl", clone))
    learned(old, "acme/widget")
    repo = paths.repo_identity(clone, env)
    new = state(env, repo.key)
    new.ensure("mechanisms")                       # what `ctx.mech_dir` leaves behind
    repos.adopt(env["TOKENECONOMIZER_HOME"], repo, env)
    assert os.listdir(new.path("mechanisms")) == ["envfacts_state"]


def test_a_pinned_session_follows_its_repository_into_a_merge(tmp_path, env):
    root = env["TOKENECONOMIZER_HOME"]
    gone = git_repo(str(tmp_path / "x"), "https://github.com/o/x")
    repo = paths.repo_identity(gone, env)
    repos.pin(root, "claude", "s1", repo, gone)
    state(env, repo.key).write("notices.json", {"a": 1})
    target = git_repo(str(tmp_path / "y"), "https://github.com/o/y")
    assert repos.merge(root, repo.key, paths.repo_identity(target, env)) is not None
    assert repos.pinned(root, "claude", "s1").key == paths.repo_identity(target, env).key
    # A session pinned to that key after the merge is the key's again, written to or not.
    repos.pin(root, "claude", "s2", repo, gone)
    assert repos.pinned(root, "claude", "s2").key == repo.key


def test_te_repos_lists_merges_and_says_what_it_did(tmp_path, env, monkeypatch, capsys):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    a = git_repo(str(tmp_path / "a"), "https://github.com/o/a")
    call(env, {"hook_event_name": "SessionStart", "session_id": "s1", "cwd": a}, "claude")
    stray = state(env, "orphan-0000000000")
    legacy_session(stray, "s0", str(tmp_path / "missing.jsonl"), cwd=str(tmp_path / "gone"))
    assert cli.main(["--host", "claude", "--cwd", a, "repos"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("o/a  [a-") and "<- this directory" in out
    assert "orphan-0000000000" in out and "identified   by its origin remote" in out
    assert cli.main(["--host", "claude", "--cwd", a, "repos", "merge", "orphan-0000000000"]) == 0
    out = capsys.readouterr().out
    assert "merged orphan-0000000000 into a-" in out and "claude 1 session(s)" in out
    assert "s0" in state(env, paths.repo_identity(a, env).key).read("folded.json")
    assert cli.main(["--host", "claude", "--cwd", a, "repos", "merge", "nope"]) == 2


# ---- deny lists are per checkout --------------------------------------------------------------
def test_a_deny_list_applied_in_one_clone_is_still_proposed_in_another(tmp_path):
    st = Store(str(tmp_path / "st"))
    a = git_repo(str(tmp_path / "a"), "https://github.com/o/r")
    b = git_repo(str(tmp_path / "b"), "https://github.com/o/r")
    assert denylist.apply(a, ["CronCreate"], st) == ["CronCreate"]
    assert denylist.applied_for(st, a) == ["CronCreate"] and denylist.applied_for(st, b) == []
    assert denylist.restore(b, st) == []                     # nothing of b's to take back
    assert denylist.applied_for(st, a) == ["CronCreate"]
    assert denylist.apply(b, ["CronCreate", "Monitor"], st) == ["CronCreate", "Monitor"]
    assert denylist.applied(st) == ["CronCreate", "Monitor"]


def test_a_deny_list_record_from_0_1_0_is_still_read(tmp_path):
    st = Store(str(tmp_path / "st"))
    a = git_repo(str(tmp_path / "a"))
    path = denylist.settings_path(a, "claude")
    st.write(denylist.APPLIED, {"tools": ["Monitor"], "path": path, "updated": 1})
    assert denylist.applied_for(st, a) == ["Monitor"]
    os.makedirs(os.path.dirname(path))
    json.dump({"permissions": {"deny": ["Monitor", "Mine"]}}, open(path, "w"))
    assert denylist.restore(a, st) == ["Monitor"]
    assert json.load(open(path)) == {"permissions": {"deny": ["Mine"]}}
    assert denylist.applied(st) == []


def test_pending_sessions_fold_oldest_first(tmp_path, env):
    from tokeneconomizer.plugin import hosts
    from tokeneconomizer.plugin.context import LiveContext
    a = git_repo(str(tmp_path / "a"), "https://github.com/o/a")
    ctx = LiveContext.build(hosts.ClaudeCode(), a, env)
    for sid, started in (("z", 1.0), ("a", 3.0), ("m", 2.0)):
        ctx.store.write(os.path.join("sessions", f"{sid}.json"),
                        {"session_id": sid, "started": started, "ended": started})
    assert fold.pending_sessions(ctx) == ["z", "m", "a"]


def test_a_session_start_says_once_what_carrying_state_over_moved(tmp_path, env):
    clone = git_repo(str(tmp_path / "other-name"), "https://github.com/acme/widget")
    old = state(env, repos.legacy_key(clone, env))
    legacy_session(old, "s1", transcript(tmp_path / "s1.jsonl", clone))
    start = {"hook_event_name": "SessionStart", "cwd": clone, "source": "startup"}
    out = call(env, dict(start, session_id="a"), "claude")
    assert "now filed by repository (1 session(s) moved, 1 directory(ies) merged" in \
        out["systemMessage"] and "/tokeneconomizer:repos" in out["systemMessage"]
    assert "systemMessage" not in call(env, dict(start, session_id="b"), "claude")
    assert "progress" not in call(env, dict(start, session_id="c"), "copilot")


# ---- carrying over each agent's state, from its own session logs --------------------------------
# Every agent keeps its own subdirectory under a repository's state, learned from its own log
# format; carrying state over must move each, and read where each agent's log says it started.
from tests.test_live_core import copilot_events  # noqa: E402
from tests.test_live_hooks import COPILOT_TRAP, pi_trap_session  # noqa: E402

AGENTS = ["claude", "codex", "copilot", "pi"]
FACT = "`python -m pytest …` works."


def claude_trap(path, cwd, i):
    """The pytest trap as a Claude Code transcript, which names its directory on every entry."""
    calls = [("Edit", {"file_path": f"{cwd}/pkg/mod{i}.py", "old_string": "a", "new_string": "b"},
              "ok", False),
             ("Bash", {"command": "pytest -q tests/test_a.py"}, "bash: pytest: command not found",
              True),
             ("Bash", {"command": "python -m pytest -q tests/test_a.py"}, "1 passed in 0.1s", False)]
    lines = [{"type": "user", "cwd": cwd,
              "message": {"role": "user", "content": f"fix bug {i}"}}]
    for n, (name, inp, result, err) in enumerate(calls):
        lines.append({"type": "assistant", "cwd": cwd, "message": {
            "id": f"msg_{i}_{n}", "model": "claude-haiku-5-5", "role": "assistant",
            "usage": {"input_tokens": 10, "cache_read_input_tokens": 1000,
                      "cache_creation_input_tokens": 100, "output_tokens": 50},
            "content": [{"type": "tool_use", "id": f"tu{i}{n}", "name": name, "input": inp}]}})
        lines.append({"type": "user", "cwd": cwd, "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"tu{i}{n}", "content": result,
             "is_error": err}]}})
    with open(path, "w") as fh:
        fh.write("\n".join(json.dumps(x) for x in lines) + "\n")
    return path


def codex_trap(path, cwd, i):
    """The pytest trap as a Codex rollout: `session_meta` names the directory."""
    items = [{"type": "FileChange", "id": f"f{i}", "status": "completed",
              "changes": [{"path": f"{cwd}/pkg/mod{i}.py", "kind": "update"}]},
             {"type": "CommandExecution", "id": f"a{i}", "command": "pytest -q tests/test_a.py",
              "aggregated_output": "bash: pytest: command not found", "exit_code": 127,
              "status": "completed"},
             {"type": "CommandExecution", "id": f"b{i}",
              "command": "python -m pytest -q tests/test_a.py",
              "aggregated_output": "1 passed in 0.1s", "exit_code": 0, "status": "completed"}]
    recs = [{"type": "session_meta", "payload": {"id": f"thread-{i}", "cwd": cwd,
                                                 "cli_version": "0.163.0"}},
            {"type": "turn_context", "payload": {"turn_id": "t1", "model": "gpt-5.4-mini"}}]
    recs += [{"type": "event_msg", "payload": {"type": "item_completed", "turn_id": "t1",
                                               "item": it}} for it in items]
    recs.append({"type": "event_msg", "payload": {"type": "token_count", "info": {
        "total_token_usage": {"input_tokens": 3000, "cached_input_tokens": 2000,
                              "cache_write_input_tokens": 0, "output_tokens": 90,
                              "reasoning_output_tokens": 10}}}})
    with open(path, "w") as fh:
        fh.write("\n".join(json.dumps(r) for r in recs) + "\n")
    return path


def agent_session(env, host, sid, cwd, i, tmp_path, monkeypatch, end=True):
    """One session on `host`, its log in that agent's own format and place; returns what its
    SessionStart answered."""
    base = {"session_id": sid, "cwd": cwd}
    if host == "copilot":                   # Copilot's log sits where its session id says
        home = tmp_path / "copilot-home"
        monkeypatch.setenv("COPILOT_HOME", str(home))
        copilot_events(str(home / "session-state" / sid / "events.jsonl"), COPILOT_TRAP,
                       cwd=cwd, sid=sid)
    else:
        make = {"claude": claude_trap, "codex": codex_trap, "pi": pi_trap_session}[host]
        base["transcript_path"] = make(str(tmp_path / f"rollout-{host}-{sid}.jsonl"), cwd, i)
    start = call(env, dict(base, hook_event_name="SessionStart", source="startup"), host)
    if end:
        call(env, dict(base, hook_event_name="SessionEnd", reason="other"), host)
    return start


def injected(out, host):
    return (out.get("additionalContext") if host == "copilot" else
            (out.get("hookSpecificOutput") or {}).get("additionalContext")) or ""


def as_0_1_0(env, key, old_key):
    """Make a state root look as 0.1.0 left it: `key`'s state under `old_key`, no index, no
    pins, no identity file, and session records that name no directory (only the logs do)."""
    root = env["TOKENECONOMIZER_HOME"]
    os.replace(os.path.join(root, "repos", key), os.path.join(root, "repos", old_key))
    for p in (os.path.join(root, "index.json"), os.path.join(root, "repos", old_key, "repo.json")):
        if os.path.exists(p):
            os.remove(p)
    import shutil
    shutil.rmtree(os.path.join(root, "sessions"), ignore_errors=True)
    for host in AGENTS:
        st = state(env, old_key, host)
        d = st.path("sessions")
        for name in (os.listdir(d) if os.path.isdir(d) else []):
            rec = st.read(os.path.join("sessions", name))
            rec.pop("cwd", None)
            st.write(os.path.join("sessions", name), rec)


@pytest.mark.parametrize("host", AGENTS)
def test_each_agents_learned_state_follows_its_repository_out_of_a_folder_named_key(
        tmp_path, env, monkeypatch, host):
    old = git_repo(str(tmp_path / "old-name"), "https://github.com/acme/widget")
    other = git_repo(str(tmp_path / "widget"), "https://github.com/acme/widget")
    key = paths.repo_identity(old, env).key
    for i in range(4):
        agent_session(env, host, f"s{i}", old, i, tmp_path, monkeypatch)
    assert FACT in state(env, key, host).read("injection.json")["rendered"]
    legacy = repos.legacy_key(old, env)
    assert legacy.startswith("old-name-") and legacy != key
    as_0_1_0(env, key, legacy)
    out = agent_session(env, host, "next", other, 9, tmp_path, monkeypatch, end=False)
    assert FACT in injected(out, host)                       # learned in the other clone
    moved = state(env, key, host)
    assert {f"s{i}" for i in range(4)} <= set(moved.read("folded.json"))
    assert len(moved.read_lines("ledger.jsonl")) == 4
    merged = os.listdir(os.path.join(env["TOKENECONOMIZER_HOME"], "merged"))
    assert [m.split(".")[0] for m in merged] == [legacy]


@pytest.mark.parametrize("host", AGENTS)
def test_each_agents_session_from_a_project_leaves_the_home_directory(
        tmp_path, env, monkeypatch, host):
    """The empty `~/.git` filed a project's sessions under ~; each agent's log says where its
    session started, and the session moves there and is folded again."""
    home = env["HOME"]
    proj = os.path.join(home, "proposal")
    os.makedirs(proj)
    agent_session(env, host, "own", home, 1, tmp_path, monkeypatch)
    agent_session(env, host, "theirs", proj, 2, tmp_path, monkeypatch)
    # 0.1.0 put both under one key: the empty ~/.git made ~ the "repository" of both.
    os.makedirs(os.path.join(home, ".git"))
    legacy = repos.legacy_key(proj, env)
    hk, pk = paths.repo_identity(home, env).key, paths.repo_identity(proj, env).key
    assert legacy == hk
    src = state(env, pk, host)
    dst_old = state(env, hk, host)
    for sid in ("theirs",):
        os.makedirs(dst_old.path("sessions"), exist_ok=True)
        os.replace(src.path("sessions", f"{sid}.json"), dst_old.path("sessions", f"{sid}.json"))
    dst_old.write("folded.json", dict(dst_old.read("folded.json"), **src.read("folded.json")))
    for r in src.read_lines("ledger.jsonl"):
        dst_old.append("ledger.jsonl", r)
    first_fold = max(r["folded_at"] for r in src.read_lines("ledger.jsonl"))
    import shutil
    shutil.rmtree(os.path.dirname(src.base))
    as_0_1_0(env, hk, hk)
    repos.run_migration(env["TOKENECONOMIZER_HOME"], env)
    assert set(dst_old.read("folded.json")) == {"own"}
    back = state(env, pk, host)
    assert set(back.read("folded.json")) == {"theirs"}       # folded again, where it ran
    (rec,) = back.read_lines("ledger.jsonl")
    assert rec["session_id"] == "theirs" and rec.get("error") is None
    assert rec["effective_tokens"] > 0 and rec["folded_at"] > first_fold
    assert os.path.exists(back.path("mechanisms", "envfacts_state", "proposal.json"))


@pytest.mark.parametrize("host", AGENTS)
def test_each_agents_state_follows_a_renamed_remote_under_its_new_name(
        tmp_path, env, monkeypatch, host):
    """A remote that moves (`aaron-jx-li/…` to `microsoft/…`) changes the key and the name the
    mechanisms file their state under; Codex's capability state is filed by name too."""
    repo_dir = git_repo(str(tmp_path / "widget"), "https://github.com/acme/widget")
    for i in range(4):
        agent_session(env, host, f"s{i}", repo_dir, i, tmp_path, monkeypatch)
    old = state(env, paths.repo_identity(repo_dir, env).key, host)
    assert os.path.exists(old.path("mechanisms", "envfacts_state", "acme__widget.json"))
    if host == "codex":
        assert os.path.exists(old.path("mechanisms", "interface_state", "acme__widget",
                                       "codex_untool.json"))
    git_repo(repo_dir, "https://github.com/acme-renamed/widget")
    out = agent_session(env, host, "next", repo_dir, 9, tmp_path, monkeypatch, end=False)
    assert FACT in injected(out, host)
    new = state(env, paths.repo_identity(repo_dir, env).key, host)
    mech = new.path("mechanisms")
    assert os.listdir(os.path.join(mech, "envfacts_state")) == ["acme-renamed__widget.json"]
    if host == "codex":
        assert os.listdir(os.path.join(mech, "interface_state")) == ["acme-renamed__widget"]
        assert new.read("untool.json")["sessions"] == 4


# ---- what a sandbox or an agent's own threads must not break ---------------------------------
@pytest.mark.parametrize("host", AGENTS)
def test_an_end_with_no_start_and_no_log_records_nothing(tmp_path, env, host):
    """Codex 0.163.0 sends SessionEnd for a thread that never ran a prompt; it is no session."""
    repo_dir = git_repo(str(tmp_path / "r"), "https://github.com/o/r")
    call(env, {"hook_event_name": "SessionEnd", "session_id": "ghost", "cwd": repo_dir,
               "reason": "other"}, host)
    st = state(env, paths.repo_identity(repo_dir, env).key, host)
    assert not os.path.exists(st.path("sessions", "ghost.json"))
    assert st.read_lines("ledger.jsonl") == []


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0,
                    reason="root writes through read-only permissions; Windows ignores chmod on "
                           "directories")
def test_a_change_where_the_state_is_read_only_is_refused_whole(tmp_path, env, monkeypatch,
                                                                 capsys):
    """Codex's default sandbox makes ~/.tokeneconomizer read-only. A change must be refused
    before it starts, never half made: a deny list in the repository's settings with no record
    of it, which `restore` would then not know to take back."""
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    repo_dir = git_repo(str(tmp_path / "r"), "https://github.com/o/r")
    call(env, {"hook_event_name": "SessionStart", "session_id": "s", "cwd": repo_dir}, "claude")
    st = state(env, paths.repo_identity(repo_dir, env).key)
    st.write("untool.json", {"proposal": ["CronCreate"], "sessions": 5})
    for d in (st.base, os.path.dirname(st.base)):
        os.chmod(d, 0o500)
    try:
        assert cli.main(["--host", "claude", "--cwd", repo_dir, "toolprune", "apply"]) == 1
        assert cli.main(["--host", "claude", "--cwd", repo_dir, "config", "set",
                         "loopguard=off"]) == 1
    finally:
        for d in (st.base, os.path.dirname(st.base)):
            os.chmod(d, 0o700)
    out = capsys.readouterr().out
    assert out.count("not changed: TokenEconomizer cannot write its state") == 2
    assert "Traceback" not in out
    assert not os.path.exists(denylist.settings_path(repo_dir, "claude"))
    assert not os.path.exists(os.path.join(os.path.dirname(st.base), "config.json"))
    # ... and reading still works there
    assert cli.main(["--host", "claude", "--cwd", repo_dir, "toolprune"]) == 0
