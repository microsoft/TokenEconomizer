"""load_calls: malformed JSONL lines and malformed inner blocks are skipped, never crash."""
import json

import pytest

from tokeneconomizer.microskills.distiller import load_calls


def _use(tid, name="Bash", inp=None):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tid, "name": name, "input": {"command": "pytest"} if inp is None else inp}]}}


def _result(tid, content):
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": tid, "content": content}]}}


def _write(path, lines):
    with open(path, "w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(line if isinstance(line, str) else json.dumps(line))
            fh.write("\n")
    return str(path)


def test_non_object_json_lines_are_skipped(tmp_path):
    good = [_use("t1"), _result("t1", [{"type": "text", "text": "1 passed"}])]
    lines = ["null", "[]", '"str"', "123", "true"] + good
    calls = load_calls(_write(tmp_path / "t.jsonl", lines))
    assert calls == [("bash", {"command": "pytest"}, "1 passed")]


@pytest.mark.parametrize("bad_id", [[], {}, ["t1"], {"x": 1}, None])
def test_non_string_tool_use_id_is_skipped(tmp_path, bad_id):
    lines = [_use(bad_id), _use("t2", "Read", {"file_path": "a.py"})]
    calls = load_calls(_write(tmp_path / "t.jsonl", lines))
    assert calls == [("read", {"file_path": "a.py"}, "")]


@pytest.mark.parametrize("bad_id", [[], {}, ["t1"], {"x": 1}, None])
def test_non_string_tool_result_id_is_ignored(tmp_path, bad_id):
    lines = [_use("t1"), _result(bad_id, [{"type": "text", "text": "x"}])]
    calls = load_calls(_write(tmp_path / "t.jsonl", lines))
    assert calls == [("bash", {"command": "pytest"}, "")]


def test_null_or_non_string_tool_name_does_not_crash(tmp_path):
    lines = [_use("t1", name=None), _use("t2", name=42), _use("t3", name=["x"])]
    calls = load_calls(_write(tmp_path / "t.jsonl", lines))
    assert [c[0] for c in calls] == ["", "", ""]


@pytest.mark.parametrize("bad_text", [None, 123, ["a"], {"k": "v"}, True])
def test_non_string_text_block_does_not_crash(tmp_path, bad_text):
    content = [{"type": "text", "text": bad_text}, {"type": "text", "text": "ok"}]
    lines = [_use("t1"), _result("t1", content)]
    calls = load_calls(_write(tmp_path / "t.jsonl", lines))
    assert calls[0][2].endswith("ok")


def test_non_dict_items_in_tool_result_content_are_skipped(tmp_path):
    content = ["stray", 3, None, {"type": "text", "text": "kept"}]
    lines = [_use("t1"), _result("t1", content)]
    calls = load_calls(_write(tmp_path / "t.jsonl", lines))
    assert calls[0][2] == "kept"


def test_non_dict_tool_input_becomes_empty_args(tmp_path):
    lines = [_use("t1", inp=["pytest"]), _use("t2", inp="pytest")]
    calls = load_calls(_write(tmp_path / "t.jsonl", lines))
    assert [c[1] for c in calls] == [{}, {}]
