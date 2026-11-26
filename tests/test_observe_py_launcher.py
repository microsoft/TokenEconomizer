"""The Windows `py` launcher is the Python interpreter.

`py` runs the default Python, or the one a `-V:<tag>` or `-3.x` option chooses ("Using Python on
Windows", CPython's documentation: "To launch your default runtime, run `python` or `py`"). The
observer read `python.exe` and `C:\\Python312\\python.exe` as the interpreter, but not `py`, so a
repository whose tests run as `py -m pytest` never got a fact about them. The version option
stays in the form: `py -3.12 -m pytest` and `py -m pytest` can run different Pythons."""
import pytest

from fixtures import write_codex_transcript
from tokeneconomizer.microskills import EnvFactsDistiller, observe


def _seen(cmd):
    return [(o.intent, o.form) for o in observe.classify_command(cmd)]


@pytest.mark.parametrize("cmd, seen", [
    ("py -m pytest -q", [("run_tests", "py -m pytest")]),
    ("py -3.12 -m pytest tests", [("run_tests", "py -3.12 -m pytest")]),
    ("py -V:3.12 -m unittest discover", [("run_tests", "py -V:3.12 -m unittest")]),
    ("py.exe -m pytest", [("run_tests", "py.exe -m pytest")]),
    (r"C:\Windows\py.exe -3 -m pytest", [("run_tests", r"C:\Windows\py.exe -3 -m pytest")]),
    ("py runtests.py", [("run_tests", "py runtests.py")]),
    ("py repro.py", [("run_interpreter", "py")]),
    ("py -3.12 --version", [("probe_env", "py -3.12")]),
])
def test_the_py_launcher_is_read_as_the_interpreter(cmd, seen):
    assert _seen(cmd) == seen


@pytest.mark.parametrize("cmd, seen", [
    ("py.test -q", [("run_tests", "py.test")]),                 # pytest's own name
    ("pyright src", []),
    ("python -m pytest", [("run_tests", "python -m pytest")]),
])
def test_other_names_are_read_as_before(cmd, seen):
    assert _seen(cmd) == seen


def test_the_learned_fact_names_the_launcher_and_its_version(tmp_path):
    """Sessions where `python -m pytest` fails and `py -3.12 -m pytest` passes: the fact injected
    into the next session names the launcher with its version."""
    def cmd(command, output, code):
        return {"type": "command_execution", "command": command,
                "aggregated_output": output, "exit_code": code}

    d = EnvFactsDistiller("acme/widget")
    for i in range(4):
        p = str(tmp_path / f"t{i}.jsonl")
        write_codex_transcript(p, [
            {"type": "file_change", "changes": [{"path": f"pkg/m{i}.py", "kind": "update"}]},
            cmd("python -m pytest tests -q", r"C:\Python39\python.exe: No module named pytest", 1),
            cmd("py -3.12 -m pytest tests -q", "1 passed in 0.2s", 0)])
        d.render()
        d.update(p, iid=f"w-{i}", resolved=True)
    assert "py -3.12 -m pytest" in d.render()
