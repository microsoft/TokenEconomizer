"""`te import`: the sessions opencode already recorded, folded into a repository's state."""
import json
import os
import sqlite3

import pytest

from tokeneconomizer.plugin import backfill, hosts
from tokeneconomizer.plugin.context import LiveContext
from tokeneconomizer import trace

PROMPT = "fix the failing test"
TOOL_OUTPUT = "1 passed"


def _call(call_id, command, output):
    """An assistant message that ran one shell command, with what it printed."""
    return json.dumps({"model": {"id": "deepseek-v4.1-flash"}, "content": [
        {"type": "tool", "id": call_id, "name": "bash",
         "state": {"status": "completed", "input": {"command": command},
                   "content": [{"type": "text", "text": output}]}}]})


def _db(path, directory, sid="ses_old"):
    conn = sqlite3.connect(path)
    conn.executescript("""
        create table session_v2 (id text primary key, directory text, parent_id text,
            time_created integer, time_updated integer, tokens_input integer,
            tokens_output integer, tokens_reasoning integer, tokens_cache_read integer,
            tokens_cache_write integer, cost real);
        create table session_message (id text, session_id text, type text, seq integer,
            data text);
    """)
    now = int(__import__("time").time() * 1000)
    conn.execute("insert into session_v2 values (?,?,?,?,?,?,?,?,?,?,?)",
                 (sid, directory, None, now - 900_000, now - 600_000, 1000, 50, 0, 0, 0, 0.1))
    conn.executemany("insert into session_message values (?,?,?,?,?)", [
        (sid + "-u", sid, "user", 1, json.dumps({"text": PROMPT})),
        (sid + "-a", sid, "assistant", 2, json.dumps({
            "model": {"id": "deepseek-v4.1-flash"},
            "content": [{"type": "tool", "id": "call-1", "name": "bash",
                         "state": {"status": "completed", "input": {"command": "pytest"},
                                   "content": [{"type": "text", "text": TOOL_OUTPUT}]}}]})),
    ])
    conn.commit()
    conn.close()
    return sid


@pytest.fixture()
def project(tmp_path, monkeypatch):
    """A directory that is no git repository, and a state root of its own."""
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    proj = str(tmp_path / "proj")
    os.makedirs(proj)
    env = {"TOKENECONOMIZER_HOME": str(tmp_path / "state"),
           # the replay learns without model calls whatever the repository is configured with
           "TE_MICROSKILLS": "envfacts+distiller"}
    db = str(tmp_path / "opencode.db")
    sid = _db(db, proj)
    env["TE_OPENCODE_DB"] = db
    return {"proj": proj, "env": env, "db": db, "sid": sid}


def _store(project):
    ctx = LiveContext.build(hosts.HOSTS["opencode"](), project["proj"], project["env"])
    return ctx.store


def test_a_recorded_session_is_folded_into_the_projects_state(project):
    summary = backfill.run(directories=[project["proj"]], env=project["env"],
                           out=lambda _line: None)

    assert summary["results"][0]["folded"] == 1
    store = _store(project)
    assert project["sid"] in (store.read("folded.json") or {})
    record = store.read(os.path.join("sessions", f"{project['sid']}.json"))
    assert record["source"] == "import" and record["cwd"] == project["proj"]
    assert record["ended"] > record["started"]

    ledger = store.read_lines("ledger.jsonl")
    assert [r["host"] for r in ledger] == ["opencode"]
    assert ledger[0]["prompts"] == 1 and ledger[0]["session_id"] == project["sid"]
    # the replay learned without the paid proposer, though the repository asked for it
    assert (store.read("injection.json") or {})["profile"] == "envfacts"


def test_a_replay_stands_for_a_while_and_force_repeats_it(project):
    backfill.run(directories=[project["proj"]], env=project["env"], out=lambda _line: None)

    again = backfill.run(directories=[project["proj"]], env=project["env"],
                         out=lambda _line: None)
    assert again["results"][0]["skipped"] == "replayed recently"

    forced = backfill.run(directories=[project["proj"]], env=project["env"], force=True,
                          out=lambda _line: None)
    assert forced["results"][0]["folded"] == 0        # folded once: `folded.json` holds it
    assert forced["results"][0]["already"] == 1


def test_dry_run_counts_without_writing(project):
    summary = backfill.run(directories=[project["proj"]], env=project["env"], dry_run=True,
                           out=lambda _line: None)

    assert summary["results"][0] == {"directory": project["proj"], "folded": 1, "already": 0,
                                     "errors": 0, "dry_run": True}
    store = _store(project)
    assert store.read("folded.json") is None and store.read("backfill.json") is None


def test_a_running_session_is_left_alone(project):
    conn = sqlite3.connect(project["db"])
    conn.execute("update session_v2 set time_updated = ?",
                 (int(__import__("time").time() * 1000),))
    conn.commit()
    conn.close()

    summary = backfill.run(directories=[project["proj"]], env=project["env"],
                           out=lambda _line: None)
    assert summary["results"][0]["folded"] == 0
    assert _store(project).read("folded.json") is None


def test_a_missing_or_foreign_database_imports_nothing(project, tmp_path):
    project["env"]["TE_OPENCODE_DB"] = str(tmp_path / "nope.db")
    assert backfill.run(directories=[project["proj"]], env=project["env"])["missing"] is True

    foreign = str(tmp_path / "foreign.db")
    sqlite3.connect(foreign).close()
    project["env"]["TE_OPENCODE_DB"] = foreign
    assert backfill.run(directories=[project["proj"]], env=project["env"])["unsupported"] is True


def test_a_replayed_failure_shape_becomes_an_injected_fact(tmp_path, monkeypatch):
    """The point of the replay: sessions that ran before the plugin did still teach it. Here the
    same environment fact was rediscovered six times -- a bare `pytest` is not on PATH, `python -m
    pytest` runs the tests -- so the free layer promotes it and the next session is told."""
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    proj = str(tmp_path / "proj")
    os.makedirs(proj)
    env = {"TOKENECONOMIZER_HOME": str(tmp_path / "state"),
           "TE_OPENCODE_DB": str(tmp_path / "opencode.db")}
    now = int(__import__("time").time() * 1000)
    conn = sqlite3.connect(env["TE_OPENCODE_DB"])
    conn.executescript("""
        create table session_v2 (id text primary key, directory text, parent_id text,
            time_created integer, time_updated integer, tokens_input integer,
            tokens_output integer, tokens_reasoning integer, tokens_cache_read integer,
            tokens_cache_write integer, cost real);
        create table session_message (id text, session_id text, type text, seq integer,
            data text);
    """)
    for i in range(6):
        sid = f"ses_shape{i}"
        conn.execute("insert into session_v2 values (?,?,?,?,?,?,?,?,?,?,?)",
                     (sid, proj, None, now - (4000 - i * 60) * 1000,
                      now - (3600 - i * 60) * 1000, 1000, 100, 0, 0, 0, 0.01))
        conn.executemany("insert into session_message values (?,?,?,?,?)", [
            (f"{sid}-u", sid, "user", 1, json.dumps({"text": "run the tests"})),
            (f"{sid}-a1", sid, "assistant", 2,
             _call("call-a", "pytest", "/bin/sh: pytest: command not found")),
            (f"{sid}-a2", sid, "assistant", 3,
             _call("call-b", "python -m pytest", "1 passed in 0.02s")),
        ])
    conn.commit()
    conn.close()

    summary = backfill.run(directories=[proj], env=env, out=lambda _line: None)
    assert summary["results"][0]["folded"] == 6

    injection = _store({"proj": proj, "env": env}).read("injection.json")
    assert "pytest" in injection["rendered"] and "python -m pytest" in injection["rendered"]
    assert injection["rendered_ids"] == ["form_repair:run_tests:pytest"]


def test_the_database_path_is_opencodes_own_unless_told_otherwise(tmp_path):
    home = str(tmp_path / "home")
    assert trace.opencode_db.db_path({"HOME": home}) == os.path.join(
        home, ".local", "share", "opencode", "opencode.db")
    assert trace.opencode_db.db_path({"USERPROFILE": home}).startswith(home)
    named = str(tmp_path / "a.db")
    assert trace.opencode_db.db_path({"TE_OPENCODE_DB": named}) == os.path.abspath(named)
    assert trace.opencode_db.db_path({}).endswith(
        os.path.join(".local", "share", "opencode", "opencode.db"))
