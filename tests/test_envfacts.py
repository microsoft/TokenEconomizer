"""Environment facts: micro-skills from generic detector families.

Every scenario is a synthetic stream of tasks (Codex transcript schema unless stated). The
assertions pin the four properties the mechanism exists for: facts FORM from recurring
evidence, FIRE only while the waste recurs, ABSTAIN where the evidence says otherwise, and
cost nothing (`opt_tokens == 0`).
"""
import json
import os

from tokeneconomizer.microskills import EnvFactsDistiller, PrefixStage
from tokeneconomizer.microskills import envfacts as ef, observe
from tokeneconomizer.config import InjectionConfig
from fixtures import write_codex_transcript, write_claude_transcript

BASE = "/opt/env/base/bin/python"
PROJ = "/opt/env/proj/bin/python"


def _cmd(command, output, code):
    return {"type": "command_execution", "command": f'/bin/bash -lc "{command}"',
            "aggregated_output": output, "exit_code": code}


def _edit(path):
    return {"type": "file_change", "changes": [{"path": path, "kind": "update"}]}


def _task(tmp_path, i, items):
    p = str(tmp_path / f"t{i}.jsonl")
    write_codex_transcript(p, items)
    return p


def _broken_env_task(tmp_path, i):
    """Bare runner fails on the base interpreter; the explicit path works."""
    return _task(tmp_path, i, [
        _edit(f"pkg/mod{i}.py"),
        _cmd("python -m pytest tests/test_a.py -q", f"{BASE}: No module named pytest", 1),
        _cmd(f"{PROJ} -m pytest tests/test_a.py -q", "1 passed in 0.2s", 0),
    ])


def _stream(tmp_path, n, maker, d=None):
    d = d or EnvFactsDistiller("acme/widget")
    for i in range(n):
        d.render()
        d.update(maker(tmp_path, i), iid=f"w-{i}", resolved=True)
    return d


# ---- observations ---------------------------------------------------------------------
def test_classify_forms_and_intents():
    obs = observe.classify_command(
        '/bin/bash -lc "source /opt/env/bin/activate proj && python -m pytest tests -q"')
    assert [(o.intent, o.form) for o in obs] == [
        ("run_tests", "source activate proj && python -m pytest")]
    obs = observe.classify_command("conda run -n proj python - <<'PY'\nimport x\nPY")
    assert [(o.intent, o.form) for o in obs] == [("run_interpreter", "conda run -n proj python")]
    obs = observe.classify_command("PYTHONPATH=/testbed ./tests/runtests.py auth_tests -v 1")
    assert [(o.intent, o.form) for o in obs] == [("run_tests", "python tests/runtests.py")]
    obs = observe.classify_command(f"{PROJ} bin/test sympy/core/tests/test_a.py")
    assert [(o.intent, o.form) for o in obs] == [("run_tests", f"{PROJ} bin/test")]
    obs = observe.classify_command("git remote -v; git tag --contains HEAD | head; git log --oneline -3")
    assert [(o.intent, o.probe) for o in obs] == [("probe_vcs_refs", "remote"),
                                                  ("probe_vcs_refs", "tag")]
    obs = observe.classify_command("curl -sS https://api.example.org/x | head -3")
    assert obs[0].intent == "fetch_remote" and obs[0].hosts == ("api.example.org",)
    # a plain `git tag` lists ALL tags, past ones included: not a beyond-HEAD probe
    assert observe.classify_command("git tag | tail -5") == []
    # heredoc bodies never become commands
    assert observe.classify_command("python - <<'PY'\npytest_is_a_word = 1\nPY")[0].intent == \
        "run_interpreter"
    # a here-string (`<<<`) has no body: the commands after it are still read
    assert [o.intent for o in observe.classify_command("read -r x <<< x\npytest -q\nx=1")] == \
        ["run_tests"]


def test_error_classes_are_environmental_only():
    assert observe.error_class("ModuleNotFoundError: No module named 'x'") == "module_missing"
    assert observe.error_class("bash: pytest: command not found") == "command_missing"
    assert observe.error_class("curl: (7) Failed to connect to h port 443: Connection refused") == \
        "connection_refused"
    assert observe.error_class("AssertionError: 1 != 2\nFAILED tests/test_a.py::t") is None


# ---- FormRepair -------------------------------------------------------------------------
def test_interpreter_fact_forms_fires_and_renders(tmp_path):
    d = _stream(tmp_path, 3, _broken_env_task)
    assert d.render() == ""                       # ACTIVE needs 3 drifted confirms AND 4 exercised
    d.update(_broken_env_task(tmp_path, 3), iid="w-3", resolved=True)
    text = d.render()
    assert text.startswith(ef.HEADER)
    assert PROJ in text and "python -m pytest …` fails here" in text
    assert d.opt_tokens == 0
    f = d.facts["form_repair:run_tests:python -m pytest"]
    assert f.state == "ACTIVE" and f.life.fire_num == 4 and f.life.fire_den == 4
    assert ef.lint_ok(text)


def test_clean_environment_forms_nothing(tmp_path):
    def clean(tmp_path, i):
        return _task(tmp_path, i, [
            _edit(f"pkg/m{i}.py"),
            _cmd("python -m pytest tests/test_a.py -q", "1 passed", 0)])
    d = _stream(tmp_path, 8, clean)
    assert d.facts == {} and d.render() == ""


def test_prevented_waste_still_counts_as_firing(tmp_path):
    d = _stream(tmp_path, 4, _broken_env_task)
    assert d.render() != ""                       # injected for the next task
    # the agent followed the fact: used the working form straight away, nothing failed
    d.update(_task(tmp_path, 9, [_edit("pkg/z.py"),
                                 _cmd(f"{PROJ} -m pytest tests/test_z.py -q", "3 passed", 0)]),
             iid="w-9", resolved=True)
    f = d.facts["form_repair:run_tests:python -m pytest"]
    assert f.life.fire_num == 5 and f.life.fire_den == 5
    assert d.render() != ""


def test_bare_runner_success_contradicts_and_retires(tmp_path):
    d = _stream(tmp_path, 4, _broken_env_task)
    assert d.render() != ""
    # environment fixed: the bare runner now works, task after task
    def fixed(tmp_path, i):
        return _task(tmp_path, i, [_edit(f"pkg/f{i}.py"),
                                   _cmd("python -m pytest tests/test_a.py -q", "1 passed", 0)])
    for i in range(20, 23):
        d.render()
        d.update(fixed(tmp_path, i), iid=f"w-{i}", resolved=True)
    f = d.facts["form_repair:run_tests:python -m pytest"]
    assert f.state == "RETIRED" and d.render() == ""


def test_bare_one_liner_success_is_not_a_contradiction(tmp_path):
    """A pure-Python package imports from the working tree under any interpreter, so a
    successful bare `python -c` proves nothing about the environment."""
    d = _stream(tmp_path, 4, _broken_env_task)
    d.update(_task(tmp_path, 9, [_edit("pkg/q.py"),
                                 _cmd("python -c 'import pkg; print(pkg.__version__)'", "1.0", 0),
                                 _cmd(f"{PROJ} -m pytest tests -q", "2 passed", 0)]),
             iid="w-9", resolved=True)
    assert d.facts["form_repair:run_tests:python -m pytest"].state == "ACTIVE"


# ---- EmptyProbe -------------------------------------------------------------------------
def test_history_fact_forms_only_when_probes_return_nothing(tmp_path):
    def empty(tmp_path, i):
        return _task(tmp_path, i, [_edit(f"pkg/e{i}.py"),
                                   _cmd("git remote -v; git tag --contains HEAD | head", "", 0),
                                   _cmd(f"{PROJ} -m pytest tests -q", "1 passed", 0)])
    d = _stream(tmp_path, 4, empty)
    assert "history ends at the checked-out commit" in d.render()

    def leaky(tmp_path, i):
        return _task(tmp_path, i, [_edit(f"pkg/l{i}.py"),
                                   _cmd("git tag --contains HEAD | head", "0.22\n0.22.1\n0.23.0", 0)])
    d2 = _stream(tmp_path, 4, leaky, EnvFactsDistiller("acme/leaky"))
    assert d2.render() == ""
    assert d2.facts["empty_probe:vcs_refs"].state == "RETIRED"


# ---- Boundary ---------------------------------------------------------------------------
def test_refused_hosts_form_a_boundary_fact_and_a_reachable_host_does_not(tmp_path):
    def refused(tmp_path, i):
        return _task(tmp_path, i, [
            _edit(f"pkg/n{i}.py"),
            _cmd("curl -sS https://raw.example.org/a/b.py",
                 "curl: (7) Failed to connect to raw.example.org port 443: Connection refused", 7),
            _cmd("curl -sS https://api.example.org/search?q=x",
                 "curl: (7) Failed to connect to api.example.org port 443: Connection refused", 7)])
    d = _stream(tmp_path, 4, refused)
    text = d.render()
    assert "raw.example.org" in text and "api.example.org" in text and "refused" in text
    # a later successful fetch from a listed host is a contradiction (hard kill)
    d.update(_task(tmp_path, 9, [_cmd("curl -sS https://api.example.org/ok", "{\"ok\": true}", 0)]),
             iid="w-9", resolved=True)
    assert d.facts["boundary:fetch_remote"].state == "RETIRED"


# ---- rendering discipline ---------------------------------------------------------------
def test_lint_refuses_completion_advice():
    assert not ef.lint_ok("You are done once the tests pass; skip the rest.")
    assert ef.lint_ok("`python …` fails here with command not found; `/x/bin/python …` works.")


def test_block_is_capped_in_facts_and_tokens(tmp_path):
    d = EnvFactsDistiller("acme/widget")
    for i in range(7):
        f = ef.EnvFact(f"form_repair:run_tests:tool{i}", "form_repair", False,
                       {"intent": "run_tests", "a": f"tool{i}",
                        "b_counts": {f"/very/long/path/number/{i}/that/keeps/going/bin/tool{i}": 3},
                        "error_counts": {"command_missing": 3}})
        f.life.state = "ACTIVE"; f.life.fire_num = f.life.fire_den = 10
        d.facts[f.id] = f
    text = d.render()
    assert text and len(text) <= ef.MAX_TOKENS * 4
    assert text.count("\n- ") <= ef.MAX_FACTS


def test_form_repair_facts_sharing_the_working_form_merge_into_one_line(tmp_path):
    def two_forms(tmp_path, i):
        return _task(tmp_path, i, [
            _edit(f"pkg/m{i}.py"),
            _cmd("pytest tests/test_a.py -q", "bash: pytest: command not found", 127),
            _cmd("python -m pytest tests/test_a.py -q", f"{BASE}: No module named pytest", 1),
            _cmd(f"{PROJ} -m pytest tests/test_a.py -q", "1 passed", 0)])
    d = _stream(tmp_path, 4, two_forms)
    text = d.render()
    assert text.count("\n- ") == 1 and "pytest …`" in text and "python -m pytest …`" in text


def test_a_merged_line_with_one_working_form_says_works(tmp_path):
    def two_forms(tmp_path, i):
        return _task(tmp_path, i, [
            _edit(f"pkg/m{i}.py"),
            _cmd("pytest tests/test_a.py -q", "bash: pytest: command not found", 127),
            _cmd("python -m pytest tests/test_a.py -q", f"{BASE}: No module named pytest", 1),
            _cmd(f"{PROJ} -m pytest tests/test_a.py -q", "1 passed", 0)])
    assert d_text(_stream(tmp_path, 4, two_forms)).endswith(f"`{PROJ} -m pytest …` works.")


def d_text(d):
    return d.render().strip().splitlines()[-1]


def test_an_environment_fact_forms_in_a_repository_where_every_session_edits_one_file(tmp_path):
    """Whether `pytest` is missing does not depend on which file a session edits: in a small
    repository where every session touches the same module, the fact still forms."""
    def same_file(tmp_path, i):
        return _task(tmp_path, i, [
            _edit("pkg/stock.py"),
            _cmd("pytest -q", "bash: pytest: command not found", 127),
            _cmd("python -m pytest -q", "4 passed in 0.01s", 0)])
    d = _stream(tmp_path, 4, same_file)
    assert d.facts["form_repair:run_tests:pytest"].state == "ACTIVE"
    assert "`pytest …` fails here with command not found" in d.render()


# ---- which working form a fact names --------------------------------------------------------
LOCAL = "~/.local/bin/pytest"


def _clean(tmp_path, i):
    """The bare runner is missing; `python -m pytest` works at once."""
    return _task(tmp_path, i, [
        _edit(f"pkg/c{i}.py"),
        _cmd("pytest -q", "bash: pytest: command not found", 127),
        _cmd("python -m pytest -q", "4 passed in 0.01s", 0)])


def _messy(tmp_path, i):
    """Seen live on Claude Code: the bare runner fails twice, a user-site pytest stops at
    collection, and only `PYTHONPATH=. ~/.local/bin/pytest` passes -- twice."""
    return _task(tmp_path, i, [
        _edit(f"pkg/m{i}.py"),
        _cmd("pytest -q", "bash: pytest: command not found", 127),
        _cmd("pytest -q", "bash: pytest: command not found", 127),
        _cmd(f"{LOCAL} -q", "E   ModuleNotFoundError: No module named 'stock'\n"
                            "Interrupted: 1 error during collection\n1 error in 0.08s", 2),
        _cmd(f"PYTHONPATH=. {LOCAL} -q", "4 passed in 0.01s", 0),
        _cmd(f"PYTHONPATH=. {LOCAL} -q", "4 passed in 0.01s", 0)])


def test_a_form_that_only_worked_with_an_assignment_before_it_is_not_named(tmp_path):
    d = _stream(tmp_path, 3, _clean)
    for i in range(3, 6):
        d.render()
        d.update(_messy(tmp_path, i), iid=f"m-{i}", resolved=True)
    f = d.facts["form_repair:run_tests:pytest"]
    assert LOCAL not in f.params["b_counts"]
    assert d_text(d).endswith("`python -m pytest …` works.")


def test_one_session_is_one_piece_of_evidence_per_form(tmp_path):
    def repeated(tmp_path, i):            # three failures, then the same repair twice
        return _task(tmp_path, i, [
            _edit(f"pkg/r{i}.py")] + [
            _cmd("pytest -q", "bash: pytest: command not found", 127)] * 3 + [
            _cmd(f"{PROJ} -m pytest -q", "1 passed", 0)] * 2)
    d = _stream(tmp_path, 2, _clean)
    d.render(); d.update(repeated(tmp_path, 2), iid="r", resolved=True)
    assert d.facts["form_repair:run_tests:pytest"].params["b_counts"] == {
        "python -m pytest": 2, PROJ + " -m pytest": 1}
    assert d.facts["form_repair:run_tests:pytest"].working_form() == "python -m pytest"


def test_a_named_form_that_stops_working_is_named_no_more(tmp_path):
    d = _stream(tmp_path, 4, _clean)
    assert "`python -m pytest …` works" in d.render()

    def gone(tmp_path, i):                # the environment lost pytest altogether
        return _task(tmp_path, i, [
            _edit(f"pkg/g{i}.py"),
            _cmd("pytest -q", "bash: pytest: command not found", 127),
            _cmd("python -m pytest -q", f"{BASE}: No module named pytest", 1)])
    for i in range(4, 9):
        d.render(); d.update(gone(tmp_path, i), iid=f"g-{i}", resolved=True)
    f = d.facts["form_repair:run_tests:pytest"]
    assert f.params["b_fails"]["python -m pytest"] == 5 and f.working_form() is None
    assert f.claim() == "" and "python -m pytest" not in d.render()


def test_env_assignments_after_env_are_part_of_how_a_command_was_typed():
    (o,) = observe.classify_command("env PYTHONPATH=. pytest -q")
    assert (o.intent, o.form, o.env) == ("run_tests", "pytest", True)
    (o,) = observe.classify_command("PYTHONPATH=. python -m pytest -q")
    assert (o.form, o.env) == ("python -m pytest", True)
    (o,) = observe.classify_command("python -m pytest -q")
    assert o.env is False


# ---- persistence, other transcript schemas, stage wiring ---------------------------------
def test_save_load_roundtrip(tmp_path):
    d = _stream(tmp_path, 4, _broken_env_task)
    d.save(str(tmp_path))
    d2 = EnvFactsDistiller.load("acme/widget", str(tmp_path))
    assert d2.now == 4 and d2.render() == d.render()
    assert json.load(open(os.path.join(str(tmp_path), ef.STATE_SUBDIR, "acme__widget.json")))["opt_tokens"] == 0.0


def test_claude_schema_yields_the_same_fact(tmp_path):
    d = EnvFactsDistiller("acme/widget")
    for i in range(4):
        p = str(tmp_path / f"c{i}.jsonl")
        write_claude_transcript(p, [
            ("Edit", {"file_path": f"/testbed/pkg/c{i}.py"}, "ok"),
            ("Bash", {"command": "python -m pytest tests/test_a.py -q"},
             f"Exit code 1\n{BASE}: No module named pytest"),
            ("Bash", {"command": f"{PROJ} -m pytest tests/test_a.py -q"}, "1 passed")])
        d.render(); d.update(p, iid=f"c-{i}", resolved=True)
    assert PROJ in d.render()


def test_prefix_stage_envfacts_profile_needs_no_runner_and_reports_zero(tmp_path):
    cfg = InjectionConfig(enabled=True, profile="envfacts")
    stage = PrefixStage(cfg, "acme/widget", str(tmp_path), fresh=True, runner=None,
                        agent="codex")
    assert stage.render() == ""
    rd = str(tmp_path / "run"); os.makedirs(rd)
    write_codex_transcript(os.path.join(rd, "transcript.jsonl"), [
        _cmd("python -m pytest -q", f"{BASE}: No module named pytest", 1),
        _cmd(f"{PROJ} -m pytest -q", "1 passed", 0)])
    srow = {"resolved": True}
    opt, reason = stage.after_task({"instance_id": "w-1"}, {}, rd, srow)
    assert opt == 0 and reason == "envfacts"
    assert srow["manual_facts"]["_profile"] == "envfacts"
    assert "form_repair:run_tests:python -m pytest" in srow["manual_facts"]


# ---- rtk's Codex mode puts `rtk` in front of every command the agent types ----------------
def test_the_classifier_sees_through_rtks_command_prefix():
    """An agent told to type `rtk pytest`, `rtk git status`, `rtk proxy <cmd>` must still be
    read as running the tests, fetching, querying the repository. Pinned as bare == prefixed,
    so a new wrapper spelling has to be added to trace.RTK_PREFIX_RE and nowhere else."""
    from tokeneconomizer.trace import strip_rtk_prefix
    cases = ("python -m pytest tests/ -q", "pytest -q", "bin/test sympy/x.py", "K=1 pytest -q",
             "pip install foo", "curl https://x", "git status", "rg -n foo src")
    for bare in cases:
        for pre in ("rtk ", "rtk proxy ", "rtk K=2 "):
            wrapped = pre + bare
            assert ([(o.intent, o.form) for o in observe.classify_command(wrapped)]
                    == [(o.intent, o.form) for o in observe.classify_command(bare)]), wrapped
    # the stripper is exact: a bare `rtk`, or a command that merely starts with the letters,
    # is not a wrapper
    assert strip_rtk_prefix("rtk pytest -q") == "pytest -q"
    assert strip_rtk_prefix("rtk proxy make") == "make"
    assert strip_rtk_prefix("rtk") == "rtk" and strip_rtk_prefix("rtkfoo bar") == "rtkfoo bar"
    assert strip_rtk_prefix("pytest") == "pytest"
