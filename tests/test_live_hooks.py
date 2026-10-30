"""The plugin's hooks, end to end, through `live.hook.main` with synthetic payloads.

Each test drives a whole stretch of a repository's life -- sessions starting, prompt-turns, tool
calls, sessions ending and being folded -- and pins one property the live layer must have:
learned facts reach the next session's context, the loop guard is silent until it has learned
the repository and then redirects before it tells the person, a deny list is written only once
the repository's sessions are idle and touches only its own deny rules, every session is folded
exactly once, and nothing here can break the agent's session.

Folds run inline (`TE_FOLD_SYNC=1`); in the plugin they are detached processes.
"""
import glob
import io
import json
import os
import time

import pytest

from tokeneconomizer.plugin import denylist, fold, hook, hosts, toolcheck
from tokeneconomizer.plugin.context import LiveContext
from tokeneconomizer.plugin.controller import LiveController
from tokeneconomizer.plugin.paths import repo_identity, repo_state_dir
from tokeneconomizer.plugin.store import Store


@pytest.fixture
def env(tmp_path):
    return {"TOKENECONOMIZER_HOME": str(tmp_path / "home"), "TE_FOLD_SYNC": "1",
            # A deny list is written at the fold here; the wait before it has its own tests.
            "TE_TOOLPRUNE_QUIET_MINUTES": "0",
            # Facts are learned here without a model call. Micro-skills is off by default, and
            # its `on` adds the proposer; both have their own tests.
            "TE_MICROSKILLS": "envfacts",
            "GIT_CEILING_DIRECTORIES": str(tmp_path), "PATH": os.environ.get("PATH", "")}


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / ".git" / "info").mkdir(parents=True)
    (r / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (r / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://github.com/acme/widget.git\n')
    (r / "pyproject.toml").write_text("[project]\nname='w'\n")
    return str(r)


def call(env, payload, host=None):
    """One hook call: its final JSON object, plus the `progress` notice lines a host shows
    ahead of it (Copilot), when there are any."""
    out = io.StringIO()
    hook.main(["--host", host] if host else [], io.StringIO(json.dumps(payload)), out, env)
    lines = out.getvalue().splitlines()
    res = json.loads(lines[-1])
    progress = [json.loads(ln) for ln in lines[:-1]]
    assert all(p["type"] == "progress" for p in progress)
    if progress:
        res["progress"] = [p["message"] for p in progress]
    return res


def store_for(env, repo, host="claude"):
    return Store(repo_state_dir(repo_identity(repo, env), host, env))


def write_session(path, prompts, model="claude-opus-5-5"):
    """An interactive Claude Code transcript: per prompt, a user entry, then per tool call an
    assistant entry (tool_use, with usage) and a user entry (tool_result)."""
    lines, n = [], 0
    for text, calls in prompts:
        lines.append({"type": "user", "message": {"role": "user", "content": text}})
        for name, inp, result, is_error in calls:
            n += 1
            lines.append({"type": "assistant", "message": {
                "id": f"msg_{os.path.basename(path)}_{n}", "model": model, "role": "assistant",
                "usage": {"input_tokens": 10, "cache_read_input_tokens": 1000,
                          "cache_creation_input_tokens": 100, "output_tokens": 50},
                "content": [{"type": "tool_use", "id": f"tu{n}", "name": name, "input": inp}]}})
            lines.append({"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": f"tu{n}", "content": result,
                 "is_error": is_error}]}})
    with open(path, "w") as fh:
        fh.write("\n".join(json.dumps(x) for x in lines) + "\n")
    return path


def trap_session(path, repo, i):
    """An environment trap every session falls into: bare `pytest` is missing here."""
    return write_session(path, [(f"fix bug {i}", [
        ("Edit", {"file_path": f"{repo}/pkg/mod{i}.py", "old_string": "a", "new_string": "b"},
         "ok", False),
        ("Bash", {"command": "pytest -q tests/test_a.py"}, "bash: pytest: command not found", True),
        ("Bash", {"command": "python -m pytest -q tests/test_a.py"}, "1 passed in 0.1s", False),
    ])])


def run_session(env, repo, sid, transcript, host=None, source="startup"):
    base = {"session_id": sid, "transcript_path": transcript, "cwd": repo}
    start = call(env, dict(base, hook_event_name="SessionStart", source=source), host)
    call(env, dict(base, hook_event_name="SessionEnd", reason="other"), host)
    return start


# ---- injection ---------------------------------------------------------------------------
def prevented_session(path, repo, i):
    """The agent was told: it runs the working form at once and never meets the trap."""
    return write_session(path, [(f"fix bug {i}", [
        ("Edit", {"file_path": f"{repo}/pkg/mod{i}.py", "old_string": "a", "new_string": "b"},
         "ok", False),
        ("Bash", {"command": "python -m pytest -q tests/test_a.py"}, "1 passed in 0.1s", False),
    ])])


def test_an_injected_fact_that_prevents_its_failure_stays_injected(env, repo, tmp_path):
    """The session-start hook injects; a fold in another process learns afterwards. The fold is
    told which facts the session was given, so a fact that prevented its failure counts as having
    fired, and stays in the block."""
    for i in range(5):
        run_session(env, repo, f"s{i}", trap_session(str(tmp_path / f"s{i}.jsonl"), repo, i))
    starts = [run_session(env, repo, f"p{i}", prevented_session(str(tmp_path / f"p{i}.jsonl"),
                                                                repo, i + 5))
              for i in range(8)]
    claim = "`pytest …` fails here with command not found; `python -m pytest …` works."
    assert all(claim in s["hookSpecificOutput"]["additionalContext"] for s in starts)
    (path,) = glob.glob(store_for(env, repo).path("mechanisms", "envfacts_state", "*.json"))
    life = json.load(open(path))["facts"]["form_repair:run_tests:pytest"]["life"]
    assert life["fire_num"] == life["fire_den"]          # every session it was given counted
    assert store_for(env, repo).read("sessions/p7.json")["injected_ids"] == [
        "form_repair:run_tests:pytest"]


def test_a_learned_fact_reaches_the_next_session_and_is_announced_once(env, repo, tmp_path):
    starts = [run_session(env, repo, f"s{i}", trap_session(str(tmp_path / f"s{i}.jsonl"), repo, i))
              for i in range(6)]
    assert all(s == {} for s in starts[:4])           # the fact must clear its firing gate first
    ctx = starts[4]["hookSpecificOutput"]["additionalContext"]
    assert "`pytest …` fails here with command not found; `python -m pytest …` works." in ctx
    assert "learned fact" in starts[4]["systemMessage"]
    assert "systemMessage" not in starts[5]           # unchanged manual: no second notice
    assert starts[5]["hookSpecificOutput"]["additionalContext"] == ctx


def test_micro_skills_is_off_by_default_and_learns_nothing(env, repo, tmp_path, monkeypatch,
                                                           capsys):
    """The sessions that teach the fact above leave nothing behind unless it is turned on."""
    from tokeneconomizer.plugin import cli
    default = {k: v for k, v in env.items() if k != "TE_MICROSKILLS"}
    starts = [run_session(default, repo, f"s{i}",
                          trap_session(str(tmp_path / f"s{i}.jsonl"), repo, i))
              for i in range(6)]
    assert all(s == {} for s in starts)
    assert store_for(default, repo).read("injection.json") is None
    monkeypatch.delenv("TE_MICROSKILLS", raising=False)
    for k, v in default.items():
        monkeypatch.setenv(k, v)
    assert cli.main(["--host", "claude", "--cwd", repo, "microskills"]) == 0
    assert "`config set microskills=on` turns it on" in capsys.readouterr().out


def test_micro_skills_on_runs_the_proposer_without_paid_and_charges_it(
        env, repo, tmp_path, monkeypatch):
    """`on` is the experiments' micro-skills: the free facts plus the proposer, here every
    third session (after the 2nd, 5th, ...), as a session carries more work than a benchmark
    task. Turning it on is what allows the proposer's model call; `paid` is the loop guard's."""
    from tokeneconomizer.plugin import sidecall
    models = []

    def fake_call(prompt, model="haiku", **kw):
        models.append(model)
        return "{}", 120.0
    monkeypatch.setattr(sidecall, "claude_call", fake_call)
    on = dict(env, TE_MICROSKILLS="on")
    for i in range(5):
        run_session(on, repo, f"s{i}", trap_session(str(tmp_path / f"s{i}.jsonl"), repo, i))
    ledger = store_for(on, repo).read_lines("ledger.jsonl")
    assert models == ["haiku", "haiku"]
    assert [r["overhead"]["optimizer_tokens"] for r in ledger] == [0, 120.0, 0, 0, 120.0]
    assert store_for(on, repo).read("injection.json")["profile"] == "envfacts+distiller"


def multi_prompt_trap_session(path, repo, i, prompts=4, every=False):
    """A session of `prompts` prompts that meets the missing `pytest` at its first test run (or,
    with `every`, at each): later prompts use the working form, as the conversation shows it."""
    def prompt(k):
        edit = ("Edit", {"file_path": f"{repo}/pkg/m{i}_{k}.py", "old_string": "a",
                         "new_string": "b"}, "ok", False)
        known = ("Bash", {"command": "python -m pytest -q tests/test_a.py"}, "1 passed in 0.1s",
                 False)
        trap = ("Bash", {"command": "pytest -q tests/test_a.py"},
                "bash: pytest: command not found", True)
        return (f"fix bug {i}.{k}", [edit, trap, known] if every or k == 0 else [edit, known])
    return write_session(path, [prompt(k) for k in range(prompts)])


def test_facts_learn_per_session_however_many_prompts_it_ran(env, repo, tmp_path):
    """Unlike tool pruning, the facts count sessions, not prompts. Within a session the agent
    already knows the fix from its own earlier failure, so its later prompts would count as
    exercising the fact without the failure: per prompt, a trap met once a session fires in a
    quarter of them and never clears the firing gate. Four-prompt sessions reach the fact at the
    fifth start, as one-prompt sessions do (above)."""
    claim = "`pytest …` fails here with command not found; `python -m pytest …` works."
    starts = [run_session(env, repo, f"s{i}",
                          multi_prompt_trap_session(str(tmp_path / f"s{i}.jsonl"), repo, i))
              for i in range(6)]
    assert all(s == {} for s in starts[:4])
    assert claim in starts[4]["hookSpecificOutput"]["additionalContext"]


def test_resume_does_not_inject_again_and_shadow_never_injects(env, repo, tmp_path):
    for i in range(5):
        run_session(env, repo, f"s{i}", trap_session(str(tmp_path / f"s{i}.jsonl"), repo, i))
    t = str(tmp_path / "later.jsonl")
    open(t, "w").close()
    assert call(env, {"hook_event_name": "SessionStart", "session_id": "s4", "source": "resume",
                      "transcript_path": t, "cwd": repo}) == {}
    shadow = dict(env, TE_SHADOW="1")
    assert call(shadow, {"hook_event_name": "SessionStart", "session_id": "z", "source": "startup",
                         "transcript_path": t, "cwd": repo}) == {}
    events = store_for(env, repo).read_lines("events.jsonl")
    assert any(e["kind"] == "inject" and e["shadow"] for e in events)


def test_a_resumed_copilot_session_gets_back_the_facts_it_started_with(env, repo, tmp_path,
                                                                       monkeypatch):
    """Copilot 1.0.102 rebuilds a resumed conversation's first message without what SessionStart
    added: the facts are gone, and that message no longer matches the prompt cache. The text the
    session started with, given again, rebuilds it as it was; a session that started without
    facts gets none, and a new session gets the facts as they are now."""
    monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "copilot-home"))
    st = store_for(env, repo, host="copilot")
    st.write("injection.json", {"rendered": "Facts:\n- one\n- two", "rendered_ids": ["a", "b"]})

    def start(sid, source):
        return json.dumps(call(env, {"hook_event_name": "SessionStart", "session_id": sid,
                                     "source": source, "cwd": repo}, "copilot"))
    assert "- two" in start("c1", "new")
    st.write("injection.json", {"rendered": "Facts:\n- one", "rendered_ids": ["a"]})
    assert "- two" in start("c1", "resume")                    # as it started, not as now
    assert "- two" not in start("c2", "new") and "- one" in start("c3", "new")
    st.write("injection.json", {"rendered": "", "rendered_ids": []})
    start("c4", "new")
    st.write("injection.json", {"rendered": "Facts:\n- one", "rendered_ids": ["a"]})
    assert "- one" not in start("c4", "resume")                # started without facts


# ---- the spiral guard --------------------------------------------------------------------
def _turn(env, repo, sid, key, calls, host=None):
    """One prompt-turn through UserPromptSubmit -> PostToolUse* -> Stop; outputs per call."""
    idk = "turn_id" if host == "codex" else "prompt_id"
    base = {"session_id": sid, "transcript_path": "", "cwd": repo, idk: key}
    call(env, dict(base, hook_event_name="UserPromptSubmit", prompt="go"), host)
    outs = [call(env, dict(base, hook_event_name="PostToolUse", tool_name=n, tool_input=i,
                           tool_response=r), host) for n, i, r in calls]
    call(env, dict(base, hook_event_name="Stop", stop_hook_active=False), host)
    return outs


GREEN = [("Read", {"file_path": "/r/a.py"}, "ok")] * 8


def stuck_calls(n, host=None):
    out = []
    for k in range(n):
        if k % 2 == 0:
            out.append(("apply_patch", {"input": "*** Begin Patch\n*** Update File: pkg/a.py\n"},
                        "Success.") if host == "codex" else
                       ("Edit", {"file_path": "/r/pkg/a.py", "old_string": "x", "new_string": "y"},
                        "ok"))
        else:
            fail = "FAILED tests/test_a.py::test_x - AssertionError: assert 1 == 2"
            out.append(("exec_command", {"command": "pytest -q"}, {"output": fail, "exit_code": 1})
                       if host == "codex" else
                       ("Bash", {"command": "pytest -q"}, {"stdout": fail, "stderr": ""}))
    return out


def _first(outs, pred):
    return next((i for i, o in enumerate(outs) if pred(o)), None)


def test_cold_start_never_intervenes(env, repo):
    outs = _turn(env, repo, "s", "p0", stuck_calls(40))
    assert all(o == {} for o in outs)                 # no envelope learned: resolve-safe


def test_warm_guard_nudges_then_only_notifies_in_an_interactive_session(env, repo):
    for k in range(5):
        _turn(env, repo, "s", f"g{k}", GREEN)          # five ended-green turns of 8 calls
    outs = _turn(env, repo, "s", "stuck", stuck_calls(40))
    nudge = _first(outs, lambda o: "hookSpecificOutput" in o)
    assert nudge is not None
    assert "Process check (automated)" in outs[nudge]["hookSpecificOutput"]["additionalContext"]
    assert "nudged" in outs[nudge]["systemMessage"]
    later = _first(outs[nudge + 1:], lambda o: o != {})
    assert later is not None
    after = outs[nudge + 1 + later]
    assert "continue" not in after and "still looks stuck" in after["systemMessage"]
    assert all(o == {} for o in outs[nudge + 2 + later:])   # the notice is delivered once


def _guard_turn(env, repo, sid, key, calls, host):
    """One prompt-turn on `host`, each call in that agent's own payload shape."""
    idk = "turn_id" if host == "codex" else "prompt_id"
    base = {"session_id": sid, "transcript_path": "", "cwd": repo}
    if host != "copilot":
        base[idk] = key
    rkey = "tool_result" if host == "copilot" else "tool_response"
    call(env, dict(base, hook_event_name="UserPromptSubmit", prompt="go"), host)
    outs = [call(env, dict(base, hook_event_name="PostToolUse", tool_name=n, tool_input=i,
                           **{rkey: r}), host) for n, i, r in calls]
    call(env, dict(base, hook_event_name="Stop", stop_hook_active=False), host)
    return outs


def _stuck(host, n=40):
    if host in ("claude", "codex"):
        return stuck_calls(n, host if host == "codex" else None)
    fail = "FAILED tests/test_a.py::test_x - AssertionError: assert 1 == 2"
    if host == "pi":
        return [("edit", {"path": "/r/pkg/a.py", "oldText": "x", "newText": "y"}, {"content": "ok"})
                if k % 2 == 0 else ("bash", {"command": "pytest -q"},
                                    {"content": fail, "isError": True, "exit_code": 1})
                for k in range(n)]
    return [("Edit", {"path": "/r/pkg/a.py"}, {"result_type": "success",
                                                "text_result_for_llm": "ok"})
            if k % 2 == 0 else ("Bash", {"command": "pytest -q"}, {
                "result_type": "success",
                "text_result_for_llm": fail + "\n<shellId: 0 completed with exit code 1>"})
            for k in range(n)]


@pytest.mark.parametrize("host", ["claude", "codex", "copilot", "pi"])
@pytest.mark.parametrize("mode", ["interactive", "headless"])
@pytest.mark.parametrize("setting", ["nudge", "stop"])
def test_the_guard_redirects_then_tells_the_person_how_to_stop_on_every_agent(
        env, repo, host, mode, setting):
    """One ladder everywhere: a redirect to the model, then one notice to the person with the
    agent's way to stop the turn. The plugin never stops it -- `stop` is read as `nudge`."""
    env = dict(env, TE_MODE=mode, TE_LOOPGUARD=setting)
    green = [("Read", {"file_path": "/r/a.py"}, "ok")] * 8
    for k in range(5):
        _guard_turn(env, repo, "s", f"g{k}", green, host)
    outs = _guard_turn(env, repo, "s", "stuck", _stuck(host), host)
    ctx_key = (lambda o: o.get("additionalContext")) if host == "copilot" else (
        lambda o: (o.get("hookSpecificOutput") or {}).get("additionalContext"))
    shown = (lambda o: " ".join(o.get("progress") or [])) if host == "copilot" else (
        lambda o: o.get("systemMessage") or "")
    to_model = [ctx_key(o) for o in outs if ctx_key(o)]
    stuck = [shown(o) for o in outs if "still looks stuck" in shown(o)]
    assert len(stuck) == 1                                             # one notice
    hint = hosts.HOSTS[host]().stop_hint(mode == "headless")
    assert stuck[0].endswith(hint)
    assert not any("continue" in o or "stopReason" in o for o in outs)  # never a stop
    if host == "claude":
        # Claude Code keeps hook messages out of the main view, so the notice also goes to the
        # model, word for word, to pass on in its reply; the redirect comes first.
        redirect, relay = to_model
        assert "Process check (automated)" in redirect
        assert stuck[0] in relay and "it does not change your task" in relay
    else:
        assert len(to_model) == 1                                      # the redirect alone


def _fake_judges(monkeypatch, seen, stuck=True):
    from tokeneconomizer.loopguard import judge as reflect

    def judge(digest, runner, model="agent"):
        seen.append(digest)
        return {"stuck": stuck, "confidence": 0.95, "reason": "loop", "_tokens": 1000.0}

    def writer(digest, runner, model="agent"):
        return {"text": "Read the failing assertion before editing again.", "_tokens": 300.0}
    monkeypatch.setattr(reflect, "llm_stuck_judge", judge)
    monkeypatch.setattr(reflect, "llm_nudge_writer", writer)


def test_the_judge_answers_from_a_job_and_its_spend_is_charged_as_it_happens(
        env, repo, monkeypatch):
    """The job runs inline here (see conftest); in the plugin it is a detached process."""
    from tokeneconomizer.plugin import accounting
    seen = []
    _fake_judges(monkeypatch, seen)
    paid = env
    for k in range(5):
        _turn(paid, repo, "s", f"g{k}", GREEN)
    outs = _turn(paid, repo, "s", "stuck", stuck_calls(40))
    nudge = _first(outs, lambda o: "hookSpecificOutput" in o)
    assert nudge is not None and seen
    assert outs[nudge]["hookSpecificOutput"]["additionalContext"] == (
        "Read the failing assertion before editing again.")
    # The call that started the job answered nothing; its verdict arrived on a later call.
    started_at = int(seen[0].split()[0].split("=")[1])       # the digest opens "turns=N"
    assert nudge + 1 > started_at
    assert all("NOT green" not in d for d in seen)
    ledger = store_for(env, repo).read_lines("ledger.jsonl")
    charged = [r for r in ledger if r.get("kind") == "overhead"]
    assert charged and charged[0]["session_id"] == "s" and charged[0]["overhead"] == {
        "spiral_judge_tokens": 1000.0, "spiral_nudge_writer_tokens": 300.0}
    assert accounting.summarize(ledger)["overhead_tokens"] == pytest.approx(
        1300.0 * len(charged))


def test_a_hook_only_starts_the_judge_and_never_waits_for_it(env, repo, monkeypatch):
    from tokeneconomizer.plugin import judge as live_judge
    seen, spawned = [], []
    _fake_judges(monkeypatch, seen)
    monkeypatch.setattr(live_judge, "spawn_job", lambda ctx, sid, job: spawned.append(job))
    paid = env
    for k in range(5):
        _turn(paid, repo, "s", f"g{k}", GREEN)
    _turn(paid, repo, "s", "stuck", stuck_calls(12))
    assert len(spawned) == 1 and seen == []           # started once, never run in the hook
    req = store_for(env, repo).read("judge/s.request.json")
    assert req["id"] == spawned[0] and "NOT green" not in req["digest"]


def _redirects(outs):
    return [i for i, o in enumerate(outs) if "hookSpecificOutput" in o]


def test_the_judge_decides_every_redirect_but_the_backstops(env, repo, monkeypatch):
    """With the guard on, a turn that shows signs of looping goes to the judge, and only a
    confident `stuck` redirects it; a `productive` verdict spares it, and only the backstop --
    far past the usual turn AND the strict stall pattern, which the judge cannot veto -- still
    acts. The rule alone, as the guard ran without a judge, redirected this turn at call 12."""
    _warm(env, repo)                                   # usual turn: 8 calls, envelope 10
    seen = []
    _fake_judges(monkeypatch, seen, stuck=False)
    spared = _turn(env, repo, "s", "spared", stuck_calls(40))
    first = _redirects(spared)
    assert seen and first and first[0] + 1 >= 25       # the backstop: 2.5 x the envelope
    _fake_judges(monkeypatch, seen, stuck=True)
    judged = _turn(env, repo, "s", "judged", stuck_calls(40))
    first = _redirects(judged)
    assert first and first[0] + 1 < 25
    assert judged[first[0]]["hookSpecificOutput"]["additionalContext"] == (
        "Read the failing assertion before editing again.")


def test_the_judge_is_not_asked_before_the_repository_is_learned(env, repo, monkeypatch):
    seen = []
    _fake_judges(monkeypatch, seen)
    assert all(o == {} for o in _turn(env, repo, "s", "cold", stuck_calls(40)))
    assert seen == []


def test_a_long_turn_with_no_sign_of_trouble_never_wakes_the_judge(env, repo, monkeypatch):
    """Interactive turns outrun a small envelope all the time: that alone asks nothing."""
    _warm(env, repo)
    seen = []
    _fake_judges(monkeypatch, seen)
    long_and_clean = [("Read", {"file_path": f"/r/m{k}.py"}, "ok") for k in range(60)]
    assert all(o == {} for o in _turn(env, repo, "s", "long", long_and_clean))
    assert seen == []


def test_while_model_calls_are_paused_only_the_backstop_redirects(env, repo, monkeypatch):
    from tokeneconomizer.plugin import sidecall
    _warm(env, repo)
    seen = []
    _fake_judges(monkeypatch, seen)
    monkeypatch.setattr(sidecall, "paused", lambda host, root=None: {"at": 1.0})
    outs = _turn(env, repo, "s", "stuck", stuck_calls(40))
    first = _redirects(outs)
    assert seen == [] and first and first[0] + 1 >= 25


def _warm(env, repo):
    for k in range(5):
        _turn(env, repo, "s", f"g{k}", GREEN)


def test_only_turns_that_end_green_feed_the_envelope(env, repo):
    fail = {"stdout": "FAILED tests/test_a.py::t - AssertionError", "stderr": ""}
    _turn(env, repo, "s", "red", [("Edit", {"file_path": "/r/a.py"}, "ok"),
                                  ("Bash", {"command": "pytest -q"}, fail)])
    _turn(env, repo, "s", "green", [("Edit", {"file_path": "/r/a.py"}, "ok"),
                                    ("Bash", {"command": "pytest -q"}, {"stdout": "3 passed"})])
    assert store_for(env, repo).read("envelope.json")["resolved_turns"] == [2]


def test_subagent_tool_calls_do_not_reach_the_guard(env, repo):
    call(env, {"hook_event_name": "PostToolUse", "session_id": "s", "cwd": repo, "prompt_id": "p",
               "agent_id": "a1", "tool_name": "Bash", "tool_input": {"command": "ls"},
               "tool_response": {"stdout": ""}})
    assert store_for(env, repo).read("sessions/s.json") is None


# ---- untooling -----------------------------------------------------------------------------
def _catalog(env, tools):
    d = os.path.join(env["TOKENECONOMIZER_HOME"], "hosts", "claude")
    os.makedirs(d, exist_ok=True)
    json.dump({"tools": tools}, open(os.path.join(d, "catalog.json"), "w"))


def test_with_propose_unused_tools_are_proposed_but_nothing_changes_until_applied(
        env, repo, tmp_path):
    env = dict(env, TE_TOOLPRUNE="propose")
    _catalog(env, ["Bash", "Read", "Edit", "Write", "AskUserQuestion", "Workflow",
                   "CronCreate", "ShareOnboardingGuide", "WebFetch"])
    for i in range(5):
        t = write_session(str(tmp_path / f"u{i}.jsonl"), [("look", [
            ("Read", {"file_path": f"{repo}/a.py"}, "x", False),
            ("Bash", {"command": "ls"}, "a.py", False)]), ("and again", [])])
        run_session(env, repo, f"u{i}", t)
    st = store_for(env, repo)
    prop = st.read("untool.json")["proposal"]
    # protected tools (incl. a person's AskUserQuestion, and the web) are never proposed
    assert sorted(prop) == ["CronCreate", "ShareOnboardingGuide", "Workflow"]
    assert not os.path.exists(denylist.settings_path(repo))      # proposing writes nothing
    t = str(tmp_path / "next.jsonl")
    open(t, "w").close()
    notice = call(env, {"hook_event_name": "SessionStart", "session_id": "n", "source": "startup",
                        "transcript_path": t, "cwd": repo})["systemMessage"]
    assert "3 tool(s) went unused" in notice and "/tokeneconomizer:toolprune" in notice
    assert "nothing changes until you run `/tokeneconomizer:toolprune apply`" in notice


def test_by_default_unused_tools_are_removed_from_the_next_session_and_it_says_so(
        env, repo, tmp_path):
    """`toolprune=auto` is the default: the fold that forms the proposal writes it, the next
    session start says so once, and `restore` gives the tools back."""
    _catalog(env, ["Bash", "Read", "Edit", "Write", "AskUserQuestion", "Workflow",
                   "CronCreate", "ShareOnboardingGuide", "WebFetch"])
    for i in range(5):
        t = write_session(str(tmp_path / f"u{i}.jsonl"), [("look", [
            ("Read", {"file_path": f"{repo}/a.py"}, "x", False),
            ("Bash", {"command": "ls"}, "a.py", False)]), ("and again", [])])
        run_session(env, repo, f"u{i}", t)
    st = store_for(env, repo)
    deny = json.load(open(denylist.settings_path(repo)))["permissions"]["deny"]
    assert sorted(deny) == ["CronCreate", "ShareOnboardingGuide", "Workflow"]
    assert st.read("untool.json")["proposal"] == []
    t = str(tmp_path / "next.jsonl")
    open(t, "w").close()

    def start(sid):
        return call(env, {"hook_event_name": "SessionStart", "session_id": sid,
                          "source": "startup", "transcript_path": t, "cwd": repo})
    notice = start("n1")["systemMessage"]
    assert "removed 3 tool(s) this repository's sessions never used" in notice
    assert "`/tokeneconomizer:toolprune restore` gives them back" in notice
    assert "systemMessage" not in start("n2")                   # said once


def test_tools_given_back_stay_back_until_named_again(env, repo, tmp_path, monkeypatch, capsys):
    """Under the default `auto`, `restore` must stick: the evidence still says the tools go
    unused, and the next fold would otherwise take them away again."""
    from tokeneconomizer.plugin import cli
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    _catalog(env, ["Bash", "Read", "Workflow", "CronCreate"])

    def sessions(prefix, n):
        for i in range(n):
            t = write_session(str(tmp_path / f"{prefix}{i}.jsonl"), [("look", [
                ("Bash", {"command": "ls"}, "a.py", False)]), ("and again", [])])
            run_session(env, repo, f"{prefix}{i}", t)
    sessions("a", 5)
    def deny():
        return json.load(open(denylist.settings_path(repo))).get("permissions", {}).get("deny", [])
    assert sorted(deny()) == ["CronCreate", "Workflow"]
    assert cli.main(["--host", "claude", "--cwd", repo, "toolprune", "restore"]) == 0
    assert "kept from now on" in capsys.readouterr().out and deny() == []
    sessions("b", 6)                                   # still unused, still kept
    assert deny() == []
    assert cli.main(["--host", "claude", "--cwd", repo, "toolprune", "show"]) == 0
    out = capsys.readouterr().out
    assert "proposed: -" in out and "kept, as you gave them back" in out
    assert cli.main(["--host", "claude", "--cwd", repo, "toolprune", "apply", "Workflow"]) == 0
    assert deny() == ["Workflow"]
    assert denylist.kept_for(store_for(env, repo), repo) == ["CronCreate"]


def _prompts(env, repo, tmp_path, sid, n):
    """One Claude Code session of `n` prompts; only the first calls a tool (Bash)."""
    t = write_session(str(tmp_path / f"{sid}.jsonl"),
                      [(f"prompt {k}", [("Bash", {"command": "ls"}, "a.py", False)] if k == 0
                        else []) for k in range(n)])
    run_session(env, repo, sid, t)


def _another_repo(tmp_path, name):
    r = tmp_path / name
    (r / ".git").mkdir(parents=True)
    (r / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    return str(r)


def test_ten_prompts_over_two_sessions_are_enough_and_one_sessions_are_not(env, repo, tmp_path):
    """The evidence counts prompts as well as sessions: a never-used tool is left out after ten
    prompts over at least two sessions. One session's prompts are one piece of work, so ten of
    them in a single session are not enough -- unless `toolprune_min_sessions=1`."""
    env = dict(env, TE_TOOLPRUNE="propose")
    _catalog(env, ["Bash", "Read", "Workflow", "CronCreate"])
    _prompts(env, repo, tmp_path, "long", 10)
    st = store_for(env, repo)
    unt = st.read("untool.json")
    assert unt["proposal"] == []
    assert (unt["streaks"]["Workflow"], unt["prompt_streaks"]["Workflow"]) == (1, 10)
    _prompts(env, repo, tmp_path, "short", 1)
    assert sorted(st.read("untool.json")["proposal"]) == ["CronCreate", "Workflow"]   # not Read

    one = dict(env, TE_TOOLPRUNE_MIN_SESSIONS="1")
    nine, ten = _another_repo(tmp_path, "nine"), _another_repo(tmp_path, "ten")
    _prompts(one, nine, tmp_path, "s9", 9)
    _prompts(one, ten, tmp_path, "s10", 10)
    assert store_for(one, nine).read("untool.json")["proposal"] == []
    assert sorted(store_for(one, ten).read("untool.json")["proposal"]) == ["CronCreate", "Workflow"]


def test_five_sessions_are_enough_however_short(env, repo, tmp_path):
    env = dict(env, TE_TOOLPRUNE="propose")
    _catalog(env, ["Bash", "Workflow"])
    for i in range(4):
        _prompts(env, repo, tmp_path, f"s{i}", 1)
    assert store_for(env, repo).read("untool.json")["proposal"] == []
    _prompts(env, repo, tmp_path, "s4", 1)
    assert store_for(env, repo).read("untool.json")["proposal"] == ["Workflow"]


def test_a_session_without_prompts_is_no_evidence(env, repo, tmp_path):
    env = dict(env, TE_TOOLPRUNE="propose")
    _catalog(env, ["Bash", "Workflow"])
    _prompts(env, repo, tmp_path, "some", 4)
    _prompts(env, repo, tmp_path, "none", 0)
    unt = store_for(env, repo).read("untool.json")
    assert (unt["sessions"], unt["streaks"]["Workflow"], unt["prompt_streaks"]["Workflow"]) == (
        1, 1, 4)


def test_a_high_risk_tool_needs_three_times_the_prompts(env, repo, tmp_path):
    env = dict(env, TE_TOOLPRUNE="propose")
    _catalog(env, ["Bash", "Workflow", "NotebookEdit"])
    _prompts(env, repo, tmp_path, "a", 20)
    _prompts(env, repo, tmp_path, "b", 9)
    assert store_for(env, repo).read("untool.json")["proposal"] == ["Workflow"]
    _prompts(env, repo, tmp_path, "c", 1)
    assert sorted(store_for(env, repo).read("untool.json")["proposal"]) == [
        "NotebookEdit", "Workflow"]


def test_a_protected_tool_is_never_left_out_and_a_rule_already_written_comes_back(
        env, repo, tmp_path):
    """`toolprune_protect` takes names and patterns. A deny rule the plugin wrote before the tool
    was protected is removed at the next fold; the person's own rules stay."""
    _catalog(env, ["Bash", "mcp__headroom__headroom_retrieve", "Workflow"])
    path = denylist.settings_path(repo, "claude")
    os.makedirs(os.path.dirname(path))
    json.dump({"permissions": {"deny": ["Mine"]}}, open(path, "w"))
    for i in range(5):
        _prompts(env, repo, tmp_path, f"a{i}", 3)
    deny = json.load(open(path))["permissions"]["deny"]
    assert "mcp__headroom__headroom_retrieve" in deny and "Workflow" in deny
    env = dict(env, TE_TOOLPRUNE_PROTECT="mcp__headroom__*, Nothing")
    _prompts(env, repo, tmp_path, "b", 3)
    deny = json.load(open(path))["permissions"]["deny"]
    assert "mcp__headroom__headroom_retrieve" not in deny
    assert "Workflow" in deny and "Mine" in deny
    assert "mcp__headroom__headroom_retrieve" not in store_for(env, repo).read(
        "untool.json")["proposal"]


def _pending_repo(env, repo, tmp_path, quiet="60"):
    """Five sessions that leave three tools unused, under the default wait before writing."""
    env = dict(env, TE_TOOLPRUNE_QUIET_MINUTES=quiet)
    _catalog(env, ["Bash", "Read", "Edit", "Write", "AskUserQuestion", "Workflow",
                   "CronCreate", "ShareOnboardingGuide", "WebFetch"])
    for i in range(5):
        t = write_session(str(tmp_path / f"u{i}.jsonl"), [("look", [
            ("Read", {"file_path": f"{repo}/a.py"}, "x", False),
            ("Bash", {"command": "ls"}, "a.py", False)]), ("and again", [])])
        run_session(env, repo, f"u{i}", t)
    return env, LiveContext.build(hosts.ClaudeCode(), repo, env)


def _deny(repo):
    path = denylist.settings_path(repo)
    return sorted(json.load(open(path))["permissions"]["deny"]) if os.path.exists(path) else []


def test_by_default_a_deny_list_waits_until_the_repositorys_sessions_have_been_idle(
        env, repo, tmp_path, monkeypatch, capsys):
    """A conversation resumed after its tools changed re-writes its whole history to the prompt
    cache (Claude Code 2.1.307), so `auto` writes a new list only once every session has been
    idle longer than a cache lives -- silently until then, and announced once written."""
    from tokeneconomizer.plugin import cli
    env, ctx = _pending_repo(env, repo, tmp_path)
    assert _deny(repo) == []
    pend = ctx.store.read("untool.json")["auto_pending"]
    assert sorted(pend["items"]) == ["CronCreate", "ShareOnboardingGuide", "Workflow"]
    t = str(tmp_path / "next.jsonl")
    open(t, "w").close()

    def start(sid):
        return call(env, {"hook_event_name": "SessionStart", "session_id": sid,
                          "source": "startup", "transcript_path": t, "cwd": repo})
    assert "went unused" not in start("n1").get("systemMessage", "")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert cli.main(["--host", "claude", "--cwd", repo, "toolprune", "show"]) == 0
    assert "pending: written once this repository's sessions have been idle 60 minutes" in (
        capsys.readouterr().out)
    now = time.time()
    wait = fold.apply_pending(ctx, now=now + 600)                   # ten idle minutes: not yet
    assert 2900 < wait <= 3000 and _deny(repo) == []
    assert fold.apply_pending(ctx, now=now + 3700) is None           # an idle hour: written
    assert _deny(repo) == ["CronCreate", "ShareOnboardingGuide", "Workflow"]
    assert "auto_pending" not in ctx.store.read("untool.json")
    assert "removed 3 tool(s)" in start("n2")["systemMessage"]


def test_a_session_still_writing_its_log_keeps_the_deny_list_waiting(env, repo, tmp_path):
    """Activity is a hook call or a line in the session's own log: a session can go an hour
    without a hook (one long answer, or the loop guard off) and still have a warm cache."""
    env, ctx = _pending_repo(env, repo, tmp_path)
    old = time.time() - 7200
    logs = glob.glob(str(tmp_path / "u*.jsonl"))
    for f in glob.glob(ctx.store.path("sessions", "*.json")) + logs:
        os.utime(f, (old, old))
    os.utime(str(tmp_path / "u4.jsonl"))          # no hook for two hours, but its log moved now
    assert fold.apply_pending(ctx) > 3500 and _deny(repo) == []
    os.utime(str(tmp_path / "u4.jsonl"), (old, old))
    assert fold.apply_pending(ctx) is None and len(_deny(repo)) == 3


def test_a_deny_list_that_waited_a_day_is_written_even_if_sessions_never_pause(
        env, repo, tmp_path):
    env, ctx = _pending_repo(env, repo, tmp_path)
    st = ctx.store.read("untool.json")
    st["auto_pending"]["since"] = time.time() - fold.PENDING_MAX_S - 1
    ctx.store.write("untool.json", st)
    assert fold.apply_pending(ctx) is None and len(_deny(repo)) == 3


def test_a_pending_deny_list_leaves_out_what_was_given_back_meanwhile(env, repo, tmp_path):
    env, ctx = _pending_repo(env, repo, tmp_path)
    denylist.set_kept(ctx.store, repo, "claude", add=["Workflow"])        # `restore`d earlier
    assert fold.apply_pending(ctx, now=time.time() + 3700) is None
    assert _deny(repo) == ["CronCreate", "ShareOnboardingGuide"]


def test_a_pending_deny_list_leaves_out_what_was_protected_meanwhile(env, repo, tmp_path):
    env, ctx = _pending_repo(env, repo, tmp_path)
    ctx = LiveContext.build(hosts.ClaudeCode(), repo,
                            dict(env, TE_TOOLPRUNE_PROTECT="Cron*,Workflow"))
    assert fold.apply_pending(ctx, now=time.time() + 3700) is None
    assert _deny(repo) == ["ShareOnboardingGuide"]


def test_a_pending_deny_list_is_dropped_once_toolprune_is_no_longer_auto(env, repo, tmp_path):
    env, ctx = _pending_repo(env, repo, tmp_path)
    ctx = LiveContext.build(hosts.ClaudeCode(), repo, dict(env, TE_TOOLPRUNE="propose"))
    assert fold.apply_pending(ctx, now=time.time() + 3700) is None
    unt = ctx.store.read("untool.json")
    assert _deny(repo) == [] and "auto_pending" not in unt and len(unt["proposal"]) == 3


def test_the_fold_leaves_one_detached_waiter_and_a_second_one_leaves(
        env, repo, tmp_path, monkeypatch):
    from tokeneconomizer.plugin import cli
    env, ctx = _pending_repo(env, repo, tmp_path)
    spawned = []
    monkeypatch.setattr(LiveContext, "spawn", lambda self, argv, log: spawned.append(argv))
    detached = LiveContext.build(hosts.ClaudeCode(), repo,
                                 {k: v for k, v in env.items() if k != "TE_FOLD_SYNC"})
    assert fold.schedule_pending(detached) > 0
    assert spawned == [["toolprune", "apply-pending"]]

    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(time, "sleep", lambda s: (_ for _ in ()).throw(AssertionError(s)))
    beat = ctx.store.path("toolprune-waiter")
    open(beat, "w").close()                                          # another waiter is alive
    assert cli.main(["--host", "claude", "--cwd", repo, "toolprune", "apply-pending"]) == 0
    assert _deny(repo) == [] and os.path.exists(beat)
    os.remove(beat)
    monkeypatch.setenv("TE_TOOLPRUNE_QUIET_MINUTES", "0")            # quiet enough at once
    assert cli.main(["--host", "claude", "--cwd", repo, "toolprune", "apply-pending"]) == 0
    assert len(_deny(repo)) == 3 and not os.path.exists(beat)


def test_the_web_is_never_proposed_and_an_old_deny_rule_for_it_is_given_back(
        env, repo, tmp_path):
    _catalog(env, ["Bash", "Read", "CronCreate", "WebFetch", "WebSearch"])
    st = store_for(env, repo)
    # A deny list accepted under an earlier version, which still proposed the web.
    denylist.apply(repo, ["CronCreate", "WebFetch", "WebSearch"], st)
    for i in range(20):                       # far past the old 3x evidence bar
        t = write_session(str(tmp_path / f"w{i}.jsonl"), [("look", [
            ("Bash", {"command": "ls"}, "a.py", False)])])
        run_session(env, repo, f"w{i}", t)
    assert denylist.applied(st) == ["CronCreate"]
    deny = json.load(open(denylist.settings_path(repo)))["permissions"]["deny"]
    assert deny == ["CronCreate"]
    assert not {"WebFetch", "WebSearch"} & set(st.read("untool.json")["proposal"])


def test_apply_and_restore_touch_only_the_plugins_own_rules(repo, tmp_path):
    st = Store(str(tmp_path / "st"))
    path = denylist.settings_path(repo)
    os.makedirs(os.path.dirname(path))
    json.dump({"permissions": {"deny": ["Bash(rm -rf:*)"], "allow": ["Read"]}, "model": "opus"},
              open(path, "w"))
    assert denylist.apply(repo, ["Workflow", "CronCreate"], st) == ["Workflow", "CronCreate"]
    data = json.load(open(path))
    assert data["permissions"]["deny"] == ["Bash(rm -rf:*)", "Workflow", "CronCreate"]
    assert data["model"] == "opus" and data["permissions"]["allow"] == ["Read"]
    assert denylist.restore(repo, st, ["CronCreate"]) == ["CronCreate"]
    assert denylist.restore(repo, st) == ["Workflow"]
    assert json.load(open(path))["permissions"]["deny"] == ["Bash(rm -rf:*)"]
    # the user wrote that file, so it was not added to the local git excludes
    exclude = os.path.join(repo, ".git", "info", "exclude")
    assert not os.path.exists(exclude) or "settings.local.json" not in open(exclude).read()


def test_a_settings_file_the_plugin_creates_is_kept_out_of_git(repo, tmp_path):
    st = Store(str(tmp_path / "st"))
    denylist.apply(repo, ["Workflow"], st)
    assert "/.claude/settings.local.json" in open(
        os.path.join(repo, ".git", "info", "exclude")).read().split()
    denylist.apply(repo, ["CronCreate"], st)                      # idempotent exclude entry
    assert open(os.path.join(repo, ".git", "info", "exclude")).read().count(
        "/.claude/settings.local.json") == 1


# ---- folding and the ledger ----------------------------------------------------------------
def test_each_session_is_folded_once_and_priced(env, repo, tmp_path):
    t = trap_session(str(tmp_path / "s.jsonl"), repo, 0)
    run_session(env, repo, "s", t)
    call(env, {"hook_event_name": "SessionEnd", "session_id": "s", "transcript_path": t,
               "cwd": repo, "reason": "other"})
    ledger = store_for(env, repo).read_lines("ledger.jsonl")
    assert len(ledger) == 1
    rec = ledger[0]
    # 3 requests x (10 fresh + 0.1 x 1000 cached + 1.25 x 100 written + 5 x 50 output)
    assert rec["requests"] == 3 and rec["effective_tokens"] == pytest.approx(3 * 485.0)
    assert rec["priced_by"] == "model" and set(rec["overhead"]) == {
        "optimizer_tokens", "spiral_judge_tokens", "spiral_nudge_writer_tokens"}


def test_a_fold_that_cannot_take_the_lock_skips_and_the_session_is_still_folded_once(
        env, repo, tmp_path, monkeypatch):
    """A paid fold holds the lock through its side call; a second fold of the same session that
    cannot take it skips, leaving the session for the holder or a later start: it is folded once."""
    from tokeneconomizer.plugin import fold, hosts
    from tokeneconomizer.plugin.store import _try_lock, _unlock
    from tokeneconomizer.plugin.context import LiveContext
    t = trap_session(str(tmp_path / "s.jsonl"), repo, 0)
    base = {"session_id": "s", "transcript_path": t, "cwd": repo}
    call(env, dict(base, hook_event_name="SessionStart", source="startup"))
    st = store_for(env, repo)
    monkeypatch.setattr(fold, "FOLD_LOCK_WAIT_S", 0.2)
    holder = open(st.path(".fold.lock"), "a+")          # another fold, mid side call
    _try_lock(holder)
    try:
        call(env, dict(base, hook_event_name="SessionEnd", reason="other"))   # folds inline
        assert st.read_lines("ledger.jsonl") == []
        assert "s" not in (st.read("folded.json") or {})
        ctx = LiveContext.build(hosts.HOSTS["claude"](), repo, env)
        assert "s" in fold.pending_sessions(ctx)        # left for the holder, or a later start
    finally:
        _unlock(holder)
        holder.close()
    assert fold.fold_session(ctx, "s") is not None
    assert fold.fold_session(ctx, "s") is None
    assert len(st.read_lines("ledger.jsonl")) == 1


def test_a_session_whose_end_was_missed_is_folded_by_the_next_start(env, repo, tmp_path):
    t = trap_session(str(tmp_path / "lost.jsonl"), repo, 0)
    call(env, {"hook_event_name": "SessionStart", "session_id": "lost", "source": "startup",
               "transcript_path": t, "cwd": repo})               # ... then the terminal died
    old = os.path.getmtime(t) - 3600
    os.utime(t, (old, old))
    t2 = str(tmp_path / "next.jsonl")
    open(t2, "w").close()
    call(env, {"hook_event_name": "SessionStart", "session_id": "next", "source": "startup",
               "transcript_path": t2, "cwd": repo})
    assert "lost" in store_for(env, repo).read("folded.json")


# ---- never break the session ---------------------------------------------------------------
def test_garbage_input_and_internal_errors_answer_no_change(env, repo, monkeypatch):
    out = io.StringIO()
    hook.main([], io.StringIO("not json"), out, env)
    assert json.loads(out.getvalue()) == {}

    def boom(self):
        raise RuntimeError("bug")
    monkeypatch.setattr(LiveController, "handle", boom)
    assert call(env, {"hook_event_name": "Stop", "session_id": "s", "cwd": repo}) == {}
    log = open(os.path.join(env["TOKENECONOMIZER_HOME"], "errors.log")).read()
    assert "RuntimeError: bug" in log


def test_a_non_ascii_checkout_is_found_whatever_the_locale(env, tmp_path):
    """The agents send UTF-8. A text stdin decodes with the locale's code page -- on a Russian
    Windows, cp1251 turns `Программирование` into `РџСЂРѕРіСЂ...`, a directory that does not
    exist -- so the hook reads the bytes, and the session is the checkout's."""
    repo = tmp_path / "Программирование" / "agentsync"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    raw = json.dumps({"hook_event_name": "SessionStart", "session_id": "s1", "source": "startup",
                      "transcript_path": "", "cwd": str(repo)}, ensure_ascii=False)
    stdin = io.TextIOWrapper(io.BytesIO(raw.encode("utf-8")), encoding="cp1251")
    hook.main([], stdin, io.StringIO(), env)
    assert store_for(env, str(repo)).read("sessions/s1.json")
    assert not os.path.exists(os.path.join(env["TOKENECONOMIZER_HOME"], "errors.log"))


def test_side_calls_a_disabled_plugin_and_a_payload_without_a_session_do_nothing(env, repo):
    for flag in ("TE_SIDECALL", "TE_DISABLE"):
        assert call(dict(env, **{flag: "1"}), {"hook_event_name": "SessionStart",
                                                "session_id": "s", "cwd": repo}) == {}
    assert call(env, {"hook_event_name": "SessionStart", "cwd": repo}) == {}
    assert not os.path.exists(env["TOKENECONOMIZER_HOME"])


def test_mechanism_prints_cannot_corrupt_the_hook_output(env, repo, tmp_path, capsys):
    """ToolingStage prints a warning when it parses no catalog; inline folds must not leak it."""
    t = str(tmp_path / "empty.jsonl")
    open(t, "w").close()
    out = io.StringIO()
    hook.main([], io.StringIO(json.dumps({"hook_event_name": "SessionEnd", "session_id": "e",
                                          "transcript_path": t, "cwd": repo})), out, env)
    assert json.loads(out.getvalue()) == {}


# ---- Pi, through the payloads its extension sends -------------------------------------------
def pi_trap_session(path, repo, i):
    """The Claude trap session, as a Pi session file."""
    lines = [{"type": "session", "version": 3, "id": f"pi{i}", "timestamp": "2026-09-30T00:00:00Z",
              "cwd": repo}]
    calls = [("edit", {"path": f"{repo}/pkg/mod{i}.py", "oldText": "a", "newText": "b"}, "ok", False),
             ("bash", {"command": "pytest -q tests/test_a.py"}, "bash: pytest: command not found", True),
             ("bash", {"command": "python -m pytest -q tests/test_a.py"}, "1 passed in 0.1s", False)]
    parent, n = None, 0
    entries = [{"role": "user", "content": f"fix bug {i}"}]
    for name, args, res, err in calls:
        entries.append({"role": "assistant", "model": "gpt-5.6-sol", "usage": {
            "input": 10, "output": 50, "cacheRead": 1000, "cacheWrite": 100, "totalTokens": 1160,
            "cost": {"total": 0.001}}, "content": [{"type": "toolCall", "id": f"c{name}{n}",
                                                    "name": name, "arguments": args}]})
        entries.append({"role": "toolResult", "toolCallId": f"c{name}{n}", "toolName": name,
                        "isError": err, "content": [{"type": "text", "text": res}]})
        n += 1
    for k, m in enumerate(entries):
        eid = f"{i}-{k}"
        lines.append({"type": "message", "id": eid, "parentId": parent,
                      "timestamp": "2026-09-30T00:00:01Z", "message": m})
        parent = eid
    with open(path, "w") as fh:
        fh.write("\n".join(json.dumps(x) for x in lines) + "\n")
    return path


def test_pi_learns_from_its_own_session_files_and_injects_at_the_next_start(env, repo, tmp_path):
    starts = [run_session(env, repo, f"pi{i}", pi_trap_session(str(tmp_path / f"p{i}.jsonl"), repo, i),
                          host="pi") for i in range(5)]
    assert all(s == {} for s in starts[:4])
    ctx = starts[4]["hookSpecificOutput"]["additionalContext"]
    assert "`python -m pytest …` works." in ctx
    assert "/te microskills" in starts[4]["systemMessage"]  # Pi's own command name
    rec = store_for(env, repo, "pi").read_lines("ledger.jsonl")[0]
    assert rec["requests"] == 3 and rec["priced_by"] == "model"


def test_pi_print_mode_counts_as_headless_for_the_stop_hint(env, repo):
    """Pi says its mode through the extension (`TE_PI_MODE`); a print-mode notice names the
    headless way to stop."""
    penv = dict(env, TE_PI_MODE="print")
    for k in range(5):
        _guard_turn(penv, repo, "p", f"g{k}",
                    [("read", {"path": "/r/a.py"}, {"content": "ok"})] * 8, "pi")
    outs = _guard_turn(penv, repo, "p", "stuck", _stuck("pi"), "pi")
    notice = next(o["systemMessage"] for o in outs
                  if "still looks stuck" in o.get("systemMessage", ""))
    assert notice.endswith(hosts.HOSTS["pi"]().stop_hint(True))


# ---- GitHub Copilot CLI, through the payloads it sends -------------------------------------
from tests.test_live_core import COPILOT_POST, copilot_events  # noqa: E402

GITHUB_TOOLS = ["github-mcp-server-list_issues", "github-mcp-server-get_commit",
                "github-mcp-server-search_code"]


def copilot_session(env, repo, tmp_path, sid, calls, monkeypatch, source="new", prompts=1):
    """One Copilot session, as its hook file delivers it: no transcript path before Stop (the
    log's place is fixed by the session id), Claude tool names, a top-level context answer."""
    home = tmp_path / "copilot-home"
    monkeypatch.setenv("COPILOT_HOME", str(home))
    copilot_events(str(home / "session-state" / sid / "events.jsonl"), calls, cwd=repo, sid=sid,
                   prompts=prompts)
    base = {"session_id": sid, "cwd": repo, "timestamp": "2026-10-08T04:22:15.157Z"}
    start = call(env, dict(base, hook_event_name="SessionStart", source=source), "copilot")
    call(env, dict(base, hook_event_name="UserPromptSubmit", prompt="fix it"), "copilot")
    for name, args, text, ok in calls:
        if ok:
            call(env, dict(base, hook_event_name="PostToolUse", tool_name=name.capitalize(),
                           tool_input=args, tool_result={"result_type": "success",
                                                         "text_result_for_llm": text}),
                 "copilot")
        else:
            call(env, dict(base, hook_event_name="PostToolUseFailure",
                           tool_name=name.capitalize(), tool_input=args, error=text), "copilot")
    call(env, dict(base, hook_event_name="Stop", stop_reason="end_turn",
                   stop_hook_active=False), "copilot")
    call(env, dict(base, hook_event_name="SessionEnd", reason="complete"), "copilot")
    return start


COPILOT_TRAP = [("bash", {"command": "pytest -q tests/test_a.py"},
                 "bash: pytest: command not found\n<shellId: 0 completed with exit code 127>", True),
                ("bash", {"command": "python -m pytest -q tests/test_a.py"},
                 "1 passed in 0.1s\n<shellId: 1 completed with exit code 0>", True)]


def test_copilot_learns_from_its_own_logs_and_injects_at_top_level(env, repo, tmp_path,
                                                                   monkeypatch):
    starts = [copilot_session(env, repo, tmp_path, f"cp{i}", COPILOT_TRAP, monkeypatch)
              for i in range(5)]
    assert all(s == {} for s in starts[:4])
    assert "hookSpecificOutput" not in starts[4]            # Copilot ignores that shape
    assert "`python -m pytest …` works." in starts[4]["additionalContext"]
    # Copilot never shows systemMessage; its TUI shows a progress line printed first
    assert "systemMessage" not in starts[4]
    (notice,) = starts[4]["progress"]
    assert "learned fact" in notice and "/tokeneconomizer:microskills" in notice
    st = store_for(env, repo, "copilot")
    rec = st.read_lines("ledger.jsonl")[0]
    assert rec["host"] == "copilot" and rec["requests"] == 4 and rec["priced_by"] == "model"
    assert rec["input_tokens"] == 4000 - 2400 - 100
    turns = [e for e in st.read_lines("events.jsonl") if e["kind"] == "turn"]
    assert len(turns) == 5 and all(t["tool_calls"] == 2 for t in turns)


SIGNED_OUT = ("Error: No authentication information found. Copilot can be authenticated with "
              "GitHub using an OAuth Token or a Fine-Grained Personal Access Token. To "
              "authenticate, you can use any of the following methods: Start 'copilot' and run "
              "the '/login' command")


def test_a_signed_out_copilot_pauses_the_model_calls_and_says_so_once(env, repo, tmp_path,
                                                                     monkeypatch):
    """Under BYOK Copilot does not pass the provider to the plugin's hooks, so the proposer's
    `copilot -p` can ask to sign in. One such answer pauses the calls; the next start says so
    once, the facts that need no model call still form, and `config set microskills=on` tries
    again."""
    import subprocess
    from tokeneconomizer.plugin import cli, sidecall
    for k, v in env.items():
        monkeypatch.setenv(k, v)                  # `_no_reply` writes under the state root
    tried = []

    def signed_out(prompt, model="", **kw):
        tried.append(prompt)
        sidecall._no_reply("copilot", subprocess.CompletedProcess([], 1, "", SIGNED_OUT))
        return "", None
    monkeypatch.setattr(sidecall, "copilot_call", signed_out)
    on = dict(env, TE_MICROSKILLS="on")
    starts = [copilot_session(on, repo, tmp_path, f"b{i}", COPILOT_TRAP, monkeypatch)
              for i in range(6)]
    assert len(tried) == 1                        # after the 2nd session; not after the 5th
    said = [p for s in starts for p in s.get("progress", []) if "not signed in" in p]
    assert len(said) == 1 and "Copilot does not pass it to the plugin's hooks" in said[0]
    assert "progress" in starts[2] and said[0] in starts[2]["progress"]
    assert "`python -m pytest …` works." in starts[4]["additionalContext"]
    assert cli.main(["--host", "copilot", "--cwd", repo, "config", "set", "microskills=on"]) == 0
    assert sidecall.paused("copilot") is None


def test_only_a_signed_out_answer_pauses_the_model_calls(env, monkeypatch):
    import subprocess
    from tokeneconomizer.plugin import sidecall
    monkeypatch.setenv("TOKENECONOMIZER_HOME", env["TOKENECONOMIZER_HOME"])
    sidecall._no_reply("pi", subprocess.CompletedProcess([], 1, "", "429 rate limited"))
    assert sidecall.paused("pi") is None
    sidecall._no_reply("claude", subprocess.CompletedProcess([], 1, "Invalid API key · Please "
                                                                    "run /login", ""))
    assert sidecall.paused("claude") and sidecall.paused("pi") is None
    assert sidecall.resume("claude") and sidecall.paused("claude") is None


def test_a_failed_copilot_tool_reaches_the_guard(env, repo, tmp_path, monkeypatch):
    copilot_session(env, repo, tmp_path, "f", [
        ("view", {"path": f"{repo}/nope.py"}, "Path does not exist", False)], monkeypatch)
    (turn,) = [e for e in store_for(env, repo, "copilot").read_lines("events.jsonl")
               if e["kind"] == "turn"]
    assert turn["tool_calls"] == 1


def _copilot_catalog(env, tools):
    d = os.path.join(env["TOKENECONOMIZER_HOME"], "hosts", "copilot")
    os.makedirs(d, exist_ok=True)
    json.dump({"tools": tools}, open(os.path.join(d, "catalog.json"), "w"))


def test_copilot_proposes_only_whole_unused_mcp_servers(env, repo, tmp_path, monkeypatch):
    env = dict(env, TE_TOOLPRUNE="propose")
    _copilot_catalog(env, ["bash", "view", "sql", "fetch_copilot_cli_documentation"]
                     + GITHUB_TOOLS)
    for i in range(5):
        copilot_session(env, repo, tmp_path, f"u{i}", [
            ("bash", {"command": "ls"}, "a.py\n<shellId: 0 completed with exit code 0>", True)],
            monkeypatch, prompts=2)
    st = store_for(env, repo, "copilot")
    unt = st.read("untool.json")
    # builtins went unused too, but Copilot has no persistent per-tool lever: only the server
    assert unt["proposal"] == ["github-mcp-server"]
    assert unt["servers"]["github-mcp-server"] == {"tools": 3, "used": False,
                                                   "unused_sessions": 5, "unused_prompts": 10}
    assert not os.path.exists(denylist.settings_path(repo, "copilot"))
    (notice,) = copilot_session(env, repo, tmp_path, "n", [], monkeypatch)["progress"]
    assert "1 MCP server(s) went unused" in notice and "/tokeneconomizer:toolprune" in notice


def test_two_copilot_sessions_of_five_prompts_are_enough_to_propose_a_server(
        env, repo, tmp_path, monkeypatch):
    env = dict(env, TE_TOOLPRUNE="propose")
    _copilot_catalog(env, ["bash", "view"] + GITHUB_TOOLS)
    for sid in ("u1", "u2"):
        copilot_session(env, repo, tmp_path, sid, [
            ("bash", {"command": "ls"}, "a.py\n<shellId: 0 completed with exit code 0>", True)],
            monkeypatch, prompts=5)
    unt = store_for(env, repo, "copilot").read("untool.json")
    assert unt["proposal"] == ["github-mcp-server"]
    assert unt["servers"]["github-mcp-server"]["unused_sessions"] == 2
    assert unt["servers"]["github-mcp-server"]["unused_prompts"] == 10


def test_by_default_copilot_turns_off_the_unused_server_and_says_so(env, repo, tmp_path,
                                                                       monkeypatch):
    _copilot_catalog(env, ["bash", "view"] + GITHUB_TOOLS)
    for i in range(5):
        copilot_session(env, repo, tmp_path, f"u{i}", [
            ("bash", {"command": "ls"}, "a.py\n<shellId: 0 completed with exit code 0>", True)],
            monkeypatch, prompts=2)
    settings = json.load(open(denylist.settings_path(repo, "copilot")))
    assert settings["disabledMcpServers"] == ["github-mcp-server"]
    (notice,) = copilot_session(env, repo, tmp_path, "n", [], monkeypatch)["progress"]
    assert "removed 1 MCP server(s)" in notice and "toolprune restore" in notice


def test_a_server_whose_tool_was_used_once_is_never_proposed(env, repo, tmp_path, monkeypatch):
    _copilot_catalog(env, ["bash"] + GITHUB_TOOLS)
    copilot_session(env, repo, tmp_path, "used", [
        ("github-mcp-server-search_code", {"query": "x"}, "[]", True)], monkeypatch)
    for i in range(8):
        copilot_session(env, repo, tmp_path, f"u{i}", [
            ("bash", {"command": "ls"}, "a.py", True)], monkeypatch)
    unt = store_for(env, repo, "copilot").read("untool.json")
    assert unt["proposal"] == [] and unt["servers"]["github-mcp-server"]["used"] is True


def test_copilot_apply_and_restore_touch_only_the_plugins_own_servers(repo, tmp_path):
    st = Store(str(tmp_path / "st"))
    path = denylist.settings_path(repo, "copilot")
    assert path.endswith(os.path.join(".github", "copilot", "settings.local.json"))
    os.makedirs(os.path.dirname(path))
    json.dump({"disabledMcpServers": ["mine"], "model": "gpt-5.4"}, open(path, "w"))
    assert denylist.apply(repo, ["github-mcp-server"], st, host="copilot") == ["github-mcp-server"]
    assert json.load(open(path)) == {"disabledMcpServers": ["mine", "github-mcp-server"],
                                     "model": "gpt-5.4"}
    assert denylist.restore(repo, st, host="copilot") == ["github-mcp-server"]
    assert json.load(open(path)) == {"disabledMcpServers": ["mine"], "model": "gpt-5.4"}
    os.remove(path)
    denylist.apply(repo, ["github-mcp-server"], st, host="copilot")      # created: kept out of git
    assert "/.github/copilot/settings.local.json" in open(
        os.path.join(repo, ".git", "info", "exclude")).read().split()


# ---- Codex capability untooling ----------------------------------------------------------
def codex_rollout(path, repo, i, exec_inputs=(), prompts=1):
    """A Codex rollout of `prompts` turns: in the first, one shell command and, per
    `exec_inputs`, one code-mode exec call."""
    recs = [{"type": "session_meta", "payload": {"id": f"thread-{i}", "cwd": repo,
                                                 "cli_version": "0.153.1"}}]
    for k in range(1, prompts + 1):
        recs += [{"type": "turn_context", "payload": {"turn_id": f"t{k}", "model": "gpt-5.6-sol"}},
                 {"type": "event_msg", "payload": {"type": "task_started", "turn_id": f"t{k}"}}]
        if k > 1:
            continue
        recs.append({"type": "event_msg", "payload": {"type": "item_completed", "turn_id": "t1",
                                                      "item": {
            "type": "CommandExecution", "id": f"c{i}", "command": ["bash", "-lc", "ls"],
            "aggregated_output": "a.py", "exit_code": 0, "status": "completed"}}})
        for n, src in enumerate(exec_inputs):
            recs.append({"type": "response_item", "payload": {
                "type": "custom_tool_call", "name": "exec", "call_id": f"x{i}-{n}",
                "status": "completed", "input": src}})
    recs.append({"type": "event_msg", "payload": {"type": "token_count", "info": {
        "total_token_usage": {"input_tokens": 3000, "cached_input_tokens": 2000,
                              "cache_write_input_tokens": 0, "output_tokens": 90,
                              "reasoning_output_tokens": 10}}}})
    with open(path, "w") as fh:
        fh.write("\n".join(json.dumps(r) for r in recs) + "\n")
    return path


def test_codex_proposes_capabilities_it_never_saw_used(env, repo, tmp_path):
    env = dict(env, TE_TOOLPRUNE="propose")
    for i in range(5):
        uses = ["await tools.view_image({path: 'a.png'})"] if i == 2 else []
        t = codex_rollout(str(tmp_path / f"rollout-{i}.jsonl"), repo, i, uses, prompts=2)
        run_session(env, repo, f"cx{i}", t, host="codex")
    unt = store_for(env, repo, "codex").read("untool.json")
    # collaboration and goals went unused over all ten prompts; image was used once: kept
    assert unt["proposal"] == ["collaboration", "goals"]
    assert unt["capabilities"]["image_media"]["used"] is True
    assert unt["capabilities"]["goals"] == {"loaded_sessions": 5, "loaded_prompts": 10,
                                            "unused_sessions": 5, "used": False}
    assert (unt["sessions"], unt["prompts"]) == (5, 10)
    assert not os.path.exists(denylist.settings_path(repo, "codex"))      # nothing written yet


def test_codex_needs_the_full_evidence_before_proposing(env, repo, tmp_path):
    """The experiments' controller demotes collaboration after ONE unused task; a person's
    sessions need `toolprune_evidence` prompts of it, however they split into sessions."""
    env = dict(env, TE_TOOLPRUNE="propose")
    for i, prompts in enumerate((3, 3, 3)):
        run_session(env, repo, f"cx{i}", codex_rollout(str(tmp_path / f"rollout-{i}.jsonl"),
                                                       repo, i, prompts=prompts), host="codex")
    assert store_for(env, repo, "codex").read("untool.json")["proposal"] == []      # nine
    run_session(env, repo, "cx3", codex_rollout(str(tmp_path / "rollout-3.jsonl"), repo, 3),
                host="codex")
    assert store_for(env, repo, "codex").read("untool.json")["proposal"] == [
        "collaboration", "goals"]                                                    # ten


@pytest.fixture
def codex_accepts(monkeypatch):
    """Codex's own check of the edited config (`codex features list`), as a stub: the tests
    must not depend on a local Codex install or its user config."""
    verdicts = []
    monkeypatch.setattr(denylist, "codex_config_error", lambda root, **kw: (
        verdicts.pop(0) if verdicts else None))
    return verdicts


def test_codex_apply_writes_marked_lines_and_restore_leaves_the_users_config(repo, tmp_path,
                                                                           codex_accepts):
    st = Store(str(tmp_path / "st"))
    path = denylist.settings_path(repo, "codex")
    os.makedirs(os.path.dirname(path))
    user = ('model_reasoning_effort = "high"\n\n[features]\nhooks = true\n\n'
            '[[skills.config]]\npath = "/x/SKILL.md"\nenabled = false\n')
    open(path, "w").write(user)
    assert denylist.apply(repo, ["collaboration", "goals", "image_media"], st, host="codex") == [
        "collaboration", "goals", "image_media"]
    text = open(path).read()
    lines = text.splitlines()
    # top-level key above the first table; feature keys inside the user's [features];
    # [agents] appended as a new table after the array of tables
    assert lines.index("include_collaboration_mode_instructions = false  "
                       "# tokeneconomizer:collaboration") < lines.index("[features]")
    feats = lines[lines.index("[features]") + 1:lines.index("hooks = true")]
    assert "goals = false  # tokeneconomizer:goals" in feats
    assert "view_image = false  # tokeneconomizer:image_media" in feats
    assert lines[-2:] == ["[agents]  # tokeneconomizer:collaboration",
                          "enabled = false  # tokeneconomizer:collaboration"]
    assert denylist.restore(repo, st, ["goals"], host="codex") == ["goals"]
    assert "goals = false" not in open(path).read()
    assert denylist.restore(repo, st, host="codex") == ["collaboration", "image_media"]
    assert open(path).read() == user.rstrip("\n") + "\n"


def test_a_config_codex_rejects_is_put_back(repo, tmp_path, codex_accepts):
    st = Store(str(tmp_path / "st"))
    path = denylist.settings_path(repo, "codex")
    os.makedirs(os.path.dirname(path))
    open(path, "w").write('model = "gpt-5.6-sol"\n')
    codex_accepts.append("Error loading config.toml: duplicate key")
    with pytest.raises(RuntimeError, match="put back"):
        denylist.apply(repo, ["goals"], st, host="codex")
    assert open(path).read() == 'model = "gpt-5.6-sol"\n' and denylist.applied(st) == []
    os.remove(path)
    codex_accepts.append("bad")
    with pytest.raises(RuntimeError):
        denylist.apply(repo, ["goals"], st, host="codex")
    assert not os.path.exists(path)                # a file it created is removed again


def test_a_committed_codex_config_is_never_edited(tmp_path, codex_accepts):
    import subprocess
    r = tmp_path / "shared"
    (r / ".codex").mkdir(parents=True)
    (r / ".codex" / "config.toml").write_text('model = "gpt-5.6-sol"\n')
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    subprocess.run(git + ["-C", str(r), "add", ".codex/config.toml"], check=True)
    subprocess.run(git + ["-C", str(r), "commit", "-qm", "team config"], check=True)
    with pytest.raises(RuntimeError, match="committed"):
        denylist.apply(str(r), ["goals"], Store(str(tmp_path / "st")), host="codex")
    assert (r / ".codex" / "config.toml").read_text() == 'model = "gpt-5.6-sol"\n'


def _codex_sessions(env, repo, tmp_path, n=5):
    for i in range(n):
        t = codex_rollout(str(tmp_path / f"rollout-{i}.jsonl"), repo, i, prompts=2)
        run_session(env, repo, f"cx{i}", t, host="codex")


def _codex_start(env, repo, tmp_path, sid):
    t = codex_rollout(str(tmp_path / f"rollout-{sid}.jsonl"), repo, 99)
    return call(env, {"hook_event_name": "SessionStart", "session_id": sid, "source": "startup",
                      "transcript_path": t, "cwd": repo}, "codex")


def test_by_default_codex_writes_the_unused_capabilities_off_and_says_so(env, repo, tmp_path,
                                                                           codex_accepts):
    _codex_sessions(env, repo, tmp_path)
    text = open(denylist.settings_path(repo, "codex")).read()
    assert "include_collaboration_mode_instructions = false" in text and "goals = false" in text
    notice = _codex_start(env, repo, tmp_path, "next")["systemMessage"]
    assert "removed 2 capability(s)" in notice and "toolprune restore" in notice


def test_a_committed_codex_config_keeps_the_default_a_proposal_and_says_why(tmp_path, env,
                                                                             codex_accepts):
    import subprocess
    r = tmp_path / "shared"
    (r / ".codex").mkdir(parents=True)
    (r / ".codex" / "config.toml").write_text('model = "gpt-5.6-sol"\n')
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    subprocess.run(git + ["-C", str(r), "add", ".codex/config.toml"], check=True)
    subprocess.run(git + ["-C", str(r), "commit", "-qm", "team config"], check=True)
    _codex_sessions(env, str(r), tmp_path)
    assert (r / ".codex" / "config.toml").read_text() == 'model = "gpt-5.6-sol"\n'
    notice = _codex_start(env, str(r), tmp_path, "next")["systemMessage"]
    assert "2 capability(s) went unused" in notice and "could not be applied" in notice
    assert "is committed in this repository" in notice


def test_codex_never_overrides_a_key_the_user_set(tmp_path):
    text, did = denylist._toml_insert('[features]\ngoals = true\n', "features", "goals", "false",
                                    "# t")
    assert not did and text == '[features]\ngoals = true\n'
    text, did = denylist._toml_insert('features.goals = true\n', "features", "goals", "false", "# t")
    assert not did
    # dotted top-level keys and no [features] table: stay dotted, never add a clashing table
    text, did = denylist._toml_insert('features.hooks = true\n', "features", "view_image", "false",
                                    "# t")
    assert did and "[features]" not in text and "features.view_image = false  # t" in text
    # an inline table cannot be extended, and a [features] table beside it would be invalid
    text, did = denylist._toml_insert('features = { hooks = true }\n', "features", "goals",
                                    "false", "# t")
    assert not did and text == 'features = { hooks = true }\n'
    # a header the plugin added, under which the user has since put a key, is kept
    kept = denylist._toml_remove('[agents]  # t\nenabled = false  # t\nmax_threads = 4\n', ["# t"])
    assert kept == "[agents]  # t\nmax_threads = 4\n"


def test_codex_trust_is_read_from_the_users_config(tmp_path, repo):
    home = tmp_path / "codex-home"
    home.mkdir()
    env = {"CODEX_HOME": str(home)}
    assert denylist.codex_project_trusted(repo, env) is None
    (home / "config.toml").write_text(f'[projects."{repo}"]\ntrust_level = "trusted"\n')
    assert denylist.codex_project_trusted(repo, env) is True
    (home / "config.toml").write_text(f'[projects."{repo}/sub"]\ntrust_level = "trusted"\n'
                                      f'[projects."{repo}"]\ntrust_level = "untrusted"\n')
    assert denylist.codex_project_trusted(repo, env) is False


@pytest.mark.skipif(os.name != "nt", reason="Windows paths are case-insensitive")
def test_codex_trust_matches_the_lowercased_path_codex_writes_on_windows(tmp_path, repo):
    home = tmp_path / "codex-home"
    home.mkdir()
    env = {"CODEX_HOME": str(home)}
    (home / "config.toml").write_text(f"[projects.'{str(repo).lower()}']\n"
                                      'trust_level = "trusted"\n')
    assert denylist.codex_project_trusted(repo, env) is True


def test_copilot_trust_is_read_from_its_config(tmp_path, repo):
    home = tmp_path / "copilot-home"
    home.mkdir()
    env = {"COPILOT_HOME": str(home)}
    assert denylist.copilot_folder_trusted(repo, env) is None
    # Copilot's config.json opens with `//` comment lines; trust covers subfolders
    (home / "config.json").write_text(
        "// User settings belong in settings.json.\n// This file is managed automatically.\n"
        + json.dumps({"trustedFolders": [os.path.dirname(repo)]}))
    assert denylist.copilot_folder_trusted(repo, env) is True
    (home / "config.json").write_text(json.dumps({"trustedFolders": [repo + "-other"]}))
    assert denylist.copilot_folder_trusted(repo, env) is False


# ---- a failed call reaches the guard on Claude Code -------------------------------------------
# Claude Code 2.1.299 reports a failed call -- a non-zero exit, a refused command -- only as
# PostToolUseFailure, with an `error` string and no `tool_response` (payload as captured). Before
# the plugin's Claude hook file registered that event, the guard never saw a failing call: live
# sessions counted 39 of 47 calls, and a failing test run after the last edit could not make a
# turn end red.
CLAUDE_FAILURE = {"hook_event_name": "PostToolUseFailure", "tool_name": "Bash",
                  "tool_input": {"command": "ls /nonexistent_dir_xyz"},
                  "tool_use_id": "toolu_01", "is_interrupt": False, "duration_ms": 25,
                  "error": "Exit code 2\nls: cannot access '/nonexistent_dir_xyz': No such file "
                           "or directory"}


def test_claude_code_failure_payloads_parse_with_their_exit_code():
    from tokeneconomizer.plugin.hosts import HOSTS
    name, args, text, code = HOSTS["claude"]().tool_call(CLAUDE_FAILURE)
    assert (name, args["command"], code) == ("Bash", "ls /nonexistent_dir_xyz", 2)
    assert "cannot access" in text
    _, _, _, code = HOSTS["claude"]().tool_call(dict(CLAUDE_FAILURE, tool_name="Read",
                                                     error="File does not exist."))
    assert code is None


def test_the_claude_hook_file_registers_failed_calls():
    hooks = json.load(open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "plugin", "hooks", "hooks.json")))["hooks"]
    assert hooks["PostToolUseFailure"] == hooks["PostToolUse"]


def _claude_turn(env, repo, sid, key, calls):
    """A Claude Code prompt-turn as it is delivered: a failed call arrives as PostToolUseFailure."""
    base = {"session_id": sid, "transcript_path": "", "cwd": repo, "prompt_id": key}
    call(env, dict(base, hook_event_name="UserPromptSubmit", prompt="go"))
    outs = []
    for name, args, result, failed in calls:
        ev = (dict(base, hook_event_name="PostToolUseFailure", tool_name=name, tool_input=args,
                   error=result) if failed else
              dict(base, hook_event_name="PostToolUse", tool_name=name, tool_input=args,
                   tool_response=result))
        outs.append(call(env, ev))
    call(env, dict(base, hook_event_name="Stop", stop_hook_active=False))
    return outs


def test_failed_calls_count_and_a_failing_check_after_the_last_edit_ends_the_turn_red(env, repo):
    edit = ("Edit", {"file_path": "/r/pkg/a.py", "old_string": "x", "new_string": "y"}, "ok", False)
    run = ("Bash", {"command": "python tests/runtests.py app"},
           "Exit code 1\nFAILED (failures=1)", True)
    _claude_turn(env, repo, "s", "p0", [edit, run, ("Read", {"file_path": "/r/b.py"},
                                                    "File does not exist.", True)])
    (turn,) = [e for e in store_for(env, repo).read_lines("events.jsonl") if e["kind"] == "turn"]
    assert turn["tool_calls"] == 3
    assert turn["resolved_proxy"] is False


def test_a_loop_the_agent_reports_only_as_failures_is_nudged(env, repo):
    for k in range(5):
        _turn(env, repo, "s", f"g{k}", GREEN)
    calls = []
    for k in range(40):
        calls.append(("Edit", {"file_path": "/r/pkg/a.py", "old_string": "x", "new_string": "y"},
                      "ok", False) if k % 2 == 0 else
                     ("Bash", {"command": "pytest -q"}, "Exit code 1\nFAILED tests/test_a.py::"
                      "test_x - AssertionError: assert 1 == 2", True))
    outs = _claude_turn(env, repo, "s", "stuck", calls)
    assert any("hookSpecificOutput" in o for o in outs)


def test_an_answer_to_a_failed_call_names_the_failure_event(env, repo):
    """Claude Code reports a failing command as PostToolUseFailure and rejects an answer whose
    `hookEventName` says PostToolUse ("Hook returned incorrect event name", 2.1.306), dropping
    the redirect and the notice with it -- and a stuck turn's calls are mostly failures."""
    green = [("Read", {"file_path": "/r/a.py"}, "ok")] * 8
    for k in range(5):
        _turn(env, repo, "s", f"g{k}", green)
    base = {"session_id": "s", "transcript_path": "", "cwd": repo, "prompt_id": "stuck"}
    call(env, dict(base, hook_event_name="UserPromptSubmit", prompt="go"))
    answered = []
    for k in range(40):
        if k % 2 == 0:
            p = dict(base, hook_event_name="PostToolUse", tool_name="Edit", tool_response="ok",
                     tool_input={"file_path": "/r/pkg/a.py", "old_string": "x", "new_string": "y"})
        else:
            p = dict(base, hook_event_name="PostToolUseFailure", tool_name="Bash",
                     tool_input={"command": "pytest -q"},
                     error="Exit code 1\nFAILED tests/test_a.py::test_x - AssertionError")
        o = call(env, p)
        if "hookSpecificOutput" in o:
            answered.append((p["hook_event_name"], o["hookSpecificOutput"]["hookEventName"]))
    assert answered and all(sent == named for sent, named in answered)
    assert any(sent == "PostToolUseFailure" for sent, _ in answered)
