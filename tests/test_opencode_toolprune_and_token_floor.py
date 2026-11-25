"""Tests for safe OpenCode tool/MCP pruning (`opencode.json`) and `toolprune_min_tokens`."""
import json
import os
import pathlib
import subprocess

import pytest

from tokeneconomizer.plugin import adapter, cli, denylist, fold, hosts
from tokeneconomizer.plugin.config import LiveConfig, load_config, write_config
from tokeneconomizer.plugin.context import LiveContext
from tokeneconomizer.plugin.store import Store


def _init_git(repo):
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)


def _env_for(tmp_path, extra=None):
    xdg = tmp_path / "xdg"
    home = tmp_path / "home"
    te_home = tmp_path / "te_home"
    xdg.mkdir(exist_ok=True)
    home.mkdir(exist_ok=True)
    te_home.mkdir(exist_ok=True)
    env = {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "XDG_CONFIG_HOME": str(xdg),
        "TOKENECONOMIZER_HOME": str(te_home),
        "TE_FOLD_SYNC": "1",
        "GIT_CEILING_DIRECTORIES": str(tmp_path),
        "PATH": os.environ.get("PATH", ""),
    }
    if extra:
        env.update(extra)
    return env


def _write_opencode_export(path, cwd, tools):
    tokens = {"input": 10, "output": 20, "reasoning": 0, "cache": {"read": 100, "write": 0}}
    msgs = [
        {"info": {"id": "u1", "role": "user"},
         "parts": [{"type": "text", "text": "check repo"}]},
    ]
    for idx, name in enumerate(tools):
        msgs.append({
            "info": {"id": f"m-{idx}", "role": "assistant", "modelID": "gpt-5.6-sol",
                     "path": {"cwd": str(cwd), "root": str(cwd)}, "tokens": tokens},
            "parts": [{"type": "tool", "callID": f"c-{idx}", "tool": name,
                       "state": {"status": "completed", "input": {}, "output": "ok",
                                 "metadata": {"exit": 0}}}],
        })
    path.write_text(json.dumps({"info": {"id": "oc-ses"}, "messages": msgs}), encoding="utf-8")
    return str(path)


def _write_claude_transcript(path, cwd, tools):
    lines = [
        {"type": "user", "cwd": str(cwd), "message": {"role": "user", "content": "check repo"}},
        {"type": "assistant", "cwd": str(cwd),
         "message": {"role": "assistant", "model": "claude-sonnet-4-6",
                     "usage": {"input_tokens": 10, "output_tokens": 20,
                               "cache_read_input_tokens": 0,
                               "cache_creation_input_tokens": 0},
                     "content": [{"type": "tool_use", "id": f"t-{i}", "name": name, "input": {}}
                                 for i, name in enumerate(tools)]}},
        {"type": "user", "cwd": str(cwd),
         "message": {"role": "user",
                     "content": [{"type": "tool_result", "tool_use_id": f"t-{i}",
                                  "content": "ok"} for i in range(len(tools))]}},
    ]
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return str(path)


# ---- denylist on opencode.json --------------------------------------------------------------
def test_opencode_denylist_apply_restore_preserves_user_entries_and_excludes_created(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git(repo)
    st = Store(str(tmp_path / "store"))

    assert denylist.supports("opencode") is True
    assert denylist.unit("opencode") == "tool"
    assert denylist.unit("opencode", plural=True) == "tools"

    added = denylist.apply(str(repo), ["unused_mcp_*", "webfetch"], st, host="opencode")
    assert added == ["unused_mcp_*", "webfetch"]
    cfg_file = repo / "opencode.json"
    assert json.loads(cfg_file.read_text(encoding="utf-8")) == {
        "permission": {"unused_mcp_*": "deny", "webfetch": "deny"}
    }
    excl = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert "opencode.json" in excl
    assert sorted(denylist.applied_for(st, str(repo), "opencode")) == ["unused_mcp_*", "webfetch"]

    # Partial restore removes only what was asked; full restore removes empty file.
    assert denylist.restore(str(repo), st, ["webfetch"], host="opencode") == ["webfetch"]
    assert json.loads(cfg_file.read_text(encoding="utf-8")) == {
        "permission": {"unused_mcp_*": "deny"}
    }
    assert denylist.restore(str(repo), st, host="opencode") == ["unused_mcp_*"]
    assert not cfg_file.exists()
    assert denylist.applied_for(st, str(repo), "opencode") == []

    # Existing user config + permissions stay intact on apply and restore.
    cfg_file.write_text(json.dumps({
        "theme": "catppuccin",
        "permission": {"bash": "allow", "custom": "deny"},
    }), encoding="utf-8")
    added2 = denylist.apply(str(repo), ["bash", "custom", "unused_mcp_*"], st, host="opencode")
    assert added2 == ["unused_mcp_*"]
    assert denylist.applied_for(st, str(repo), "opencode") == ["unused_mcp_*"]
    assert json.loads(cfg_file.read_text(encoding="utf-8")) == {
        "theme": "catppuccin",
        "permission": {"bash": "allow", "custom": "deny", "unused_mcp_*": "deny"},
    }
    assert denylist.restore(str(repo), st, host="opencode") == ["unused_mcp_*"]
    assert json.loads(cfg_file.read_text(encoding="utf-8")) == {
        "theme": "catppuccin",
        "permission": {"bash": "allow", "custom": "deny"},
    }


def test_opencode_denylist_refuses_committed_or_symlinked_config(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git(repo)
    st = Store(str(tmp_path / "store"))

    cfg_file = repo / "opencode.json"
    cfg_file.write_text('{"permission": {"bash": "allow"}}\n', encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "opencode.json"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "commit opencode"], check=True)

    with pytest.raises(RuntimeError, match="committed to git"):
        denylist.apply(str(repo), ["unused_mcp_*"], st, host="opencode")
    assert json.loads(cfg_file.read_text(encoding="utf-8")) == {"permission": {"bash": "allow"}}

    # Symlinked opencode.json in an untracked repo is also refused.
    sym_repo = tmp_path / "sym_repo"
    sym_repo.mkdir()
    _init_git(sym_repo)
    target = tmp_path / "outside.json"
    target.write_text("{}\n", encoding="utf-8")
    try:
        os.symlink(target, sym_repo / "opencode.json")
    except OSError:
        pytest.skip("symlinks unavailable on this platform")
    with pytest.raises(RuntimeError):
        denylist.apply(str(sym_repo), ["unused_mcp_*"], st, host="opencode")
    assert target.read_text(encoding="utf-8") == "{}\n"


# ---- OpenCode MCP pruning + protected built-ins ---------------------------------------------
def test_opencode_mcp_toolprune_proposes_unused_and_keeps_active_and_protected(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git(repo)
    env = _env_for(tmp_path, {
        "TE_TOOLPRUNE": "auto",
        "TE_TOOLPRUNE_EVIDENCE": "2",
        "TE_TOOLPRUNE_MIN_SESSIONS": "2",
        "TE_TOOLPRUNE_SESSIONS": "2",
        "TE_TOOLPRUNE_QUIET_MINUTES": "0",
        "TE_MICROSKILLS": "off",
        "TE_LOOPGUARD": "off",
    })
    oc_dir = pathlib.Path(env["XDG_CONFIG_HOME"]) / "opencode"
    oc_dir.mkdir(parents=True)
    (oc_dir / "opencode.json").write_text(json.dumps({
        "mcp": {
            "unusedmcp": {"type": "local", "enabled": True},
            "activemcp": {"type": "local"},
            "disabledmcp": {"type": "local", "enabled": False},
        }
    }), encoding="utf-8")

    for core in ("*", "read", "task", "bash", "edit", "write", "glob", "grep"):
        assert core in adapter.OPENCODE_PROTECTED

    s1 = _write_opencode_export(tmp_path / "s1.json", repo, ["bash", "read", "activemcp_search"])
    s2 = _write_opencode_export(tmp_path / "s2.json", repo, ["bash", "edit", "read"])

    ctx1 = LiveContext.build(cwd=str(repo), host=hosts.HOSTS["opencode"](), env=env)
    fold.fold_session(ctx1, "oc-1", s1)
    ctx2 = LiveContext.build(cwd=str(repo), host=hosts.HOSTS["opencode"](), env=env)
    fold.fold_session(ctx2, "oc-2", s2)

    unt = ctx2.store.read("untool.json")
    assert "activemcp_*" in unt["used_tools"]
    assert unt["streaks"]["activemcp_*"] == 0
    assert "disabledmcp_*" not in unt["catalog_tools"]
    assert denylist.applied_for(ctx2.store, str(repo), "opencode") == ["unusedmcp_*"]

    repo_cfg = json.loads((repo / "opencode.json").read_text(encoding="utf-8"))
    assert repo_cfg["permission"] == {"unusedmcp_*": "deny"}
    for core in ("read", "task", "bash", "edit", "*"):
        assert core not in repo_cfg["permission"]


# ---- toolprune_min_tokens floor -------------------------------------------------------------
def test_toolprune_min_tokens_filters_small_tools_and_keeps_large_or_unknown(tmp_path):
    cfg = LiveConfig()
    assert cfg.set("toolprune_min_tokens", 120)
    assert cfg.toolprune_min_tokens == 120
    assert not cfg.set("toolprune_min_tokens", -5)

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git(repo)
    env = _env_for(tmp_path, {
        "TE_TOOLPRUNE": "propose",
        "TE_TOOLPRUNE_EVIDENCE": "2",
        "TE_TOOLPRUNE_MIN_SESSIONS": "2",
        "TE_TOOLPRUNE_SESSIONS": "2",
        "TE_TOOLPRUNE_MIN_TOKENS": "100",
        "TE_MICROSKILLS": "off",
        "TE_LOOPGUARD": "off",
    })
    write_config(str(pathlib.Path(env["TOKENECONOMIZER_HOME"]) / "config.json"),
                 {"toolprune_min_tokens": 100})
    loaded = load_config(env["TOKENECONOMIZER_HOME"], env={})
    assert loaded.toolprune_min_tokens == 100

    cat_dir = pathlib.Path(env["TOKENECONOMIZER_HOME"]) / "hosts" / "claude"
    cat_dir.mkdir(parents=True)
    (cat_dir / "catalog.json").write_text(json.dumps({
        "tools": ["Read", "SmallTool", "LargeDescTool", "ExplicitSmallTool",
                  "ExplicitLargeTool", "UnknownSchemaTool"],
        "descriptions": {
            "SmallTool": "tiny",
            "LargeDescTool": "x" * 800,
        },
        "tokens": {
            "ExplicitSmallTool": 40,
            "ExplicitLargeTool": 300,
        },
    }), encoding="utf-8")

    t1 = _write_claude_transcript(tmp_path / "c1.jsonl", repo, ["Read"])
    t2 = _write_claude_transcript(tmp_path / "c2.jsonl", repo, ["Read"])
    ctx1 = LiveContext.build(cwd=str(repo), host=hosts.HOSTS["claude"](), env=env)
    fold.fold_session(ctx1, "cl-1", t1)
    ctx2 = LiveContext.build(cwd=str(repo), host=hosts.HOSTS["claude"](), env=env)
    fold.fold_session(ctx2, "cl-2", t2)

    unt = ctx2.store.read("untool.json")
    assert unt["tool_tokens"]["SmallTool"] == 1
    assert unt["tool_tokens"]["LargeDescTool"] == 200
    assert unt["tool_tokens"]["ExplicitSmallTool"] == 40
    assert unt["tool_tokens"]["ExplicitLargeTool"] == 300
    assert "UnknownSchemaTool" not in unt["tool_tokens"]

    proposal = unt["proposal"]
    assert "LargeDescTool" in proposal
    assert "ExplicitLargeTool" in proposal
    assert "UnknownSchemaTool" in proposal
    assert "SmallTool" not in proposal
    assert "ExplicitSmallTool" not in proposal


# ---- CLI output -----------------------------------------------------------------------------
def test_cli_toolprune_show_opencode_and_token_estimates_and_config_view(
        tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git(repo)
    env = _env_for(tmp_path, {"TE_TOOLPRUNE_MIN_TOKENS": "150"})
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.chdir(repo)

    ctx = LiveContext.build(cwd=str(repo), host=hosts.HOSTS["opencode"](), env=env)
    ctx.store.write("untool.json", {
        "sessions": 3,
        "prompts": 12,
        "catalog_source": "observed",
        "catalog_tools": ["bash", "activemcp_*", "unusedmcp_*"],
        "used_tools": ["bash", "activemcp_*"],
        "streaks": {"bash": 0, "activemcp_*": 0, "unusedmcp_*": 3},
        "prompt_streaks": {"bash": 0, "activemcp_*": 0, "unusedmcp_*": 12},
        "tool_tokens": {"unusedmcp_*": 420},
        "proposal": ["unusedmcp_*"],
    })

    assert cli.main(["--host", "opencode", "--cwd", str(repo), "toolprune"]) == 0
    out = capsys.readouterr().out
    assert "unusedmcp_* (~420 tok)" in out
    assert "activemcp_*" in out
    assert "used" in out
    assert "refresh-catalog" not in out

    assert cli.main(["config", "show"]) == 0
    cfg_out = json.loads(capsys.readouterr().out)
    assert cfg_out["toolprune"]["toolprune_min_tokens"] == 150
    assert "toolprune_min_tokens" not in cfg_out["other"]
