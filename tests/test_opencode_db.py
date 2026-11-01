"""Reading opencode's own database back into the export its trace reader already knows."""
import json
import os
import sqlite3

import pytest

from tokeneconomizer import trace

SESSION = "ses_aaa"
DIR = "C:/work/proj"


def _db(path):
    """A file-shaped opencode 2.x database: the two tables this reader touches."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        create table session_v2 (id text primary key, directory text, parent_id text,
            time_created integer, time_updated integer, tokens_input integer,
            tokens_output integer, tokens_reasoning integer, tokens_cache_read integer,
            tokens_cache_write integer, cost real);
        create table session_message (id text, session_id text, type text, seq integer,
            data text);
    """)
    return conn


def _session(conn, sid=SESSION, directory=DIR, parent=None, updated=1_000_000,
             messages=3, tokens=(100, 40, 5, 900, 20), cost=0.25):
    conn.execute("insert into session_v2 values (?,?,?,?,?,?,?,?,?,?,?)",
                 (sid, directory, parent, updated - 60_000, updated, *tokens, cost))
    for i in range(messages):
        conn.execute("insert into session_message values (?,?,?,?,?)",
                     (f"{sid}-m{i}", sid, "user" if i == 0 else "assistant", i,
                      json.dumps({"text": f"prompt {i}", "files": []}
                                 if i == 0 else {"content": [{"type": "text",
                                                              "text": f"reply {i}"}]})))


@pytest.fixture()
def db(tmp_path):
    path = str(tmp_path / "opencode.db")
    conn = _db(path)
    _session(conn)
    # a subagent's session and a session that never ran a message: neither teaches anything
    _session(conn, sid="ses_sub", parent="ses_parent", messages=2)
    _session(conn, sid="ses_empty", messages=0)
    conn.commit()
    conn.close()
    return path


def test_connect_refuses_to_read_a_foreign_database(tmp_path):
    path = str(tmp_path / "other.db")
    sqlite3.connect(path).close()

    assert trace.opencode_db.has_schema(trace.opencode_db.connect(path)) is False


def test_sessions_skip_subagents_empty_sessions_and_throwaway_directories(db):
    def add(**kwargs):
        """Write through a connection of our own: what `connect` hands out is read-only."""
        writer = sqlite3.connect(db)
        try:
            _session(writer, **kwargs)
            writer.commit()
        finally:
            writer.close()

    conn = trace.opencode_db.connect(db)
    try:
        got = trace.opencode_db.sessions(conn, 0)
        assert [s["id"] for s in got] == [SESSION]
        assert got[0]["directory"] == DIR                     # normalized, forward slashes
        assert trace.opencode_db.directories(conn, 0) == [DIR]

        add(sid="ses_side", directory="C:/Temp/te-side-abc", messages=1)
        assert [s["id"] for s in trace.opencode_db.sessions(conn, 0)] == [SESSION]
        # excluded (still running) and outside the window: both drop out
        assert trace.opencode_db.sessions(conn, 0, exclude=(SESSION,)) == []
        assert trace.opencode_db.sessions(conn, 2_000_000) == []
        # a subagent is read only when asked for
        assert "ses_sub" in [s["id"] for s in trace.opencode_db.sessions(
            conn, 0, include_subagents=True)]
    finally:
        conn.close()


def test_document_rebuilds_the_export_the_trace_reader_reads(db):
    writer = sqlite3.connect(db)
    try:                                          # the messages of one session, rewritten
        writer.execute("delete from session_message where session_id = ?", (SESSION,))
        writer.executemany(
            "insert into session_message values (?,?,?,?,?)",
            [(f"{SESSION}-u", SESSION, "user", 1, json.dumps({"text": "fix the bug"})),
             (f"{SESSION}-a1", SESSION, "assistant", 2, json.dumps({
                 "model": {"id": "deepseek-v4.1", "providerID": "opencode"},
                 "content": [{"type": "reasoning", "text": "thinking"},
                             {"type": "text", "text": "looking"},
                             {"type": "tool", "id": "call-1", "name": "grep",
                              "state": {"status": "completed", "input": {"pattern": "x"},
                                        "content": [{"type": "text", "text": "match"}]}},
                             {"type": "tool", "id": "call-2", "name": "bash",
                              "state": {"status": "error", "input": {"command": "x"},
                                        "error": "boom"}}]})),
             (f"{SESSION}-a2", SESSION, "assistant", 3, json.dumps({
                 "content": [{"type": "text", "text": "done"}]}))])
        writer.commit()
    finally:
        writer.close()

    conn = trace.opencode_db.connect(db)
    try:
        session = trace.opencode_db.sessions(conn, 0)[0]
        doc = trace.opencode_db.document(conn, session)
        assert [m["info"]["role"] for m in doc["messages"]] == ["user", "assistant", "assistant"]
        first = doc["messages"][1]["parts"]
        assert [p["type"] for p in first] == ["text", "tool", "tool"]
        assert "thinking" not in json.dumps(doc)               # reasoning dropped: nothing reads it
        assert first[1] == {"type": "tool", "callID": "call-1", "tool": "grep",
                            "state": {"status": "completed", "input": {"pattern": "x"},
                                      "output": "match"}}
        assert first[2]["state"] == {"status": "error", "input": {"command": "x"},
                                     "error": "boom"}
        # the session's own totals ride on the first assistant message, once
        assert doc["messages"][1]["info"]["tokens"]["input"] == 100
        assert doc["messages"][1]["info"]["cost"] == 0.25
        assert "tokens" not in doc["messages"][2]["info"]
        assert doc["messages"][1]["info"]["modelID"] == "deepseek-v4.1"
        assert doc["messages"][1]["info"]["path"]["cwd"] == DIR
    finally:
        conn.close()


def test_a_written_export_feeds_the_opencode_readers(db, tmp_path):
    conn = trace.opencode_db.connect(db)
    try:
        session = trace.opencode_db.sessions(conn, 0)[0]
        path = str(tmp_path / "export.json")
        assert trace.opencode_db.write_export(conn, session, path) == 3

        assert trace.opencode_prompt_count(path) == 1                 # one user message with text
        usage = trace.opencode_session_usage(path)
        assert usage["input_tokens"] == 100 and usage["output_tokens"] == 40 + 5
        assert usage["cache_read_input_tokens"] == 900
        # opencode totals tokens per session, so one message carries them: one request
        assert usage["requests"] == 1 and usage["model"] is None       # no model named
        assert trace.opencode_session_info(path)["directory"] == DIR
    finally:
        conn.close()


def test_a_session_whose_only_message_is_empty_writes_no_export(tmp_path):
    """opencode's own read tool leaves assistant rows with no `content`: a row is not a message."""
    path = str(tmp_path / "opencode.db")
    conn = _db(path)
    _session(conn, messages=0)
    conn.execute("insert into session_message values ('m', ?, 'assistant', 1, ?)",
                 (SESSION, json.dumps({"content": []})))
    conn.commit()
    try:
        assert [s["id"] for s in trace.opencode_db.sessions(conn, 0)] == [SESSION]
        session = {"id": SESSION, "directory": DIR}
        assert trace.opencode_db.document(conn, session) is None
        assert trace.opencode_db.write_export(conn, session, str(tmp_path / "x.json")) is None
        assert not os.path.exists(str(tmp_path / "x.json"))
    finally:
        conn.close()
