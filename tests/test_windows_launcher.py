"""`~/.tokeneconomizer/bin/te` on Windows (#167). Written in text mode there, the launcher's lines
ended in CRLF and `sh` handed every command a stray `\\r` argument, and the check that skips an
unchanged launcher read it back in text mode, so it was never repaired. On Windows the launcher
also ran `scripts/te` by a backslash path, which the shell omp runs commands in cannot `exec`."""
import os
import shlex
import subprocess
import sys

import pytest

from tokeneconomizer.plugin import paths
from tokeneconomizer.plugin.paths import ensure_launcher
from tests.conftest import git_sh
from tests.test_live_hooks import repo  # noqa: F401

PLUGIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugin")
CORE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(paths.__file__))))
HEAD = ("#!/bin/sh\n# Written by the TokenEconomizer plugin at session start: its `te` "
        "command line.\n")


def _env(tmp_path):
    return {"TOKENECONOMIZER_HOME": str(tmp_path / "home"), "TE_PLUGIN_ROOT": PLUGIN}


def test_a_launcher_left_with_crlf_endings_is_rewritten_with_lf(tmp_path):
    env = _env(tmp_path)
    path = ensure_launcher(env)
    with open(path, "rb") as fh:
        lf = fh.read()
    assert b"\r" not in lf
    with open(path, "wb") as fh:
        fh.write(lf.replace(b"\n", b"\r\n"))
    ensure_launcher(env)
    with open(path, "rb") as fh:
        assert fh.read() == lf


@pytest.mark.parametrize("windows", [False, True])
def test_the_launcher_execs_scripts_te_on_posix_and_the_hooks_python_on_windows(
        tmp_path, monkeypatch, windows):
    monkeypatch.setattr(paths, "_WINDOWS", windows)
    monkeypatch.setattr(sys, "executable", r"C:\Program Files\Python313\python.exe")
    if windows:
        core, bootstrap = (shlex.quote(p.replace("\\", "/")) for p in (
            CORE, os.path.join(PLUGIN, "scripts", "te_bootstrap.py")))
        expected = HEAD + ('[ -n "${TE_CORE_PATH:-}" ] || TE_CORE_PATH=%s\n'
                           "export TE_CORE_PATH PYTHONUTF8=1\n"
                           "exec 'C:/Program Files/Python313/python.exe' %s cli \"$@\"\n"
                           % (core, bootstrap))
    else:
        expected = HEAD + 'exec %s "$@"\n' % shlex.quote(os.path.join(PLUGIN, "scripts", "te"))
    with open(ensure_launcher(_env(tmp_path)), encoding="utf-8", newline="") as fh:
        assert fh.read() == expected


def test_the_windows_launcher_runs_the_command_line(repo, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(paths, "_WINDOWS", True)
    env = _env(tmp_path)
    run_env = dict(os.environ, TOKENECONOMIZER_HOME=env["TOKENECONOMIZER_HOME"], TE_CORE_PATH="",
                   PYTHONPATH="")
    p = subprocess.run([git_sh() or "sh", ensure_launcher(env), "--cwd", repo, "config", "set",
                        "spiral=off"], env=run_env, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
