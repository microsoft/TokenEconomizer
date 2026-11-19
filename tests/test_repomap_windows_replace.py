"""`repomap._write_state` goes through `store._replace`: on Windows a state file another process
holds is retried briefly instead of crashing the refresh, and no `.tmp` is left behind."""
import os
from types import SimpleNamespace

import pytest

from tokeneconomizer.plugin import repomap
from tokeneconomizer.plugin import store as store_mod


def _ctx(tmp_path):
    base = tmp_path / "repo" / "store"
    base.mkdir(parents=True)
    return SimpleNamespace(store=SimpleNamespace(base=str(base)))


def _flaky_replace(monkeypatch, failures):
    real, calls = os.replace, []

    def replace(src, dst):
        calls.append(dst)
        if len(calls) <= failures:
            raise PermissionError(13, "Access is denied", dst)
        return real(src, dst)

    monkeypatch.setattr(store_mod.os, "replace", replace)
    monkeypatch.setattr(store_mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(store_mod, "_WINDOWS", True)
    return calls


def test_a_state_write_blocked_once_is_retried_and_lands(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    calls = _flaky_replace(monkeypatch, failures=1)
    repomap._write_state(ctx, {"map": "x"})
    assert len(calls) == 2
    assert repomap.read_state(ctx) == {"map": "x"}
    assert not list((tmp_path / "repo").glob("*.tmp"))


def test_a_state_write_that_stays_blocked_raises_and_leaves_no_tmp(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    repomap._write_state(ctx, {"map": "old"})
    calls = _flaky_replace(monkeypatch, failures=10 ** 6)
    with pytest.raises(PermissionError):
        repomap._write_state(ctx, {"map": "new"})
    assert len(calls) == len(store_mod._REPLACE_BACKOFF) + 1
    assert repomap.read_state(ctx) == {"map": "old"}
    assert not list((tmp_path / "repo").glob("*.tmp"))
