"""What the plugin writes stays where it belongs and stays private.

  * A checkout decides what `.claude`, `.codex`, `.github` and `.git` are, so a write the
    deny list makes must not follow a symlink, or a `.git` file, out of the repository.
  * A settings file the plugin cannot parse is the person's: refused, never written over.
  * The state root holds what was derived from the agents' private session logs, and the
    launcher the slash commands run: it is private to its owner.
"""
import json
import os
import stat
import subprocess
import sys

import pytest

from tokeneconomizer.plugin import denylist, toolcheck
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
