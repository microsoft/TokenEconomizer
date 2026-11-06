"""`te microskills forget` / `unforget`: a person retires a learned fact by hand.

A forgotten fact leaves the injected block at once, stays out however often the sessions that
taught it recur, and comes back as it was with `unforget`. An unknown id changes nothing.
"""
import glob
import json
import os

import pytest

from tokeneconomizer.microskills import Distiller, EnvFactsDistiller, forget_facts
from tokeneconomizer.microskills.distiller import Fact
from tokeneconomizer.microskills.lifecycle import write_json_atomic
from tokeneconomizer.plugin import cli

from test_live_hooks import env, repo, run_session, store_for, trap_session  # noqa: F401

FID = "form_repair:run_tests:pytest"
CLAIM = "`pytest …` fails here with command not found; `python -m pytest …` works."


@pytest.fixture
def te(env, repo, monkeypatch, capsys):  # noqa: F811
    """`te --host claude --cwd REPO ...` in the hooks' environment: (exit code, output)."""
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    def run(*argv):
        capsys.readouterr()
        code = cli.main(["--host", "claude", "--cwd", repo, *argv])
        return code, capsys.readouterr().out
    return run


def learn(env, repo, tmp_path, names):  # noqa: F811
    """Fold one trap session per name (the same transcript for a name seen before), and return
    the last session start's injected context."""
    start = {}
    for name in names:
        path = str(tmp_path / f"{name.split('-')[0]}.jsonl")
        if not os.path.exists(path):
            trap_session(path, repo, int(name.split("-")[0][1:]))
        start = run_session(env, repo, name, path)
    return (start.get("hookSpecificOutput") or {}).get("additionalContext", "")


def env_state(env, repo):  # noqa: F811
    (path,) = glob.glob(store_for(env, repo).path("mechanisms", "envfacts_state", "*.json"))
    return path


def test_forget_takes_a_fact_out_of_the_block_and_it_is_not_learned_again(
        env, repo, tmp_path, te):  # noqa: F811
    assert CLAIM in learn(env, repo, tmp_path, [f"s{i}" for i in range(6)])
    code, out = te("microskills")
    assert code == 0 and FID in out

    code, out = te("microskills", "forget", FID)
    assert code == 0 and f"forgotten: {FID}" in out
    inj = store_for(env, repo).read("injection.json")
    assert CLAIM not in inj["rendered"] and FID not in inj["rendered_ids"]
    assert FID not in inj["facts"]

    # The same sessions again, folded as new ones: the evidence recurs, the fact does not.
    again = learn(env, repo, tmp_path, [f"s{i}-again" for i in range(6)])
    assert CLAIM not in again
    assert CLAIM not in store_for(env, repo).read("injection.json")["rendered"]
    saved = json.load(open(env_state(env, repo)))
    assert FID not in saved["facts"] and FID in saved["forgotten"]
    code, out = te("microskills")
    assert "forgotten (" in out and FID in out.split("forgotten (")[1]


def test_forget_is_idempotent_and_unforget_brings_the_fact_back(
        env, repo, tmp_path, te):  # noqa: F811
    learn(env, repo, tmp_path, [f"s{i}" for i in range(6)])
    life = json.load(open(env_state(env, repo)))["facts"][FID]["life"]
    assert te("microskills", "forget", FID)[0] == 0
    code, out = te("microskills", "forget", FID)
    assert code == 0 and f"already forgotten: {FID}" in out

    code, out = te("microskills", "unforget", FID)
    assert code == 0 and f"brought back: {FID}" in out
    assert CLAIM in store_for(env, repo).read("injection.json")["rendered"]
    saved = json.load(open(env_state(env, repo)))
    assert saved["facts"][FID]["life"] == life and saved["forgotten"] == {}
    code, out = te("microskills", "unforget", FID)
    assert code == 0 and "not forgotten" in out


def test_an_unknown_id_is_an_error_and_changes_nothing(env, repo, tmp_path, te):  # noqa: F811
    learn(env, repo, tmp_path, [f"s{i}" for i in range(6)])
    before = open(env_state(env, repo)).read()
    inj = store_for(env, repo).read("injection.json")
    code, out = te("microskills", "forget", FID, "no-such-fact")
    assert code == 1 and "no-such-fact" in out and "not changed" in out
    assert open(env_state(env, repo)).read() == before
    assert store_for(env, repo).read("injection.json") == inj
    assert te("microskills", "unforget", "no-such-fact")[0] == 1
    assert te("microskills", "forget")[0] == 2           # no id at all


def test_unknown_id_with_nothing_learned(repo, te):  # noqa: F811
    code, out = te("microskills", "forget", "anything")
    assert code == 1 and "anything" in out


def test_state_from_before_forgetting_still_loads(env, repo, tmp_path, te):  # noqa: F811
    learn(env, repo, tmp_path, [f"s{i}" for i in range(6)])
    path = env_state(env, repo)
    d = json.load(open(path))
    del d["forgotten"]
    json.dump(d, open(path, "w"))
    loaded = EnvFactsDistiller.load(d["repo"], os.path.dirname(os.path.dirname(path)))
    assert loaded.forgotten == {} and FID in loaded.facts
    assert te("microskills", "forget", FID)[0] == 0
    assert FID in json.load(open(path))["forgotten"]


# ---- the paid proposer's facts --------------------------------------------------------------
def _fact(fid):
    return Fact({"id": fid, "claim": f"claim of {fid}", "injection": f"say {fid}",
                 "detector": {}})


def test_a_forgotten_proposed_fact_is_not_admitted_again(tmp_path):
    d = Distiller("acme/widget")
    d.facts["entrypoint"] = _fact("entrypoint")
    d.facts["entrypoint"].life.state = "ACTIVE"
    d.save(str(tmp_path))

    [(fid, changed, claim)] = forget_facts("acme/widget", str(tmp_path), ["entrypoint"])
    assert (fid, changed, claim) == ("entrypoint", True, "claim of entrypoint")
    d = Distiller.load("acme/widget", str(tmp_path))
    assert "entrypoint" not in d.facts and "entrypoint" in d.forgotten
    assert d._admit({"id": "entrypoint", "claim": "again", "cite": []}) == (False, "forgotten")
    assert "say entrypoint" not in d.render()

    with pytest.raises(KeyError):
        forget_facts("acme/widget", str(tmp_path), ["entrypoint", "unknown"])
    assert forget_facts("acme/widget", str(tmp_path), ["entrypoint"])[0][1] is False


def test_proposer_state_without_the_forgotten_key_loads(tmp_path):
    d = Distiller("acme/widget")
    d.facts["x"] = _fact("x")
    d.save(str(tmp_path))
    (path,) = glob.glob(str(tmp_path / "manual_state" / "*.json"))
    saved = json.load(open(path))
    del saved["forgotten"]
    json.dump(saved, open(path, "w"))
    d = Distiller.load("acme/widget", str(tmp_path))
    assert d.forgotten == {} and set(d.facts) == {"x"}


def test_a_failed_write_leaves_the_state_as_it_was(tmp_path):
    path = str(tmp_path / "state.json")
    write_json_atomic(path, {"a": 1})
    with pytest.raises(TypeError):
        write_json_atomic(path, {"a": object()})
    assert json.load(open(path)) == {"a": 1}
    assert os.listdir(tmp_path) == ["state.json"]
