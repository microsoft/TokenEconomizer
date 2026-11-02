"""The Hermes plugin and what it hands the package.

The offline tests need no Hermes: they run `plugin/hermes/__init__.py` against a real SQLite file
with the columns it reads and, where a test says so, the real dispatcher in a process of its own.
They run in CI's `tests` job. The test at the end drives a real gateway (`TE_HERMES_E2E=1`,
`TE_HERMES_BIN`): an isolated `HERMES_HOME`, a local stand-in for the model.
"""
import importlib.util
import json
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tokeneconomizer import trace
from tokeneconomizer.plugin import hosts, sidecall

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_FILE = os.path.join(REPO, "plugin", "hermes", "__init__.py")

_spec = importlib.util.spec_from_file_location("te_hermes_plugin", PLUGIN_FILE)
plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin)

SCHEMA = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, created_source TEXT, cwd TEXT, model TEXT,
    parent_session_id TEXT, started_at REAL, ended_at REAL, end_reason TEXT, title TEXT,
    system_prompt TEXT, last_activity_description TEXT, a_later_column TEXT);
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT,
    content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL,
    message_uid TEXT, _compressed_summary INTEGER DEFAULT 0, active INTEGER DEFAULT 1,
    api_content TEXT);
CREATE TABLE session_model_usage (session_id TEXT, model TEXT, task TEXT, api_call_count INTEGER,
    input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
    cache_write_tokens INTEGER, reasoning_tokens INTEGER, estimated_cost_usd REAL);
"""


class Home:
    """A Hermes home holding a `state.db` with rows a test adds."""

    def __init__(self, path):
        self.path = str(path)
        os.makedirs(self.path, exist_ok=True)
        self.db = os.path.join(self.path, "state.db")
        with self._con() as con:
            con.executescript(SCHEMA)

    def _con(self):
        return sqlite3.connect(self.db)

    def session(self, sid, source="local", created="cli", cwd=None, parent=None, ended=None,
                reason=None, title=None):
        with self._con() as con:
            con.execute("INSERT INTO sessions (id, source, created_source, cwd, model, "
                        "parent_session_id, started_at, ended_at, end_reason, title) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (sid, source, created, cwd, "te-model", parent, time.time(), ended,
                         reason, title))

    def message(self, sid, role, content="", **kw):
        kw.setdefault("timestamp", time.time())
        cols = ", ".join(["session_id", "role", "content", *kw])
        with self._con() as con:
            con.execute(f"INSERT INTO messages ({cols}) VALUES ({','.join('?' * (3 + len(kw)))})",
                        (sid, role, content, *kw.values()))

    def usage(self, sid, task="", calls=1, fresh=400, cached=600, out=10, model="te-model"):
        with self._con() as con:
            con.execute("INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?,0,0,0)",
                        (sid, model, task, calls, fresh, out, cached))

    def gone(self, sid):
        with self._con() as con:
            con.execute("DELETE FROM session_model_usage WHERE session_id = ?", (sid,))
            con.execute("DELETE FROM sessions WHERE id = ?", (sid,))


@pytest.fixture
def home(tmp_path):
    return Home(tmp_path / "hermes")


def _call(name, args):
    return json.dumps([{"id": f"c-{name}", "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)}}])


class FakeDispatcher:
    """Stands in for `plugin.dispatch`; records calls, and answers from `answers[event]`."""

    def __init__(self):
        self.calls, self.answers, self.lock = [], {}, threading.Lock()

    def __call__(self, payload, cwd, mode, home):
        with self.lock:
            self.calls.append(dict(payload, _cwd=cwd, _mode=mode, _home=home))
        return self.answers.get(payload["hook_event_name"], {})

    def events(self):
        return [c["hook_event_name"] for c in self.calls]


@pytest.fixture
def bridge(home, tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENECONOMIZER_HOME", str(tmp_path / "te"))
    monkeypatch.delenv("TE_DISABLE", raising=False)
    monkeypatch.delenv("TE_SIDECALL", raising=False)
    fake = FakeDispatcher()
    b = plugin.Bridge(call=fake, home=lambda: home.path)
    b.fake = fake
    return b


def _redirect(text):
    return {"hookSpecificOutput": {"additionalContext": text}}


# ---- reading a tool call and its result -------------------------------------------------------
def test_tools_get_the_names_the_mechanisms_read():
    assert trace.hermes_tool("terminal", {"command": "ls"}) == ("bash", {"command": "ls"})
    assert trace.hermes_tool("patch", {"path": "a.py"})[0] == "edit"
    assert trace.hermes_tool("write_file", {"path": "a.py"})[0] == "write"
    assert trace.hermes_tool("read_file", {"path": "a.py"})[0] == "read"
    assert trace.hermes_tool("search_files", {"pattern": "x"})[0] == "grep"
    assert trace.hermes_tool("execute_code", {"code": "1"})[0] == "execute_code"
    # a deferred tool runs through the bridge; it is the tool that counts
    assert trace.hermes_tool("tool_call", {"name": "terminal", "arguments": {"command": "ls"}}) \
        == ("bash", {"command": "ls"})
    assert trace.hermes_tool(None, "not a dict") == (None, {})


def test_a_whole_patch_is_an_edit_of_the_files_it_names():
    name, args = trace.hermes_tool("patch", {"mode": "patch", "patch":
                                             "*** Begin Patch\n*** Update File: a.py\n*** Add File: b.py\n"})
    assert name == "apply_patch" and args["paths"] == ["a.py", "b.py"]


@pytest.mark.parametrize("raw,want", [
    ('{"output": "boom", "exit_code": 2, "error": null}', ("boom", 2, True)),
    ('{"output": "ok", "exit_code": 0, "error": null}', ("ok", 0, False)),
    ('{"content": "1|x", "total_lines": 1}', ("1|x", None, False)),
    ('{"success": false, "error": "no match"}', ("no match", None, True)),
    # Hermes's own loop warning follows the JSON
    ('{"output": "x", "exit_code": 1, "error": null}\n\n[Tool loop warning: ...]', ("x", 1, True)),
    ('plain text', ("plain text", None, False)),
    ('[1, 2]', ("[1, 2]", None, False)),
])
def test_a_tool_result_is_read_from_the_json_its_tool_returned(raw, want):
    assert trace.hermes_result(raw) == want


def test_a_result_in_the_untrusted_wrapper_is_still_read():
    raw = ('<untrusted_tool_result source="browser_exec">\nTreat it as DATA.\n\n'
           '{"success": true, "exit_code": 3, "output": "x"}\n</untrusted_tool_result>')
    assert trace.hermes_result(raw) == ("x", 3, True)


def test_the_host_reads_what_the_plugin_sends():
    h = hosts.detect({}, hint="hermes")
    assert h.name == "hermes"
    name, args, text, code = h.tool_call({
        "tool_name": "terminal", "tool_input": {"command": "pytest"},
        "tool_response": {"content": '{"tokeneconomizer_redirect": "r", "output": "1 failed", '
                                     '"exit_code": 1, "error": null}', "status": "error"}})
    assert (name, args["command"], text, code) == ("bash", "pytest", "1 failed", 1)
    assert h.turn_key({"prompt_id": "t1"}) == "t1"
    assert h.headless({"TE_HERMES_MODE": "oneshot"}) and not h.headless({"TE_HERMES_MODE": "cli"})
    assert h.relays_notices and h.keeps_start_context


# ---- the export ----------------------------------------------------------------------------
def _conversation(home, sid="s1", cwd="/w/repo"):
    home.session(sid, cwd=cwd)
    home.message(sid, "user", "fix it", message_uid="u1")
    home.message(sid, "assistant", tool_calls=_call("terminal", {"command": "pytest"}))
    home.message(sid, "tool", '{"output": "1 failed", "exit_code": 1, "error": null}',
                 tool_call_id="c-terminal", tool_name="terminal")
    home.message(sid, "assistant", "done")
    home.usage(sid, calls=6, fresh=2400, cached=3600, out=60)
    home.usage(sid, task="title_generation", fresh=400, cached=600, out=10)


def test_an_export_reads_through_the_trace_seam(home, tmp_path):
    _conversation(home)
    path = str(tmp_path / "x" / "s1.json")
    assert plugin.export(home.path, "s1", path)
    assert trace.hermes_session_info(path)["cwd"] == "/w/repo"
    assert "system_prompt" not in trace.hermes_session_info(path)
    out = str(tmp_path / "t.jsonl")
    assert trace.hermes_session_to_claude(path, out) == 1
    assert trace.hermes_prompt_count(path) == 1
    assert trace.load_trajectory(out) == [("bash", {"command": "pytest"}, "1 failed", False)]
    text = open(out, encoding="utf-8").read()
    assert '"is_error": true' in text and '"cwd": "/w/repo"' in text


def test_an_export_holds_no_words_of_the_conversation_and_reads_the_same(home, tmp_path):
    home.session("s", cwd="/w/repo", title="SECRET title")
    with home._con() as con:
        con.execute("UPDATE sessions SET last_activity_description = 'SECRET activity', "
                    "a_later_column = 'SECRET column', system_prompt = 'SECRET system'")
    home.message("s", "user", "SECRET prompt", message_uid="u1")
    home.message("s", "user", "SECRET summary", _compressed_summary=1)
    home.message("s", "assistant", "SECRET reply", tool_calls=_call("terminal", {"command": "ls"}),
                 api_content="SECRET api")
    home.message("s", "tool", '{"output": "kept", "exit_code": 1, "error": null}',
                 tool_call_id="c-terminal", tool_name="terminal")
    home.message("s", "user", "SECRET second", message_uid="u2")
    path = str(tmp_path / "s.json")
    assert plugin.export(home.path, "s", path)
    assert "SECRET" not in open(path, encoding="utf-8").read()
    assert trace.hermes_prompt_count(path) == 2
    out = str(tmp_path / "t.jsonl")
    assert trace.hermes_session_to_claude(path, out) == 1
    assert "SECRET" not in open(out, encoding="utf-8").read()
    assert trace.load_trajectory(out) == [("bash", {"command": "ls"}, "kept", False)]
    assert '"is_error": true' in open(out, encoding="utf-8").read()


def test_a_session_is_priced_with_its_title_and_its_subagents(home, tmp_path):
    _conversation(home)
    home.session("kid", source="subagent", created="subagent", parent="s1")
    home.usage("kid", fresh=100, cached=0, out=5)
    home.session("other", parent="s1", reason=None)     # a branch of it is another session
    home.usage("other", fresh=9999, cached=0, out=9999)
    path = str(tmp_path / "s1.json")
    plugin.export(home.path, "s1", path)
    u = trace.hermes_session_usage(path)
    # input excludes the cache, as Hermes stores it; the title call is the session's spending
    assert (u["input_tokens"], u["cache_read_input_tokens"], u["output_tokens"]) == (2900, 4200, 75)
    assert u["requests"] == 8 and u["model"] == "te-model"


def test_a_rotated_session_is_read_as_one_with_each_call_once(home, tmp_path):
    home.session("root", cwd="/w/repo", reason="compression", ended=1.0)
    home.message("root", "user", "fix it", message_uid="u1")
    home.message("root", "assistant", tool_calls=_call("terminal", {"command": "ls"}))
    home.message("root", "tool", '{"output": "a", "exit_code": 0}', tool_call_id="c-terminal")
    home.usage("root")
    home.session("tip", cwd="/w/repo", parent="root")
    home.message("tip", "user", "summary of what came before", _compressed_summary=1)
    home.message("tip", "user", "fix it", message_uid="u1")       # the tail it kept
    home.message("tip", "assistant", tool_calls=_call("terminal", {"command": "ls"}))
    home.message("tip", "tool", '{"output": "a", "exit_code": 0}', tool_call_id="c-terminal")
    home.message("tip", "user", "and again", message_uid="u2")
    home.usage("tip")
    path = str(tmp_path / "root.json")
    assert plugin.export(home.path, "tip", path)
    assert trace.hermes_session_info(path)["id"] == "tip"
    assert trace.hermes_prompt_count(path) == 2
    assert trace.hermes_session_to_claude(path, str(tmp_path / "t.jsonl")) == 1
    assert trace.hermes_session_usage(path)["requests"] == 2


def test_an_unreadable_export_is_no_session(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("not json", encoding="utf-8")
    assert trace.hermes_session_to_claude(str(path), str(tmp_path / "t")) is None
    assert trace.hermes_session_to_claude(str(tmp_path / "none"), str(tmp_path / "t")) is None
    assert trace.hermes_prompt_count(str(path)) == 0
    assert trace.hermes_session_usage(str(path))["input_tokens"] == 0


# ---- a redirect keeps the tool's result what it was --------------------------------------------
def test_a_redirect_goes_first_in_the_object_and_changes_nothing_else():
    raw = '{"output": "x", "n": 12345678901234567890, "exit_code": 2, "error": null}'
    got = plugin.with_redirect(raw, 'read "the" traceback')
    obj = json.loads(got)
    assert next(iter(obj)) == plugin.REDIRECT_FIELD and obj[plugin.REDIRECT_FIELD] == 'read "the" traceback'
    assert {k: v for k, v in obj.items() if k != plugin.REDIRECT_FIELD} == json.loads(raw)
    assert got.endswith(raw[1:])                                     # byte for byte
    assert trace.hermes_result(got) == ("x", 2, True)


def test_a_redirect_in_an_empty_object_and_around_plain_text():
    assert json.loads(plugin.with_redirect(" {} ", "r")) == {plugin.REDIRECT_FIELD: "r"}
    assert plugin.with_redirect("plain output", "r") == "plain output\n\nr"


def test_a_result_with_no_place_for_a_redirect_is_left_alone():
    assert plugin.with_redirect("[1, 2]", "r") is None
    assert plugin.with_redirect("3", "r") is None
    assert plugin.with_redirect(["parts"], "r") is None


# ---- which sessions are followed ---------------------------------------------------------------
@pytest.mark.parametrize("source,created", [("tool", "tool"), ("subagent", "subagent"),
                                            ("telegram", "telegram"), ("cron", "cron"),
                                            ("local", "tui"), ("local", "acp")])
def test_only_a_persons_classic_or_one_shot_session_is_followed(bridge, home, tmp_path, source,
                                                                created):
    home.session("x", source=source, created=created, cwd=str(tmp_path))
    assert bridge.section({"session_id": "x"}) == ""
    assert bridge.pre_llm_call(session_id="x", turn_id="t") is None
    assert bridge.transform_tool_result("terminal", {}, "{}", session_id="x") is None
    bridge.on_session_end(session_id="x", completed=True)
    assert bridge.on_session_finalize(session_id="x") is None
    assert bridge.fake.calls == []


def test_a_side_call_records_no_directory_and_is_not_followed_on_that_either(bridge, home):
    home.session("x", source="local", created="cli", cwd=None)
    assert bridge.section({"session_id": "x"}) == "" and bridge.fake.calls == []


def test_a_directory_not_on_this_machine_is_not_followed(bridge, home):
    home.session("x", cwd="/no/such/directory/here")
    assert bridge.section({"session_id": "x"}) == "" and bridge.fake.calls == []


def test_a_row_that_cannot_be_read_is_not_followed_yet_and_is_tried_again(bridge, home, tmp_path):
    path = tmp_path / "later"
    late = plugin.Bridge(call=bridge.fake, home=lambda: str(path))
    assert late.section({"session_id": "x"}) == "" and bridge.fake.calls == []
    later = Home(path)
    later.session("x", cwd=str(tmp_path))
    late.section({"session_id": "x"})
    assert bridge.fake.events() == ["SessionStart"]


def test_nothing_runs_when_disabled(bridge, home, tmp_path, monkeypatch):
    home.session("x", cwd=str(tmp_path))
    monkeypatch.setenv("TE_DISABLE", "1")
    assert bridge.section({"session_id": "x"}) == "" and bridge.fake.calls == []


# ---- the hooks ---------------------------------------------------------------------------------
def test_a_session_runs_every_event_from_its_recorded_directory(bridge, home, tmp_path):
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    _conversation(home, "s", cwd=repo)
    bridge.fake.answers = {"SessionStart": _redirect("the facts")}
    assert bridge.section({"session_id": "s", "cwd": "/somewhere/else"}) == "the facts"
    bridge.pre_llm_call(session_id="s", user_message="go", turn_id="t1")
    assert bridge.transform_tool_result("terminal", {"command": "ls"}, '{"output": ""}',
                                        session_id="s", turn_id="t1", tool_call_id="c1") is None
    bridge.on_session_end(session_id="s", turn_id="t1", completed=True)
    assert bridge.on_session_finalize(session_id="s", reason="cli_close") is None
    assert bridge.fake.events() == ["SessionStart", "UserPromptSubmit", "PostToolUse", "Stop",
                                    "SessionEnd"]
    assert {c["cwd"] for c in bridge.fake.calls} == {repo}
    assert {c["_cwd"] for c in bridge.fake.calls} == {repo}
    assert {c["_home"] for c in bridge.fake.calls} == {home.path}
    tool = bridge.fake.calls[2]
    assert (tool["prompt_id"], tool["tool_name"], tool["tool_use_id"]) == ("t1", "terminal", "c1")
    assert tool["tool_input"] == {"command": "ls"}
    assert os.path.exists(tool["transcript_path"])        # the export the fold will read


def test_a_resumed_session_starts_at_its_prompt_and_gets_no_facts_twice(bridge, home, tmp_path):
    home.session("s", cwd=str(tmp_path))
    bridge.fake.answers = {"SessionStart": _redirect("facts")}
    assert bridge.pre_llm_call(session_id="s", user_message="again", turn_id="t") is None
    assert bridge.fake.events() == ["SessionStart", "UserPromptSubmit"]
    assert bridge.fake.calls[0]["source"] == "resume"


def test_context_for_a_prompt_is_returned_for_that_prompt(bridge, home, tmp_path):
    home.session("s", cwd=str(tmp_path))
    bridge.section({"session_id": "s"})
    bridge.fake.answers = {"UserPromptSubmit": _redirect("the map")}
    parts = [{"type": "text", "text": "look"}, {"type": "image_url"}]
    assert bridge.pre_llm_call(session_id="s", user_message=parts, turn_id="t") == {"context": "the map"}
    assert bridge.fake.calls[-1]["prompt"] == "look\n"


@pytest.mark.parametrize("kw", [{"completed": False}, {"completed": True, "interrupted": True},
                                {"completed": True, "failed": True}])
def test_a_turn_cut_short_sends_no_stop(bridge, home, tmp_path, kw):
    home.session("s", cwd=str(tmp_path))
    bridge.section({"session_id": "s"})
    bridge.on_session_end(session_id="s", turn_id="t", **kw)
    assert "Stop" not in bridge.fake.events()


def test_a_one_shot_session_ends_with_its_turn_since_z_sends_no_finalize(bridge, home, tmp_path):
    home.session("s", source="oneshot", created="oneshot", cwd=str(tmp_path))
    bridge.section({"session_id": "s"})
    bridge.on_session_end(session_id="s", completed=True, turn_id="t")
    assert bridge.fake.events() == ["SessionStart", "Stop", "SessionEnd"]
    assert {c["_mode"] for c in bridge.fake.calls} == {"oneshot"}
    bridge.on_session_finalize(session_id="s", reason="cli_close")      # `chat --oneshot` also does
    assert bridge.fake.events().count("SessionEnd") == 1


def test_ended_one_shot_sessions_do_not_pile_up_in_a_long_lived_gateway(bridge, home, tmp_path,
                                                                        monkeypatch):
    """`hermes -z` sends no finalize, so nothing else would ever let go of its record."""
    monkeypatch.setattr(plugin, "MAX_ENDED", 3)
    for i in range(10):
        home.session(f"z{i}", source="oneshot", created="oneshot", cwd=str(tmp_path))
        bridge.section({"session_id": f"z{i}"})
        bridge.on_session_end(session_id=f"z{i}", completed=True)
    assert len(bridge.sessions) <= 4
    home.session("live", cwd=str(tmp_path))
    bridge.section({"session_id": "live"})
    for i in range(10, 16):
        home.session(f"z{i}", source="oneshot", created="oneshot", cwd=str(tmp_path))
        bridge.section({"session_id": f"z{i}"})
        bridge.on_session_end(session_id=f"z{i}", completed=True)
    assert "live" in bridge.sessions                   # a session still running is never dropped


def test_notices_are_shown_at_the_end_of_the_session(bridge, home, tmp_path):
    home.session("s", cwd=str(tmp_path))
    bridge.fake.answers = {"SessionStart": {"systemMessage": "added 2 facts"}}
    bridge.section({"session_id": "s"})
    assert bridge.on_session_finalize(session_id="s") == "added 2 facts"


def test_a_redirect_reaches_the_tool_result_and_its_status_survives(bridge, home, tmp_path):
    home.session("s", cwd=str(tmp_path))
    bridge.fake.answers = {"PostToolUse": _redirect("look again")}
    raw = '{"output": "boom", "exit_code": 1, "error": "bad"}'
    got = bridge.transform_tool_result("terminal", {}, raw, session_id="s")
    obj = json.loads(got)
    assert obj[plugin.REDIRECT_FIELD] == "look again" and obj["error"] == "bad"


def test_a_redirect_waits_for_a_result_outside_the_untrusted_wrapper(bridge, home, tmp_path):
    home.session("s", cwd=str(tmp_path))
    bridge.fake.answers = {"PostToolUse": _redirect("look again")}
    for tool in ("web_search", "web_extract", "browser_exec", "mcp__x__y", "tool_call"):
        assert bridge.transform_tool_result(tool, {}, '{"a": 1}', session_id="s") is None
    bridge.fake.answers = {}
    got = bridge.transform_tool_result("read_file", {}, '{"content": "x"}', session_id="s")
    assert json.loads(got)[plugin.REDIRECT_FIELD].startswith("look again")
    assert bridge.transform_tool_result("read_file", {}, '{"content": "x"}', session_id="s") is None


def test_a_held_redirect_is_dropped_with_its_turn(bridge, home, tmp_path):
    home.session("s", cwd=str(tmp_path))
    bridge.section({"session_id": "s"})
    bridge.fake.answers = {"PostToolUse": _redirect("look again")}
    bridge.transform_tool_result("web_search", {}, '{"a": 1}', session_id="s")
    bridge.fake.answers = {}
    bridge.pre_llm_call(session_id="s", turn_id="t2")
    assert bridge.transform_tool_result("read_file", {}, "{}", session_id="s") is None


def test_a_compression_that_changes_the_id_is_the_same_session(bridge, home, tmp_path):
    home.session("root", cwd=str(tmp_path))
    bridge.section({"session_id": "root"})
    with home._con() as con:
        con.execute("UPDATE sessions SET end_reason = 'compression' WHERE id = 'root'")
    home.session("tip", cwd=str(tmp_path), parent="root")
    bridge.transform_tool_result("terminal", {}, "{}", session_id="tip")
    assert [c["session_id"] for c in bridge.fake.calls] == ["root", "root"]


def test_a_child_that_is_no_compression_is_its_own_session(bridge, home, tmp_path):
    home.session("root", cwd=str(tmp_path))
    home.session("branch", cwd=str(tmp_path), parent="root")
    bridge.section({"session_id": "branch"})
    assert bridge.fake.calls[0]["session_id"] == "branch"


def test_sessions_in_two_repositories_at_once_keep_their_own_directories(bridge, home, tmp_path):
    dirs = {}
    for name in ("a", "b"):
        dirs[name] = str(tmp_path / name)
        os.makedirs(dirs[name])
        home.session(name, cwd=dirs[name])
    barrier = threading.Barrier(2)

    def turn(sid):
        barrier.wait()
        bridge.section({"session_id": sid})
        for i in range(25):
            bridge.transform_tool_result("terminal", {}, "{}", session_id=sid, tool_call_id=f"{i}")
    threads = [threading.Thread(target=turn, args=(s,)) for s in dirs]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(bridge.fake.calls) == 52
    for c in bridge.fake.calls:
        assert c["cwd"] == c["_cwd"] == dirs[c["session_id"]]


def test_a_sessions_parallel_tool_calls_reach_the_guard_one_at_a_time(home, tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENECONOMIZER_HOME", str(tmp_path / "te"))
    home.session("s", cwd=str(tmp_path))
    inside, most = [], []

    def call(payload, cwd, mode, h):
        inside.append(1)
        most.append(len(inside))
        time.sleep(0.01)
        inside.pop()
        return {}
    b = plugin.Bridge(call=call, home=lambda: home.path)
    threads = [threading.Thread(target=b.transform_tool_result,
                                args=("terminal", {}, "{}"), kwargs={"session_id": "s"})
               for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(most) == 6 and max(most) == 1


def test_a_stuck_call_cannot_hold_a_hook_past_its_lock_timeout(bridge, home, tmp_path, monkeypatch):
    monkeypatch.setattr(plugin, "LOCK_TIMEOUT_S", 0.05)
    home.session("s", cwd=str(tmp_path))
    rec = bridge.session("s")
    rec["lock"].acquire()
    try:
        assert bridge.transform_tool_result("terminal", {}, "{}", session_id="s") is None
        assert bridge.pre_llm_call(session_id="s", turn_id="t") is None
        bridge.on_session_end(session_id="s", completed=True)
    finally:
        rec["lock"].release()
    assert bridge.fake.calls == []


def test_the_limits_stay_inside_hermes_own_hook_timeout():
    # One overrun of `plugins.hook_callback_timeout` (30s) silences a callback for 60s, everywhere.
    assert plugin.HOOK_TIMEOUT_S + plugin.LOCK_TIMEOUT_S < 30


# ---- the dispatcher itself ---------------------------------------------------------------------
def test_a_dispatcher_that_fails_changes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin, "BOOTSTRAP", str(tmp_path / "none.py"))
    assert plugin.dispatch({"hook_event_name": "Stop"}, str(tmp_path), "cli", str(tmp_path)) == {}
    assert plugin.dispatch({}, str(tmp_path / "no-such-dir"), "cli", str(tmp_path)) == {}


def test_a_dispatcher_that_hangs_is_abandoned(tmp_path, monkeypatch):
    hang = tmp_path / "hang.py"
    hang.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    monkeypatch.setattr(plugin, "BOOTSTRAP", str(hang))
    monkeypatch.setattr(plugin, "HOOK_TIMEOUT_S", 0.5)
    t0 = time.time()
    assert plugin.dispatch({}, str(tmp_path), "cli", str(tmp_path)) == {}
    assert time.time() - t0 < 10


def _repo(path):
    os.makedirs(path)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return str(path)


def test_two_repositories_fold_apart_through_the_real_dispatcher(home, tmp_path, monkeypatch):
    """The whole path: the plugin, a process per event, the session's repository resolved from its
    row, the export, and the fold pricing each session from it."""
    root = tmp_path / "te"
    monkeypatch.setenv("TOKENECONOMIZER_HOME", str(root))
    monkeypatch.setenv("TE_FOLD_SYNC", "1")
    monkeypatch.setenv("TE_CORE_PATH", REPO)
    for var in ("TE_DISABLE", "TE_SIDECALL"):
        monkeypatch.delenv(var, raising=False)
    b = plugin.Bridge(home=lambda: home.path)
    repos = {}
    for name in ("alpha", "beta"):
        repos[name] = _repo(tmp_path / name)
        _conversation(home, name, cwd=repos[name])
        b.section({"session_id": name})

    def turn(sid):
        b.pre_llm_call(session_id=sid, user_message="fix it", turn_id=f"{sid}-t")
        b.transform_tool_result("terminal", {"command": "pytest"},
                                '{"output": "1 failed", "exit_code": 1, "error": null}',
                                session_id=sid, turn_id=f"{sid}-t", tool_call_id=f"{sid}-1")
        b.on_session_end(session_id=sid, turn_id=f"{sid}-t", completed=True)
        b.on_session_finalize(session_id=sid, reason="cli_close")
    threads = [threading.Thread(target=turn, args=(s,)) for s in repos]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not (root / "errors.log").exists() or not (root / "errors.log").read_text("utf-8").strip()
    ledgers = sorted(root.rglob("ledger.jsonl"))
    records = [json.loads(ln) for p in ledgers for ln in p.read_text("utf-8").splitlines()]
    sessions = [r for r in records if r.get("kind") == "session"]
    assert sorted(r["session_id"] for r in sessions) == ["alpha", "beta"]
    assert len(ledgers) == 2                                 # one state directory per repository
    assert {(r["repo"], r["session_id"]) for r in sessions} == {("alpha", "alpha"), ("beta", "beta")}
    for r in sessions:
        assert r["input_tokens"] == 2800 and r["requests"] == 7 and not r.get("error")


# ---- the plugin's own model calls --------------------------------------------------------------
class FakeCli:
    """`subprocess.run` for the `hermes` commands a side call runs, on a real `state.db`."""

    def __init__(self, home, sid="side", reply="an answer", rc=0, title=True, timeout=False,
                 delete_ok=True):
        self.home, self.sid, self.reply, self.rc = home, sid, reply, rc
        self.title, self.timeout, self.delete_ok = title, timeout, delete_ok
        self.argvs, self.envs, self.log = [], [], []

    def __call__(self, argv, **kw):
        self.argvs.append(argv)
        self.envs.append(kw.get("env") or {})
        if argv[1:3] == ["sessions", "delete"]:
            rows = sqlite3.connect(self.home.db).execute(
                "SELECT COUNT(*) FROM session_model_usage WHERE session_id = ?",
                (argv[-1],)).fetchone()[0]
            self.log.append(("delete", argv[-1], rows))
            if self.delete_ok:
                self.home.gone(argv[-1])
            return subprocess.CompletedProcess(argv, 0, "", "")
        prompt = open(argv[argv.index("--query-file") + 1], encoding="utf-8").read()
        self.home.session(self.sid, source="tool", created="tool",
                          ended=None if self.timeout else time.time())
        self.home.message(self.sid, "user", prompt)
        self.home.usage(self.sid)
        if self.title:
            self.home.usage(self.sid, task="title_generation", fresh=100, cached=0, out=5)
        if self.timeout:
            raise subprocess.TimeoutExpired(argv, 1)
        return subprocess.CompletedProcess(argv, self.rc, self.reply,
                                           f"note\n\nsession_id: {self.sid}\n")


@pytest.fixture
def side(home, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", home.path)
    monkeypatch.setenv("TOKENECONOMIZER_HOME", str(tmp_path / "te"))
    monkeypatch.setattr(sidecall, "HERMES_TITLE_WAIT_S", 0.0)


def _eff(*rows):
    return sum(sidecall.accounting.effective(dict(
        input_tokens=f, cache_read_input_tokens=c, cache_creation_input_tokens=0,
        output_tokens=o, model="te-model"))[0] for f, c, o in rows)


@pytest.mark.real_sidecalls
def test_a_side_call_offers_no_way_to_act_and_uses_the_accepted_source(home, side, monkeypatch):
    cli = FakeCli(home)
    monkeypatch.setattr(sidecall.subprocess, "run", cli)
    assert sidecall.hermes_call("is it stuck?", model="p/m", cli="hermes")[0] == "an answer"
    argv = cli.argvs[0]
    assert "-z" not in argv and "--oneshot" in argv and "--ignore-rules" in argv
    assert argv[argv.index("--source") + 1] == "tool"
    assert argv[argv.index("-t") + 1] == "bot_room" and argv[argv.index("--max-turns") + 1] == "1"
    assert argv[argv.index("--model") + 1] == "p/m"
    assert "is it stuck?" not in argv                 # by file: a digest outgrows a command line
    assert sidecall.HERMES_SOURCE not in plugin.USER_SOURCES


@pytest.mark.real_sidecalls
def test_a_side_call_cannot_switch_off_a_gateway_it_starts(home, side, monkeypatch):
    """With no gateway running, the call starts one, which keeps the environment it was given."""
    monkeypatch.setenv("TE_SIDECALL", "1")
    monkeypatch.setenv("TE_HERMES_MODE", "cli")
    monkeypatch.setenv("PYTHONPATH", "/core")
    cli = FakeCli(home)
    monkeypatch.setattr(sidecall.subprocess, "run", cli)
    sidecall.hermes_call("q", cli="hermes")
    for env in cli.envs:
        assert not {"TE_SIDECALL", "TE_HERMES_MODE", "PYTHONPATH"} & set(env)
        assert env["HERMES_HOME"] == home.path


@pytest.mark.real_sidecalls
def test_all_a_side_call_spent_is_read_before_its_session_is_deleted(home, side, monkeypatch):
    cli = FakeCli(home)
    monkeypatch.setattr(sidecall.subprocess, "run", cli)
    text, eff = sidecall.hermes_call("q", cli="hermes")
    assert cli.log == [("delete", "side", 2)]          # both rows still there when it went
    assert eff == pytest.approx(_eff((400, 600, 10), (100, 0, 5)))
    assert sidecall.trace.hermes_session_row(home.db, "side")[0] is None


@pytest.mark.real_sidecalls
def test_a_title_on_another_model_is_priced_on_that_model(home, side, monkeypatch):
    class TwoModels(FakeCli):
        def __call__(self, argv, **kw):
            done = super().__call__(argv, **kw)
            if "--query-file" in argv:
                self.home.usage(self.sid, task="compression", model="cheap", fresh=7, cached=0, out=1)
            return done
    monkeypatch.setattr(sidecall.subprocess, "run", TwoModels(home))
    seen = []
    real = sidecall.accounting.effective
    monkeypatch.setattr(sidecall.accounting, "effective",
                        lambda u: (seen.append(u["model"]), real(u))[1])
    sidecall.hermes_call("q", cli="hermes")
    assert sorted(seen) == ["cheap", "te-model", "te-model"]


@pytest.mark.real_sidecalls
def test_a_timed_out_side_call_keeps_its_session_until_a_later_call_settles_it(home, side,
                                                                               monkeypatch):
    late = FakeCli(home, sid="late", timeout=True)
    monkeypatch.setattr(sidecall.subprocess, "run", late)
    text, eff = sidecall.hermes_call("first", cli="hermes", timeout=1)
    assert (text, eff) == ("", None)
    assert late.log == [] and sidecall.trace.hermes_session_row(home.db, "late")[0]
    with home._con() as con:
        con.execute("UPDATE sessions SET ended_at = 1 WHERE id = 'late'")   # it did finish
    next_call = FakeCli(home, sid="next")
    monkeypatch.setattr(sidecall.subprocess, "run", next_call)
    text, eff = sidecall.hermes_call("second", cli="hermes")
    assert text == "an answer"
    assert ("delete", "late", 2) in next_call.log
    assert eff == pytest.approx(2 * _eff((400, 600, 10), (100, 0, 5)))
    assert sidecall.hermes_call("third", cli="hermes") is not None


@pytest.mark.real_sidecalls
def test_a_failed_delete_is_retried_and_charged_once(home, side, monkeypatch):
    stuck = FakeCli(home, sid="stuck", delete_ok=False)
    monkeypatch.setattr(sidecall.subprocess, "run", stuck)
    _, first = sidecall.hermes_call("q", cli="hermes")
    assert first == pytest.approx(_eff((400, 600, 10), (100, 0, 5)))
    assert sidecall.trace.hermes_session_row(home.db, "stuck")[0]
    next_call = FakeCli(home, sid="next")
    monkeypatch.setattr(sidecall.subprocess, "run", next_call)
    _, second = sidecall.hermes_call("q2", cli="hermes")
    assert second == pytest.approx(first)              # `next`'s spending, not `stuck`'s again
    assert sidecall.trace.hermes_session_row(home.db, "stuck")[0] is None


@pytest.mark.real_sidecalls
def test_hermes_saying_it_could_not_start_is_no_reply(home, side, monkeypatch):
    cli = FakeCli(home, reply="Hermes couldn't start the model connection", rc=1)
    monkeypatch.setattr(sidecall.subprocess, "run", cli)
    assert sidecall.hermes_call("q", cli="hermes")[0] == ""


def test_a_notice_names_the_command_a_side_call_runs():
    assert sidecall.call_name("hermes") == "hermes chat"


def test_the_host_runs_its_side_calls_through_hermes(monkeypatch):
    cfg = type("C", (), {"paid_model": "m", "hermes_cli": "~/bin/hermes"})()
    seen = {}
    monkeypatch.setattr(sidecall, "hermes_call", lambda p, **k: seen.update(k) or ("", None))
    sidecall.runner_for("hermes", cfg)("q")
    assert seen == {"model": "m", "cli": os.path.expanduser("~/bin/hermes")}


# ---- the package ---------------------------------------------------------------------------
class Ctx:
    def __init__(self):
        self.hooks, self.sections = {}, {}

    def register_hook(self, name, cb):
        self.hooks[name] = cb

    def register_system_prompt_section(self, sid, cb, position="", max_chars=0):
        self.sections[sid] = (cb, position, max_chars)


def test_the_plugin_registers_what_its_manifest_declares():
    text = open(os.path.join(REPO, "plugin", "hermes", "plugin.yaml"), encoding="utf-8").read()
    ctx = Ctx()
    plugin.register(ctx)
    assert set(re.findall(r"^  - (\w+)$", text, re.M)) == set(ctx.hooks)
    assert re.search(r"^name: tokeneconomizer$", text, re.M)
    with open(os.path.join(REPO, "plugin", ".claude-plugin", "plugin.json"), encoding="utf-8") as fh:
        version = json.load(fh)["version"]
    assert f"version: {version}\n" in text
    # Hermes's limits (hooks.md): 1-128 lowercase id characters; at most 4,000 per section.
    (_, position, max_chars), = ctx.sections.values()
    assert re.fullmatch(r"[a-z0-9._-]{1,128}", plugin.SECTION_ID)
    assert position == "after_memory" and max_chars == 4000


def test_the_plugin_finds_its_launcher_through_a_link(tmp_path):
    """`~/.hermes/plugins/tokeneconomizer` is a symlink to `plugin/hermes`."""
    link = tmp_path / "tokeneconomizer"
    try:
        os.symlink(os.path.dirname(PLUGIN_FILE), link)
    except OSError as e:
        pytest.skip(f"this system makes no symlinks: {e}")
    code = ("import importlib.util as u, os; s = u.spec_from_file_location('p', %r); "
            "m = u.module_from_spec(s); s.loader.exec_module(m); print(os.path.isfile(m.BOOTSTRAP))"
            % str(link / "__init__.py"))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, encoding="utf-8")
    assert out.stdout.strip() == "True", out.stderr


def test_a_hermes_session_is_folded_after_the_transcript_cwd_the_repository_reads(home, tmp_path):
    """`repos` learns where a session started from the first `cwd` in its log."""
    from tokeneconomizer.plugin import repos
    _conversation(home, "s", cwd="/w/repo")
    path = str(tmp_path / "s.json")
    plugin.export(home.path, "s", path)
    assert repos.session_cwd({"transcript": path}) == "/w/repo"


# ---- a real gateway ----------------------------------------------------------------------------
HERMES = os.environ.get("TE_HERMES_BIN")
e2e = pytest.mark.skipif(os.environ.get("TE_HERMES_E2E") != "1" or not HERMES,
                         reason="drives a real Hermes: set TE_HERMES_E2E=1 and TE_HERMES_BIN")


class _Chat(BaseHTTPRequestHandler):
    """An OpenAI-compatible endpoint. A prompt holding `FAIL n` gets n failing `terminal` calls;
    a call with no tools (a side call, a title, a compression) gets text. Every request is kept."""
    requests = []
    pace = 0
    filled = False

    def log_message(self, *args):
        pass

    def _send(self, kind, body):
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send("application/json", json.dumps({"object": "list", "data": [
            {"id": "te-model", "object": "model", "context_length": 200000}]}).encode())

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        _Chat.requests.append(req)
        msgs = req.get("messages") or []
        last = max((i for i, m in enumerate(msgs) if m.get("role") == "user"), default=0)
        prompt = str(msgs[last].get("content") if msgs else "")
        done = sum(1 for m in msgs[last:] if m.get("role") == "tool")
        want = re.search(r"FAIL (\d+)", prompt)
        call = bool(req.get("tools") and want and done < int(want.group(1)))
        if call:
            time.sleep(_Chat.pace)
        delta = {"role": "assistant"}
        # By its own words: a side call has no tools either, and its system prompt says "summar".
        if not req.get("tools") and "summarization agent" in json.dumps(msgs[:2]).lower():
            delta["content"] = "## Goal\nThe work so far: the same command failed twelve times."
        elif call:
            delta["tool_calls"] = [{
                "index": 0, "id": f"call_{time.time_ns()}", "type": "function",
                "function": {"name": "terminal", "arguments": json.dumps(
                    {"command": "echo AssertionError: boom; false"})}}]
        else:
            delta["content"] = ('{"stuck": true, "confidence": 0.9, "text": "Read the traceback.", '
                                '"reason": "same failure"}' if "stuck" in prompt else "done")

        def chunk(choices, **extra):
            return "data: " + json.dumps(dict(id="c", object="chat.completion.chunk", created=0,
                                              model="te-model", choices=choices, **extra)) + "\n\n"
        # `FILL` reports a nearly full context, which makes Hermes compress before its next call.
        fill = 95000 if "FILL" in prompt and done >= 9 and not _Chat.filled else 1000
        _Chat.filled = _Chat.filled or fill > 1000
        usage = {"prompt_tokens": fill, "completion_tokens": 10, "total_tokens": fill + 10,
                 "prompt_tokens_details": {"cached_tokens": 600}}
        if not req.get("stream"):
            self._send("application/json", json.dumps({
                "id": "c", "object": "chat.completion", "created": 0, "model": "te-model",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": delta.get("content")}}],
                "usage": usage}).encode())
            return
        self._send("text/event-stream", (
            chunk([{"index": 0, "delta": delta, "finish_reason": None}])
            + chunk([{"index": 0, "delta": {}, "finish_reason": "tool_calls" if call else "stop"}])
            + chunk([], usage=usage) + "data: [DONE]\n\n").encode())


@pytest.fixture
def world(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Chat)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _Chat.requests, _Chat.filled = [], False
    home = tmp_path / "hermes-home"
    (home / "plugins").mkdir(parents=True)
    os.symlink(os.path.dirname(PLUGIN_FILE), home / "plugins" / "tokeneconomizer")
    (home / "config.yaml").write_text(
        "model:\n  default: te-model\n  provider: custom\n"
        f"  base_url: http://127.0.0.1:{server.server_address[1]}/v1\n  api_key: none\n"
        "  context_length: 100000\n"
        "plugins:\n  enabled: [tokeneconomizer]\nterminal:\n  backend: local\n  cwd: \".\"\n",
        encoding="utf-8")
    te = tmp_path / "te"
    te.mkdir()
    env = dict(os.environ, HERMES_HOME=str(home), TOKENECONOMIZER_HOME=str(te), TE_CORE_PATH=REPO,
               TE_MICROSKILLS="envfacts", TE_TOOLPRUNE="off",
               TE_HERMES_CLI=HERMES, HERMES_RUNTIME_DIR=str(tmp_path / "rt"), NO_COLOR="1",
               TE_LOOPGUARD_THRESHOLDS=json.dumps({"min_turns": 2, "gate_min_turns": 2}))
    for var in ("TE_DISABLE", "TE_SIDECALL", "HERMES_INFERENCE_MODEL"):
        env.pop(var, None)

    def run(*args, cwd, timeout=120):
        return subprocess.run([HERMES, *args], capture_output=True, encoding="utf-8",
                              errors="replace", cwd=cwd, env=env, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    yield type("World", (), {"run": staticmethod(run), "home": home, "te": te, "env": env,
                             "tmp": tmp_path})
    pid = home / "gateway.pid"
    try:
        os.kill(json.loads(pid.read_text(encoding="utf-8"))["pid"], 15)
    except (OSError, ValueError, KeyError):
        pass
    server.shutdown()
    server.server_close()


def _wait(check, seconds=60):
    end = time.time() + seconds
    while time.time() < end:
        got = check()
        if got:
            return got
        time.sleep(0.5)
    return check()


def _sessions(world):
    records = []
    for p in world.te.rglob("ledger.jsonl"):
        records += [json.loads(ln) for ln in p.read_text("utf-8").splitlines()]
    return [r for r in records if r.get("kind") == "session"]


def _rows(world):
    con = sqlite3.connect(pathlib.Path(world.home, "state.db").as_uri() + "?mode=ro", uri=True)
    try:
        return con.execute("SELECT source, created_source, cwd FROM sessions").fetchall()
    finally:
        con.close()


@e2e
def test_concurrent_repositories_through_a_real_gateway(world):
    repos = [_repo(world.tmp / n) for n in ("alpha", "beta")]
    world.run("chat", "-Q", "--oneshot", "-q", "warm the gateway", cwd=repos[0])
    with_threads = []
    for r in repos:
        t = threading.Thread(target=world.run, args=("-z", "FAIL 3 please"), kwargs={"cwd": r})
        t.start()
        with_threads.append(t)
    [t.join() for t in with_threads]
    done = _wait(lambda: len(_sessions(world)) >= 3 and _sessions(world))
    assert done, (list(world.te.rglob("*.log")), _rows(world))
    assert {os.path.basename(r["repo"]) for r in done} == {"alpha", "beta"}
    assert all(r["requests"] and not r.get("error") for r in done)
    errors = world.te / "errors.log"
    assert not errors.exists() or "Traceback" not in errors.read_text("utf-8")


@e2e
def test_nothing_a_real_session_was_told_is_left_in_the_plugins_state(world):
    repo = _repo(world.tmp / "repo")
    out = world.run("-z", "FAIL 2 SECRET-WORDS", cwd=repo)
    assert out.returncode == 0, (out.stdout[-500:], out.stderr[-500:])
    (done,) = _wait(lambda: _sessions(world))
    assert done["prompts"] == 1 and done["requests"] and not done.get("error")
    held = [p for p in world.te.rglob("*") if p.is_file() and b"SECRET-WORDS" in p.read_bytes()]
    assert not held
    (export,) = world.te.rglob("hermes-sessions/*.json")
    assert '"exit_code": 1' in export.read_text("utf-8").replace('\\"', '"')    # what is learned from


@e2e
def test_facts_reach_the_system_prompt_of_a_real_session(world):
    repo = _repo(world.tmp / "repo")
    world.run("-z", "warm up", cwd=repo)
    (state,) = _wait(lambda: [p.parent for p in world.te.rglob("ledger.jsonl")])
    (state / "injection.json").write_text(json.dumps({
        "rendered": "<tokeneconomizer>\n- FACT-MARKER\n</tokeneconomizer>"}), encoding="utf-8")
    _Chat.requests = []
    world.run("-z", "second", cwd=repo)
    sent = [json.dumps(r["messages"][0]) for r in _Chat.requests if r.get("tools")]
    assert sent and "FACT-MARKER" in sent[0]


@e2e
def test_a_failure_loop_gets_a_structured_redirect_from_a_real_side_call(world):
    """Gateway -> hook -> detached judge -> `hermes chat` -> the same gateway, with the side call's
    session filtered out by the plugin, charged, and deleted, while the turn it judges runs on."""
    repo = _repo(world.tmp / "repo")
    world.run("-z", "warm up", cwd=repo)
    (state,) = _wait(lambda: [p.parent for p in world.te.rglob("ledger.jsonl")])
    (state / "envelope.json").write_text(json.dumps({"resolved_turns": [4] * 8}), encoding="utf-8")
    _Chat.requests = []
    _Chat.pace = 1.5
    out = world.run("-z", "FAIL 30 please", cwd=repo, timeout=170)
    _Chat.pace = 0
    assert out.returncode == 0, (out.stdout[-500:], out.stderr[-500:])
    results = [m.get("content") for r in _Chat.requests for m in r.get("messages") or []
               if m.get("role") == "tool"]
    hits = [c for c in results if plugin.REDIRECT_FIELD in str(c)]
    assert hits, (results[-1:], [str(p) for p in state.iterdir()])
    obj, _ = json.JSONDecoder().raw_decode(hits[0])
    assert next(iter(obj)) == plugin.REDIRECT_FIELD and obj["exit_code"] == 1
    # The judge's sessions ran in this gateway, were not followed, and are gone.
    assert _wait(lambda: not [r for r in _rows(world) if r[1] == "tool"], 30), _rows(world)
    assert {r["repo"] for r in _sessions(world)} == {"repo"}
    ledger = (state / "ledger.jsonl").read_text("utf-8").splitlines()
    assert [json.loads(ln) for ln in ledger if '"overhead"' in ln and '"kind": "overhead"' in ln]
    assert not os.path.exists(world.te / "errors.log") or "Traceback" not in (
        world.te / "errors.log").read_text("utf-8")


@e2e
def test_a_real_side_call_offers_the_model_no_tool(world):
    """`bot_room` holds no tool by Hermes's choice, not by contract: a later Hermes that puts one
    there gives the model a way to spend the call's only turn, as `clarify` did."""
    repo = _repo(world.tmp / "repo")
    world.run("-z", "warm up", cwd=repo)
    _Chat.requests = []
    query = world.tmp / "query.txt"
    query.write_text("write the facts", encoding="utf-8")
    out = world.run(*sidecall.HERMES_FLAGS, "--query-file", str(query), cwd=str(world.tmp))
    assert out.returncode == 0 and out.stdout.strip() == "done", (out.stdout, out.stderr[-500:])
    assert _Chat.requests and not any(r.get("tools") for r in _Chat.requests)


@e2e
def test_compression_in_a_turn_stays_one_session(world):
    repo = _repo(world.tmp / "repo")
    out = world.run("chat", "-Q", "--oneshot", "-q", "FILL FAIL 12 please", cwd=repo, timeout=150)
    assert out.returncode == 0, (out.stdout[-500:], out.stderr[-500:])
    con = sqlite3.connect(pathlib.Path(world.home, "state.db").as_uri() + "?mode=ro", uri=True)
    rows = con.execute("SELECT id, parent_session_id, end_reason, created_source FROM sessions "
                       "ORDER BY started_at").fetchall()
    usage = con.execute("SELECT task FROM session_model_usage").fetchall()
    con.close()
    print("compression rows:", rows, usage)
    assert ("compression",) in usage, (rows, usage)        # it did compress
    done = _wait(lambda: _sessions(world))
    assert len(done) == 1 and done[0]["session_id"] == rows[0][0], (done, rows)
    assert done[0]["prompts"] == 1 and not done[0].get("error")
    (state,) = [p.parent for p in world.te.rglob("ledger.jsonl")]
    assert len(os.listdir(state / "sessions")) == 1        # the guard saw one session throughout
