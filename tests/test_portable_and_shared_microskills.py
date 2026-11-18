"""Tests for repository-scoped shared micro-skills and sanitized portable repo state."""
import io
import json
import os

import pytest

from tokeneconomizer.plugin import cli, config as live_config, hook, portable
from tokeneconomizer.plugin.paths import repo_identity, repo_state_dir
from tokeneconomizer.plugin.store import Store

from tests.conftest import symlink_or_skip
from tests.test_live_hooks import call, trap_session


def _repo(tmp_path, name="repo"):
    r = tmp_path / name
    (r / ".git" / "info").mkdir(parents=True)
    (r / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (r / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://github.com/acme/widget.git\n', encoding="utf-8")
    (r / "pyproject.toml").write_text("[project]\nname='w'\n", encoding="utf-8")
    return str(r)


def _run_claude_session(env, repo, sid, transcript_path):
    p = {"session_id": sid, "transcript_path": transcript_path, "cwd": repo}
    start = call(env, dict(p, hook_event_name="SessionStart", source="startup"))
    call(env, dict(p, hook_event_name="UserPromptSubmit", prompt_id="t1", prompt="fix"))
    call(env, dict(p, hook_event_name="Stop", stop_hook_active=False))
    call(env, dict(p, hook_event_name="SessionEnd", reason="exit"))
    return start


def test_config_accepts_microskills_scope_and_portable_state(tmp_path):
    c = live_config.LiveConfig()
    assert (c.microskills_scope, c.portable_state) == ("host", "auto")
    assert c.set("microskills_scope", "repo") and c.microskills_scope == "repo"
    assert c.set("portable_state", "on") and c.portable_state == "on"
    assert not c.set("microskills_scope", "global")
    assert not c.set("portable_state", "always")

    loaded = live_config.load_config(str(tmp_path), env={
        "TE_MICROSKILLS_SCOPE": "repo",
        "TE_PORTABLE_STATE": "off",
    })
    assert loaded.microskills_scope == "repo"
    assert loaded.portable_state == "off"


def test_repo_scoped_microskills_shares_facts_across_agents(tmp_path):
    repo = _repo(tmp_path)
    home = tmp_path / "home"
    env = {
        "TOKENECONOMIZER_HOME": str(home),
        "TE_FOLD_SYNC": "1",
        "TE_MICROSKILLS": "envfacts",
        "TE_MICROSKILLS_SCOPE": "repo",
        "GIT_CEILING_DIRECTORIES": str(tmp_path),
        "PATH": os.environ.get("PATH", ""),
    }
    for i in range(4):
        t = trap_session(str(tmp_path / f"c{i}.jsonl"), repo, i)
        _run_claude_session(env, repo, f"s{i}", t)

    # OpenCode starting its very first session in the same repo receives the learned fact.
    oc_start = call(env, {"hook_event_name": "SessionStart", "session_id": "oc1",
                          "cwd": repo, "source": "startup"}, host="opencode")
    ctx_text = (oc_start.get("hookSpecificOutput") or {}).get("additionalContext") or ""
    assert "python -m pytest" in ctx_text

    # Without repo scope (host default), OpenCode has its own isolated store.
    env_host = dict(env, TE_MICROSKILLS_SCOPE="host")
    oc_isolated = call(env_host, {"hook_event_name": "SessionStart", "session_id": "oc2",
                                  "cwd": repo, "source": "startup"}, host="opencode")
    assert oc_isolated == {}


def test_portable_facts_export_and_inject_on_fresh_machine(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path)
    home_a = tmp_path / "machine-a"
    env_a = {
        "TOKENECONOMIZER_HOME": str(home_a),
        "TE_FOLD_SYNC": "1",
        "TE_MICROSKILLS": "envfacts",
        "TE_MICROSKILLS_SCOPE": "repo",
        "TE_PORTABLE_STATE": "on",
        "GIT_CEILING_DIRECTORIES": str(tmp_path),
        "PATH": os.environ.get("PATH", ""),
    }
    for i in range(4):
        t = trap_session(str(tmp_path / f"a{i}.jsonl"), repo, i)
        _run_claude_session(env_a, repo, f"a{i}", t)

    fpath = portable.facts_path(repo)
    assert os.path.isfile(fpath)
    data = json.loads(open(fpath, encoding="utf-8").read())
    assert data["facts"] and any("python -m pytest" in v["claim"] for v in data["facts"].values())

    # Fresh machine B with default microskills=off and portable_state=auto gets Session #1 facts.
    home_b = tmp_path / "machine-b"
    env_b = {
        "TOKENECONOMIZER_HOME": str(home_b),
        "TE_FOLD_SYNC": "1",
        "GIT_CEILING_DIRECTORIES": str(tmp_path),
        "PATH": os.environ.get("PATH", ""),
    }
    start_b = call(env_b, {"hook_event_name": "SessionStart", "session_id": "b1",
                           "cwd": repo, "source": "startup"})
    ctx_b = (start_b.get("hookSpecificOutput") or {}).get("additionalContext") or ""
    assert "python -m pytest" in ctx_b

    # `portable_state=off` suppresses portable injection.
    assert call(dict(env_b, TE_PORTABLE_STATE="off"),
                {"hook_event_name": "SessionStart", "session_id": "b2",
                 "cwd": repo, "source": "startup"}) == {}

    # CLI `export` / `import` round-trip.
    monkeypatch.setenv("TOKENECONOMIZER_HOME", str(home_a))
    monkeypatch.setenv("TE_MICROSKILLS_SCOPE", "repo")
    assert cli.main(["--cwd", repo, "--host", "claude", "microskills", "export"]) == 0
    assert "exported" in capsys.readouterr().out
    monkeypatch.setenv("TOKENECONOMIZER_HOME", str(home_b))
    assert cli.main(["--cwd", repo, "--host", "claude", "microskills", "import"]) == 0
    assert "imported" in capsys.readouterr().out


def test_portable_sanitizer_drops_home_paths_and_secrets(tmp_path):
    repo = _repo(tmp_path)
    env = {"HOME": "/Users/alice", "USERPROFILE": r"C:\Users\alice"}
    assert portable.sanitize_claim("Run `python -m pytest` here.", env=env) is not None
    assert portable.sanitize_claim("Use `/Users/alice/.venv/bin/pytest`.", env=env) is None
    assert portable.sanitize_claim(r"Use `C:\Users\alice\venv\pytest.exe`.", env=env) is None
    assert portable.sanitize_claim("Set OPENAI_API_KEY=sk-abcdefghijklmnop1234", env=env) is None
    assert portable.sanitize_claim("Export ghp_1234567890abcdefghij", env=env) is None

    _, written = portable.write_facts(repo, {
        "good": {"claim": "Run `.venv/bin/python -m pytest` here."},
        "leak_home": {"claim": "Use `/home/bob/secret/pytest`."},
        "leak_secret": {"claim": "Bearer secret_token_1234567890 is required."},
    }, env=env)
    assert list(written) == ["good"]


def test_portable_refuses_symlinked_dir_or_file(tmp_path):
    repo = _repo(tmp_path)
    outside = tmp_path / "victim.json"
    outside.write_text('{"untouched": true}\n', encoding="utf-8")
    pdir = tmp_path / "repo" / ".tokeneconomizer"
    pdir.mkdir()
    symlink_or_skip(str(outside), str(pdir / "facts.json"))

    with pytest.raises(RuntimeError):
        portable.write_facts(repo, {"f1": {"claim": "Run `python -m pytest`."}})
    assert outside.read_text(encoding="utf-8") == '{"untouched": true}\n'
    assert portable.read_facts(repo) == {}


def test_local_forget_suppresses_portable_fact(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    home = tmp_path / "home"
    env = {
        "TOKENECONOMIZER_HOME": str(home),
        "TE_FOLD_SYNC": "1",
        "TE_MICROSKILLS": "envfacts",
        "TE_MICROSKILLS_SCOPE": "repo",
        "TE_PORTABLE_STATE": "on",
        "GIT_CEILING_DIRECTORIES": str(tmp_path),
        "PATH": os.environ.get("PATH", ""),
    }
    for i in range(4):
        t = trap_session(str(tmp_path / f"f{i}.jsonl"), repo, i)
        _run_claude_session(env, repo, f"f{i}", t)

    fid = next(iter(portable.read_facts(repo)))
    monkeypatch.setenv("TOKENECONOMIZER_HOME", str(home))
    monkeypatch.setenv("TE_MICROSKILLS_SCOPE", "repo")
    monkeypatch.setenv("TE_MICROSKILLS", "envfacts")
    assert cli.main(["--cwd", repo, "--host", "claude", "microskills", "forget", fid]) == 0

    out = call(env, {"hook_event_name": "SessionStart", "session_id": "after-forget",
                     "cwd": repo, "source": "startup"})
    assert out == {}
