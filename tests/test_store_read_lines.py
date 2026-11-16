"""Store.read_lines: only JSON-object records are returned, and `limit` counts those."""
import os

from tokeneconomizer.plugin import accounting
from tokeneconomizer.plugin.store import Store


def _write(store, name, lines):
    os.makedirs(store.base, exist_ok=True)
    with open(store.path(name), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def test_read_lines_skips_non_object_json(tmp_path):
    s = Store(str(tmp_path / "st"))
    _write(s, "ledger.jsonl", ['{"n": 1}', "null", "[]", '"str"', "3", '{"n": 2}', "null"])
    assert s.read_lines("ledger.jsonl") == [{"n": 1}, {"n": 2}]


def test_read_lines_limit_counts_only_object_records(tmp_path):
    s = Store(str(tmp_path / "st"))
    _write(s, "ledger.jsonl", ['{"n": 1}', '{"n": 2}', "null"])
    assert s.read_lines("ledger.jsonl", limit=1) == [{"n": 2}]


def test_summarize_tolerates_non_object_records(tmp_path):
    s = Store(str(tmp_path / "st"))
    valid = {"kind": "session", "session_id": "a", "imported": False}
    _write(s, "ledger.jsonl", ["null", "[]", '"str"', '{"kind": "session", "session_id": "a", '
                                                       '"imported": false}'])
    assert accounting.summarize(s.read_lines("ledger.jsonl")) == accounting.summarize([valid])
    assert accounting.summarize([None, [], "str", valid]) == accounting.summarize([valid])
