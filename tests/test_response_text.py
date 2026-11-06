from tokeneconomizer.plugin.hosts import response_text

# Claude Code's Edit result: a dict with the edited file and both strings, none of them an output.
EDIT_RESULT = {
    "filePath": "/repo/NOTES.md",
    "oldString": "the failed case asserts the value",
    "newString": "the failed case asserts the value twice",
    "originalFile": "assert failed: FAILED 3 failures",
    "structuredPatch": [],
    "replaceAll": False,
    "userModified": False,
}


def test_dict_without_an_output_key_has_no_text():
    # Serializing it would feed the edited file's prose to the loop guard as if it were output.
    assert response_text(EDIT_RESULT) == ""


def test_dict_output_keys_are_still_read():
    assert response_text({"stdout": "ok", "stderr": "boom"}) == "ok\nboom"
