"""DeepSeek Harness (dsh): the export reader, the host adapter, side calls, and the plugin.

The plugin tests load `plugin/dsh/index.js` under Node with a stand-in for dsh's Cordis context
-- the events the plugin subscribes to and the per-agent `systemPrompt` it registers its section
with -- drive whole sessions through it against the real dispatcher, and read back what dsh
would have been handed. They skip where `node` is not on PATH.
"""
import json
import os
import shutil
import subprocess
import sys

import pytest

from tokeneconomizer import trace
from tokeneconomizer.plugin import accounting, hosts, sidecall

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE = shutil.which("node")

FAIL = "python: can't open file 'tests/runtests.py': [Errno 2] No such file or directory"
OK = "Ran 3 tests in 0.01s\nOK"
TEST_CALLS = [("bash", {"command": "python tests/runtests.py"}, FAIL, True, 2),
              ("bash", {"command": "python runtests.py"}, OK, False, 0)]


def _events(prompt, calls, model="deepseek-flash", usage=None):
    """dsh session events (`SessionEvent`, 0.2.2-rc.5) for one prompt and its tool calls."""
    usage = usage or {"inputTokens": 100, "outputTokens": 20, "cacheReadTokens": 900,
                      "cacheWriteTokens": 0}
    evs = [{"type": "request/context", "seq": 0, "time": 0,
            "data": {"provider": "deepseek-official", "model": model}},
           {"type": "user/message", "seq": 1, "time": 1, "data": {
               "id": "u0", "role": "user", "content": [{"type": "text", "text": "AGENTS.md"}],
               "source": {"kind": "agent-instructions"}}},
           {"type": "user/message", "seq": 2, "time": 2, "data": {
               "id": "u1", "role": "user", "content": [{"type": "text", "text": prompt}],
               "source": {"kind": "user"}}}]
    for i, (name, args, result, is_error, _code) in enumerate(calls):
        cid = f"c{i}"
        evs.append({"type": "assistant/message", "seq": len(evs), "time": 3, "data": {
            "turn": 1, "step": i + 1, "usage": usage, "stream": [], "message": {
                "id": f"a{i}", "role": "assistant", "content": [
                    {"type": "text", "text": "running"},
                    {"type": "tool-call", "id": cid, "name": name,
                     "arguments": json.dumps(args)}]}}})
        evs.append({"type": "tool/result", "seq": len(evs), "time": 4, "data": {
            "turn": 1, "step": i + 1, "message": {
                "id": f"r{i}", "role": "tool", "toolCallId": cid, "isError": is_error,
                "content": [{"type": "text", "text": result}]}}})
    evs.append({"type": "assistant/message", "seq": len(evs), "time": 5, "data": {
        "turn": 1, "step": len(calls) + 1, "usage": usage, "stream": [], "message": {
            "id": "af", "role": "assistant", "content": [{"type": "text", "text": "done"}]}}})
    evs.append({"type": "turn/end", "seq": len(evs), "time": 6,
                "data": {"turn": 1, "reason": {"kind": "completed"}}})
    return evs


def _export(tmp_path, sid="session-1", cwd="/work/repo", **kw):
    path = tmp_path / f"{sid}.json"
    path.write_text(json.dumps({"header": {"id": sid, "cwd": cwd, "delegationDepth": 0},
                                "events": _events("run the tests", TEST_CALLS, **kw)}))
    return str(path)


# ---- the reader -----------------------------------------------------------------------------
def test_the_export_becomes_a_claude_transcript_with_claude_tool_names(tmp_path):
    out = tmp_path / "trace.jsonl"
    assert trace.dsh_session_to_claude(_export(tmp_path), str(out)) == 2
    lines = [json.loads(ln) for ln in out.read_text().splitlines()]
    # The injected AGENTS.md is not a prompt; the person's message is.
    prompts = [ln for ln in lines if ln["type"] == "user" and isinstance(
        ln["message"]["content"], str)]
    assert [p["message"]["content"] for p in prompts] == ["run the tests"]
    uses = [b for ln in lines if ln["type"] == "assistant"
            for b in ln["message"]["content"] if b["type"] == "tool_use"]
    assert [(u["name"], u["input"]["command"]) for u in uses] == [
        ("Bash", "python tests/runtests.py"), ("Bash", "python runtests.py")]
    results = [b for ln in lines if ln["type"] == "user" and isinstance(
        ln["message"]["content"], list) for b in ln["message"]["content"]]
    assert [(r["tool_use_id"], r["is_error"]) for r in results] == [("c0", True), ("c1", False)]
    first = next(ln for ln in lines if ln["type"] == "assistant")
    assert first["cwd"] == "/work/repo" and first["message"]["model"] == "deepseek-flash"
    calls = trace.load_trajectory(str(out))
    assert [(c[0], c[3]) for c in calls] == [("Bash", False), ("Bash", True)]


def test_every_builtin_tool_has_a_claude_name():
    assert trace.DSH_TOOL_NAMES == {
        "bash": "Bash", "read": "Read", "write": "Write", "edit": "Edit", "apply_patch": "Edit",
        "glob": "Glob", "grep": "Grep", "web_fetch": "WebFetch", "web_search": "WebSearch",
        "subagent": "Task", "todo_write": "TodoWrite"}


def test_prompt_count_and_usage(tmp_path):
    path = _export(tmp_path)
    assert trace.dsh_prompt_count(path) == 1
    u = trace.dsh_session_usage(path)
    # Three steps; dsh's counts are disjoint, so nothing is subtracted.
    assert u == {"input_tokens": 300, "cache_read_input_tokens": 2700,
                 "cache_creation_input_tokens": 0, "output_tokens": 60, "requests": 3,
                 "model": "deepseek-flash"}
    assert trace.dsh_session_usage(str(tmp_path / "missing.json"))["requests"] == 0
    assert trace.dsh_session_to_claude(str(tmp_path / "missing.json"), str(tmp_path / "o")) \
        is None


def test_reasoning_tokens_are_not_added_to_output(tmp_path):
    path = _export(tmp_path, usage={"inputTokens": 10, "outputTokens": 50,
                                    "reasoningTokens": 40})
    assert trace.dsh_session_usage(path)["output_tokens"] == 150


def test_the_headless_json_reply_is_read():
    stdout = "\n".join(json.dumps(e) for e in [
        {"type": "session", "sessionId": "session-x", "cwd": "/tmp/a"},
        {"type": "status", "phase": "turn_start", "turn": 1},
        {"type": "text", "text": "partial "},
        {"type": "status", "phase": "step_end", "turn": 1, "step": 1,
         "usage": {"inputTokens": 7, "outputTokens": 3, "cacheReadTokens": 1}},
        {"type": "final", "sessionId": "session-x", "reason": {"kind": "completed"},
         "text": "the answer"}]) + "\nnot json\n"
    text, usage, sid, reason = trace.dsh_headless_reply(stdout)
    assert (text, sid, reason) == ("the answer", "session-x", "completed")
    assert usage == {"input_tokens": 7, "cache_read_input_tokens": 1,
                     "cache_creation_input_tokens": 0, "output_tokens": 3}
    assert trace.dsh_headless_reply("")[0] == ""


# ---- the host adapter -------------------------------------------------------------------------
def test_host_maps_dsh_tools_for_the_guard():
    host = hosts.Dsh()
    assert hosts.detect({}, hint="dsh").name == "dsh"
    assert host.prompt_context and host.relays_notices and not host.keeps_start_context
    name, args, text, code = host.tool_call({"tool_name": "bash", "tool_input": {
        "command": "pytest -q"}, "tool_response": {"content": "1 failed", "isError": True,
                                                   "exit_code": 1}})
    assert (name, args["command"], text, code) == ("bash", "pytest -q", "1 failed", 1)
    name, args, _t, _c = host.tool_call({"tool_name": "write", "tool_input": {
        "file_path": "/r/a.py", "content": "x"}, "tool_response": {"content": "ok"}})
    assert (name, args["path"]) == ("create", "/r/a.py")
    name, args, _t, _c = host.tool_call({"tool_name": "apply_patch", "tool_input": {
        "input": "*** Begin Patch\n*** Update File: src/b.py\n@@\n-x\n+y\n*** End Patch"},
        "tool_response": {"content": "ok"}})
    assert (name, args["paths"]) == ("edit", ["src/b.py"])
    assert host.turn_key({"prompt_id": "s:2"}) == "s:2"
    assert host.headless({"TE_DSH_MODE": "headless"}) and not host.headless({})


def test_host_reads_the_finished_session(tmp_path):
    host = hosts.Dsh()
    path = _export(tmp_path)
    assert host.prompt_count(path) == 1
    out = host.mechanism_trace(path, str(tmp_path))
    assert out and os.path.basename(out) == "dsh_trace.jsonl"
    eff, priced = accounting.effective(host.session_usage(path))
    assert eff and priced == "fallback"       # no DeepSeek price in the table yet


# ---- side calls -------------------------------------------------------------------------------
def test_side_calls_are_off_unless_the_person_enables_them(tmp_path, monkeypatch):
    from tokeneconomizer.plugin.config import load_config
    cfg = load_config(str(tmp_path), env={})
    monkeypatch.delenv("TE_DSH_SIDECALL", raising=False)
    assert sidecall.dsh_enabled({}) is False
    assert sidecall.runner_for("dsh", cfg) is None
    monkeypatch.setenv("TE_DSH_SIDECALL", "1")
    assert callable(sidecall.runner_for("dsh", cfg))
    assert sidecall.call_name("dsh") == "dsh --profile headless"


def test_a_side_call_disables_every_tool_row_and_reads_the_reply(monkeypatch):
    overlay = sidecall.dsh_overlay()
    for row in ("tool-bash", "tool-fs", "tool-web", "tool-subagent", "mcp-resources"):
        assert f"- id: {row}\n  disabled: true\n" in overlay
    assert "tool-executor-native" not in overlay and "agent-default-model" not in overlay
    assert ('- id: agent-default-model\n  config:\n    provider: "openrouter"\n'
            '    model: "deepseek-v4"\n') in sidecall.dsh_overlay("openrouter/deepseek-v4")

    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        seen["overlay"] = open(cmd[cmd.index("--patch") + 1], encoding="utf-8").read()
        out = "\n".join(json.dumps(e) for e in [
            {"type": "status", "phase": "step_end", "usage": {"inputTokens": 5,
                                                              "outputTokens": 2}},
            {"type": "final", "sessionId": "s", "reason": {"kind": "completed"},
             "text": "ok"}])
        return subprocess.CompletedProcess(cmd, 0, out, "")

    monkeypatch.setattr(sidecall.subprocess, "run", fake_run)
    monkeypatch.setattr(sidecall, "program", lambda cli: [cli])
    text, eff = sidecall.dsh_call("the prompt")
    assert text == "ok" and eff
    assert seen["cmd"][:3] == ["dsh", "--profile", "headless"] and seen["cmd"][-2:] == [
        "--json", "-"]
    assert seen["kw"]["input"] == "the prompt" and seen["kw"]["env"]["TE_SIDECALL"] == "1"
    assert "- id: tool-bash\n  disabled: true" in seen["overlay"]


# ---- the plugin under Node --------------------------------------------------------------------
# dsh's plugin context, as far as the plugin reads it: `ctx.on` for its events, and per agent a
# session (header, snapshotEvents) and an agent-scoped `ctx.inject(['systemPrompt'], fn)`. One
# session per run: created, one prompt, the spec's tool calls, idle, disposed.
HARNESS = r'''
import * as plugin from %s
const spec = JSON.parse(process.env.TE_HARNESS_SPEC)
const handlers = {}
const ctx = { on: (ev, fn) => { (handlers[ev] ??= []).push(fn) }, logger: { info() {} } }
plugin.apply(ctx)
const result = { sections: [] }
const events = [...(spec.history ?? [])]
const agent = {
  session: {
    id: spec.sid,
    header: { id: spec.sid, cwd: spec.cwd, delegationDepth: spec.depth ?? 0 },
    snapshotEvents: () => events,
  },
  ctx: { inject: (deps, fn) => fn({ systemPrompt: { section: (s) => result.sections.push(s) } }) },
}
const emit = async (ev, ...args) => { for (const fn of handlers[ev] ?? []) await fn(...args) }
const waterfall = async (ev, args, last) => {
  const fns = handlers[ev] ?? []
  const run = (i) => i < fns.length ? fns[i](...args, () => run(i + 1)) : Promise.resolve(last)
  return run(0)
}
await emit('agent/created', { agent, source: 'startup' })
result.sectionText = result.sections.map((s) => s.text()).join('\n')
const prompt = { id: 'u1', role: 'user', content: [{ type: 'text', text: spec.prompt }],
                 source: { kind: 'user' } }
events.push(...spec.events)
result.preStep = []
result.tools = []
for (const turn of spec.turns) {
  const step = await waterfall('agent/pre-step', [{ agent, messages: [prompt], turn: 1, step: 1 }],
                               { kind: 'enter', messages: [prompt] })
  result.preStep.push(...step.messages.slice(1).map((m) => ({ source: m.source.kind,
                                                               text: m.content[0].text })))
  for (const c of turn) {
    const exec = { callId: c.id, name: c.name, arguments: c.args, agent }
    const res = { isError: c.isError, content: [{ type: 'text', text: c.result }],
                  value: { kind: 'foreground', exitCode: c.exitCode } }
    const d = await waterfall('tools/post-execute', [exec, res], { kind: 'accept' })
    result.tools.push(d.content ? d.content.map((b) => b.text) : null)
  }
  await emit('agent/status', { agent, status: 'idle' })
}
await emit('agent/disposed', { agent })
console.log(JSON.stringify(result))
'''


def _drive(tmp_path, state, repo, sid, extra_env=None, depth=0, turns=(TEST_CALLS,),
           history=()):
    """One session through the plugin: a prompt-turn per entry of `turns`, each its calls.
    `history` is what the session already holds when it is created (a resumed one)."""
    spec = {"sid": sid, "cwd": str(repo), "prompt": "run the tests", "depth": depth,
            "history": list(history),
            "events": _events("run the tests", TEST_CALLS),
            "turns": [[{"id": f"c{i}", "name": n, "args": a, "result": r, "isError": e,
                        "exitCode": x} for i, (n, a, r, e, x) in enumerate(t)] for t in turns]}
    harness = tmp_path / "harness.mjs"
    from pathlib import Path
    harness.write_text(HARNESS % json.dumps(Path(REPO, "plugin", "dsh", "index.js").as_uri()),
                       encoding="utf-8")
    env = dict(os.environ, TE_HARNESS_SPEC=json.dumps(spec), TE_PYTHON=sys.executable,
               TE_CORE_PATH=REPO, TOKENECONOMIZER_HOME=str(state), TE_FOLD_SYNC="1",
               TE_MICROSKILLS="envfacts", TE_TOOLPRUNE="off", TE_DSH_MODE="interactive")
    for k in ("TE_DISABLE", "TE_SIDECALL"):
        env.pop(k, None)
    env.update(extra_env or {})
    out = subprocess.run([NODE, str(harness)], capture_output=True, text=True, env=env,
                         cwd=str(repo), timeout=300)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not NODE, reason="needs node")
def test_the_plugin_folds_sessions_and_puts_the_learned_fact_in_its_section(tmp_path):
    state, repo = tmp_path / "state", tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "runtests.py").write_text("print('OK')\n")
    for i in range(5):
        r = _drive(tmp_path, state, repo, f"session-{i}")
        assert r["tools"] == [None, None]          # no redirect for a loop that recovered
    exported = state / "dsh-sessions" / "session-0.json"
    assert json.loads(exported.read_text())["header"]["id"] == "session-0"
    r = _drive(tmp_path, state, repo, "session-5")
    assert len(r["sections"]) == 1
    section = r["sections"][0]
    assert section["name"] == "tokeneconomizer" and section["interpolate"] is False
    assert "`python runtests.py" in r["sectionText"]


@pytest.mark.skipif(not NODE, reason="needs node")
def test_a_redirect_is_appended_to_the_tool_result_the_model_reads(tmp_path):
    state, repo = tmp_path / "state", tmp_path / "repo"
    repo.mkdir()
    fail = "FAILED tests/test_a.py::test_x - AssertionError: assert 1 == 2"
    green = [("read", {"file_path": "/r/a.py"}, "ok", False, None)] * 8
    stuck = [("edit", {"file_path": "/r/pkg/a.py", "old_string": "x", "new_string": "y"},
              "ok", False, None) if k % 2 == 0 else
             ("bash", {"command": "pytest -q"}, fail, True, 1) for k in range(40)]
    r = _drive(tmp_path, state, repo, "session-g", extra_env={"TE_LOOPGUARD": "nudge"},
               turns=[green] * 5 + [stuck])
    rewritten = [t for t in r["tools"] if t]
    assert rewritten, "no redirect reached a tool result"
    original, *added = rewritten[0]
    assert original in (fail, "ok") and any("Process check (automated)" in a for a in added)


@pytest.mark.skipif(not NODE, reason="needs node")
def test_the_plugin_does_nothing_for_subagents_or_under_te_disable(tmp_path):
    state, repo = tmp_path / "state", tmp_path / "repo"
    repo.mkdir()
    r = _drive(tmp_path, state, repo, "session-sub", depth=1)
    assert r["sections"] == [] and r["preStep"] == []
    assert not (state / "dsh-sessions").exists()
    r = _drive(tmp_path, state, repo, "session-off", extra_env={"TE_SIDECALL": "1"})
    assert r["sections"] == [] and r["tools"] == [None, None]
    assert not (state / "dsh-sessions").exists()


@pytest.mark.skipif(not NODE, reason="needs node")
def test_a_resumed_session_keeps_counting_its_prompts(tmp_path):
    state, repo = tmp_path / "state", tmp_path / "repo"
    repo.mkdir()
    _drive(tmp_path, state, repo, "session-r")
    _drive(tmp_path, state, repo, "session-r", history=_events("run the tests", TEST_CALLS))
    s = json.loads(next(state.glob("repos/*/dsh/sessions/session-r.json")).read_text())
    assert s["source"] == "resume"
    assert [t["turn_key"] for t in s["turns"]] == ["session-r:1", "session-r:2"]


def test_the_plugin_package_mounts_one_row():
    pkg = json.load(open(os.path.join(REPO, "plugin", "dsh", "package.json")))
    assert pkg["name"] == "tokeneconomizer-dsh" and pkg["type"] == "module"
    assert pkg["dsh"]["bundle"]["patch"] == "./cordis.patch.yml"
    assert "dependencies" not in pkg and "peerDependencies" not in pkg
    patch = open(os.path.join(REPO, "plugin", "dsh", "cordis.patch.yml")).read()
    assert "name: tokeneconomizer-dsh" in patch and "id: tokeneconomizer" in patch
