"""On Windows `os.replace` and `open` raise PermissionError while another process holds or is
replacing the file; `Store.write` and `Store.read` retry briefly there, and only there, so a state
write is not lost and a blocked read is not taken for a missing document."""
import builtins
import os
import threading

import pytest

from tokeneconomizer.plugin import store as store_mod
from tokeneconomizer.plugin.store import Store


def _flaky_replace(monkeypatch, failures):
    """`os.replace` that raises PermissionError `failures` times, then renames; returns the
    list of calls it saw."""
    real, calls = os.replace, []

    def replace(src, dst):
        calls.append(dst)
        if len(calls) <= failures:
            raise PermissionError(13, "The process cannot access the file", dst)
        return real(src, dst)

    monkeypatch.setattr(store_mod.os, "replace", replace)
    monkeypatch.setattr(store_mod.time, "sleep", lambda s: None)
    return calls


def test_windows_retries_a_replace_blocked_by_an_open_file(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "_WINDOWS", True)
    calls = _flaky_replace(monkeypatch, failures=2)
    store = Store(tmp_path)
    store.write("state.json", {"new": True})
    assert len(calls) == 3
    assert store.read("state.json") == {"new": True}
    assert not list(tmp_path.glob("*.tmp"))


def test_windows_gives_up_after_a_bounded_number_of_tries(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "_WINDOWS", True)
    store = Store(tmp_path)
    store.write("state.json", {"old": True})
    calls = _flaky_replace(monkeypatch, failures=10 ** 6)
    with pytest.raises(PermissionError):
        store.write("state.json", {"new": True})
    assert len(calls) == len(store_mod._REPLACE_BACKOFF) + 1
    assert store.read("state.json") == {"old": True}
    assert not list(tmp_path.glob("*.tmp"))


def test_posix_does_not_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "_WINDOWS", False)
    calls = _flaky_replace(monkeypatch, failures=1)
    with pytest.raises(PermissionError):
        Store(tmp_path).write("state.json", {"new": True})
    assert len(calls) == 1


def _flaky_open(monkeypatch, store, failures):
    """`open` that refuses the state file `failures` times; returns the list of tries."""
    real, calls = builtins.open, []
    target = store.path("state.json")

    def fake(path, *a, **k):
        if path == target:
            calls.append(path)
            if len(calls) <= failures:
                raise PermissionError(13, "The process cannot access the file", path)
        return real(path, *a, **k)

    monkeypatch.setattr(store_mod, "open", fake, raising=False)
    monkeypatch.setattr(store_mod.time, "sleep", lambda s: None)
    return calls


def test_windows_retries_a_read_blocked_by_another_process(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "_WINDOWS", True)
    store = Store(tmp_path)
    store.write("state.json", {"old": True})
    calls = _flaky_open(monkeypatch, store, failures=2)
    assert store.read("state.json", {}) == {"old": True}
    assert len(calls) == 3


def test_a_read_that_stays_blocked_raises_rather_than_reads_as_empty(tmp_path, monkeypatch):
    """Taken for a missing document, the block would hand the caller a fresh state, and its next
    write would replace the real one."""
    monkeypatch.setattr(store_mod, "_WINDOWS", True)
    store = Store(tmp_path)
    store.write("state.json", {"old": True})
    calls = _flaky_open(monkeypatch, store, failures=10 ** 6)
    with pytest.raises(PermissionError):
        store.read("state.json", {})
    assert len(calls) == len(store_mod._REPLACE_BACKOFF) + 1


def test_posix_does_not_retry_a_read(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "_WINDOWS", False)
    store = Store(tmp_path)
    store.write("state.json", {"old": True})
    calls = _flaky_open(monkeypatch, store, failures=1)
    with pytest.raises(PermissionError):
        store.read("state.json", {})
    assert len(calls) == 1


@pytest.mark.parametrize("content", [None, b"{not json", b"\xff\xfe"],
                         ids=["missing", "not JSON", "not text"])
def test_a_missing_or_broken_document_reads_as_the_default(tmp_path, content):
    if content is not None:
        (tmp_path / "state.json").write_bytes(content)
    assert Store(tmp_path).read("state.json", {"default": True}) == {"default": True}


@pytest.mark.skipif(os.name != "nt",
                    reason="Windows refuses to replace a file another handle has open")
def test_a_write_lands_once_a_reader_lets_go_of_the_file(tmp_path):
    """The real case: another process (here a thread) is reading the state file when the write
    comes; the write waits it out instead of being lost."""
    store = Store(tmp_path)
    store.write("state.json", {"old": True})
    held = open(store.path("state.json"), encoding="utf-8")
    threading.Timer(0.05, held.close).start()
    try:
        store.write("state.json", {"new": True})
    finally:
        held.close()
    assert store.read("state.json") == {"new": True}
    assert not list(tmp_path.glob("*.tmp"))
