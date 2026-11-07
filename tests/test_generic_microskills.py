"""Micro-skills read generically in the plugin: any language or toolchain, builds and checks as
well as tests, every edited file -- while the experiments keep the paper's reading."""
import hashlib
import json

import pytest

from tokeneconomizer.microskills import GENERIC, NODE_WEB, PYTHON_GIT, observe
from tokeneconomizer.microskills import distiller as dist
from tokeneconomizer.microskills import envfacts as ef
from tokeneconomizer.plugin.adapter import detect_vocabulary
from fixtures import write_claude_transcript


def _form(cmd, vocab):
    obs = observe.classify_command(cmd, vocab=vocab)
    return (obs[0].intent, obs[0].form) if obs else None


def test_the_generic_reading_knows_builds_and_other_ecosystems():
    assert _form("npm run build", GENERIC) == ("run_build", "npm run build")
    assert _form("npm run test:unit", GENERIC) == ("run_tests", "npm run test:unit")
    assert _form("cargo build --release", GENERIC) == ("run_build", "cargo build")
    assert _form("tsc -p .", GENERIC) == ("run_build", "tsc")
    assert _form("go test ./...", GENERIC) == ("run_tests", "go test")
    assert _form("./gradlew test", GENERIC) == ("run_tests", "gradlew test")
    assert _form("bundle install", GENERIC) == ("package_tool", "bundle")
    # The experiments' vocabularies read as they did.
    assert _form("tsc -p .", PYTHON_GIT) is None and _form("npm run build", NODE_WEB) == (
        "package_tool", "npm")
    assert _form("./gradlew test", PYTHON_GIT) is None


def _session(tmp_path, i, calls):
    return write_claude_transcript(str(tmp_path / f"s{i}.jsonl"), calls)


def test_a_build_tool_repair_becomes_a_fact(tmp_path):
    """Not a test command at all: `tsc` missing, `npx tsc` working, session after session."""
    d = ef.EnvFactsDistiller("acme/web", vocab=GENERIC)
    for i in range(6):
        d.update(_session(tmp_path, i, [
            ("Edit", {"file_path": f"/r/src/m{i}.ts", "old_string": "a", "new_string": "b"}, "ok"),
            ("Bash", {"command": "tsc -p ."}, "Exit code 127\nbash: tsc: command not found"),
            ("Bash", {"command": "npx tsc -p ."}, "")]), iid=f"s{i}")
    text = d.render()
    assert text.startswith(ef.GENERIC_HEADER)
    assert "`tsc …` fails here with command not found; `npx tsc …` works." in text


def test_an_absent_linter_becomes_a_fact_and_a_working_form_does_not(tmp_path):
    """`ruff` missing with no working form: an absent_tool fact. `tsc` missing with `npx tsc`
    working is still a FormRepair, and `make build` never repairs `ruff`."""
    d = ef.EnvFactsDistiller("acme/py", vocab=GENERIC)
    for i in range(4):
        d.render()
        d.update(_session(tmp_path, i, [
            ("Edit", {"file_path": f"/r/pkg/m{i}.py", "old_string": "a", "new_string": "b"}, "ok"),
            ("Bash", {"command": "ruff check ."}, "Exit code 127\nbash: ruff: command not found"),
            ("Bash", {"command": "make build"}, "built")]), iid=f"a{i}")
    f = d.facts["absent_tool:ruff"]
    assert f.family == "absent_tool" and f.state == "ACTIVE"
    assert "`ruff …` fails here with command not found" in d.render()
    assert not [x for x in d.facts if x.startswith("form_repair") and "ruff" in x]


def test_an_absent_tool_retires_when_it_runs(tmp_path):
    d = ef.EnvFactsDistiller("acme/py", vocab=GENERIC)
    for i in range(4):
        d.render()
        d.update(_session(tmp_path, i, [
            ("Edit", {"file_path": f"/r/pkg/m{i}.py", "old_string": "a", "new_string": "b"}, "ok"),
            ("Bash", {"command": "ruff check ."}, "Exit code 127\nbash: ruff: command not found")]),
            iid=f"a{i}")
    assert d.facts["absent_tool:ruff"].state == "ACTIVE"
    d.render()
    d.update(_session(tmp_path, 9, [
        ("Edit", {"file_path": "/r/pkg/z.py", "old_string": "a", "new_string": "b"}, "ok"),
        ("Bash", {"command": "ruff check ."}, "All checks passed!")]), iid="a9")
    assert d.facts["absent_tool:ruff"].state == "RETIRED"


def test_a_program_missing_inside_make_is_not_learned_as_make_missing(tmp_path):
    """`make` ran, and its recipe found no `cc`: the not-found line names `cc`, so nothing is
    learned about `make`. A `ruff` whose own not-found line names it still is."""
    d = ef.EnvFactsDistiller("acme/c", vocab=GENERIC)
    for i in range(4):
        d.render()
        d.update(_session(tmp_path, i, [
            ("Edit", {"file_path": f"/r/src/m{i}.c", "old_string": "a", "new_string": "b"}, "ok"),
            ("Bash", {"command": "make"},
             "Exit code 2\ncc -o app main.c\n/bin/sh: 1: cc: not found\n"
             "make: *** [Makefile:2: app] Error 127"),
            ("Bash", {"command": "ruff check ."}, "Exit code 127\n/bin/sh: 1: ruff: not found")]),
            iid=f"m{i}")
    assert "absent_tool:make" not in d.facts
    assert d.facts["absent_tool:ruff"].state == "ACTIVE"


def test_an_injected_absent_tool_counts_only_sessions_that_ran_a_check(tmp_path):
    """A session that never needed a check says nothing about `ruff`; one that ran another
    check (`mypy`) did without it."""
    d = ef.EnvFactsDistiller("acme/py", vocab=GENERIC)
    for i in range(4):
        d.render()
        d.update(_session(tmp_path, i, [
            ("Edit", {"file_path": f"/r/pkg/m{i}.py", "old_string": "a", "new_string": "b"}, "ok"),
            ("Bash", {"command": "ruff check ."}, "Exit code 127\nbash: ruff: command not found")]),
            iid=f"a{i}")
    assert "`ruff …` fails here with command not found" in d.render()
    d.update(_session(tmp_path, 8, [
        ("Edit", {"file_path": "/r/pkg/y.py", "old_string": "a", "new_string": "b"}, "ok"),
        ("Bash", {"command": "ls"}, "pkg")]), iid="a8")
    assert d.last_digest["verdicts"]["absent_tool:ruff"] == (None, False, False)
    d.render()
    d.update(_session(tmp_path, 9, [
        ("Edit", {"file_path": "/r/pkg/z.py", "old_string": "a", "new_string": "b"}, "ok"),
        ("Bash", {"command": "mypy ."}, "Success: no issues found in 3 source files")]),
        iid="a9")
    assert d.last_digest["verdicts"]["absent_tool:ruff"] == ("confirm", True, True)


def test_module_and_launcher_runs_name_the_tool_they_run():
    """`python -m mypy`, `uvx ruff` and `pipx run black` run the tool, not the interpreter or
    the launcher, so they pool with the bare `mypy`, `ruff`, `black`."""
    assert _form("python -m mypy .", GENERIC) == ("run_build", "python -m mypy")
    assert _form("python3 -m ruff check .", GENERIC) == ("run_build", "python3 -m ruff")
    assert _form("uvx ruff check .", GENERIC) == ("run_build", "uvx ruff")
    assert _form("uv tool run ruff check .", GENERIC) == ("run_build", "uvx ruff")
    assert _form("pipx run black .", GENERIC) == ("run_build", "pipx run black")
    assert _form("uvx pytest -q", GENERIC) == ("run_tests", "uvx pytest")
    assert _form("cd pkg && python -m mypy .", GENERIC) == ("run_build", "cd pkg && python -m mypy")
    # A module that is not a checker is still the interpreter.
    assert _form("python -m json.tool f.json", GENERIC) == ("run_interpreter", "python")
    assert not observe.is_bare("uvx ruff") and not observe.is_bare("pipx run black")
    # The experiments' vocabulary reads as it did.
    assert _form("python -m mypy .", PYTHON_GIT) == ("run_interpreter", "python")
    assert _form("uvx ruff check .", PYTHON_GIT) is None
    assert _form("pipx run pytest", PYTHON_GIT) is None


REPAIRS = [("mypy .", "python -m mypy ."), ("mypy .", "python3 -m mypy ."),
           ("ruff check .", "python -m ruff check ."), ("ruff check .", "uvx ruff check ."),
           ("ruff check .", "uv tool run ruff check ."), ("black .", "pipx run black ."),
           ("ruff check .", "uv run ruff check ."), ("ruff check .", ".venv/bin/ruff check .")]


@pytest.mark.parametrize("bare,working", REPAIRS)
def test_a_tool_that_works_another_way_is_not_learned_as_absent(tmp_path, bare, working):
    """The bare tool is not found, but the same tool works through `python -m`, a launcher or
    an environment: never "`X …` fails here with command not found" on its own."""
    tool = bare.split()[0]
    d = ef.EnvFactsDistiller("acme/py", vocab=GENERIC)
    for i in range(6):
        d.render()
        d.update(_session(tmp_path, i, [
            ("Edit", {"file_path": f"/r/pkg/m{i}.py", "old_string": "a", "new_string": "b"}, "ok"),
            ("Bash", {"command": bare}, f"Exit code 127\nbash: {tool}: command not found"),
            ("Bash", {"command": working}, "All checks passed!")]), iid=f"s{i}")
    assert f"absent_tool:{tool}" not in d.facts


@pytest.mark.parametrize("working", ["python -m mypy .", "uvx mypy .", "pipx run mypy ."])
def test_the_working_form_is_named_as_the_repair(tmp_path, working):
    d = ef.EnvFactsDistiller("acme/py", vocab=GENERIC)
    for i in range(6):
        d.render()
        d.update(_session(tmp_path, i, [
            ("Edit", {"file_path": f"/r/pkg/m{i}.py", "old_string": "a", "new_string": "b"}, "ok"),
            ("Bash", {"command": "mypy ."}, "Exit code 127\nbash: mypy: command not found"),
            ("Bash", {"command": working}, "Success: no issues found in 3 source files")]),
            iid=f"s{i}")
    form = working.rsplit(" ", 1)[0]
    assert f"`mypy …` fails here with command not found; `{form} …` works." in d.render()


@pytest.mark.parametrize("working", ["python -m mypy .", "uvx mypy .", "pipx run mypy ."])
def test_an_absent_tool_retires_when_it_runs_another_way(tmp_path, working):
    d = ef.EnvFactsDistiller("acme/py", vocab=GENERIC)
    for i in range(4):
        d.render()
        d.update(_session(tmp_path, i, [
            ("Edit", {"file_path": f"/r/pkg/m{i}.py", "old_string": "a", "new_string": "b"}, "ok"),
            ("Bash", {"command": "mypy ."}, "Exit code 127\nbash: mypy: command not found")]),
            iid=f"a{i}")
    assert d.facts["absent_tool:mypy"].state == "ACTIVE"
    d.render()
    d.update(_session(tmp_path, 9, [
        ("Edit", {"file_path": "/r/pkg/z.py", "old_string": "a", "new_string": "b"}, "ok"),
        ("Bash", {"command": working}, "Success: no issues found in 3 source files")]), iid="a9")
    assert d.facts["absent_tool:mypy"].state == "RETIRED"


@pytest.mark.parametrize("failing,other", [("python -m mypy .", "python -m ruff check ."),
                                           ("uvx mypy .", "uvx ruff check ."),
                                           ("pipx run mypy .", "pipx run ruff check .")])
def test_a_shared_launcher_does_not_make_one_tool_repair_another(tmp_path, failing, other):
    """`python -m ruff` working says nothing about a `python -m mypy` that failed: they share
    only the launcher."""
    d = ef.EnvFactsDistiller("acme/py", vocab=GENERIC)
    for i in range(6):
        d.render()
        d.update(_session(tmp_path, i, [
            ("Edit", {"file_path": f"/r/pkg/m{i}.py", "old_string": "a", "new_string": "b"}, "ok"),
            ("Bash", {"command": failing}, "Exit code 1\n/usr/bin/python: No module named mypy"),
            ("Bash", {"command": other}, "All checks passed!")]), iid=f"s{i}")
    assert not [f for f in d.facts if f.startswith("form_repair")]


def test_every_edited_file_counts_in_the_generic_reading(tmp_path):
    p = _session(tmp_path, 0, [
        ("Edit", {"file_path": "/r/src/main.go", "old_string": "a", "new_string": "b"}, "ok"),
        ("Write", {"file_path": "/r/web/app.ts", "content": "x"}, "ok"),
        ("Edit", {"file_path": "/r/pkg/m.py", "old_string": "a", "new_string": "b"}, "ok")])
    assert observe.observe_task(p, GENERIC)["edited"] == ["app.ts", "m.py", "main.go"]
    assert observe.observe_task(p, PYTHON_GIT)["edited"] == ["m.py"]


def test_the_generic_digest_is_language_neutral_and_relative(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    r = str(repo)
    p = write_claude_transcript(str(tmp_path / "t.jsonl"), [
        ("Edit", {"file_path": f"{r}/src/main.ts", "old_string": "a", "new_string": "b"}, "ok", r),
        ("Bash", {"command": f"cd {r} && npm run build"},
         f"Exit code 2\n{r}/src/main.ts(3,5): error TS2304: Cannot find name 'foo'.", r),
        ("Bash", {"command": "node scripts/gen.js"},
         "Error: Cannot find module 'left-pad'\nExit code 1", r),
        ("Bash", {"command": "cargo build"}, "Finished dev", r)])
    dg = dist.build_generic_digest("acme/web", p, "s0")
    assert dg["edited_files"] == ["main.ts"]
    assert dg["check_commands"] == ["npm run build", "cargo build"]
    assert dg["failed_commands"] == [
        # line and column masked; the error's own code kept, as it names the failure
        {"cmd": "npm run build", "error": "src/main.ts(N,N): error TS2304: Cannot find name 'foo'."},
        {"cmd": "node scripts/gen.js", "error": "Error: Cannot find module 'left-pad'"}]
    assert dg["missing_dependency"] and not dg["missing_command"]
    shown = json.dumps(dist.proposer_view(dg))
    assert r not in shown and "_cmd_blob" not in shown


def test_the_plugin_reads_every_repository_generically(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    v = detect_vocabulary(str(tmp_path))
    assert v.generic and v.canonical_interpreter == "node"
    assert detect_vocabulary(str(tmp_path / "nowhere")) is GENERIC
    assert detect_vocabulary(str(tmp_path), "python-git") is PYTHON_GIT


def test_the_proposer_keeps_its_window_across_folds(tmp_path, monkeypatch):
    """The plugin reloads the proposer for every session, so its window of recent sessions is
    saved with its state: a fact must cite two sessions."""
    monkeypatch.setattr(dist, "PROPOSE_EVERY", 3)
    prompts = []

    def runner(prompt, model):
        prompts.append(prompt)
        return json.dumps({"extractors": [], "facts": [{
            "id": "build-before-test", "claim": "make test needs make build first",
            "injection": "`make test` runs the built binary: run `make build` first.",
            "detector": {"scope": r"make test", "confirm": r"make build"},
            "cite": ["s0", "s1"]}]}), 100.0
    root = str(tmp_path / "state")
    for i in range(2):
        d = dist.Distiller.load("acme/tool", root, runner=runner, vocab=GENERIC)
        d.update(_session(tmp_path, i, [
            ("Bash", {"command": "make build"}, "built"),
            ("Bash", {"command": "make test"}, "ok  ./... 0.01s")]), iid=f"s{i}")
        d.save(root)
    assert len(prompts) == 1 and "whatever its language" in prompts[0]
    assert "build-before-test" in d.facts
    again = dist.Distiller.load("acme/tool", root, vocab=GENERIC)
    assert [dg["iid"] for dg in again.window] == ["s0", "s1"]


def test_the_proposer_is_shown_the_facts_it_already_has(tmp_path, monkeypatch):
    """The plugin's prompt shows each fact it holds with its claim, so a later run does not
    author one again under a new id."""
    monkeypatch.setattr(dist, "PROPOSE_EVERY", 3)
    prompts = []

    def runner(prompt, model):
        prompts.append(prompt)
        return json.dumps({"extractors": [], "facts": [{
            "id": "build-before-test", "claim": "make test needs make build first",
            "injection": "`make test` runs the built binary: run `make build` first.",
            "detector": {"scope": r"make test", "confirm": r"make build"},
            "cite": ["s0", "s1"]}]}), 100.0
    root = str(tmp_path / "state")
    for i in range(5):
        d = dist.Distiller.load("acme/tool", root, runner=runner, vocab=GENERIC)
        d.update(_session(tmp_path, i, [
            ("Bash", {"command": "make build"}, "built"),
            ("Bash", {"command": "make test"}, "ok  ./... 0.01s")]), iid=f"s{i}")
        d.save(root)
    assert len(prompts) == 2
    assert "Existing facts, as `id: claim`" in prompts[0] and "(none)" in prompts[0]
    assert "  - build-before-test: make test needs make build first" in prompts[1]
    # the experiments' prompt still names the ids alone, as the paper ran it
    p = dist.build_proposer_prompt("acme/tool", [], {"build-before-test": "x y z"}, set(), [])
    assert "Existing fact ids (re-emit" in p and "\nbuild-before-test\n" in p and "x y z" not in p


def test_the_experiments_proposer_prompt_is_the_papers():
    """The generic prompt is the plugin's; the experiments' must stay what the paper ran."""
    assert hashlib.sha256(dist.PROPOSER_SYSTEM.encode()).hexdigest()[:16] == "fad9adfb8fda5875"
    g = dist.PROPOSER_SYSTEM_GENERIC
    assert "repro_written" not in g and "importerror" not in g
    assert g.count(", ".join(dist.GENERIC_PREDICATES)) == 2


def test_a_label_rewrite_the_runner_loads_becomes_a_fact_under_generic(tmp_path):
    d = ef.EnvFactsDistiller("acme/api", vocab=GENERIC)
    for i in range(6):
        d.update(_session(tmp_path, i, [
            ("Edit", {"file_path": f"/r/app/m{i}.rb", "old_string": "a", "new_string": "b"}, "ok"),
            ("Bash", {"command": "rspec spec/models/user_spec.rb"},
             "ERROR: file or directory not found: spec/models/user_spec.rb"),
            ("Bash", {"command": "rspec models/user_spec.rb"}, "3 examples, 0 failures")]),
            iid=f"s{i}")
    facts = [f for f in d.facts.values() if f.family == "arg_repair"]
    assert facts and facts[0].params["form"] == "rspec"
    assert facts[0].params["examples"] == {"spec/models/user_spec.rb models/user_spec.rb": 6}


def test_the_generic_digest_keeps_line_breaks_of_list_tool_results(tmp_path):
    blocks = [{"type": "text", "text": "Exit code 1\nerror: missing script: lint"}]
    p = write_claude_transcript(str(tmp_path / "t.jsonl"), [
        ("Bash", {"command": "npm run lint"}, blocks),
        ("Bash", {"command": "npm test"}, [
            {"type": "text", "text": "Tests run: 3"}, {"type": "text", "text": "ok"}])])
    calls = dist.load_calls(p)
    assert calls[0][2] == "Exit code 1\nerror: missing script: lint"
    assert calls[1][2] == "Tests run: 3\nok"
    dg = dist.build_generic_digest("acme/web", p, "s0")
    assert dg["failed_commands"] == [
        {"cmd": "npm run lint", "error": "error: missing script: lint"}]
