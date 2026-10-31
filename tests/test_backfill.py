"""Learning from the session logs that already exist when the plugin is first installed.

The plugin's folds start with its first session, so a repository worked in for months would
start empty. `backfill` reads the agent's own transcripts of earlier sessions in the same
repository, folds them with the folds' own code, and marks them folded so their own end does
not learn from them again: the facts, the tool evidence and the loop guard's envelope begin
from the history rather than from install.
"""
import io
import json
import os
import time

import pytest

from tokeneconomizer import loopguard
from tokeneconomizer.plugin import backfill, cli, hook
from tokeneconomizer.plugin.context import LiveContext
from tokeneconomizer.plugin.hosts import HOSTS
from tokeneconomizer.plugin.paths import repo_identity


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return {"TOKENECONOMIZER_HOME": str(tmp_path / "te"), "TE_FOLD_SYNC": "1",
            # A proposal, not a written deny list: backfill's own tests leave settings alone.
            "TE_TOOLPRUNE": "propose",
            "TE_MICROSKILLS": "envfacts",
            "HOME": str(home), "GIT_CEILING_DIRECTORIES": str(tmp_path),
            "PATH": os.environ.get("PATH", "")}


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / ".git" / "info").mkdir(parents=True)
    (r / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (r / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://github.com/acme/widget.git\n')
    return str(r)


def project_dir(env, path="/some/other/project"):
    """A Claude Code `projects/<dir>` as the agent writes it: one folder per project path."""
    d = os.path.join(backfill.claude_home(env), "projects",
                     path.replace("/", "-").lstrip("-"))
    os.makedirs(d, exist_ok=True)
    return d


def write_log(directory, sid, cwd, prompt="do the thing", prompt_id="p1", age_s=3600):
    """A Claude Code transcript with one prompt: a user entry, then per call an assistant
    `tool_use` (with usage) and its `tool_result`, each entry carrying `cwd` and `promptId`
    as 2.1.278 writes them."""
    calls = [
        ("Edit", {"file_path": os.path.join(cwd, "pkg/a.py"), "old_string": "a",
                  "new_string": "b"}, "ok", False),
        ("Bash", {"command": "pytest -q"}, "bash: pytest: command not found", True),
        ("Bash", {"command": "python -m pytest -q"}, "1 passed in 0.1s", False),
    ]
    base = {"cwd": cwd, "sessionId": sid, "promptId": prompt_id,
            "version": "2.1.285", "isSidechain": False}
    lines = [dict(base, type="user", message={"role": "user", "content": prompt})]
    for n, (name, inp, result, is_error) in enumerate(calls):
        lines.append(dict(base, type="assistant", message={
            "id": f"msg_{sid}_{n}", "model": "claude-opus-5-5", "role": "assistant",
            "usage": {"input_tokens": 10, "cache_read_input_tokens": 1000,
                      "cache_creation_input_tokens": 100, "output_tokens": 50},
            "content": [{"type": "tool_use", "id": f"tu{n}", "name": name, "input": inp}]}))
        lines.append(dict(base, type="user", message={"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"tu{n}", "content": result,
             "is_error": is_error}]}))
    path = os.path.join(directory, f"{sid}.jsonl")
    with open(path, "w") as fh:
        fh.write("\n".join(json.dumps(x) for x in lines) + "\n")
    old = time.time() - age_s
    os.utime(path, (old, old))
    return path


def ctx_for(env, repo, session_id="now"):
    return LiveContext.build(HOSTS["claude"](), repo, env, session_id=session_id)


def test_only_this_repositories_logs_are_old_enough_to_read(env, repo):
    """A log counts when its own `cwd` is this repository (a projects folder's name is a lossy
    encoding, so it is never trusted), and when it has gone quiet: the session that started the
    backfill is still being written, and its own end folds it for real."""
    here = project_dir(env)
    write_log(here, "old", repo, age_s=3600)
    write_log(here, "fresh", repo, age_s=5)
    write_log(project_dir(env, "/elsewhere"), "other", "/elsewhere", age_s=3600)
    sub = os.path.join(project_dir(env), "old", "subagents")
    os.makedirs(sub)
    open(os.path.join(sub, "agent-1.jsonl"), "w").close()

    found = backfill.sessions_for(repo_identity(repo, env), "claude", env)
    assert [sid for _p, sid in found] == ["old"]
    assert all(sid != "agent-1" for _p, sid in found)    # a subagent log is no session
    forced = backfill.sessions_for(repo_identity(repo, env), "claude", env, min_age_s=0)
    assert [sid for _p, sid in forced] == ["fresh", "old"]   # newest first


def test_a_backfill_learns_facts_evidence_and_the_envelope(env, repo):
    """The folds' own mechanisms run over the history: the tool evidence counts its sessions,
    the loop guard's envelope learns its ended-green turns, and the ledger prices them."""
    here = project_dir(env)
    for i in range(4):
        write_log(here, f"hist{i}", repo, prompt=f"fix bug {i}", prompt_id=f"p{i}",
                  age_s=3600 + i)
    ctx = ctx_for(env, repo)
    state = backfill.run(ctx)
    assert state["done"] and state["sessions"] == 4

    folded = ctx.store.read("folded.json") or {}
    assert {f"hist{i}" for i in range(4)} <= set(folded)
    assert all(v.get("backfill") for k, v in folded.items() if k.startswith("hist"))

    unt = ctx.store.read("untool.json") or {}
    assert unt["sessions"] == 4
    assert unt["prompts"] == 4

    tracker = loopguard.load_envelope(ctx.store)
    assert len(tracker.resolved_turns) == 4

    ledger = ctx.store.read_lines("ledger.jsonl")
    assert len(ledger) == 4 and all(r.get("backfill") for r in ledger)
    assert all(r.get("effective_tokens") for r in ledger)


def test_a_session_already_folded_is_not_learned_from_twice(env, repo):
    """Its own SessionEnd (or the next start's sweep) folds it for real: a backfill of a log
    whose id is already in `folded.json` takes nothing from it."""
    here = project_dir(env)
    write_log(here, "hist0", repo)
    ctx = ctx_for(env, repo)
    ctx.store.write("folded.json", {"hist0": {"at": time.time()}})
    state = backfill.run(ctx)
    assert state["sessions"] == 0
    assert ctx.store.read_lines("ledger.jsonl") == []


def test_the_limit_keeps_the_sessions_closest_to_now(env, repo):
    """A cap keeps the most recent sessions, whose facts and tools the next sessions meet.
    `backfill_sessions=0` reads the whole history."""
    here = project_dir(env)
    for i, age in enumerate((10, 5, 1)):        # hist0 oldest, hist2 newest
        write_log(here, f"hist{i}", repo, prompt_id=f"p{i}", age_s=3600 * age)
    ctx = ctx_for(env, repo)
    state = backfill.run(ctx, limit=2)
    assert state["sessions"] == 2
    assert set(ctx.store.read("folded.json") or {}) == {"hist1", "hist2"}

    # 0 reads the whole history: a later run takes the session the cap left behind.
    everything = dict(env, TE_BACKFILL_SESSIONS="0")
    ctx2 = ctx_for(everything, repo)
    assert backfill.run(ctx2)["sessions"] == 1
    assert set(ctx2.store.read("folded.json") or {}) == {"hist0", "hist1", "hist2"}


def test_the_first_session_starts_it_once(env, repo):
    """SessionStart starts the backfill on a repository's first session here; the marker it
    writes keeps every later start from reading the history again."""
    here = project_dir(env)
    for i in range(3):
        write_log(here, f"hist{i}", repo, prompt_id=f"p{i}")

    def start(sid):
        out = io.StringIO()
        hook.main([], io.StringIO(json.dumps({
            "hook_event_name": "SessionStart", "session_id": sid, "source": "startup",
            "cwd": repo, "transcript_path": ""})), out, env)
        return json.loads(out.getvalue().splitlines()[-1])

    start("live0")
    ctx = ctx_for(env, repo, session_id="live0")
    assert (ctx.store.read(backfill.STATE) or {}).get("done")
    assert (ctx.store.read("folded.json") or {}).get("hist0")
    assert backfill.should_run(ctx) is False

    for i in range(3, 5):
        write_log(here, f"hist{i}", repo, prompt_id=f"p{i}")
    start("live1")
    assert not (ctx.store.read("folded.json") or {}).get("hist3")


def test_backfill_off_reads_nothing(env, repo):
    here = project_dir(env)
    write_log(here, "hist0", repo)
    off = dict(env, TE_BACKFILL="off")
    ctx = ctx_for(off, repo)
    assert backfill.should_run(ctx) is False
    assert backfill.run(ctx)["skipped"] == "backfill=off"
    assert not (ctx.store.read("folded.json") or {})


def test_status_and_the_command_report_what_was_read(env, repo, capsys, monkeypatch):
    """`te status` says what the history read did, and `te backfill` is the command line for
    running it by hand. The CLI reads `os.environ`, as a real `te` run does."""
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    here = project_dir(env)
    write_log(here, "hist0", repo)
    assert cli.main(["--cwd", repo, "--host", "claude", "backfill"]) == 0
    out = capsys.readouterr().out
    assert '"sessions": 1' in out
    assert cli.main(["--cwd", repo, "--host", "claude", "status"]) == 0
    assert "backfill" in capsys.readouterr().out


def test_a_prompt_turn_is_split_where_the_agent_says(env, repo):
    """The envelope learns per prompt-turn: Claude Code's `promptId` groups the calls, and a
    log without one splits on the person's text entries."""
    here = project_dir(env)
    p1 = write_log(here, "a", repo, prompt="first", prompt_id="p1")
    p2 = write_log(here, "b", repo, prompt="second", prompt_id=None)
    base = {"cwd": repo, "sessionId": "b", "version": "2.1.285"}
    with open(p2, "a") as fh:
        fh.write(json.dumps(dict(base, type="user",
                                 message={"role": "user", "content": "third"})) + "\n")
        fh.write(json.dumps(dict(base, type="assistant", message={
            "id": "m2", "usage": {}, "content": [
                {"type": "tool_use", "id": "x", "name": "Read",
                 "input": {"file_path": "a"}}]})) + "\n")
    assert len(backfill._prompt_turn_calls(p1)) == 1
    assert len(backfill._prompt_turn_calls(p2)) == 2
