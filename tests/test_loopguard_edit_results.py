"""A Claude Code edit that applied is not an error output, whatever the file says (#151).

The result of an Edit, MultiEdit or Write that came back as PostToolUse carries the file's text
(`originalFile`, `newString`, a Write's `content`), not an outcome; an edit that did not apply
comes as PostToolUseFailure. Edit and Write results are shaped as Claude Code 2.1 logs them
(`toolUseResult`)."""
from tokeneconomizer.loopguard.turn import TurnGuard
from tokeneconomizer.plugin import hosts

DOC = ("## Checks\n\nThe failed case asserts the value; a FAILED run names the AssertionError.\n"
       "assert total == 3\n")


def _success(name, args, response):
    return {"hook_event_name": "PostToolUse", "tool_name": name, "tool_input": args,
            "tool_response": response}


EDIT = _success("Edit", {"file_path": "/r/docs/checks.md", "old_string": "value",
                         "new_string": "value; assert it"},
                {"filePath": "/r/docs/checks.md", "oldString": "value",
                 "newString": "value; assert it", "originalFile": DOC,
                 "structuredPatch": [{"oldStart": 3, "oldLines": 1, "newStart": 3,
                                      "newLines": 1, "lines": ["-" + DOC, "+" + DOC]}],
                 "replaceAll": False, "userModified": False})
MULTI_EDIT = _success("MultiEdit", {"file_path": "/r/docs/checks.md", "edits": []},
                      {"filePath": "/r/docs/checks.md", "edits": [],
                       "originalFileContents": DOC, "structuredPatch": [],
                       "userModified": False})
WRITE = _success("Write", {"file_path": "/r/docs/checks.md", "content": DOC},
                 {"type": "update", "filePath": "/r/docs/checks.md", "content": DOC,
                  "originalFile": DOC, "structuredPatch": [], "userModified": False})


def test_a_claude_code_edit_that_came_back_succeeded():
    h = hosts.ClaudeCode()
    for p in (EDIT, MULTI_EDIT, WRITE):
        name, _, text, code = h.tool_call(p)
        assert "assert" in text                         # the file's text is what is folded
        assert code == 0, name


def test_edits_to_a_file_that_mentions_failures_are_not_error_outputs():
    h, g = hosts.ClaudeCode(), TurnGuard()
    for p in (EDIT, EDIT, MULTI_EDIT, WRITE):
        g.fold(*h.tool_call(p))
    assert g.whole.error_outputs == 0 and g.whole.repeated_fail_sigs == 0
    assert not any("[err#" in line for line in g.whole.recent)
    assert g.whole.edit_counts["checks.md"] == 4


def test_an_edit_that_did_not_apply_and_a_failing_run_still_count():
    h, g = hosts.ClaudeCode(), TurnGuard()
    g.fold(*h.tool_call({"hook_event_name": "PostToolUseFailure", "tool_name": "Edit",
                         "tool_input": EDIT["tool_input"],
                         "error": "<tool_use_error>String to replace not found in file.\n"
                                  "String: value</tool_use_error>"}))
    g.fold(*h.tool_call({"hook_event_name": "PostToolUseFailure", "tool_name": "Bash",
                         "tool_input": {"command": "pytest -q"},
                         "error": "Exit code 1\nFAILED tests/test_a.py::test_x - "
                                  "AssertionError: assert 1 == 2"}))
    assert g.whole.error_outputs == 2
