"""Claude Code on Windows runs commands through its `PowerShell` tool, not `Bash`.

The tool's input is `{"command": ...}`, as Bash's is, so the mechanisms read it as shell text:
micro-skills learn from it, the loop guard counts it, and tool pruning never proposes it."""
from fixtures import write_claude_transcript
from tokeneconomizer.loopguard.state import canon_tool
from tokeneconomizer.microskills import EnvFactsDistiller
from tokeneconomizer.microskills.distiller import bash_cmds
from tokeneconomizer.plugin.adapter import INTERACTIVE_PROTECTED
from tokeneconomizer.toolprune.tools import PROTECTED_TOOLS
from tokeneconomizer.trace import SHELL_TOOLS

PROJ = "C:/repo/.venv/Scripts/python.exe"


def _session(tmp_path, i):
    p = str(tmp_path / f"t{i}.jsonl")
    return write_claude_transcript(p, [
        ("Edit", {"file_path": f"pkg/mod{i}.py", "old_string": "a", "new_string": "b"}, "ok"),
        ("PowerShell", {"command": "python -m pytest tests -q"},
         "C:\\Python312\\python.exe: No module named pytest"),
        ("PowerShell", {"command": f"{PROJ} -m pytest tests -q"}, "1 passed in 0.2s"),
    ])


def test_the_powershell_tool_is_a_shell_tool():
    assert "powershell" in SHELL_TOOLS
    assert canon_tool("PowerShell") == "bash"
    assert bash_cmds([("powershell", {"command": "git status"}, "clean")]) == [
        ("git status", "clean")]


def test_a_fact_forms_from_powershell_sessions(tmp_path):
    d = EnvFactsDistiller("acme/widget")
    for i in range(4):
        d.render()
        d.update(_session(tmp_path, i), iid=f"w-{i}", resolved=True)
    text = d.render()
    assert PROJ in text and "python -m pytest" in text


def test_the_powershell_tool_is_never_pruned():
    assert "PowerShell" in PROTECTED_TOOLS
    assert "PowerShell" in INTERACTIVE_PROTECTED


def test_a_windows_interpreter_path_is_the_interpreter():
    from tokeneconomizer.microskills import observe
    for cmd in (".\\.venv\\Scripts\\python.exe -m pytest tests",
                "C:\\Python312\\python.exe -m pytest", "python.exe -m pytest"):
        assert [o.intent for o in observe.classify_command(cmd)] == ["run_tests"], cmd
