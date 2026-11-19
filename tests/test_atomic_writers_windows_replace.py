"""The atomic writers outside `Store` go through the same Windows retry as `Store.write`: a
`PermissionError` from `os.replace` (another process has the target open) is retried briefly, and a
temporary file never outlives a write, whether it landed or failed."""
import json
import os

import pytest

from tokeneconomizer.plugin import config, denylist, hookqueue, repos
from tokeneconomizer.plugin import store as store_mod
from tokeneconomizer.plugin.store import Store


def _flaky_replace(monkeypatch, failures):
    real, calls = os.replace, []

    def replace(src, dst):
        calls.append(dst)
        if len(calls) <= failures:
            raise PermissionError(13, "The process cannot access the file", dst)
        return real(src, dst)

    monkeypatch.setattr(store_mod.os, "replace", replace)
    monkeypatch.setattr(store_mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(store_mod, "_WINDOWS", True)
    return calls


def _config(d):
    p = d / "cfg" / "config.json"
    return (lambda: config._write(str(p), {"a": 1}),
            lambda: json.loads(p.read_text(encoding="utf-8")) == {"a": 1}, p.parent)


def _deny_json(d):
    p = d / "s" / "settings.json"
    return (lambda: denylist.write_json(str(p), {"a": 1}),
            lambda: json.loads(p.read_text(encoding="utf-8")) == {"a": 1}, p.parent)


def _deny_text(d):
    p = d / "s" / "config.toml"
    return (lambda: denylist._write_text(str(p), "x = 1\n"),
            lambda: p.read_text(encoding="utf-8") == "x = 1\n", p.parent)


def _lines(d):
    st = Store(d / "st")
    return (lambda: repos._write_lines(st, "log.jsonl", [{"a": 1}]),
            lambda: (d / "st" / "log.jsonl").read_text(encoding="utf-8") == '{"a": 1}\n',
            d / "st")


def _queue(d):
    q = d / "q"
    box = {}
    return (lambda: box.setdefault("ok", hookqueue.enqueue(str(q), {"a": 1})),
            lambda: box["ok"] is True and [json.loads(f.read_text(encoding="utf-8"))
                                           for f in q.glob("*.json")] == [{"a": 1}], q)


CASES = [_config, _deny_json, _deny_text, _lines, _queue]


@pytest.mark.parametrize("make", CASES)
def test_a_blocked_replace_is_retried_and_leaves_no_temporary_file(tmp_path, monkeypatch, make):
    write, landed, folder = make(tmp_path)
    calls = _flaky_replace(monkeypatch, failures=2)
    write()
    assert len(calls) == 3
    assert landed()
    assert not list(folder.glob("*.tmp"))


@pytest.mark.parametrize("make", [_config, _deny_json, _deny_text, _lines])
def test_a_replace_that_keeps_failing_raises_and_leaves_no_temporary_file(
        tmp_path, monkeypatch, make):
    write, _, folder = make(tmp_path)
    calls = _flaky_replace(monkeypatch, failures=10 ** 6)
    with pytest.raises(PermissionError):
        write()
    assert len(calls) == len(store_mod._REPLACE_BACKOFF) + 1
    assert not list(folder.glob("*.tmp"))


def test_the_hook_queue_reports_failure_and_leaves_no_temporary_file(tmp_path, monkeypatch):
    calls = _flaky_replace(monkeypatch, failures=10 ** 6)
    assert hookqueue.enqueue(str(tmp_path / "q"), {"a": 1}) is False
    assert len(calls) == len(store_mod._REPLACE_BACKOFF) + 1
    assert not list((tmp_path / "q").iterdir())


@pytest.mark.parametrize("make", [_config, _deny_json, _deny_text, _lines, _queue])
def test_posix_does_not_retry(tmp_path, monkeypatch, make):
    write, _, folder = make(tmp_path)
    calls = _flaky_replace(monkeypatch, failures=1)
    monkeypatch.setattr(store_mod, "_WINDOWS", False)
    try:
        write()
    except PermissionError:
        pass
    assert len(calls) == 1
    assert not list(folder.glob("*.tmp"))
