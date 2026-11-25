"""`tool_roles`: a person gives a tool the mechanisms do not know by name the role of a
built-in one, so an MCP server's edit tool counts as an edit and its shell tool's commands are
read as shell text."""
import pytest

from fixtures import write_claude_transcript
from tokeneconomizer.loopguard.state import SpiralState, canon_tool
from tokeneconomizer.microskills import observe
from tokeneconomizer.plugin.config import LiveConfig
from tokeneconomizer.trace import parse_tool_role, set_tool_roles, tool_kind

SERENA = ["mcp__serena__replace_content=edit", "mcp__serena__replace_symbol_body=edit",
          "mcp__serena__insert_*_symbol=edit",
          "mcp__serena__execute_shell_command=shell", "mcp__serena__read_file=read"]


@pytest.fixture
def roles():
    set_tool_roles(SERENA)
    yield
    set_tool_roles(())


def test_an_entry_names_a_tool_and_a_known_role():
    assert parse_tool_role("mcp__serena__replace_content=edit") == (
        "mcp__serena__replace_content", "edit")
    assert parse_tool_role(" mcp__X__* = Shell ") == ("mcp__x__*", "shell")
    for bad in ("mcp__serena__replace_content", "=edit", "mcp__x__y=write"):
        assert parse_tool_role(bad) is None


def test_the_setting_rejects_a_malformed_entry():
    c = LiveConfig()
    assert c.set("tool_roles", ",".join(SERENA))
    assert c.tool_roles == SERENA
    assert not c.set("tool_roles", "mcp__serena__replace_content=write")
    assert c.tool_roles == SERENA and "tool_roles" in c.warnings[-1]


def test_without_a_role_a_tool_reads_as_itself():
    set_tool_roles(())
    assert tool_kind("mcp__serena__replace_content") == "mcp__serena__replace_content"
    assert canon_tool("mcp__serena__replace_content") == "mcp__serena__replace_content"


def test_a_role_maps_to_the_built_in_tool(roles):
    assert tool_kind("mcp__serena__replace_content") == "edit"
    assert tool_kind("mcp__serena__replace_symbol_body") == "edit"
    assert tool_kind("mcp__serena__insert_after_symbol") == "edit"       # the pattern matches
    assert canon_tool("mcp__serena__execute_shell_command") == "bash"
    assert canon_tool("mcp__serena__read_file") == "view"
    assert (tool_kind("mcp__serena__find_referencing_symbols")
            == "mcp__serena__find_referencing_symbols")
    assert tool_kind("mcp__serena__list_dir") == "mcp__serena__list_dir"


def test_the_loop_guard_counts_an_mcp_edit(roles):
    s = SpiralState()
    for _ in range(3):
        s.update("mcp__serena__replace_content",
                 {"relative_path": "src/app.py", "needle": "a", "repl": "b"}, "OK")
    assert s.edit_counts == {"app.py": 3}


def test_micro_skills_read_an_mcp_shell_tool_and_edit_tool(roles, tmp_path):
    p = write_claude_transcript(str(tmp_path / "t.jsonl"), [
        ("mcp__serena__replace_content", {"relative_path": "pkg/mod.py"}, "OK"),
        ("mcp__serena__execute_shell_command", {"command": "uv run pytest -q"}, "1 passed"),
    ])
    digest = observe.observe_task(p)
    assert digest["edited"] == ["mod.py"]
    assert [o.intent for o in digest["obs"]] == ["run_tests"]
