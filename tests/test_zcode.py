"""ZCode (Z.ai): the session reader over its SQLite store, its host adapter and host detection,
side calls, and the hook entry points end to end.

ZCode keeps sessions in `<ZCODE_STORAGE_DIR or ~/.zcode>/cli/db/db.sqlite` (tables `session`,
`message`, `part`, `tool_usage`, `model_usage`). The database here is synthetic: the schema is the
columns the reader names, and the JSON of `message.data` and `part.data` has the keys read off a
real ZCode 0.17.1 store (keys only; no conversation was copied). The words in it are markers that
must never reach an export.

Proves TC-980-1 (TASK-980): the suite passes on a ZCode session fixture.
Proves TC-980-2 (TASK-980): an unknown payload is not taken for ZCode (see
`test_an_unknown_payload_is_not_taken_for_zcode`); Kimi's half is in `test_kimi.py`.
"""
import io
import json
import os
import sqlite3
import subprocess
import time

import pytest

from tokeneconomizer import trace
from tokeneconomizer.plugin import accounting, denylist, fold, hook, hosts
from tokeneconomizer.plugin.context import LiveContext

SCHEMA = """
create table session (id text primary key, project_id text not null default 'p',
    parent_id text, slug text not null default 's', directory text not null,
    title text not null default 'MARKER-TITLE', version text not null default '0.17.1',
    time_created integer not null default 0, time_updated integer not null default 0);
create table message (id text primary key, session_id text not null,
    time_created integer not null, time_updated integer not null default 0,
    data text not null, sequence integer);
create table part (id text primary key, message_id text not null, session_id text not null,
    time_created integer not null, time_updated integer not null default 0,
    data text not null, sequence integer);
create table tool_usage (id text primary key, session_id text not null, turn_id text,
    tool_call_id text not null, tool_name text not null, status text not null,
    started_at integer not null default 0, exit_code integer);
create table model_usage (id text primary key, logical_request_id text not null default '',
    session_id text not null, turn_id text, query_source text not null,
    provider_id text not null default 'zai', model_id text not null,
    status text not null default 'completed', started_at integer not null default 0,
    input_tokens integer not null default 0, output_tokens integer not null default 0,
    reasoning_tokens integer not null default 0,
    cache_creation_input_tokens integer not null default 0,
    cache_read_input_tokens integer not null default 0);
"""

FAIL = "python: can't open file 'tests/runtests.py': [Errno 2] No such file or directory"
OK = "Ran 3 tests in 0.01s\nOK"
# (tool, input, output, status, exit_code)
TEST_CALLS = [("Bash", {"command": "python tests/runtests.py"}, FAIL, "error", 2),
              ("Bash", {"command": "python runtests.py"}, OK, "completed", 0)]


class Db:
    """A synthetic ZCode store being written; every row gets a rising time and id."""

    def __init__(self, path):
        self.path = str(path)
        self.con = sqlite3.connect(self.path)
        self.con.executescript(SCHEMA)
        self.n = 0

    def _id(self, prefix):
        self.n += 1
        return f"{prefix}_{self.n:05d}"

    def session(self, sid, cwd, parent=None):
        self.con.execute("insert into session (id, directory, parent_id) values (?, ?, ?)",
                         (sid, str(cwd), parent))

    def message(self, sid, data, parts):
        mid = self._id("msg")
        self.con.execute("insert into message (id, session_id, time_created, data) "
                         "values (?, ?, ?, ?)", (mid, sid, self.n, json.dumps(data)))
        for p in parts:
            self.con.execute("insert into part (id, message_id, session_id, time_created, data) "
                             "values (?, ?, ?, ?, ?)",
                             (self._id("prt"), mid, sid, self.n, json.dumps(p)))

    def user(self, sid, text, **extra):
        self.message(sid, dict({"role": "user", "agent": "main", "time": {"created": self.n},
                                "model": {"providerID": "zai", "modelID": "glm-5.1"}}, **extra),
                     [{"type": "text", "text": text}])

    def assistant(self, sid, cwd, calls=(), text="MARKER-ANSWER"):
        parts = [{"type": "step-start"}, {"type": "reasoning", "text": "MARKER-REASONING"},
                 {"type": "text", "text": text}]
        for i, (tool, inp, out, status, code) in enumerate(calls):
            call_id = f"call_{self.n}_{i}"
            state = {"status": status, "input": inp, "metadata": {}, "time": {}}
            state["error" if status == "error" else "output"] = out
            parts.append({"type": "tool", "callID": call_id, "tool": tool, "state": state})
            self.con.execute("insert into tool_usage (id, session_id, tool_call_id, tool_name, "
                             "status, exit_code) values (?, ?, ?, ?, ?, ?)",
                             (self._id("tu"), sid, call_id, tool, status, code))
        parts.append({"type": "step-finish", "reason": "stop"})
        self.message(sid, {"role": "assistant", "modelID": "glm-5.1", "providerID": "zai",
                           "path": {"cwd": str(cwd), "root": str(cwd)}}, parts)

    def usage(self, sid, source="main_turn", model="glm-5.1", i=1000, o=100, cache_read=0):
        self.con.execute(
            "insert into model_usage (id, session_id, query_source, model_id, input_tokens, "
            "output_tokens, cache_read_input_tokens) values (?, ?, ?, ?, ?, ?, ?)",
            (self._id("mu"), sid, source, model, i, o, cache_read))

    def close(self):
        self.con.commit()
        self.con.close()
        return self.path


def make_db(path, cwd, sid="sess_1", calls=TEST_CALLS):
    """One session: a real prompt, two tool calls, machine messages around them, a subagent."""
    db = Db(path)
    db.session(sid, cwd)
    db.user(sid, "MARKER-PROMPT run the tests")
    db.assistant(sid, cwd, calls)
    # Messages ZCode writes itself: not prompts.
    db.user(sid, "MARKER-REMINDER", source="todo_reminder")
    db.user(sid, "MARKER-SYNTH", synthetic=True)
    db.session("sess_sub", cwd, parent=sid)
    db.user("sess_sub", "MARKER-SUBPROMPT")
    db.assistant("sess_sub", cwd, [("Bash", {"command": "ls"}, "a", "completed", 0)])
    db.usage(sid, i=5000, o=400, cache_read=4000)
    db.usage(sid, source="session_title", i=300, o=20)
    db.usage("sess_sub", source="subagent", i=700, o=50, cache_read=500)
    return db.close()


def export(tmp_path, **kw):
    db = make_db(tmp_path / "db.sqlite", tmp_path, **kw)
    out = str(tmp_path / "export.json")
    assert trace.zcode_export(db, kw.get("sid", "sess_1"), out) is True
    return db, out


# ---- the reader -----------------------------------------------------------------------------
def test_export_keeps_the_calls_and_their_exit_codes_and_none_of_the_words(tmp_path):
    _db, out = export(tmp_path)
    text = open(out, encoding="utf-8").read()
    for word in ("MARKER-PROMPT", "MARKER-ANSWER", "MARKER-REASONING", "MARKER-TITLE",
                 "MARKER-REMINDER", "MARKER-SYNTH", "MARKER-SUBPROMPT"):
        assert word not in text
    assert trace.zcode_prompt_count(out) == 1
    assert trace.zcode_session_info(out)["directory"] == str(tmp_path)


def test_a_session_reads_as_claude_lines_with_exit_codes_deciding_failure(tmp_path):
    _db, out = export(tmp_path)
    claude = str(tmp_path / "claude.jsonl")
    assert trace.zcode_session_to_claude(out, claude) == 2      # the subagent's call is not here
    calls = trace.load_trajectory(claude)
    assert [c[0] for c in calls] == ["Bash", "Bash"]
    assert calls[0][1]["command"] == "python tests/runtests.py"
    assert FAIL in calls[0][2] and calls[0][3] is False         # (name, args, result, ok)
    assert calls[1][3] is True
    assert trace.call_cwds(claude) == [str(tmp_path), str(tmp_path)]


def test_a_non_zero_exit_is_a_failure_though_the_tool_finished(tmp_path):
    calls = [("Bash", {"command": "pytest"}, "1 failed", "completed", 1)]
    _db, out = export(tmp_path, calls=calls)
    claude = str(tmp_path / "claude.jsonl")
    trace.zcode_session_to_claude(out, claude)
    assert trace.load_trajectory(claude)[0][3] is False


def test_prompts_are_the_typed_ones_only(tmp_path):
    """ZCode writes reminders, goal continuations and background-task notices as user messages;
    each is marked by `source` or `synthetic`, and none is a prompt."""
    _db, out = export(tmp_path)
    assert trace.zcode_prompt_count(out) == 1
    db = Db(tmp_path / "many.sqlite")
    db.session("s", tmp_path)
    for src in ("todo_reminder", "background_task", "goal-continuation", "goal_state_change",
                "agent_listing_delta"):
        db.user("s", "x", source=src)
    db.user("s", "one")
    db.user("s", "two")
    db.user("s", "   ")                                         # no words, no prompt
    path = db.close()
    assert trace.zcode_export(path, "s", str(tmp_path / "e.json"))
    assert trace.zcode_prompt_count(str(tmp_path / "e.json")) == 2


def test_usage_excludes_the_cache_from_input_and_adds_the_subagent(tmp_path):
    """ZCode's `input_tokens` counts the cached reads (its total is input + output), the ledger's
    does not."""
    _db, out = export(tmp_path)
    u = trace.zcode_session_usage(out)
    assert u["input_tokens"] == (5000 - 4000) + 300 + (700 - 500)
    assert u["cache_read_input_tokens"] == 4000 + 500
    assert u["output_tokens"] == 400 + 20 + 50
    assert u["requests"] == 3 and u["model"] == "glm-5.1"
    eff, priced_by = accounting.effective(u)
    assert eff and priced_by in ("model", "fallback")


def test_the_subagents_own_session_is_not_folded_into_the_parents_calls(tmp_path):
    _db, out = export(tmp_path)
    assert "sess_sub" not in open(out, encoding="utf-8").read()


def test_the_store_is_opened_read_only(tmp_path):
    db = make_db(tmp_path / "db.sqlite", tmp_path)
    before = (os.path.getsize(db), os.stat(db).st_mtime_ns)
    assert trace.zcode_export(db, "sess_1", str(tmp_path / "e.json"))
    assert (os.path.getsize(db), os.stat(db).st_mtime_ns) == before
    assert not os.path.exists(db + "-wal") or os.path.getsize(db + "-wal") == 0


def test_the_store_location_follows_zcode_storage_dir(tmp_path):
    assert trace.zcode_db({"ZCODE_STORAGE_DIR": str(tmp_path / "z")}) == str(
        tmp_path / "z" / "cli" / "db" / "db.sqlite")
    assert trace.zcode_db({}) == os.path.join(os.path.expanduser("~"), ".zcode", "cli", "db",
                                              "db.sqlite")


def test_what_cannot_be_read_is_no_session_and_creates_nothing(tmp_path):
    out = str(tmp_path / "e.json")
    assert trace.zcode_export(str(tmp_path / "missing.sqlite"), "sess_1", out) is False
    assert not os.path.exists(str(tmp_path / "missing.sqlite")) and not os.path.exists(out)
    db = make_db(tmp_path / "db.sqlite", tmp_path)
    assert trace.zcode_export(db, "sess_nope", out) is False
    junk = tmp_path / "junk.sqlite"
    junk.write_bytes(b"this is not a database")
    assert trace.zcode_export(str(junk), "sess_1", out) is False
    # A store of another shape (a later ZCode that renamed a table) is no session either.
    other = tmp_path / "other.sqlite"
    con = sqlite3.connect(other)
    con.execute("create table session (id text)")
    con.execute("insert into session values ('sess_1')")
    con.commit()
    con.close()
    assert trace.zcode_export(str(other), "sess_1", out) is False
    assert not os.path.exists(out)
    assert trace.zcode_session_to_claude(str(tmp_path / "nope.json"), str(tmp_path / "c")) is None
    assert trace.zcode_prompt_count(str(tmp_path / "nope.json")) == 0
    assert trace.zcode_session_usage(str(tmp_path / "nope.json"))["input_tokens"] == 0


@pytest.mark.parametrize("sid", ["", "../x", "a b", "sess_1; drop table session", None])
def test_a_session_id_that_is_no_id_is_refused(tmp_path, sid):
    db = make_db(tmp_path / "db.sqlite", tmp_path)
    assert trace.zcode_export(db, sid, str(tmp_path / "e.json")) is False


def test_an_apply_patch_call_is_an_edit_of_the_files_it_names():
    patch = "*** Begin Patch\n*** Update File: src/a.py\n@@\n-x\n+y\n*** End Patch"
    assert trace.zcode_tool("ApplyPatch", {"input": patch}) == (
        "apply_patch", {"path": "src/a.py", "paths": ["src/a.py"]})
    assert trace.zcode_tool("Bash", {"command": "ls"}) == ("Bash", {"command": "ls"})
    assert trace.zcode_tool("Bash", None) == ("Bash", {})


# ---- detection: what is ZCode, and what is not ------------------------------------------------
def zcode_payload(event="PostToolUse", sid="sess_1", tmp="/var/folders/ab/T"):
    """The payload ZCode 0.17.1 builds (`createCompatibleHookStdin` in its bundle): its own
    camelCase fields, the snake_case aliases of them, and a throwaway transcript."""
    tp = f"{tmp}/zcode-hook-Ab12Cd/transcript.jsonl"
    p = {"hookEventName": event, "sessionId": sid, "cwd": "/repo", "mode": "yolo",
         "agentName": "main", "traceId": "t", "turnId": "u",
         "agent_type": "main", "hook_event_name": event, "permission_mode": "yolo",
         "session_id": sid, "transcript_path": tp, "transcriptPath": tp}
    if event in ("PostToolUse", "PostToolUseFailure"):
        p.update(toolName="Bash", toolInput={"command": "ls"}, toolCallId="c1",
                 tool_name="Bash", tool_input={"command": "ls"}, tool_use_id="c1")
    if event == "PostToolUse":
        p.update(toolResponse={"output": "a\n", "exit_code": 0},
                 tool_response={"output": "a\n", "exit_code": 0})
    return p


def test_detect_tells_a_zcode_hook_from_claude_code_and_from_grok():
    """Grok also sends `hookEventName`, so a ZCode payload read as Grok's before this host
    existed; the throwaway transcript is what only ZCode sends."""
    for event in ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"):
        assert isinstance(hosts.detect(zcode_payload(event), {}), hosts.ZCode), event
    p = zcode_payload()
    p["transcript_path"] = p["transcriptPath"] = p["transcript_path"].replace("/", "\\")
    assert isinstance(hosts.detect(p, {}), hosts.ZCode)         # a Windows temp directory


def test_an_explicit_host_hint_wins_without_a_marker():
    assert isinstance(hosts.detect({}, {}, hint="zcode"), hosts.ZCode)
    assert isinstance(hosts.detect({"session_id": "s"}, {}, hint="zcode"), hosts.ZCode)


CLAUDE = {"session_id": "s", "hook_event_name": "PostToolUse", "cwd": "/repo",
          "transcript_path": "/home/me/.claude/projects/-repo/s.jsonl", "tool_name": "Bash"}


@pytest.mark.parametrize("payload, env", [
    # TC-980-2: nothing but a ZCode hook's own throwaway transcript makes a payload ZCode's.
    (CLAUDE, {}),
    (CLAUDE, {"ZCODE_SESSION_ID": "sess_1", "ZCODE_PROJECT_DIR": "/repo"}),   # started in ZCode
    ({}, {"ZCODE_SESSION_ID": "sess_1"}),
    ({}, {}),
    ({"hookEventName": "PostToolUse", "sessionId": "s", "transcriptPath": "/home/me/.grok/"
      "sessions/x/s.jsonl"}, {}),                                              # Grok's
    ({"hookEventName": "PostToolUse", "sessionId": "s"}, {}),                  # camelCase only
    (dict(zcode_payload(), transcriptPath="/tmp/zcode-hook-Zz9/transcript.jsonl"), {}),   # mismatch
    (dict(zcode_payload(), transcript_path="/tmp/zcode-hook-Ab12Cd/other.jsonl",
          transcriptPath="/tmp/zcode-hook-Ab12Cd/other.jsonl"), {}),           # another file
    (dict(zcode_payload(), transcript_path="/tmp/my-zcode-hook-1/transcript.jsonl",
          transcriptPath="/tmp/my-zcode-hook-1/transcript.jsonl"), {}),        # not that folder
    (dict(zcode_payload(), transcript_path="/tmp/zcode-hook-/transcript.jsonl",
          transcriptPath="/tmp/zcode-hook-/transcript.jsonl"), {}),            # empty suffix
    (dict(zcode_payload(), transcript_path=None, transcriptPath=None), {}),
    (dict(zcode_payload(), transcript_path=7, transcriptPath=7), {}),
    ({"transcript_path": "/home/me/.kimi-code/sessions/wd_x/session_1/agents/main/wire.jsonl"},
     {}),
])
def test_an_unknown_payload_is_not_taken_for_zcode(payload, env):
    assert not isinstance(hosts.detect(payload, env), hosts.ZCode)


@pytest.mark.parametrize("hint", ["kimi", "zcode2", "ZCODE", "", None, "../zcode"])
def test_an_unknown_host_hint_is_not_zcode(hint):
    assert not isinstance(hosts.detect(CLAUDE, {}, hint=hint), hosts.ZCode)


def test_detect_does_not_fail_on_an_empty_payload():
    for bad in (None, [], {}):
        assert not isinstance(hosts.detect(bad, {}), hosts.ZCode)


def test_a_zcode_shell_is_about_zcode_unless_another_agent_runs_in_it(monkeypatch):
    for k in ("TE_HOST", "CLAUDECODE", "COPILOT_CLI", "CURSOR_AGENT", "CURSOR_PROJECT_DIR",
              "GROK_SESSION_ID", "FACTORY_SESSION_ID", "DROID_PLUGIN_ROOT"):
        monkeypatch.delenv(k, raising=False)
    assert hosts.default_host({"ZCODE_SESSION_ID": "sess_1"}) == "zcode"
    assert hosts.default_host({"ZCODE_SESSION_ID": "s", "CLAUDECODE": "1"}) == "claude"
    assert hosts.default_host({"ZCODE_SESSION_ID": "s", "GROK_SESSION_ID": "g"}) == "grok"
    assert hosts.default_host({"ZCODE_SESSION_ID": "s", "TE_HOST": "codex"}) == "codex"
    assert hosts.default_host({}) == "claude"


# ---- the host ---------------------------------------------------------------------------------
def test_the_host_reads_the_payload_zcode_sends():
    host = hosts.ZCode()
    p = zcode_payload()
    assert host.name == "zcode" and host.session_id(p) == "sess_1"
    assert host.session_id({"sessionId": "sess_2"}) == "sess_2"
    assert host.turn_key(p) == "u"
    name, args, text, code = host.tool_call(p)
    assert (name, args["command"], text.strip(), code) == ("Bash", "ls", "a", 0)
    # ZCode's failure hook sends `error` (a message) and no response.
    fail = dict(zcode_payload("PostToolUseFailure"), error="Exit code 2\nboom")
    name, _args, text, code = host.tool_call(fail)
    assert name == "Bash" and "boom" in text and code == 2


def test_the_throwaway_transcript_is_never_the_sessions_log():
    """ZCode writes one line to a temp file and deletes it when the hook returns: recording its
    path would send the fold to a file that is gone."""
    host = hosts.ZCode()
    assert host.transcript(zcode_payload()) == ""
    assert host.mechanism_trace("", "/tmp") is None
    assert host.prompt_count("") is None or host.prompt_count("") == 0


def test_the_project_dir_is_the_payload_cwd_then_zcodes_variable():
    host = hosts.ZCode()
    assert host.cwd({"cwd": "/a"}, {"ZCODE_PROJECT_DIR": "/b"}) == "/a"
    assert host.cwd({}, {"ZCODE_PROJECT_DIR": "/b"}) == "/b"
    assert host.env_session_id({"ZCODE_SESSION_ID": "sess_9"}) == "sess_9"
    assert host.env_session_id({"TE_SESSION_ID": "t", "ZCODE_SESSION_ID": "z"}) == "t"


def test_the_host_answers_in_the_shape_zcode_parses():
    """`hookSpecificOutput.additionalContext` with the event's own name is what ZCode's schema
    takes; a name that differs from the event's is an error there. Hook messages are not shown
    to the person, so a notice goes through the model."""
    host = hosts.ZCode()
    assert host.prompt_context is True and host.relays_notices is True
    assert host.context("SessionStart", "facts") == {
        "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "facts"}}
    assert host.notice("hello") == {}
    assert "continue" not in host.context("Stop", "x")          # `continue: false` would block


def test_complete_exports_the_session_from_the_store_into_the_state(tmp_path, monkeypatch):
    db = make_db(tmp_path / "db.sqlite", tmp_path)
    monkeypatch.setenv("TOKENECONOMIZER_HOME", str(tmp_path / "state"))
    host = hosts.ZCode()
    path = host.complete("", "sess_1", str(tmp_path), {"zcode_db": db})
    assert path and path.startswith(str(tmp_path / "state"))
    assert trace.zcode_prompt_count(path) == 1
    out = host.mechanism_trace(path, str(tmp_path))
    assert trace.load_trajectory(out)[0][0] == "Bash"
    assert host.prompt_count(path) == 1
    assert host.session_usage(path)["output_tokens"] == 470
    # The fold reads the file it just wrote; the host's own log is the store, and its
    # activity is what the plugin saved at each hook.
    assert host.last_active("", {"updated": 12.5}) == 12.5


def test_complete_waits_for_a_store_that_is_not_there_yet_then_gives_up(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENECONOMIZER_HOME", str(tmp_path / "state"))
    host = hosts.ZCode()
    now = time.time()
    with pytest.raises(hosts.FoldLater):
        host.complete("", "sess_1", str(tmp_path), {"zcode_db": str(tmp_path / "no.sqlite"),
                                                    "updated": now})
    # A session nobody has touched for a week is folded as unreadable, not retried for ever.
    assert host.complete("", "sess_1", str(tmp_path), {
        "zcode_db": str(tmp_path / "no.sqlite"), "updated": now - host.FOLD_RETRY_S - 5}) == ""
    # A session id that is no id never reaches a query.
    assert host.complete("", "../x", str(tmp_path), {"zcode_db": "x"}) == ""


def test_session_state_records_where_the_store_is():
    assert hosts.ZCode().session_state({"ZCODE_STORAGE_DIR": "/z"}) == {
        "zcode_db": "/z/cli/db/db.sqlite"}


def test_tool_pruning_is_not_supported_for_zcode():
    assert denylist.supports("zcode") is False


# ---- side calls -------------------------------------------------------------------------------
def test_side_calls_are_off_unless_the_person_enables_them(tmp_path, monkeypatch):
    from tokeneconomizer.plugin import sidecall
    from tokeneconomizer.plugin.config import load_config, write_config
    assert sidecall.zcode_enabled({}) is False
    assert sidecall.zcode_enabled({"TE_ZCODE_SIDECALL": "1"}) is True
    monkeypatch.delenv("TE_ZCODE_SIDECALL", raising=False)
    cfg = load_config(str(tmp_path), env={})
    assert cfg.zcode_sidecall is False and sidecall.runner_for("zcode", cfg) is None
    monkeypatch.setenv("TE_ZCODE_SIDECALL", "1")
    assert callable(sidecall.runner_for("zcode", cfg))
    monkeypatch.delenv("TE_ZCODE_SIDECALL")
    write_config(str(tmp_path / "config.json"), {"zcode_sidecall": "on"})
    assert callable(sidecall.runner_for("zcode", load_config(str(tmp_path), env={})))


def test_the_setting_is_shown_by_config_show():
    from tokeneconomizer.plugin.cli import _config_view
    from tokeneconomizer.plugin.config import LiveConfig
    assert _config_view(LiveConfig())["zcode"] == {"state": "off", "zcode_sidecall": False}


@pytest.mark.real_sidecalls
def test_a_zcode_side_call_runs_in_plan_mode_in_an_empty_directory_with_tools_removed(
        monkeypatch):
    from tokeneconomizer.plugin import sidecall
    monkeypatch.setattr(sidecall, "program", lambda cli: [cli])
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(cmd=cmd, **kw)
        return subprocess.CompletedProcess(cmd, 0, stdout="- use python -m pytest\n", stderr="")

    monkeypatch.setattr(sidecall.subprocess, "run", fake_run)
    text, eff = sidecall.zcode_call("propose facts")
    assert text == "- use python -m pytest" and eff is None       # ZCode reports no usage here
    cmd = seen["cmd"]
    assert cmd[:3] == ["zcode", "-p", "propose facts"]
    assert cmd[cmd.index("--mode") + 1] == "plan"                  # its `-p` default is `yolo`
    removed = cmd[cmd.index("--disallowed-tools") + 1].replace(",", " ").split()
    assert {"Bash", "Edit", "Write", "WebFetch", "WebSearch", "Agent"} <= set(removed)
    assert "--cwd" in cmd and cmd[cmd.index("--cwd") + 1] != os.getcwd()
    assert seen["env"]["TE_SIDECALL"] == "1" and seen["stdin"] == subprocess.DEVNULL
    assert seen["cwd"] == cmd[cmd.index("--cwd") + 1]


@pytest.mark.real_sidecalls
def test_a_zcode_side_call_that_answers_nothing_is_noted(monkeypatch, tmp_path):
    from tokeneconomizer.plugin import sidecall
    monkeypatch.setenv("TOKENECONOMIZER_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(sidecall, "program", lambda cli: [cli])
    monkeypatch.setattr(sidecall.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 1, stdout="", stderr="Please run /login"))
    assert sidecall.zcode_call("p") == ("", None)
    assert sidecall.paused("zcode") is not None


# ---- the hooks, end to end ----------------------------------------------------------------------
@pytest.fixture
def state(tmp_path):
    return {"TOKENECONOMIZER_HOME": str(tmp_path / "home"), "TE_FOLD_SYNC": "1",
            "TE_MICROSKILLS": "envfacts", "TE_TOOLPRUNE": "off",
            "GIT_CEILING_DIRECTORIES": str(tmp_path), "PATH": os.environ.get("PATH", "")}


def run_hook(env, payload, *args):
    out = io.StringIO()
    hook.main(list(args), io.StringIO(json.dumps(payload)), out, env)
    return json.loads(out.getvalue().splitlines()[-1])


def test_a_zcode_session_is_followed_and_folded_from_the_store(tmp_path, state):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[project]\nname='w'\n")
    store = make_db(tmp_path / "db.sqlite", repo)
    env = dict(state, ZCODE_STORAGE_DIR=str(tmp_path / "zstore"))
    os.makedirs(tmp_path / "zstore" / "cli" / "db")
    os.replace(store, tmp_path / "zstore" / "cli" / "db" / "db.sqlite")
    for event in ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"):
        p = dict(zcode_payload(event), cwd=str(repo))
        out = run_hook(env, p, "--host", "zcode")
        assert isinstance(out, dict)
        assert "continue" not in out                              # a hook never stops ZCode
    ctx = LiveContext.build(hosts.ZCode(), str(repo), env)
    assert ctx.store.read("sessions/sess_1.json")["host"] == "zcode"
    assert not ctx.store.read("sessions/sess_1.json").get("transcript")
    rec = fold.fold_session(ctx, "sess_1")
    assert rec["host"] == "zcode" and rec["prompts"] == 1
    assert rec["effective_tokens"] and "error" not in rec
    # Folded once: a second fold finds nothing to do.
    assert fold.fold_session(ctx, "sess_1") is None


def test_a_payload_without_a_marker_and_without_a_hint_is_followed_as_claude_not_zcode(
        tmp_path, state):
    """What a Claude-shaped payload does with this plugin is unchanged by the new host."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    out = run_hook(state, dict(CLAUDE, cwd=str(repo), hook_event_name="SessionStart"))
    assert isinstance(out, dict)
    ctx = LiveContext.build(hosts.ClaudeCode(), str(repo), state)
    assert ctx.store.read("sessions/s.json")["host"] == "claude"
