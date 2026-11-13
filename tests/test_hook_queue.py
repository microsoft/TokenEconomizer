"""One session's hooks run one at a time (`plugin/hookqueue.py`).

The first test is the report in #142: real hook processes started together, against a throwaway
state root where no agent CLI is reachable, so nothing calls a model. The others hold the session's
lock themselves, which makes a hook queue every time.
"""
import glob
import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from tokeneconomizer.plugin import hookqueue
from tokeneconomizer.plugin.controller import LiveController
from tokeneconomizer.plugin.store import Store

from test_live_hooks import (GREEN, _first, _post, _turn, call, env, repo,  # noqa: F401
                             store_for, stuck_calls)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READ = ("Read", {"file_path": "/r/a.py"}, "ok")


def base(repo, sid="s", key="k"):
    return {"session_id": sid, "transcript_path": "", "cwd": repo, "prompt_id": key}


def start_turn(env, repo, sid="s", key="k"):
    call(env, dict(base(repo, sid, key), hook_event_name="UserPromptSubmit", prompt="go"))


def stop_turn(env, repo, sid="s", key="k"):
    call(env, dict(base(repo, sid, key), hook_event_name="Stop", stop_hook_active=False))


def locks(env):
    return os.path.join(env["TOKENECONOMIZER_HOME"], "locks", "claude")


def queue_of(env, sid="s"):
    return os.path.join(locks(env), f"{sid}.queue")


def waiting(env, sid="s"):
    return len(glob.glob(os.path.join(queue_of(env, sid), "*.json")))


def behind_the_lock(env, payload, sid="s"):
    """One hook call while another hook of the session holds its lock."""
    with Store(locks(env)).locked(sid, timeout=2) as got:
        assert got
        return call(env, payload)


def doc(env, repo, sid="s"):
    return store_for(env, repo).read(os.path.join("sessions", f"{sid}.json"))


def events(env, repo, kind, sid="s"):
    return [e for e in store_for(env, repo).read_lines("events.jsonl")
            if e.get("kind") == kind and e.get("session_id") == sid]


@pytest.fixture
def short_waits(monkeypatch):
    for event in hookqueue.WAITS:
        monkeypatch.setitem(hookqueue.WAITS, event, 0.01)


# ---- the report -----------------------------------------------------------------------------
def test_tool_calls_made_in_parallel_are_all_recorded(tmp_path):
    home, state, cwd = tmp_path / "home", tmp_path / "state", tmp_path / "work"
    for d in (home, state, cwd):
        d.mkdir()
    env = dict(os.environ, TOKENECONOMIZER_HOME=str(state), HOME=str(home), USERPROFILE=str(home),
               CLAUDE_CONFIG_DIR=str(home / ".claude"), PYTHONUTF8="1", PYTHONPATH=REPO)
    env.pop("TE_SIDECALL", None)
    hook = [sys.executable, os.path.join(REPO, "plugin", "scripts", "te_bootstrap.py"), "hook"]

    def payload(i):
        return json.dumps({"hook_event_name": "PostToolUse", "session_id": "race", "cwd": str(cwd),
                           "transcript_path": str(cwd / "t.jsonl"), "tool_name": "Read",
                           "tool_input": {"file_path": str(cwd / f"f{i}")},
                           "tool_response": {"type": "text"}})

    subprocess.run(hook, input=payload(0), text=True, capture_output=True, env=env, cwd=str(cwd),
                   timeout=60)
    hooks = [subprocess.Popen(hook, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, env=env, cwd=str(cwd))
             for _ in range(8)]
    for i, p in enumerate(hooks, 1):
        p.stdin.write(payload(i))
        p.stdin.close()
    for p in hooks:
        p.wait(timeout=60)
        assert p.returncode == 0 and p.stdout.read().strip() == "{}", p.stderr.read()
    for p in hooks:
        p.stdout.close()
        p.stderr.close()
    docs = glob.glob(str(state / "repos" / "*" / "*" / "sessions" / "race.json"))
    assert len(docs) == 1
    with open(docs[0], encoding="utf-8") as fh:
        assert json.load(fh)["turn"]["cum_turns"] == 9
    assert not (state / "errors.log").exists()


# ---- queueing -------------------------------------------------------------------------------
def test_a_hook_without_the_lock_queues_and_the_next_one_applies_it(env, repo, short_waits):
    start_turn(env, repo)
    assert behind_the_lock(env, _post(base(repo), *READ)) == {}
    assert waiting(env) == 1 and doc(env, repo)["turn"]["cum_turns"] == 0
    call(env, _post(base(repo), *READ))
    assert waiting(env) == 0 and doc(env, repo)["turn"]["cum_turns"] == 2


def test_the_holder_applies_what_queued_behind_it(env, repo, monkeypatch):
    start_turn(env, repo)
    real = LiveController.post_tool

    def holder(self):
        out = real(self)
        if not self.replaying:
            hookqueue.enqueue(queue_of(env), _post(base(repo), *READ))
        return out

    monkeypatch.setattr(LiveController, "post_tool", holder)
    call(env, _post(base(repo), *READ))
    assert waiting(env) == 0 and doc(env, repo)["turn"]["cum_turns"] == 2


def test_queued_events_apply_oldest_first(env, repo, short_waits):
    prompt = dict(base(repo), hook_event_name="UserPromptSubmit", prompt="go")
    assert behind_the_lock(env, prompt) == {}
    assert behind_the_lock(env, _post(base(repo), *READ)) == {}
    call(env, _post(base(repo), *READ))
    turn = doc(env, repo)["turn"]
    assert turn["turn_key"] == "k" and turn["cum_turns"] == 2


def test_session_start_answers_itself(env, repo, short_waits):
    start = dict(base(repo), hook_event_name="SessionStart", source="startup")
    behind_the_lock(env, start)
    assert waiting(env) == 0 and doc(env, repo)["source"] == "startup"


def test_a_prompt_that_may_carry_the_map_is_not_queued():
    def ctrl(repomap):
        return SimpleNamespace(cfg=SimpleNamespace(repomap=repomap))
    assert hookqueue._queues(ctrl("off"), "UserPromptSubmit")
    assert hookqueue._queues(ctrl("start"), "UserPromptSubmit")
    assert not hookqueue._queues(ctrl("on"), "UserPromptSubmit")
    assert hookqueue._queues(ctrl("on"), "PostToolUse")
    assert not hookqueue._queues(ctrl("off"), "SessionStart")


def test_a_hook_that_cannot_queue_runs(env, repo, short_waits, monkeypatch):
    start_turn(env, repo)
    monkeypatch.setattr(hookqueue, "enqueue", lambda queue, payload: False)
    behind_the_lock(env, _post(base(repo), *READ))
    assert doc(env, repo)["turn"]["cum_turns"] == 1


def test_a_replay_that_fails_does_not_fail_the_hook_replaying_it(env, repo, short_waits,
                                                                  monkeypatch):
    start_turn(env, repo)
    behind_the_lock(env, _post(base(repo), *READ))
    real = LiveController.post_tool

    def broken(self):
        if self.replaying:
            raise RuntimeError("replayed")
        return real(self)

    monkeypatch.setattr(LiveController, "post_tool", broken)
    assert call(env, _post(base(repo), *READ)) == {}
    assert waiting(env) == 0 and doc(env, repo)["turn"]["cum_turns"] == 1
    with open(os.path.join(env["TOKENECONOMIZER_HOME"], "errors.log"), encoding="utf-8") as fh:
        assert "RuntimeError: replayed" in fh.read()


def test_an_unreadable_queue_entry_is_dropped(env, repo):
    start_turn(env, repo)
    os.makedirs(queue_of(env))
    with open(os.path.join(queue_of(env), "0.json"), "w", encoding="utf-8") as fh:
        fh.write("{not json")
    call(env, _post(base(repo), *READ))
    assert waiting(env) == 0 and doc(env, repo)["turn"]["cum_turns"] == 1


def test_every_wait_is_short_of_every_agents_hook_timeout():
    """Codex gives SessionEnd 3 s. A hook also has to start, so a second is kept in hand."""
    seen = 0
    for path in glob.glob(os.path.join(REPO, "plugin", "hooks", "*.json")):
        with open(path, encoding="utf-8") as fh:
            spec = json.load(fh).get("hooks") or {}
        for event, groups in spec.items():
            for group in groups if event in hookqueue.WAITS else ():
                for h in group.get("hooks") or []:
                    if h.get("timeout"):
                        seen += 1
                        assert hookqueue.WAITS[event] + 1 < h["timeout"], (path, event)
    assert seen >= 15


def test_idle_locks_go_and_a_queue_with_events_stays(tmp_path):
    base_dir = str(tmp_path)
    old = time.time() - hookqueue.IDLE_S - 60
    paths = {name: os.path.join(base_dir, name)
             for name in (".gone.lock", ".kept.lock", "gone.queue", "kept.queue")}
    for name in (".gone.lock", ".kept.lock"):
        open(paths[name], "w", encoding="utf-8").close()
    os.makedirs(paths["gone.queue"])
    os.makedirs(paths["kept.queue"])
    open(os.path.join(paths["kept.queue"], "1.json"), "w", encoding="utf-8").close()
    for name in (".gone.lock", "gone.queue", "kept.queue"):
        os.utime(paths[name], (old, old))
    hookqueue._prune(base_dir)
    assert sorted(n for n in os.listdir(base_dir) if n != ".pruned") == [".kept.lock", "kept.queue"]


def test_the_prune_leaves_the_lock_of_a_session_older_than_idle(tmp_path):
    """A session's lock file is made at its first hook, and taking the lock writes nothing to it,
    so the file of a session running for longer than IDLE_S looked idle. The prune removed it
    while a hook held it, and on POSIX a parallel hook of that session then locked a new file
    and ran beside the holder."""
    base_dir = os.path.join(str(tmp_path), "locks", "claude")
    os.makedirs(base_dir)
    lock = os.path.join(base_dir, ".s.lock")
    open(lock, "w", encoding="utf-8").close()
    old = time.time() - hookqueue.IDLE_S - 60
    os.utime(lock, (old, old))
    ctrl = SimpleNamespace(ctx=SimpleNamespace(root=str(tmp_path)),
                           host=SimpleNamespace(name="claude"), sid="s", p={},
                           cfg=SimpleNamespace(repomap="off"))
    beside = []

    def handler():
        with Store(base_dir).locked("s", timeout=0.1) as got:
            beside.append(got)
        return {}
    assert hookqueue.run(ctrl, "PostToolUse", handler) == {}
    assert beside == [False]
    assert os.path.getmtime(lock) > old + hookqueue.IDLE_S


# ---- what a queued hook would have said -----------------------------------------------------
def stuck_turn(env, repo):
    """A repository the guard has learned, the calls of a stuck turn, and at which of them the
    redirect and the stop notice come when every hook answers."""
    for k in range(5):
        _turn(env, repo, "warm", f"g{k}", GREEN)
    calls = stuck_calls(40)
    outs = _turn(env, repo, "probe", "p", calls)
    nudge = _first(outs, lambda o: "hookSpecificOutput" in o)
    notice = nudge + 1 + _first(outs[nudge + 1:], lambda o: o != {})
    return calls, nudge, notice


def run_until(env, repo, calls, end):
    start_turn(env, repo)
    return [call(env, _post(base(repo), *c)) for c in calls[:end]]


@pytest.mark.parametrize("ahead", [1, 2], ids=["next call", "the call after"])
def test_a_redirect_reached_on_a_queued_call_goes_out_with_the_next_one(env, repo, short_waits,
                                                                        ahead):
    """`stuck_calls` alternates success and failure, so the two cases are the two events the
    answer can be labelled with; a failed call's answer labelled PostToolUse would be dropped."""
    calls, nudge, _ = stuck_turn(env, repo)
    assert all(o == {} for o in run_until(env, repo, calls, nudge))
    assert behind_the_lock(env, _post(base(repo), *calls[nudge])) == {}
    assert not events(env, repo, "nudge")                     # queued: nothing decided or spent
    payload = _post(base(repo), *calls[nudge + ahead])
    out = call(env, payload)
    specific = out["hookSpecificOutput"]
    assert specific["hookEventName"] == payload["hook_event_name"]
    assert "Process check (automated)" in specific["additionalContext"]
    assert "nudged" in out["systemMessage"]
    assert len(events(env, repo, "nudge")) == 1 and doc(env, repo)["nudges"] == 1


def test_a_redirect_held_for_the_next_call_is_said_once(env, repo, short_waits):
    calls, nudge, _ = stuck_turn(env, repo)
    run_until(env, repo, calls, nudge)
    behind_the_lock(env, _post(base(repo), *calls[nudge]))
    behind_the_lock(env, _post(base(repo), *calls[nudge + 1]))     # a second one behind it
    assert waiting(env) == 2
    outs = [call(env, _post(base(repo), *c)) for c in calls[nudge + 2:nudge + 4]]
    assert "hookSpecificOutput" in outs[0] and outs[1] == {}
    assert len(events(env, repo, "nudge")) == 1


def test_a_turn_that_ends_first_drops_the_redirect_uncounted(env, repo, short_waits):
    calls, nudge, _ = stuck_turn(env, repo)
    run_until(env, repo, calls, nudge)
    behind_the_lock(env, _post(base(repo), *calls[nudge]))
    stop_turn(env, repo)
    session = doc(env, repo)
    assert session["nudges"] == 0 and session["turns"][-1]["nudges"] == 0
    assert not events(env, repo, "nudge")
    assert [e["action"] for e in events(env, repo, "undelivered")] == ["nudge"]
    start_turn(env, repo, key="k2")                           # and nothing leaks into the next
    assert call(env, _post(base(repo, key="k2"), *READ)) == {}


def test_the_stop_notice_reached_on_a_queued_call_goes_out_too(env, repo, short_waits):
    calls, _, notice = stuck_turn(env, repo)
    run_until(env, repo, calls, notice)
    assert behind_the_lock(env, _post(base(repo), *calls[notice])) == {}
    out = call(env, _post(base(repo), *calls[notice + 1]))
    assert "still looks stuck" in out["systemMessage"]
    assert doc(env, repo)["aborts"] == 1
    assert call(env, _post(base(repo), *calls[notice + 2])) == {}
