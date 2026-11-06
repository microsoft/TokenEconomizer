"""The `sh` the tests run the launchers with. Windows runs no shebang, and Git for Windows puts
only `<Git>\\cmd` on the PATH of PowerShell and cmd.exe, so a bare `sh` is not found there."""
import glob
import os
import re

from tests import conftest
from tests.conftest import git_sh

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _which(found):
    return lambda name: found.get(name)


def test_an_sh_on_path_wins(monkeypatch):
    monkeypatch.setattr(conftest.shutil, "which", _which({"sh": "/bin/sh", "git": "/usr/bin/git"}))
    assert git_sh() == "/bin/sh"


def test_without_sh_on_path_it_is_found_beside_git(tmp_path, monkeypatch):
    git = tmp_path / "Git" / "cmd" / "git.exe"
    sh = tmp_path / "Git" / "bin" / "sh.exe"
    for p in (git, sh):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("", encoding="utf-8")
    monkeypatch.setattr(conftest.shutil, "which", _which({"git": str(git)}))
    assert os.path.samefile(git_sh(), sh)


def test_no_sh_beside_git_and_none_on_path_is_none(tmp_path, monkeypatch):
    git = tmp_path / "Git" / "cmd" / "git.exe"
    git.parent.mkdir(parents=True)
    git.write_text("", encoding="utf-8")
    monkeypatch.setattr(conftest.shutil, "which", _which({"git": str(git)}))
    assert git_sh() is None


def test_neither_sh_nor_git_is_none(monkeypatch):
    monkeypatch.setattr(conftest.shutil, "which", _which({}))
    assert git_sh() is None


def test_no_test_runs_a_bare_sh():
    """A command list that starts with a literal "sh" fails on a Windows runner whose PATH has
    no `sh`; the tests take `git_sh()` instead."""
    bare = re.compile(r"""\[\s*["']sh["']""")
    found = []
    for tree in ("tests", os.path.join("experiments", "tests")):
        for path in glob.glob(os.path.join(REPO, tree, "**", "*.py"), recursive=True):
            with open(path, encoding="utf-8") as fh:
                found += [f"{os.path.relpath(path, REPO)}:{n}"
                          for n, line in enumerate(fh, 1) if bare.search(line)]
    assert not found, found
