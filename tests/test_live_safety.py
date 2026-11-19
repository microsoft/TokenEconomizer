"""What the plugin writes stays where it belongs and stays private.

  * A checkout decides what `.claude`, `.codex`, `.github` and `.git` are, so a write the
    deny list makes must not follow a symlink, or a `.git` file, out of the repository; nor
    must its temporary file, nor the read of the file before it.
  * A settings file the plugin cannot parse is the person's: refused, never written over.
  * The state root holds what was derived from the agents' private session logs, and the
    launcher the slash commands run: it is private to its owner.
  * The `te` a hook starts in the background runs in the repository but imports only the
    plugin's own code, never a module the checkout carries, whatever PYTHONPATH it inherits;
    nor does the Python it probes for aider.
"""
import json
import os
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tokeneconomizer.plugin import context, denylist, repomap, toolcheck, winspawn
from tokeneconomizer.plugin.paths import ensure_launcher, secure_root
from tokeneconomizer.plugin.store import Store
from tests.conftest import git_sh, symlink_or_skip
from tests.test_live_hooks import repo  # noqa: F401

PLUGIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugin")


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.mark.parametrize("host,rel", [("claude", ".claude"), ("copilot", ".github"),
                                      ("codex", ".codex")])
def test_a_symlinked_settings_directory_is_refused(repo, tmp_path, host, rel):
    outside = tmp_path / "outside"
    outside.mkdir()
    symlink_or_skip(outside, os.path.join(repo, rel))
    item = {"claude": "Workflow", "copilot": "github-mcp-server", "codex": "goals"}[host]
    with pytest.raises(RuntimeError, match="symlink"):
        denylist.apply(repo, [item], Store(str(tmp_path / "st")), host=host)
    assert list(outside.rglob("*")) == []


def test_a_symlinked_settings_file_is_refused_and_its_target_untouched(repo, tmp_path):
    target = tmp_path / "elsewhere.json"
    target.write_text('{"keep": true}\n')
    os.makedirs(os.path.join(repo, ".claude"))
    symlink_or_skip(target, os.path.join(repo, ".claude", "settings.local.json"))
    st = Store(str(tmp_path / "st"))
    with pytest.raises(RuntimeError, match="symlink"):
        denylist.apply(repo, ["Workflow"], st)
    assert denylist.restore(repo, st, ["Workflow"]) == []      # nothing of its own to remove
    assert target.read_text() == '{"keep": true}\n'


def test_a_settings_file_that_does_not_parse_is_refused_not_overwritten(repo, tmp_path):
    path = os.path.join(repo, ".claude", "settings.local.json")
    os.makedirs(os.path.dirname(path))
    broken = '{"permissions": {"allow": ["Bash(ls)"],}}\n'          # a trailing comma
    open(path, "w").write(broken)
    with pytest.raises(RuntimeError, match="not valid JSON"):
        denylist.apply(repo, ["Workflow"], Store(str(tmp_path / "st")))
    assert open(path).read() == broken
    open(path, "w").write("")                                     # empty is the same as none
    assert denylist.apply(repo, ["Workflow"], Store(str(tmp_path / "st2"))) == ["Workflow"]
    assert json.load(open(path)) == {"permissions": {"deny": ["Workflow"]}}


def test_a_git_file_pointing_out_of_the_repository_gets_no_exclude_line(repo, tmp_path):
    import shutil
    shutil.rmtree(os.path.join(repo, ".git"))
    elsewhere = tmp_path / "not-a-git-dir"
    elsewhere.mkdir()
    open(os.path.join(repo, ".git"), "w").write(f"gitdir: {elsewhere}\n")
    denylist.apply(repo, ["Workflow"], Store(str(tmp_path / "st")))
    assert list(elsewhere.rglob("*")) == []


def test_a_symlinked_exclude_file_is_not_written_through(repo, tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("keep\n")
    symlink_or_skip(victim, os.path.join(repo, ".git", "info", "exclude"))
    denylist.apply(repo, ["Workflow"], Store(str(tmp_path / "st")))
    assert victim.read_text() == "keep\n"


@pytest.mark.parametrize("write", [
    lambda path: denylist.write_json(path, {"permissions": {"deny": ["Workflow"]}}),
    lambda path: denylist._write_text(path, '{"permissions": {"deny": ["Workflow"]}}\n'),
], ids=["write_json", "_write_text"])
def test_a_symlink_at_the_temporary_name_is_not_written_through(repo, tmp_path, write):
    victim = tmp_path / "victim.json"
    victim.write_text('{"keep": true}\n')
    path = os.path.join(repo, ".claude", "settings.local.json")
    os.makedirs(os.path.dirname(path))
    symlink_or_skip(victim, path + ".te.tmp")
    write(path)
    assert victim.read_text() == '{"keep": true}\n'
    assert json.load(open(path)) == {"permissions": {"deny": ["Workflow"]}}
    assert not os.path.lexists(path + ".te.tmp")


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="no O_NOFOLLOW on this system")
def test_a_settings_file_swapped_for_a_symlink_after_the_guard_is_not_read(repo, tmp_path):
    victim = tmp_path / "settings.json"
    victim.write_text('{"hooks": {"Stop": []}}\n')
    path = os.path.join(repo, ".claude", "settings.local.json")
    os.makedirs(os.path.dirname(path))
    symlink_or_skip(victim, path)
    with pytest.raises(RuntimeError, match="cannot read"):
        denylist._read_settings(path)


def _plant(repo, tmp_path, modules):
    """Each module of `modules` (a path relative to `repo`) creates its marker when it runs;
    returns the markers by module."""
    planted = {}
    for module in modules:
        planted[module] = tmp_path / ("planted-" + module.replace(os.sep, "-"))
        os.makedirs(os.path.dirname(os.path.join(repo, module)) or repo, exist_ok=True)
        open(os.path.join(repo, module), "w").write(
            f"open({str(planted[module])!r}, 'w').close()\n")
    return planted


@pytest.mark.parametrize("inherited", [None, os.pathsep + "elsewhere", "."],
                         ids=["no PYTHONPATH", "PYTHONPATH with an empty element", "PYTHONPATH ."])
def test_the_background_te_imports_nothing_the_repository_carries(repo, tmp_path, monkeypatch,
                                                                    inherited):
    planted = _plant(repo, tmp_path, ["json.py", "runpy.py", "sitecustomize.py",
                                      os.path.join("tokeneconomizer", "__init__.py")])
    started = []
    monkeypatch.setattr(context.subprocess, "Popen", lambda cmd, **kw: started.append((cmd, kw)))
    monkeypatch.setattr(winspawn, "spawn", lambda cmd, cwd, env, log: started.append(
        (cmd, {"cwd": cwd, "env": env})) or True)
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONSAFEPATH", "PYTHONPATH")}
    if inherited is not None:
        env["PYTHONPATH"] = inherited
    ctx = SimpleNamespace(env=env, host=SimpleNamespace(name="claude"),
                          repo=SimpleNamespace(root=repo), store=Store(str(tmp_path / "st")))
    context.LiveContext.spawn(ctx, ["--help"], "spawn.log")
    monkeypatch.undo()
    (cmd, kw), = started
    assert kw["cwd"] == repo
    done = subprocess.run(cmd, cwd=kw["cwd"], env=kw["env"], capture_output=True, text=True,
                          timeout=120)
    assert [module for module, marker in planted.items() if marker.exists()] == []
    assert done.returncode == 0, done.stderr
    assert "usage" in done.stdout


def test_the_aider_probe_imports_no_aider_the_repository_carries(repo, tmp_path, monkeypatch):
    planted = _plant(repo, tmp_path, [os.path.join("aider", "__init__.py"),
                                      os.path.join("aider", "repomap.py")])
    monkeypatch.chdir(repo)
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONSAFEPATH", "PYTHONPATH")}
    repomap._has_aider(sys.executable, env)
    assert [module for module, marker in planted.items() if marker.exists()] == []


@pytest.mark.skipif(os.name == "nt",
                    reason="POSIX permission bits: Windows keeps a profile private by ACL")
def test_a_loose_state_root_is_made_private_and_the_launcher_is_owner_only(tmp_path):
    env = {"TOKENECONOMIZER_HOME": str(tmp_path / "home"), "TE_PLUGIN_ROOT": PLUGIN}
    os.makedirs(env["TOKENECONOMIZER_HOME"], mode=0o775)
    os.chmod(env["TOKENECONOMIZER_HOME"], 0o775)
    secure_root(env)
    assert _mode(env["TOKENECONOMIZER_HOME"]) == 0o700
    assert _mode(ensure_launcher(env)) == 0o700


@pytest.mark.skipif(os.name == "nt",
                    reason="POSIX permission bits: Windows keeps a profile private by ACL")
def test_the_command_line_creates_its_state_private(repo, tmp_path):
    env = dict(os.environ, TOKENECONOMIZER_HOME=str(tmp_path / "home"))
    subprocess.run([git_sh() or "sh", os.path.join(PLUGIN, "scripts", "te"), "--cwd", repo,
                    "config", "set", "spiral=off"], env=dict(env, TE_PYTHON=sys.executable), check=True,
                   capture_output=True)
    made = [os.path.join(d, n) for d, ds, fs in os.walk(env["TOKENECONOMIZER_HOME"])
            for n in ds + fs] + [env["TOKENECONOMIZER_HOME"]]
    assert made and all(_mode(p) & 0o077 == 0 for p in made), {p: oct(_mode(p)) for p in made}
