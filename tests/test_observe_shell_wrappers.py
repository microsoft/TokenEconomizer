"""A command recorded inside a shell wrapper is observed by the command it wraps.

Codex records each shell call as `<shell> -lc '<script>'`: the exec stream and a rollout's argv,
joined with `shlex.join`, name the shell by name or by path (`bash`, `/bin/bash`, and the user's
`/bin/zsh` on macOS). The loop guard reads any of them by its script (`loopguard.turn._pieces`);
the observer read only `/bin/bash` and `/bin/sh`, so a session recorded any other way showed it
no test run, no interpreter run and no package tool, and taught it nothing."""
import json
import shlex

import pytest

from tokeneconomizer import trace
from tokeneconomizer.loopguard import turn
from tokeneconomizer.microskills import EnvFactsDistiller, observe

WRAPPERS = [
    ["bash", "-lc"],
    ["/bin/zsh", "-lc"],
    ["zsh", "-lc"],
    ["/usr/bin/bash", "-lc"],
    ["dash", "-c"],
    ["/bin/dash", "-c"],
]


def _seen(cmd):
    return [(o.intent, o.form) for o in observe.classify_command(cmd)]


@pytest.mark.parametrize("wrapper", WRAPPERS, ids=lambda w: " ".join(w))
@pytest.mark.parametrize("script, seen", [
    ("pytest -q", [("run_tests", "pytest")]),
    ("python -m pytest -q tests", [("run_tests", "python -m pytest")]),
    ("pip install -e .", [("fetch_remote", "pip install")]),
    ("cd tests && python runtests.py", [("run_tests", "cd tests && python runtests.py")]),
])
def test_a_wrapped_command_is_observed_by_its_script(wrapper, script, seen):
    cmd = shlex.join([*wrapper, script])
    assert observe.unwrap(cmd) == script
    assert _seen(cmd) == seen


def test_a_script_with_quotes_is_unwrapped_whole():
    """`shlex.join` quotes a script holding a `'` as `'"'"'`: the script comes back as typed."""
    script = "pytest -k 'parse and not slow' -q"
    cmd = shlex.join(["/bin/zsh", "-lc", script])
    assert cmd == "/bin/zsh -lc 'pytest -k '\"'\"'parse and not slow'\"'\"' -q'"
    assert observe.unwrap(cmd) == script
    assert _seen(cmd) == [("run_tests", "pytest")]


@pytest.mark.parametrize("wrapper", WRAPPERS, ids=lambda w: " ".join(w))
def test_the_observer_and_the_loop_guard_read_the_same_wrappers(wrapper):
    cmd = shlex.join([*wrapper, "pytest -q"])
    assert turn.status_of_tests(cmd) == "own"
    assert _seen(cmd) == [("run_tests", "pytest")]


@pytest.mark.parametrize("cmd, unwrapped", [
    ('/bin/bash -lc "pytest -q"', "pytest -q"),
    ("/bin/bash -lc pytest -q", "pytest -q"),
    ("bash run_tests.sh", "bash run_tests.sh"),
    ("bash -n check.sh", "bash -n check.sh"),
    ("sh ./configure", "sh ./configure"),
    ("zsh -lc", "zsh -lc"),
    ("python -c 'print(1)'", "python -c 'print(1)'"),
])
def test_anything_else_is_read_as_before(cmd, unwrapped):
    assert observe.unwrap(cmd) == unwrapped


def test_a_rollout_recorded_through_zsh_teaches_a_fact(tmp_path):
    """Four Codex sessions on macOS, each recorded through `/bin/zsh -lc`, where `pytest` is not
    on PATH and `python -m pytest` passes: the fact injected into the next session names the
    working form."""
    d = EnvFactsDistiller("acme/widget")
    for i in range(4):
        rollout = tmp_path / f"rollout-{i}.jsonl"
        records = [{"type": "session_meta", "payload": {"id": f"s{i}", "cwd": "/work/widget"}}]
        for n, (script, output, code) in enumerate([
                ("pytest -q", "zsh:1: command not found: pytest", 127),
                ("python -m pytest -q", "3 passed in 0.2s", 0)]):
            records.append({"type": "event_msg", "payload": {"type": "item_completed", "item": {
                "type": "CommandExecution", "id": f"c{n}",
                "command": ["/bin/zsh", "-lc", script],
                "aggregated_output": output, "exit_code": code}}})
        records.append({"type": "event_msg", "payload": {"type": "item_completed", "item": {
            "type": "FileChange", "id": "f1",
            "changes": {f"pkg/mod{i}.py": {"type": "update"}}}}})
        rollout.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        stream = tmp_path / f"exec-{i}.jsonl"
        assert trace.codex_rollout_to_exec(str(rollout), str(stream)) == 3
        d.render()
        d.update(str(stream), iid=f"w-{i}", resolved=True)
    block = d.render()
    assert "`pytest …` fails here with command not found" in block
    assert "`python -m pytest …` works" in block
