"""The live layer's plumbing: repository identity, state, config, session logs, host I/O.

Offline and synthetic. The transcript fixtures reproduce the two properties of real session logs
that the harness's own transcripts never had, and that the live readers exist to handle: Claude
Code writes one entry PER CONTENT BLOCK, each repeating its message's usage, and Codex's rollout
wraps exec-stream item fields in PascalCase `event_msg/item_completed` records.
"""
import json
import os
import subprocess

import pytest

from tokeneconomizer import trace
from tokeneconomizer.plugin import config as live_config
from tokeneconomizer.plugin import hosts, paths
from tokeneconomizer.plugin.store import Store


# ---- repository identity ---------------------------------------------------------------
def _git_repo(root, remote=None):
    os.makedirs(os.path.join(root, ".git"), exist_ok=True)
    with open(os.path.join(root, ".git", "HEAD"), "w") as fh:
        fh.write("ref: refs/heads/main\n")
    cfg = "[core]\n\trepositoryformatversion = 0\n"
    if remote:
        cfg += f'[remote "origin"]\n\turl = {remote}\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n'
    with open(os.path.join(root, ".git", "config"), "w") as fh:
        fh.write(cfg)
    return root


def test_repo_identity_follows_the_remote_not_the_spelling(tmp_path):
    a = _git_repo(str(tmp_path / "a"), "git@github.com:Owner/Repo.git")
    b = _git_repo(str(tmp_path / "b" / "repo"), "https://github.com/owner/repo")
    ra, rb = paths.repo_identity(a), paths.repo_identity(b)
    assert ra.remote == rb.remote == "github.com/owner/repo"
    assert ra.label == "owner/repo"
    # the key carries the checkout's name for readability, and the remote for identity
    assert ra.key.endswith(rb.key.split("-")[-1])


def test_a_subdirectory_and_a_worktree_share_their_repository(tmp_path):
    main = _git_repo(str(tmp_path / "main"), "https://github.com/o/r.git")
    os.makedirs(os.path.join(main, "src", "pkg"))
    wt_git = os.path.join(main, ".git", "worktrees", "feature")
    os.makedirs(wt_git)
    with open(os.path.join(wt_git, "commondir"), "w") as fh:
        fh.write("../..")
    wt = str(tmp_path / "feature")
    os.makedirs(wt)
    with open(os.path.join(wt, ".git"), "w") as fh:
        fh.write(f"gitdir: {wt_git}\n")
    sub = paths.repo_identity(os.path.join(main, "src", "pkg"))
    assert sub.root == main
    assert paths.repo_identity(wt).remote == sub.remote == "github.com/o/r"


def test_a_directory_without_git_is_keyed_by_its_path(tmp_path, monkeypatch):
    d = str(tmp_path / "scratch")
    os.makedirs(d)
    # Fence the walk at tmp_path: a machine may well have a stray .git in /tmp itself.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    r = paths.repo_identity(d)
    assert r.remote == "" and r.label == "scratch" and r.root == d


def test_state_root_honours_the_environment(tmp_path):
    assert paths.state_root({"TOKENECONOMIZER_HOME": str(tmp_path)}) == str(tmp_path)


# ---- store and config ----------------------------------------------------------------------
def test_store_writes_atomically_and_logs(tmp_path):
    s = Store(str(tmp_path / "st"))
    s.write("a/b.json", {"x": 1})
    assert s.read("a/b.json") == {"x": 1}
    assert s.read("missing.json", default={}) == {}
    s.append("log.jsonl", {"n": 1})
    s.append("log.jsonl", {"n": 2})
    assert [r["n"] for r in s.read_lines("log.jsonl")] == [1, 2]
    assert [r["n"] for r in s.read_lines("log.jsonl", limit=1)] == [2]
    with s.locked("x") as got_x:
        with s.locked("y") as got_y:              # distinct locks never deadlock
            s.write("c.json", [1])
        with s.locked("x", timeout=0.1) as again:  # held: a hook's caller still proceeds,
            assert again is False                  # and is told it is unlocked
    assert got_x and got_y
    assert not [f for f in os.listdir(s.base) if f.endswith(".tmp")]


def test_config_layers_and_never_raises_on_bad_values(tmp_path):
    root = str(tmp_path)
    repo_dir = os.path.join(root, "repos", "r")
    os.makedirs(repo_dir)
    with open(os.path.join(root, "config.json"), "w") as fh:
        # names from before the mechanisms were renamed: still read
        json.dump({"spiral": "guard", "untool_evidence": 7, "bogus": 1}, fh)
    with open(os.path.join(repo_dir, "config.json"), "w") as fh:
        json.dump({"spiral": "nope", "inject": "envfacts+distiller"}, fh)
    cfg = live_config.load_config(root, repo_dir=repo_dir, env={"TE_UNTOOL": "auto"})
    assert cfg.loopguard == "stop"                # the invalid repo value is ignored
    assert cfg.toolprune_evidence == 7 and cfg.toolprune == "auto"
    assert cfg.microskills == "envfacts+distiller"     # its proposer needs no `paid`
    joined = " ".join(cfg.warnings)
    assert "bogus" in joined and "nope" in joined and "paid" not in joined
    assert live_config.load_config(root, env={"TE_DISABLE": "1"}).enabled is False


def test_write_config_validates_before_writing(tmp_path):
    path = str(tmp_path / "config.json")
    live_config.write_config(path, {"loopguard": "off", "paid_model": "haiku"})
    assert json.load(open(path)) == {"loopguard": "off", "paid_model": "haiku"}
    with pytest.raises(ValueError, match="always asks its judge"):
        live_config.write_config(path, {"paid": "true"})          # gone: refused, and why
    assert "paid" not in json.load(open(path))
    with open(path, "w") as fh:
        json.dump({"paid": True, "loopguard": "off"}, fh)          # an old file still reads
    cfg = live_config.load_config(str(tmp_path), repo_dir=None, env={})
    assert not cfg.warnings
    with pytest.raises(ValueError):
        live_config.write_config(path, {"loopguard": "sometimes"})
    assert json.load(open(path))["loopguard"] == "off"


# ---- Claude Code transcripts -----------------------------------------------------------------
def _claude_lines(path, blocks_per_message=3, n_messages=2, model="claude-opus-5-5"):
    lines = []
    tag = os.path.splitext(os.path.basename(path))[0]     # message ids are unique per request
    for m in range(n_messages):
        usage = {"input_tokens": 10, "cache_read_input_tokens": 1000,
                 "cache_creation_input_tokens": 100, "output_tokens": 50}
        for b in range(blocks_per_message):     # one entry per content block, usage repeated
            lines.append({"type": "assistant", "message": {
                "id": f"msg_{tag}_{m}", "model": model, "role": "assistant", "usage": usage,
                "content": [{"type": "text", "text": f"block {b}"}]}})
    with open(path, "w") as fh:
        fh.write("\n".join(json.dumps(x) for x in lines) + "\n")
    return path


def test_claude_usage_counts_each_message_once_and_includes_subagents(tmp_path):
    main = _claude_lines(str(tmp_path / "sess.jsonl"))
    sub_dir = tmp_path / "sess" / "subagents"
    sub_dir.mkdir(parents=True)
    _claude_lines(str(sub_dir / "agent-1.jsonl"), blocks_per_message=2, n_messages=1)
    u = trace.claude_session_usage(main)
    assert u["requests"] == 3                     # 2 main messages + 1 subagent message
    assert u["cache_read_input_tokens"] == 3000   # not 3 x 2 x 1000 + 2 x 1000
    assert u["model"] == "claude-opus-5-5" and u["files"] == 2
    assert trace.claude_session_usage(main, include_subagents=False)["requests"] == 2


def test_deferred_tools_are_read_from_attachments(tmp_path):
    p = str(tmp_path / "t.jsonl")
    with open(p, "w") as fh:
        fh.write(json.dumps({"type": "attachment", "attachment": {
            "type": "deferred_tools_delta", "addedNames": ["CronCreate", "WebSearch"],
            "removedNames": []}}) + "\n")
        fh.write(json.dumps({"type": "attachment", "attachment": {
            "type": "deferred_tools_delta", "addedNames": [], "removedNames": ["WebSearch"]}}) + "\n")
    assert trace.claude_deferred_tools(p) == {"CronCreate"}


# ---- Codex rollouts ---------------------------------------------------------------------------
def _rollout(path):
    recs = [
        {"type": "session_meta", "payload": {"id": "thread-1", "cwd": "/work/r",
                                             "cli_version": "0.153.1"}},
        {"type": "turn_context", "payload": {"turn_id": "t1", "model": "gpt-6-astra"}},
        {"type": "event_msg", "payload": {"type": "item_completed", "turn_id": "t1", "item": {
            "type": "CommandExecution", "id": "c1", "command": ["/bin/bash", "-lc", "pytest -q"],
            "aggregated_output": "1 failed", "exit_code": 1, "status": "completed"}}},
        {"type": "event_msg", "payload": {"type": "item_completed", "turn_id": "t1", "item": {
            "type": "FileChange", "id": "f1", "changes": {"/work/r/pkg/a.py": {"type": "update"}},
            "status": "completed"}}},
        {"type": "event_msg", "payload": {"type": "item_completed", "turn_id": "t1", "item": {
            "type": "Reasoning", "id": "r1"}}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {
            "input_tokens": 1000, "cached_input_tokens": 600, "cache_write_input_tokens": 100,
            "output_tokens": 40, "reasoning_output_tokens": 10}}}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {
            "input_tokens": 3000, "cached_input_tokens": 2000, "cache_write_input_tokens": 200,
            "output_tokens": 90, "reasoning_output_tokens": 30}}}},
    ]
    with open(path, "w") as fh:
        fh.write("\n".join(json.dumps(r) for r in recs) + "\n")
    return path


def test_codex_rollout_reads_through_the_existing_trace_seam(tmp_path):
    src = _rollout(str(tmp_path / "rollout-x.jsonl"))
    out = str(tmp_path / "exec.jsonl")
    assert trace.codex_rollout_to_exec(src, out) == 2       # reasoning is not a tool call
    traj = trace.load_trajectory(out)
    assert traj[0][0] == "shell" and traj[0][1]["command"] == "/bin/bash -lc 'pytest -q'"
    assert traj[0][2] == "1 failed" and traj[0][3] is False
    assert traj[1][0] == "apply_patch" and traj[1][1]["path"] == "/work/r/pkg/a.py"


def test_codex_usage_is_converted_as_the_harness_converts_it(tmp_path):
    u = trace.codex_session_usage(_rollout(str(tmp_path / "rollout-x.jsonl")))
    # last cumulative total; fresh input excludes both cache fields; reasoning joins output
    assert u["input_tokens"] == 3000 - 2000 - 200
    assert u["cache_read_input_tokens"] == 2000 and u["cache_creation_input_tokens"] == 200
    assert u["output_tokens"] == 90 + 30 and u["model"] == "gpt-6-astra"


def test_codex_capability_evidence_includes_the_models_own_exec_calls(tmp_path):
    """In code mode every tool call is one `exec` custom tool; `tools.view_image(` inside its
    input is image use, and only the capability trace carries it."""
    from tokeneconomizer.toolprune import codex as cu
    src = _rollout(str(tmp_path / "rollout-x.jsonl"))
    with open(src, "a") as fh:
        fh.write(json.dumps({"type": "response_item", "payload": {
            "type": "custom_tool_call", "name": "exec", "call_id": "call_9",
            "status": "completed",
            "input": "const r = await tools.view_image({path: 'a.png'});"}}) + "\n")
    out = str(tmp_path / "cap.jsonl")
    assert trace.codex_capability_trace(src, out) == 3
    usage = cu.inspect_usage(out)
    assert usage["completed"]["image_media"] == 1 and usage["completed"]["collaboration"] == 0
    plain = str(tmp_path / "exec.jsonl")
    trace.codex_rollout_to_exec(src, plain)
    assert cu.inspect_usage(plain)["completed"]["image_media"] == 0


def test_a_compressed_rollout_is_reported_unreadable(tmp_path):
    p = str(tmp_path / "rollout-x.jsonl.zst")
    open(p, "wb").write(b"\x28\xb5\x2f\xfd")
    assert trace.codex_rollout_to_exec(p, str(tmp_path / "o.jsonl")) is None


# ---- hosts ---------------------------------------------------------------------------------
def test_host_detection():
    assert hosts.detect({"turn_id": "t"}).name == "codex"
    assert hosts.detect({"transcript_path": "/h/.codex/sessions/2026/rollout-a.jsonl"}).name == "codex"
    assert hosts.detect({"transcript_path": "/h/.claude/projects/x/s.jsonl"}).name == "claude"
    assert hosts.detect({"turn_id": "t"}, hint="claude").name == "claude"
    assert hosts.detect({"transcript_path": "/h/.copilot/session-state/s/events.jsonl"}
                        ).name == "copilot"
    # Copilot sends no transcript before Stop, and marks its hook processes; a Claude Code
    # session started from inside Copilot inherits the marker but always sends a transcript.
    copilot_env = {"COPILOT_PLUGIN_ROOT": "/p", "COPILOT_CLI": "1"}
    assert hosts.detect({"session_id": "s"}, copilot_env).name == "copilot"
    assert hosts.detect({"session_id": "s", "transcript_path": "/h/.claude/projects/x/s.jsonl"},
                        copilot_env).name == "claude"
    assert hosts.detect({"session_id": "s"}, {}).name == "claude"


def test_the_te_command_defaults_to_the_agent_whose_shell_runs_it():
    assert hosts.default_host({}) == "claude"
    assert hosts.default_host({"COPILOT_CLI": "1"}) == "copilot"
    assert hosts.default_host({"COPILOT_CLI": "1", "CLAUDECODE": "1"}) == "claude"
    assert hosts.default_host({"TE_HOST": "codex", "CLAUDECODE": "1"}) == "codex"
    assert hosts.default_host({"TE_HOST": "nonsense"}) == "claude"
    assert hosts.default_host({}, recent="pi") == "pi"
    assert hosts.default_host({"CLAUDECODE": "1"}, recent="pi") == "claude"


def test_from_a_plain_terminal_te_is_about_the_agent_that_last_worked_here(tmp_path, monkeypatch,
                                                                          capsys):
    """Seen with every non-Claude tester: `te status` from a terminal showed the empty Claude
    Code state of a repository only Codex (or Pi) had worked in."""
    from tokeneconomizer.plugin import cli
    from tokeneconomizer.plugin.paths import repo_identity, repo_state_dir
    for k in ("TE_HOST", "CLAUDECODE", "COPILOT_CLI"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TOKENECONOMIZER_HOME", str(tmp_path / "te"))
    work = tmp_path / "work"
    work.mkdir()
    assert cli._recent_host(str(work)) is None
    d = repo_state_dir(repo_identity(str(work)), "codex")
    os.makedirs(d)
    open(os.path.join(d, "ledger.jsonl"), "w").close()
    assert cli._recent_host(str(work)) == "codex"
    assert cli.main(["--cwd", str(work), "status"]) == 0
    assert "(codex)" in capsys.readouterr().out.splitlines()[0]


# Payloads copilot 1.0.83 sent to a Claude-format plugin hook (PascalCase events), verbatim in
# shape, captured against a scripted local model endpoint on 2026-10-07.
COPILOT_POST = {"hook_event_name": "PostToolUse", "session_id": "fcb5fc6b",
                "timestamp": "2026-10-08T04:27:38.418Z", "cwd": "/w/repo", "tool_name": "Bash",
                "tool_input": {"command": "python3 -c 'import sys; sys.exit(3)'",
                               "description": "run it"},
                "tool_result": {"result_type": "success",
                                "text_result_for_llm": "boom\n<shellId: 0 completed with exit code 3>"}}
COPILOT_FAILURE = {"hook_event_name": "PostToolUseFailure", "session_id": "10bbc978",
                   "timestamp": "2026-10-08T04:27:51.286Z", "cwd": "/w/repo", "tool_name": "Read",
                   "tool_input": {"path": "/w/repo/nope.py"}, "error": "Path does not exist"}


def test_the_copilot_payloads_observed_live_are_read():
    c = hosts.detect(COPILOT_POST, {"COPILOT_PLUGIN_ROOT": "/p"})
    assert c.name == "copilot"
    name, args, text, code = c.tool_call(COPILOT_POST)
    assert (name, args["description"], code) == ("Bash", "run it", 3)   # exit code from the text
    assert text.startswith("boom")
    name, args, text, code = c.tool_call(COPILOT_FAILURE)
    assert (name, args["path"], code) == ("Read", "/w/repo/nope.py", None)
    assert "Path does not exist" in text
    # Copilot reads a TOP-LEVEL additionalContext and ignores hookSpecificOutput, and shows a
    # notice only as a progress line ahead of the output (both verified in its TUI)
    assert c.context("SessionStart", "x") == {"additionalContext": "x"}
    m = hosts.merge_outputs(c.context("SessionStart", "a"), c.notice("n"),
                            c.context("SessionStart", "b"))
    assert m == {"additionalContext": "a\n\nb", hosts.PROGRESS: ["n"]}


def test_a_copilot_session_log_is_found_from_the_session_id(monkeypatch, tmp_path):
    monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "cp"))
    c = hosts.Copilot()
    assert c.transcript({"session_id": "abc"}) == str(
        tmp_path / "cp" / "session-state" / "abc" / "events.jsonl")
    assert c.transcript({"session_id": "abc", "transcript_path": "/x/events.jsonl"}) == \
        "/x/events.jsonl"
    assert c.cwd({}, {"COPILOT_PROJECT_DIR": "/w/repo"}) == "/w/repo"


def test_claude_tool_results_and_outputs():
    h = hosts.ClaudeCode()
    name, args, text, code = h.tool_call({
        "tool_name": "Bash", "tool_input": {"command": "pytest"},
        "tool_response": {"stdout": "1 failed", "stderr": "Traceback", "interrupted": False}})
    assert (name, args["command"], code) == ("Bash", "pytest", None)   # no exit code (verified)
    assert "1 failed" in text and "Traceback" in text
    assert h.context("PostToolUse", "x") == {
        "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "x"}}
    # the hook environment of a `claude -p` session, as observed on 2.1.285
    assert h.headless({"CLAUDE_CODE_SESSION_ATTENDED": "0", "CLAUDE_CODE_ENTRYPOINT": "sdk-cli"})
    assert not h.headless({"CLAUDE_CODE_SESSION_ATTENDED": "1", "CLAUDE_CODE_ENTRYPOINT": "sdk-cli"})
    assert h.headless({"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}) and not h.headless({})


def test_the_codex_posttooluse_payload_observed_live_is_read():
    """The payload codex-cli 0.153.1 sent for `echo one` (gpt-5.6-sol, 2026-09-30), verbatim in
    shape: tool_name "Bash", a string command, a plain-string result, turn_id and model."""
    payload = {"session_id": "01a0f5e1", "turn_id": "t1", "model": "gpt-5.6-sol",
               "permission_mode": "default", "hook_event_name": "PostToolUse",
               "cwd": "/w/repo", "transcript_path": "/h/.codex/sessions/2026/09/30/rollout-x.jsonl",
               "tool_name": "Bash", "tool_use_id": "call_1", "tool_input": {"command": "echo one"},
               "tool_response": "one\n"}
    host = hosts.detect(payload)
    assert host.name == "codex"
    assert host.tool_call(payload) == ("bash", {"command": "echo one"}, "one\n", None)


def test_codex_tool_calls_normalize():
    c = hosts.Codex()
    name, args, text, code = c.tool_call({
        "tool_name": "exec_command", "tool_input": {"command": ["bash", "-lc", "go test ./..."]},
        "tool_response": {"output": "FAIL", "exit_code": 1}})
    assert (name, args["command"], code) == ("bash", "bash -lc 'go test ./...'", 1)
    name, args, _, _ = c.tool_call({"tool_name": "apply_patch", "tool_input": {
        "input": "*** Begin Patch\n*** Update File: src/a.py\n@@\n-x\n+y\n*** End Patch"}})
    assert name == "apply_patch" and args["paths"] == ["src/a.py"]
    _, _, _, code = c.tool_call({"tool_name": "shell", "tool_input": {"command": "ls"},
                                 "tool_response": "Exit code: 2\nWall time: 0.1s\nOutput:\n"})
    assert code == 2


def test_merge_outputs_concatenates_contexts_and_notices():
    h = hosts.ClaudeCode()
    m = hosts.merge_outputs(h.context("SessionStart", "a"), h.notice("n1"),
                            h.context("SessionStart", "b"), h.notice("n2"), None, {})
    assert m["hookSpecificOutput"]["additionalContext"] == "a\n\nb"
    assert m["systemMessage"] == "n1\nn2"


# ---- Pi sessions -------------------------------------------------------------------------
def _pi_session(path, cwd="/w/repo", sid="pi-1", abandoned=False):
    """A Pi v3 session tree: header, a user prompt, a bash call that fails, an edit, a bash
    call that passes -- and, optionally, an abandoned branch hanging off the first prompt."""
    lines = [{"type": "session", "version": 3, "id": sid, "timestamp": "2026-09-30T00:00:00Z",
              "cwd": cwd}]
    state = {"parent": None, "n": 0}

    def add(entry, parent="__chain__"):
        state["n"] += 1
        eid = f"e{state['n']:03d}"
        entry.update(id=eid, timestamp="2026-09-30T00:00:01Z",
                     parentId=state["parent"] if parent == "__chain__" else parent)
        lines.append(entry)
        if parent == "__chain__":
            state["parent"] = eid
        return eid

    usage = {"input": 10, "output": 50, "cacheRead": 1000, "cacheWrite": 100, "reasoning": 5,
             "totalTokens": 1160, "cost": {"total": 0.002}}

    def call(name, args, text, err):
        tc = f"call{state['n']}"
        add({"type": "message", "message": {"role": "assistant", "provider": "openai",
             "model": "gpt-5.6-sol", "usage": usage,
             "content": [{"type": "toolCall", "id": tc, "name": name, "arguments": args}]}})
        add({"type": "message", "message": {"role": "toolResult", "toolCallId": tc,
             "toolName": name, "isError": err, "content": [{"type": "text", "text": text}]}})

    first = add({"type": "message", "message": {"role": "user", "content": "fix the bug"}})
    call("bash", {"command": "pytest -q"}, "bash: pytest: command not found", True)
    call("edit", {"path": "pkg/a.py", "oldText": "x", "newText": "y"}, "ok", False)
    call("bash", {"command": "python -m pytest -q"}, "1 passed", False)
    if abandoned:                        # a branch the user walked away from, earlier in the file
        lines.insert(3, {"type": "message", "id": "zzz", "parentId": first,
                         "timestamp": "2026-09-30T00:00:02Z", "message": {
                             "role": "assistant", "model": "gpt-5.6-sol", "usage": usage,
                             "content": [{"type": "toolCall", "id": "dead", "name": "bash",
                                          "arguments": {"command": "rm -rf build"}}]}})
    with open(path, "w") as fh:
        fh.write("\n".join(json.dumps(x) for x in lines) + "\n")
    return path


def test_a_pi_session_reads_through_the_trace_seam_on_its_active_branch(tmp_path):
    src = _pi_session(str(tmp_path / "s.jsonl"), abandoned=True)
    out = str(tmp_path / "trace.jsonl")
    assert trace.pi_session_to_claude(src, out) == 3
    traj = trace.load_trajectory(out)
    assert [(n, a.get("command") or a.get("path"), ok) for n, a, _, ok in traj] == [
        ("bash", "pytest -q", False), ("edit", "pkg/a.py", True), ("bash", "python -m pytest -q", True)]
    assert "rm -rf build" not in open(out).read()          # the abandoned branch is not read


def test_a_pi_call_into_the_session_directory_is_the_plain_form(tmp_path):
    """Pi starts every command afresh in the session's directory, so `cd <that dir> && tox`
    is the same form as `tox`. Without a cwd per call it was learned as a second form, with the
    absolute path in the claim, and the two split one fact's evidence."""
    from tokeneconomizer.microskills.observe import PYTHON_GIT, observe_task
    src = str(tmp_path / "s.jsonl")
    _pi_session(src, cwd="/w/repo")
    lines = [json.loads(l) for l in open(src)]
    for e in lines:
        for b in (e.get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("arguments", {}).get("command") == "pytest -q":
                b["arguments"]["command"] = "cd /w/repo && pytest -q"
    open(src, "w").write("\n".join(json.dumps(e) for e in lines) + "\n")
    out = str(tmp_path / "trace.jsonl")
    trace.pi_session_to_claude(src, out)
    assert trace.call_cwds(out) == ["/w/repo"] * 3
    forms = [o.form for o in observe_task(out, PYTHON_GIT)["obs"] if o.intent == "run_tests"]
    assert forms == ["pytest", "python -m pytest"]


def test_pi_usage_maps_directly_and_keeps_pis_own_cost(tmp_path):
    u = trace.pi_session_usage(_pi_session(str(tmp_path / "s.jsonl"), abandoned=True))
    # three assistant messages on the active branch; output already includes reasoning
    assert u["requests"] == 3
    assert (u["input_tokens"], u["cache_read_input_tokens"], u["cache_creation_input_tokens"],
            u["output_tokens"]) == (30, 3000, 300, 150)
    assert u["cost_usd_vendor"] == pytest.approx(0.006) and u["model"] == "gpt-5.6-sol"
    assert trace.pi_session_header(str(tmp_path / "s.jsonl"))["cwd"] == "/w/repo"


def test_the_pi_host_reads_what_the_extension_sends():
    h = hosts.HOSTS["pi"]()
    name, args, text, code = h.tool_call({"tool_name": "bash", "tool_input": {"command": "go test"},
                                          "tool_response": {"content": "FAIL\nCommand exited with code 1",
                                                            "isError": True, "exit_code": 1}})
    assert (name, args["command"], code) == ("bash", "go test", 1) and "FAIL" in text
    assert h.headless({"TE_PI_MODE": "print"}) and not h.headless({"TE_PI_MODE": "tui"})
    assert hosts.detect({"turn_id": "t"}, hint="pi").name == "pi"


# ---- opencode sessions -------------------------------------------------------------------
def _opencode_export(path, cwd="/w/repo"):
    """An opencode export: a prompt, a bash call that fails, an edit, a bash call that passes."""
    tokens = {"input": 10, "output": 45, "reasoning": 5, "cache": {"read": 1000, "write": 100}}

    def call(cid, tool, args, state):
        return {"info": {"id": f"m-{cid}", "role": "assistant", "modelID": "gpt-5.6-sol",
                         "path": {"cwd": cwd, "root": cwd}, "cost": 0.002, "tokens": tokens},
                "parts": [{"type": "tool", "callID": cid, "tool": tool,
                           "state": dict(state, input=args)}]}

    msgs = [{"info": {"id": "u1", "role": "user"},
             "parts": [{"type": "text", "text": "fix the bug"},
                       {"type": "text", "text": "(added by opencode)", "synthetic": True}]},
            call("c1", "bash", {"command": "pytest -q"},
                 {"status": "error", "error": "bash: pytest: command not found"}),
            call("c2", "edit", {"filePath": "pkg/a.py", "oldString": "x", "newString": "y"},
                 {"status": "completed", "output": "ok", "metadata": {}}),
            call("c3", "bash", {"command": "python -m pytest -q"},
                 {"status": "completed", "output": "1 passed", "metadata": {"exit": 0}})]
    with open(path, "w") as fh:
        json.dump({"info": {"id": "oc-1"}, "messages": msgs}, fh)
    return path


def test_an_opencode_export_reads_through_the_trace_seam(tmp_path):
    src = _opencode_export(str(tmp_path / "s.json"))
    out = str(tmp_path / "t.jsonl")
    assert trace.opencode_session_to_claude(src, out) == 3
    assert trace.opencode_prompt_count(src) == 1          # opencode's own text is not a prompt
    text = open(out).read()
    assert "command not found" in text and "python -m pytest -q" in text
    assert '"is_error": true' in text and "fix the bug" in text and "added by opencode" not in text


def test_an_opencode_list_export_and_a_missing_file(tmp_path):
    src = _opencode_export(str(tmp_path / "s.json"))
    listed = str(tmp_path / "l.json")
    json.dump(json.load(open(src))["messages"], open(listed, "w"))
    assert trace.opencode_session_to_claude(listed, str(tmp_path / "t.jsonl")) == 3
    assert trace.opencode_session_to_claude(str(tmp_path / "none.json"),
                                            str(tmp_path / "x")) is None


def test_opencode_usage_counts_reasoning_as_output(tmp_path):
    u = trace.opencode_session_usage(_opencode_export(str(tmp_path / "s.json")))
    assert (u["input_tokens"], u["output_tokens"]) == (30, 150)
    assert (u["cache_read_input_tokens"], u["cache_creation_input_tokens"]) == (3000, 300)
    assert u["requests"] == 3 and u["model"] == "gpt-5.6-sol"
    assert u["cost_usd_vendor"] == 0.006


def test_the_opencode_host_reads_what_the_plugin_sends():
    h = hosts.HOSTS["opencode"]()
    name, args, text, code = h.tool_call({"tool_name": "bash", "tool_input": {"command": "go test"},
                                          "tool_response": {"content": "FAIL", "isError": True,
                                                            "exit_code": 1}})
    assert (name, args["command"], code) == ("bash", "go test", 1) and "FAIL" in text
    assert h.headless({"TE_OPENCODE_MODE": "run"}) and not h.headless({"TE_OPENCODE_MODE": "tui"})
    assert h.turn_key({"prompt_id": "s:1"}) == "s:1"
    assert hosts.detect({}, hint="opencode").name == "opencode"


def _run_events(sid="ses_x"):
    ev = [{"type": "step_start", "sessionID": sid, "part": {"type": "step-start"}},
          {"type": "text", "sessionID": sid, "part": {"type": "text", "text": "po"}},
          {"type": "text", "sessionID": sid, "part": {"type": "text", "text": "ng"}},
          {"type": "step_finish", "sessionID": sid, "part": {
              "type": "step-finish", "cost": 0.5,
              "tokens": {"input": 10, "output": 4, "reasoning": 1,
                         "cache": {"read": 7, "write": 2}}}}]
    return "\n".join(json.dumps(e) for e in ev) + "\nnot json\n"


def test_an_opencode_run_reply_is_read_from_its_json_events():
    text, usage, sid = trace.opencode_run_reply(_run_events())
    assert (text, sid) == ("pong", "ses_x")
    assert usage == {"input_tokens": 10, "cache_read_input_tokens": 7,
                     "cache_creation_input_tokens": 2, "output_tokens": 5, "cost_usd": 0.5}
    assert trace.opencode_run_reply("") == ("", None, "")


def test_the_opencode_side_call_denies_tools_and_deletes_its_session(monkeypatch):
    from tokeneconomizer.plugin import sidecall
    seen = []

    def fake_run(cmd, **kw):
        seen.append((cmd, kw))
        out = _run_events() if "--format" in cmd else "--pure --standalone"
        return subprocess.CompletedProcess(cmd, 0, out, "")

    monkeypatch.setattr(sidecall.subprocess, "run", fake_run)
    sidecall._OC_FLAGS.clear()
    text, eff = sidecall.opencode_call("is it stuck?", model="p/m", cli="oc")
    assert text == "pong" and eff
    run = next(c for c, _ in seen if "--format" in c)
    assert run[:2] == ["oc", "run"] and "--pure" in run and "--standalone" in run
    assert run[-3:] == ["--model", "p/m", "is it stuck?"]
    kw = next(k for c, k in seen if "--format" in c)
    assert kw["env"]["TE_SIDECALL"] == "1"
    perm = json.loads(kw["env"]["OPENCODE_CONFIG_CONTENT"])["permission"]
    assert perm["bash"] == "deny" and perm["edit"] == "deny"
    assert any(c[:3] == ["oc", "session", "delete"] and c[3] == "ses_x" for c, _ in seen)


def test_the_opencode_side_call_adds_only_the_flags_its_cli_lists(monkeypatch):
    from tokeneconomizer.plugin import sidecall
    monkeypatch.setattr(sidecall.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0, "  --format  json\n", ""))
    sidecall._OC_FLAGS.clear()
    assert sidecall.opencode_flags("oc") == []
    assert sidecall.runner_for("opencode", type("C", (), {"paid_model": ""})()) is not None


# ---- the guard sees edits made through the shell ---------------------------------------------
from tokeneconomizer import loopguard as guard  # noqa: E402
from tokeneconomizer.loopguard import turn  # noqa: E402


@pytest.mark.parametrize("cmd,want", [
    ("sed -i 's/a/b/' pkg/mod.py", ["mod.py"]),
    ("cd /t && sed -i.bak -e 's/a/b/' -e 's/c/d/' x.py y.py && pytest -q", ["x.py", "y.py"]),
    ("sed -i '' 's/a/b/' x.py", ["x.py"]),                         # BSD sed's empty suffix
    ("perl -pi -e 's/foo/bar/' src/a.rs", ["a.rs"]),
    ("cat > repro.py <<'EOF'\nprint(1 > 0)\nEOF\npython repro.py", ["repro.py"]),
    ("echo 'x' | tee -a setup.cfg", ["setup.cfg"]),
    # how Sonnet 5.5 edits: an inline script that reads the file and writes it back
    ("python - <<'E'\np='sympy/printing/str.py'; s=open(p).read()\nopen(p,'w').write(s)\nE",
     ["str.py"]),
    ("python - <<'E'\nfor p in ['x/a.py', 'x/b.py']:\n    open(p, 'w').write('')\nE",
     ["a.py", "b.py"]),
    ("python - <<'E'\nfrom pathlib import Path\nPath('m/n.py').write_text('x')\nE", ["n.py"]),
    ("git apply /tmp/fix.diff", ["(patch)"]),
    # Codex 0.163.0 edits like this, then tests, in ONE command (seen live)
    ("apply_patch <<'PATCH'\n*** Begin Patch\n*** Update File: pkg/__init__.py\n@@\n-    x\n"
     "+    y\n*** End Patch\nPATCH\npython3 -m unittest", ["__init__.py"]),
    ("apply_patch <<'P'\n*** Begin Patch\n*** Add File: a/new.py\n+x\n*** Update File: b/old.py\n"
     "*** End Patch\nP", ["new.py", "old.py"]),
    ("git checkout -- a.py b/c.py", ["a.py", "c.py"]),
    # not edits: output captures, scratch, reads, and operators inside quotes
    ("python -m pytest -q > /tmp/out.txt 2>&1; tail -5 /tmp/out.txt", []),
    ("grep -n 'x' a.py > hits.txt", []),
    ("grep -n '>' x.py", []),
    ("grep -nE 'a|b' x.py | head", []),
    ("python -c \"print(1>2)\"", []),
    ("git stash -q; python bin/test x; git stash pop -q", []),
    ("sed -n '1,20p' pkg/mod.py", []),
    # a quoted `|` in a sed script must not split the command (seen live: sympy-13091)
    ("sed -i -E 's/x (<|<=|>|>=) y/z/' numbers.py expr.py", ["numbers.py", "expr.py"]),
    ("sed -i 's/a/b/' x.py 2>&1 | tail -2", ["x.py"]),
])
def test_edits_made_through_the_shell_are_recognised(cmd, want):
    assert guard.shell_edit_targets(cmd) == want


def test_shell_edits_reach_the_state_the_judge_reads_and_the_ended_green_proxy():
    g = guard.TurnGuard(turn_key="t")
    edit = "python - <<'E'\np='pkg/mod.py'; s=open(p).read()\nopen(p,'w').write(s[::-1])\nE"
    for _ in range(2):
        g.fold("Bash", {"command": edit}, "")
    assert g.st.edit_churn == 2 and g.whole.distinct_edit_files == 1 and g.edits == 2
    digest = g.st.recent_digest()
    assert "edit_churn=2" in digest and "[edits mod.py]" in digest
    # one command that edits and then fails its tests: the turn did not end green
    g.fold("Bash", {"command": "sed -i 's/b/c/' pkg/mod.py && pytest -q"},
           "FAILED tests/test_m.py::t - AssertionError")
    assert g.edits == 3 and g.last_verify > g.last_edit and not g.resolved_proxy()


def test_reading_a_file_full_of_asserts_is_not_a_failure():
    """A test file read back is full of `assert` and `Error`: reading it, three times over, is
    no failure and no repeated failure, and opens no gate to the judge."""
    test_file = "def test_total():\n    assert total([1, 2]) == 3\n    raise ValueError('no')\n"
    g = guard.TurnGuard(turn_key="t")
    for _ in range(3):
        g.fold("read", {"path": "tests/test_total.py"}, test_file)
        g.fold("grep", {"pattern": "assert"}, "tests/test_total.py:2:    assert total")
    g.fold("bash", {"command": "cat tests/test_total.py"}, test_file, 0)
    assert (g.whole.error_outputs, g.whole.repeated_fail_sigs) == (0, 0)
    assert not guard.gate_for_judge(g.whole, dict(guard.DEFAULT_THRESHOLDS, gate_min_turns=1))
    # what did fail still counts: a non-zero exit, or a read the tool itself reports as failed
    g.fold("bash", {"command": "pytest -q"}, "FAILED tests/test_total.py - AssertionError", 1)
    g.fold("bash", {"command": "pytest -q"}, "FAILED tests/test_total.py - AssertionError", None)
    assert (g.whole.error_outputs, g.whole.repeated_fail_sigs) == (2, 1)
    s = guard.SpiralState()
    s.update("Read", {"file_path": "x.py"}, "Error: File does not exist.", failed=True)
    assert s.error_outputs == 1


def test_the_judge_is_told_how_the_last_verification_went_not_that_tests_are_red():
    g = guard.TurnGuard(turn_key="t")
    vendored = "turns=3\nrepo_context: ... = 1.2x that typical length. Tests are still NOT green. A"
    assert "No test or verification run has been seen" in g.live_digest(vendored)
    g.fold("Bash", {"command": "python bin/test sympy/core"}, "tests finished: 10 passed")
    told = g.live_digest(vendored)
    assert "The last verification run passed." in told and "NOT green" not in told
    g.fold("Edit", {"file_path": "/r/a.py", "old_string": "a", "new_string": "b"}, "ok")
    assert g.live_digest(vendored).count("There have been edits since.") == 1


class _BackgroundJudge:
    """`plugin.judge.AsyncJudge`'s interface, with the verdict handed in by the test."""

    def __init__(self):
        self.started, self.verdict = [], None

    def start(self, g, digest):
        self.started.append(digest)
        return f"job{len(self.started)}"

    def poll(self, g):
        pending = g.judge_pending or {}
        if self.verdict is None or pending.get("id") != f"job{len(self.started)}":
            return False, None
        v, self.verdict = self.verdict, None
        return True, v


def _spin(g, judge, n, envelope=6.0):
    """An edit and an identical failing test run, alternately: a loop the gate opens on."""
    out = []
    for k in range(n):
        call = (("Edit", {"file_path": "/r/a.py", "old_string": "x", "new_string": "y"}, "ok")
                if k % 2 == 0 else
                ("Bash", {"command": "pytest -q"}, "FAILED tests/test_a.py::t - AssertionError"))
        out.append(g.fold(*call, envelope=envelope, judge=judge))
    return out


def test_a_background_judge_starts_once_and_its_verdict_nudges_a_later_call():
    g, j = guard.TurnGuard(turn_key="t"), _BackgroundJudge()
    assert _spin(g, j, 10) == [None] * 10            # nothing is decided while it is out
    assert len(j.started) == 1                       # and no second job is started
    assert "NOT green" not in j.started[0]
    j.verdict = {"stuck": True, "confidence": 0.9, "reason": "loop",
                 "_nudge": "Read the failing assertion before editing again."}
    assert _spin(g, j, 1)[0] == ("nudge", "judge-stuck@0.90:loop")
    assert g.nudge_body(6.0) == "Read the failing assertion before editing again."
    assert g.nudge_body(6.0) != "Read the failing assertion before editing again."  # used once


def test_a_productive_verdict_backs_the_judge_off():
    g, j = guard.TurnGuard(turn_key="t"), _BackgroundJudge()
    _spin(g, j, 7)
    j.verdict = {"stuck": False, "confidence": 0.8, "reason": "progress"}
    assert _spin(g, j, 1) == [None]
    assert g.judge_gap == g.JUDGE_BASE_GAP * 2 and g.judge_pending is None


def test_a_job_that_never_answers_stops_blocking_the_next_one():
    g, j = guard.TurnGuard(turn_key="t"), _BackgroundJudge()
    _spin(g, j, 7)
    assert len(j.started) == 1 and g.judge_pending
    g.judge_pending["since"] = 0                     # its process died
    _spin(g, j, g.judge_gap)                         # well before the backstop at 2.5x
    assert len(j.started) == 2


# ---- Copilot session logs ------------------------------------------------------------------
def copilot_events(path, calls, *, shutdown=True, model="gpt-5.4", cwd="/w/repo", sid="cp1",
                   prompts=1):
    """A Copilot `session-state/<id>/events.jsonl`: session.start, `prompts` user messages (the
    tool calls, as execution_start/complete pairs, in the first), and (unless killed) a
    session.shutdown."""
    ev = [{"type": "session.start", "data": {"sessionId": sid, "copilotVersion": "1.0.83",
                                            "selectedModel": model, "context": {"cwd": cwd}}},
          {"type": "user.message", "data": {"content": "fix it", "turnId": "0"}}]
    for n, (name, args, text, ok) in enumerate(calls):
        ev.append({"type": "tool.execution_start", "data": {
            "toolCallId": f"c{n}", "toolName": name, "arguments": args, "turnId": "0"}})
        ev.append({"type": "tool.execution_complete", "data": {
            "toolCallId": f"c{n}", "success": ok, "turnId": "0",
            "result": {"content": text, "detailedContent": text}}})
    for k in range(1, prompts):
        ev.append({"type": "user.message", "data": {"content": "and then", "turnId": str(k)}})
    if shutdown:
        ev.append({"type": "session.shutdown", "data": {
            "modelMetrics": {model: {"requests": {"count": 4}, "usage": {
                "inputTokens": 4000, "outputTokens": 80, "cacheReadTokens": 2400,
                "cacheWriteTokens": 100, "reasoningTokens": 0}}},
            "toolDefinitionsTokens": 13149}})
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write("\n".join(json.dumps(e) for e in ev) + "\n")
    return path


def test_copilot_logs_read_through_the_existing_trace_seam(tmp_path):
    p = copilot_events(str(tmp_path / "s" / "events.jsonl"), [
        ("bash", {"command": "pytest -q"}, "1 failed\n<shellId: 0 completed with exit code 1>",
         True),
        ("view", {"path": "/w/repo/a.py"}, "x = 1", True)])
    traj = trace.load_trajectory(p)
    assert [t[0] for t in traj] == ["bash", "view"] and traj[0][1]["command"] == "pytest -q"
    assert trace.tool_usage(p) == {"bash": 1, "view": 1}
    (turn,) = trace.copilot_turns(p)
    assert turn[0][0] == "bash" and turn[0][3] == 1 and turn[1][3] is None
    assert trace.copilot_session_meta(p)["cwd"] == "/w/repo"


def test_a_copilot_call_into_the_session_directory_is_the_plain_form(tmp_path):
    """Copilot's models type `cd <repo> && ` before every command: `cd /w/repo && python -m
    unittest` is the same form as the bare one."""
    from tokeneconomizer.microskills.observe import PYTHON_GIT, observe_task
    p = copilot_events(str(tmp_path / "s" / "events.jsonl"), [
        ("bash", {"command": "cd /w/repo && pytest -q"},
         "bash: pytest: command not found\n<shellId: 0 completed with exit code 127>", False),
        ("bash", {"command": "cd /w/repo && python -m unittest -q"},
         "Ran 4 tests in 0.001s\nOK\n<shellId: 0 completed with exit code 0>", True)])
    assert trace.call_cwds(p) == ["/w/repo", "/w/repo"]
    assert [o.form for o in observe_task(p, PYTHON_GIT)["obs"] if o.intent == "run_tests"] == [
        "pytest", "python -m unittest"]


def test_copilot_usage_is_converted_from_its_grand_input_total(tmp_path):
    p = copilot_events(str(tmp_path / "s" / "events.jsonl"), [])
    u = trace.copilot_session_usage(p)
    # inputTokens counts cache reads and writes (verified against a scripted endpoint)
    assert u["input_tokens"] == 4000 - 2400 - 100
    assert u["cache_read_input_tokens"] == 2400 and u["cache_creation_input_tokens"] == 100
    assert u["output_tokens"] == 80 and u["requests"] == 4 and u["model"] == "gpt-5.4"
    assert u["tool_definitions_tokens"] == 13149
    killed = trace.copilot_session_usage(
        copilot_events(str(tmp_path / "k" / "events.jsonl"), [], shutdown=False))
    assert killed["input_tokens"] is None and killed["model"] == "gpt-5.4"


def test_copilot_tools_are_grouped_by_mcp_server(tmp_path):
    servers = {"github-mcp-server", "github", "acme-db"}
    assert trace.mcp_server_of("github-mcp-server-list_issues", servers) == \
        "github-mcp-server"                                  # the longest prefix wins
    assert trace.mcp_server_of("acme-db-query", servers) == "acme-db"
    assert trace.mcp_server_of("bash", servers) is None
    home = tmp_path / "cp"
    home.mkdir()
    (home / "mcp-config.json").write_text(json.dumps({"mcpServers": {"acme-db": {}}}))
    assert trace.copilot_mcp_servers({"COPILOT_HOME": str(home)}) == {
        "github-mcp-server", "acme-db"}


@pytest.mark.real_sidecalls
def test_a_copilot_side_call_is_a_bare_completion_priced_from_its_own_log(tmp_path,
                                                                          monkeypatch):
    import subprocess
    from tokeneconomizer.plugin import sidecall
    monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "cp"))
    copilot_events(str(tmp_path / "cp" / "session-state" / "side1" / "events.jsonl"), [])
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["env"] = cmd, kw["env"]
        out = "\n".join(json.dumps(e) for e in (
            {"type": "assistant.message", "data": {"content": "- use python -m pytest"}},
            {"type": "result", "sessionId": "side1", "exitCode": 0}))
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(sidecall.subprocess, "run", fake_run)
    text, eff = sidecall.copilot_call("propose facts")
    assert text == "- use python -m pytest" and eff and eff > 0
    cmd = seen["cmd"]
    assert cmd[:3] == ["copilot", "-p", "propose facts"] and "--disable-builtin-mcps" in cmd
    assert cmd.count("--excluded-tools") == len(sidecall.COPILOT_TOOLS)
    assert seen["env"]["TE_SIDECALL"] == "1"            # its own hooks must not recurse
    cfg = live_config.LiveConfig()
    assert sidecall.runner_for("copilot", cfg).__code__.co_names.count("copilot_call") == 1
    assert sidecall.runner_for("claude", cfg).__code__.co_names.count("claude_call") == 1
    assert sidecall.runner_for("pi", cfg).__code__.co_names.count("pi_call") == 1


@pytest.mark.real_sidecalls
def test_a_side_call_with_no_reply_is_noted_and_keeps_to_a_bare_completion(tmp_path,
                                                                           monkeypatch):
    """Under Copilot's BYOK the provider's variables do not reach the hooks, so a side call can
    run signed out and answer nothing: that is noted, and the call stays a bare completion."""
    import subprocess
    from tokeneconomizer.plugin import sidecall
    monkeypatch.setenv("TOKENECONOMIZER_HOME", str(tmp_path / "te"))
    home = tmp_path / "cp"
    home.mkdir()
    (home / "mcp-config.json").write_text(json.dumps({"mcpServers": {"acme-db": {}}}))
    monkeypatch.setenv("COPILOT_HOME", str(home))
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 1, stdout="",
                                           stderr="Error: No authentication information found.")

    monkeypatch.setattr(sidecall.subprocess, "run", fake_run)
    assert sidecall.copilot_call("propose facts") == ("", None)
    cmd = seen["cmd"]
    assert cmd[cmd.index("--disable-mcp-server") + 1] == "acme-db"
    log = (tmp_path / "te" / "errors.log").read_text()
    assert "sidecall copilot: no reply (exit 1)" in log and "No authentication" in log


def test_claude_side_calls_stay_out_of_the_persons_history_where_the_cli_allows(monkeypatch):
    import subprocess
    from tokeneconomizer.plugin import sidecall
    helps = {"new": "  --no-session-persistence   Disable session persistence", "old": "  -p"}
    monkeypatch.setattr(sidecall.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0, stdout=helps[cmd[0]], stderr=""))
    monkeypatch.setattr(sidecall, "_HELP", {})
    assert sidecall.claude_flags("new") == ["--no-session-persistence"]
    assert sidecall.claude_flags("old") == []


@pytest.mark.real_sidecalls
def test_a_claude_side_call_attaches_none_of_the_persons_mcp_servers(monkeypatch):
    """`--tools ""` leaves every MCP server attached: the side call drops them where the CLI can,
    while the catalog probes, which need the tools, keep them."""
    import subprocess
    from tokeneconomizer.plugin import sidecall
    helps = {"new": "  --no-session-persistence\n  --strict-mcp-config   Only use MCP servers "
                    "from --mcp-config\n  --mcp-config <configs...>", "old": "  -p"}
    seen = {}

    def fake_run(cmd, **kw):
        if cmd[1:] == ["--help"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=helps[cmd[0]], stderr="")
        seen.update(cmd=cmd, cwd=kw["cwd"], env=kw["env"])
        if "--mcp-config" in cmd:
            with open(cmd[cmd.index("--mcp-config") + 1], encoding="utf-8") as fh:
                seen["config"] = json.load(fh)
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({
            "result": "OK", "usage": {"input_tokens": 10, "output_tokens": 1},
            "modelUsage": {"claude-haiku-4-5": {}}}), stderr="")

    monkeypatch.setattr(sidecall.subprocess, "run", fake_run)
    monkeypatch.setattr(sidecall, "_HELP", {})
    assert sidecall.claude_call("Reply with exactly: OK", cli="new")[0] == "OK"
    cmd = seen["cmd"]
    assert "--strict-mcp-config" in cmd and seen["config"] == {"mcpServers": {}}
    config = cmd[cmd.index("--mcp-config") + 1]
    assert os.path.dirname(config) == seen["cwd"]
    # `--mcp-config` takes several values: what follows its file is an option
    assert cmd[cmd.index("--mcp-config") + 2].startswith("--")
    assert seen["env"]["TE_SIDECALL"] == "1"
    assert "--strict-mcp-config" not in sidecall.claude_flags("new")     # the probes' flags
    # a Claude Code without the flags would reject them: the call goes out without
    sidecall.claude_call("Reply with exactly: OK", cli="old")
    assert "--strict-mcp-config" not in seen["cmd"] and "--mcp-config" not in seen["cmd"]


def test_claude_cli_falls_back_to_the_native_install_when_not_on_path(monkeypatch, tmp_path):
    import os
    from tokeneconomizer.plugin import sidecall
    monkeypatch.setattr(sidecall.shutil, "which", lambda name: None)
    monkeypatch.setattr(sidecall.os.path, "expanduser",
                        lambda p: p.replace("~", str(tmp_path), 1))
    assert sidecall.claude_cli() == "claude"
    exe = tmp_path / ".local" / "bin" / ("claude.exe" if os.name == "nt" else "claude")
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    assert sidecall.claude_cli() == str(exe)
    monkeypatch.setattr(sidecall.shutil, "which", lambda name: "/opt/bin/claude")
    assert sidecall.claude_cli() == "/opt/bin/claude"


# `pi --print --mode json` on Pi 1.2.0 through OpenRouter, trimmed: the header, the system and
# user messages, the reply (a thinking block, then the text), and the run's end.
PI_JSON_RUN = [
    {"type": "session", "version": 3, "id": "01a13c79", "cwd": "/tmp/te-side-x"},
    {"type": "message_end", "message": {"role": "system", "content": "",
                                        "sections": {"preamble": "You are an expert..."}}},
    {"type": "message_end", "message": {"role": "user", "content": [
        {"type": "text", "text": "propose facts"}]}},
    {"type": "message_update", "usage": {"input": 745, "output": 3}},
    {"type": "message_end", "message": {
        "role": "assistant", "provider": "openrouter", "model": "anthropic/claude-haiku-4.5",
        "content": [{"type": "thinking", "thinking": "Two lines."},
                    {"type": "text", "text": "- use python -m pytest"}],
        "usage": {"input": 745, "output": 298, "cacheRead": 0, "cacheWrite": 0,
                  "totalTokens": 1043, "cacheWrite1h": 0, "reasoning": 281,
                  "cost": {"input": 0.000745, "output": 0.00149, "total": 0.002235}},
        "stopReason": "stop"}},
    {"type": "agent_settled", "aborted": False},
]


def test_a_pi_reply_is_read_from_its_json_run():
    text, usage = trace.pi_json_reply("\n".join(json.dumps(e) for e in PI_JSON_RUN))
    assert text == "- use python -m pytest"
    assert usage == {"input_tokens": 745, "cache_read_input_tokens": 0,
                     "cache_creation_input_tokens": 0, "output_tokens": 298,
                     "cost_usd": 0.002235, "model": "anthropic/claude-haiku-4.5"}
    assert trace.pi_json_reply("") == ("", None)


@pytest.mark.real_sidecalls
def test_a_pi_side_call_is_a_bare_run_on_the_persons_own_pi(monkeypatch):
    import subprocess
    from tokeneconomizer.plugin import sidecall
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        if "--no-mcp" in cmd:                       # Pi before 1.0 has no such flag
            return subprocess.CompletedProcess(cmd, 1, stdout="",
                                               stderr="Error: Unknown option: --no-mcp\n")
        out = "\n".join(json.dumps(e) for e in PI_JSON_RUN)
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(sidecall.subprocess, "run", fake_run)
    text, eff = sidecall.pi_call("propose facts")
    assert text == "- use python -m pytest" and eff and eff > 0
    (first, kw), (second, _) = calls
    assert first[:4] == ["pi", "--print", "--mode", "json"] and first[-2:] == ["--", "propose facts"]
    for flag in ("--no-session", "--no-tools", "--no-extensions", "--no-context-files"):
        assert flag in first
    assert "--model" not in first                   # the person's own default model and login
    assert "--no-mcp" in first and "--no-mcp" not in second
    assert kw["env"]["TE_SIDECALL"] == "1" and kw["stdin"] is subprocess.DEVNULL
    calls.clear()
    sidecall.pi_call("propose facts", model="openrouter/anthropic/claude-haiku-4.5")
    assert calls[-1][0][-4:-2] == ["--model", "openrouter/anthropic/claude-haiku-4.5"]


@pytest.mark.real_sidecalls
def test_a_pi_side_call_loads_the_provider_extensions_and_the_pi_the_person_names(
        monkeypatch, tmp_path):
    """A model whose provider an extension registers is not there under `--no-extensions`; Pi
    still loads an explicit `--extension`, and `--no-tools` keeps the extension's tools out."""
    import subprocess
    from tokeneconomizer.plugin import sidecall
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd, 0, stdout="\n".join(json.dumps(e) for e in PI_JSON_RUN), stderr="")

    monkeypatch.setattr(sidecall.subprocess, "run", fake_run)
    monkeypatch.chdir(tmp_path)
    cfg = live_config.load_config(str(tmp_path / "te"), env={
        "TE_PI_EXTENSIONS": "ext/gateway.ts, builtin:web", "TE_PI_CLI": "/opt/pi/bin/pi",
        "TE_PAID_MODEL": "gateway/claude-haiku"})
    assert not cfg.warnings
    text, _ = sidecall.runner_for("pi", cfg)("propose facts")
    cmd = calls[-1]
    assert text == "- use python -m pytest" and cmd[0] == "/opt/pi/bin/pi"
    assert "--no-extensions" in cmd and "--no-tools" in cmd
    assert [cmd[i + 1] for i, a in enumerate(cmd) if a == "--extension"] == [
        os.path.join(os.getcwd(), "ext", "gateway.ts"), "builtin:web"]  # the call runs elsewhere
    assert cmd[-4:] == ["--model", "gateway/claude-haiku", "--", "propose facts"]
    # unset, the call is what it was: the first `pi` on PATH, no extension
    sidecall.runner_for("pi", live_config.LiveConfig())("propose facts")
    assert calls[-1][0] == "pi" and "--extension" not in calls[-1]


def test_a_copilot_notice_is_a_progress_line_before_the_hook_output():
    import io
    from tokeneconomizer.plugin import hook
    out = io.StringIO()
    hook.main([], io.StringIO("{}"), out, {"TE_DISABLE": "1"})
    assert out.getvalue() == "{}\n"
    # what hook.main writes for an output carrying a Copilot notice
    from unittest import mock
    with mock.patch.object(hook, "run", return_value=dict(
            hosts.Copilot().notice("hello"), additionalContext="ctx")):
        out = io.StringIO()
        hook.main([], io.StringIO('{"session_id": "s"}'), out, {})
    first, last = out.getvalue().splitlines()
    assert json.loads(first) == {"type": "progress", "message": "hello"}
    assert json.loads(last) == {"additionalContext": "ctx"}


def test_copilot_side_calls_name_a_model_a_byok_provider_can_serve():
    from tokeneconomizer.plugin.sidecall import copilot_model
    byok = {"COPILOT_PROVIDER_BASE_URL": "https://api.anthropic.com",
            "COPILOT_PROVIDER_TYPE": "anthropic"}
    # `auto` is the subscription's router; a BYOK provider has none, and the call answered nothing
    assert copilot_model("", dict(byok, COPILOT_MODEL="auto")) == "claude-haiku-4-5"
    assert copilot_model("", dict(byok, COPILOT_MODEL="claude-sonnet-5")) == "claude-sonnet-5"
    assert copilot_model("claude-opus-5", byok) == "claude-opus-5"           # paid_model wins
    assert copilot_model("", {"COPILOT_MODEL": "auto"}) == ""                # subscription
    assert copilot_model("", {"COPILOT_PROVIDER_BASE_URL": "http://localhost:11434/v1"}) == ""
    # OpenRouter wants its own ids, whichever wire Copilot speaks to it
    for wire in ("anthropic", "openai"):
        assert copilot_model("", {"COPILOT_PROVIDER_BASE_URL": "https://openrouter.ai/api",
                                  "COPILOT_PROVIDER_TYPE": wire}) == "anthropic/claude-haiku-4.5"


@pytest.mark.skipif(os.name == "nt", reason="the probe trap is made of sh scripts")
def test_the_probe_repository_carries_its_trap(tmp_path):
    import subprocess
    from tokeneconomizer.plugin import probe
    repo, trap = probe.make_repo(str(tmp_path))
    bare = subprocess.run([os.path.join(trap, "pytest"), "-q"], cwd=repo, capture_output=True,
                          text=True)
    assert bare.returncode == 127 and "command not found" in bare.stderr
    # pytest is "not installed" for any interpreter the session runs, whatever really is
    for py in ("python", "python3"):
        mod = subprocess.run([os.path.join(trap, py), "-m", "pytest", "-q"], cwd=repo,
                             capture_output=True, text=True)
        assert mod.returncode == 1 and "No module named 'pytest'" in mod.stderr
    ok = subprocess.run([os.path.join(trap, "python"), "-m", "unittest", "-q"], cwd=repo,
                        capture_output=True, text=True)
    assert ok.returncode == 0 and "Ran 1 test" in ok.stderr
    assert subprocess.run(["git", "-C", repo, "status", "--porcelain"], capture_output=True,
                          text=True).stdout == ""                          # committed clean


def test_the_probe_counts_pytest_attempts_not_exit_codes():
    from tokeneconomizer.plugin import probe
    # commands two real Haiku sessions ran: the piped one exited 0 although pytest is missing
    runs, tried = probe.test_commands([
        'find . -name "pytest.ini" -o -name "go.mod" | head -20',
        "cd /r && python -m pytest tests/ -v 2>&1 | tail -20",
        "cd /r && python -m unittest discover -s tests -v 2>&1 | tail -20"])
    assert len(runs) == 2 and tried == 1
    runs, tried = probe.test_commands(["pytest -q", "python3 -m unittest", "ls tests/"])
    assert len(runs) == 2 and tried == 1
    assert probe.test_commands(['python -m unittest discover -s tests -p "test_*.py"']) == (
        ['python -m unittest discover -s tests -p "test_*.py"'], 0)


def test_a_small_envelopes_backstop_never_preempts_the_nudge():
    """Interactive repositories learn envelopes of ~4-10 calls, so the hard backstop
    (1.8-2.5x the envelope) lies inside the stuck rule's own floor: before this fix, the first
    stuck call tripped the backstop and the turn was stopped without ever being redirected."""
    from tokeneconomizer import loopguard as guard
    thr = guard.scaled_thresholds("aggressive")
    fail = ("FAIL: test_floats (tests.test_calc.TestAdd)\nTraceback (most recent call last):\n"
            "AssertionError: 0.30000000000000004 != 0.3\n\nFAILED (failures=1)\n")
    g = guard.TurnGuard(turn_key="t")
    calls = [("Bash", {"command": "python -m unittest"}, fail, 1)]
    for k in range(30):
        calls += [("Edit", {"file_path": "src/calc/__init__.py", "old_string": "x",
                            "new_string": f"y{k}"}, "ok", None),
                  ("Bash", {"command": "python -m unittest"}, fail, 1)]
    fired = []
    for i, (n, a, r, c) in enumerate(calls, 1):
        d = g.fold(n, a, r, c, envelope=4.0, thr=thr, nudge_budget=1)
        if d:
            fired.append((i, d[0]))
            if d[0] == "nudge":
                g.after_nudge()
            else:
                break
    assert fired == [(14, "nudge"), (28, "abort")]


@pytest.mark.parametrize("cmd", ["python tests/runtests.py model_inheritance -v 2",
                                 "./runtests.py basic", ".venv/bin/pytest -q tests/test_a.py",
                                 "cd web && ./node_modules/.bin/jest src", "/usr/bin/python3 -m pytest",
                                 "/w/venv/bin/pytest -q", "~/.local/bin/pytest -q",
                                 "python3 /w/repo/tests/runtests.py basic", "node --test test/"])
def test_a_runner_named_by_path_counts_as_a_verification(cmd):
    """A failing run after the last edit means the turn did not end green, whatever path the
    runner was called by -- otherwise long failing turns feed the envelope and the guard
    stops seeing them as long."""
    g = guard.TurnGuard(turn_key="t")
    g.fold("Edit", {"file_path": "/r/a.py", "old_string": "x", "new_string": "y"}, "ok")
    g.fold("Bash", {"command": cmd}, "FAILED (failures=1)", 1)
    assert g.verifications == 1 and not g.resolved_proxy()


@pytest.mark.parametrize("cmd", ["cat /w/pytest.ini", "cat pytest.ini", "ls /w/tests",
                                 "vim tests/runtests.py.bak"])
def test_a_file_named_like_a_runner_is_not_a_verification(cmd):
    assert not guard.VERIFY_RE.search(cmd)


TAP_FAILING = "# tests 9\n# suites 0\n# pass 8\n# fail 1\n# cancelled 0"


def test_a_test_run_whose_status_was_replaced_is_judged_by_its_output():
    """A pipe, `||` or a report of the status gives the command line another command's status:
    `npm test 2>&1 | tail -n 8` exits with `tail`'s 0 whatever the tests did."""
    g = guard.TurnGuard(turn_key="t")
    for _ in range(3):
        g.fold("bash", {"command": "npm test 2>&1 | tail -n 8"}, TAP_FAILING, 0)
    assert (g.whole.error_outputs, g.whole.repeated_fail_sigs) == (3, 2)
    assert not g.resolved_proxy()
    # Codex records a call as `bash -lc '<script>'`
    g = guard.TurnGuard(turn_key="t")
    g.fold("bash", {"command": "/bin/bash -lc 'cd /w && timeout 60 go test ./... 2>&1 | tail -3'"},
           "--- FAIL: TestAdd (0.00s)\nFAIL\tacme/x\t0.01s", 0)
    assert g.whole.error_outputs == 1 and not g.resolved_proxy()
    for cmd in ('python3 -m unittest tests.test_x; echo "exit=$?"', "npm test || true"):
        g = guard.TurnGuard(turn_key="t")
        g.fold("bash", {"command": cmd}, "FAILED (failures=1)\nexit=1", 0)
        assert g.whole.error_outputs == 1 and not g.resolved_proxy()
    # under pipefail the status is the tests' own; `| grep` finding nothing is no failure
    g = guard.TurnGuard(turn_key="t")
    g.fold("bash", {"command": "set -o pipefail; npm test | tail -n 8"}, TAP_FAILING, 0)
    g.fold("bash", {"command": "pytest -q | grep FAILED"}, "", 1)
    assert g.whole.error_outputs == 0 and g.resolved_proxy()


def test_a_test_run_followed_by_other_commands_is_judged_by_its_runners_summary():
    """`pytest -q; git diff` exits with `git`'s status, and its output holds the diff too: the
    runner's own summary line says how the tests went."""
    diff = "diff --git a/m.py b/m.py\n+    raise ValueError('no')\n"
    g = guard.TurnGuard(turn_key="t")
    g.fold("bash", {"command": "pytest -q; git diff"},
           "F.\n1 failed, 1 passed in 0.12s\n" + diff, 0)
    assert g.whole.error_outputs == 1 and not g.resolved_proxy()
    # passing tests, whatever the later command's status or the words in its output
    for code in (1, 0, None):
        g = guard.TurnGuard(turn_key="t")
        g.fold("bash", {"command": "pytest -q\ngit diff --exit-code"}, "2 passed in 0.10s\n" + diff,
               code)
        assert g.whole.error_outputs == 0 and g.resolved_proxy()


@pytest.mark.parametrize("out, failed", [
    ("1 failed, 4 passed, 2 warnings in 1.23s", True), ("== 1 error in 0.12s ==", True),
    ("2 passed, 1 warning in 0.30s", False), ("no tests ran in 0.01s", None),
    ("Ran 3 tests in 0.1s\n\nFAILED (failures=1)", True), ("Ran 3 tests in 0.1s\n\nOK", False),
    ("tests finished: 10 passed, 1 failed, in 3.20 seconds", True), (TAP_FAILING, True),
    ("# pass 9\n# fail 0", False), ("Tests:       1 failed, 3 passed, 4 total", True),
    ("  3 passing (20ms)\n  1 failing", True), ("--- FAIL: TestAdd (0.00s)", True),
    ("ok  \tacme/x\t0.012s", False), ("test result: FAILED. 1 passed; 1 failed", True),
    ("test result: ok. 3 passed; 0 failed", False), ("5 examples, 1 failure", True),
    ("Tests run: 4, Failures: 0, Errors: 1", True), ("[INFO] BUILD SUCCESS", False),
    ("+    raise ValueError('no')", None), ("ok 1 - add", None)])
def test_a_runners_summary_gives_its_verdict(out, failed):
    assert turn.runner_verdict(out) is failed


@pytest.mark.parametrize("cmd", ["tar -xOf pkg.tgz pkg/x.py | sed -n 1,80p",
                                 "cat tests/test_x.py | head -40", "grep -rn pytest src | head",
                                 "python -m pytest x.py --collect-only -q | rg test_",
                                 "bin/test --help | head", "python repro.py 2>&1 | tail",
                                 "pytest -q; git diff --stat", "pytest -q\npython - <<'PY'\nx\nPY"])
def test_any_other_command_keeps_its_exit_status(cmd):
    """Piped reads of source, and a test run among other commands with no runner's summary in
    the result, keep the status the agent reported: `assert` and `Error` in such output are no
    verdict."""
    g = guard.TurnGuard(turn_key="t")
    g.fold("bash", {"command": cmd}, "def f():\n    assert x\n    raise ValueError('no')\n", 0)
    assert g.whole.error_outputs == 0


@pytest.mark.parametrize("out, failing", [
    (TAP_FAILING, True), ("# pass 9\n# fail 0", False), ("not ok 3 - rates", True),
    ("  2 failing", True), ("5 examples, 0 failures", False), ("5 examples, 2 failures", True),
    ("Tests run: 4, Failures: 1, Errors: 0", True), ("Tests run: 4, Failures: 0, Errors: 0", False),
    ("--- FAIL: TestAdd (0.00s)", True), ("ok  \tacme/x\t0.01s", False)])
def test_other_ecosystems_failing_test_summaries_count_as_failures(out, failing):
    g = guard.TurnGuard(turn_key="t")
    g.fold("bash", {"command": "make test"}, out, None)
    assert (g.whole.error_outputs == 1) is failing


def test_a_unittest_failure_is_known_by_its_traceback():
    """unittest's `FAIL: test_x (...)` is not Go's `FAIL`: a Django failure is known by its
    traceback, not its test name, so the same failure in another test still repeats."""
    from tokeneconomizer.loopguard.state import _failure_sig
    out = "FAIL: test_x (validators.tests.T)\nTraceback (most recent call last):\n  ..."
    assert _failure_sig(out) == _failure_sig("Traceback (most recent call last):")


def test_codex_exec_and_copilot_p_are_told_from_their_terminal_uis(tmp_path, monkeypatch):
    """The still-stuck notice names the headless way to stop in `codex exec` and `copilot -p`.
    Codex says how a session was started only in its rollout's first record; Copilot only in its
    own command line (0.163.0, 1.0.101: the hook payloads and environments are the same)."""
    exec_log, tui_log = tmp_path / "rollout-exec.jsonl", tmp_path / "rollout-tui.jsonl"
    exec_log.write_text(json.dumps({"type": "session_meta", "payload": {
        "originator": "codex_exec", "source": "exec"}}) + "\n")
    tui_log.write_text(json.dumps({"type": "session_meta", "payload": {
        "originator": "codex-tui", "source": "vscode"}}) + "\n")
    c = hosts.Codex()
    assert c.headless({}, str(exec_log)) and not c.headless({}, str(tui_log))
    assert not c.headless({}, None) and not c.headless({}, str(tmp_path / "missing"))
    cp = hosts.Copilot()
    monkeypatch.setattr(hosts, "_ancestor_args", lambda: [
        "/bin/sh /p/scripts/te-hook --host copilot",
        "copilot -p Reply with the word ready. --model claude-haiku-4.5"])
    assert cp.headless({})
    monkeypatch.setattr(hosts, "_ancestor_args", lambda: ["copilot", "sh -c cd ~/proj && copilot"])
    assert not cp.headless({})
    monkeypatch.setattr(hosts, "_ancestor_args", lambda: ["copilot --prompt=fix it"])
    assert cp.headless({})
    assert cp.stop_hint(True) == "Press Ctrl+C to end the run."
    assert cp.stop_hint(False) == "Press Esc twice to interrupt it."
