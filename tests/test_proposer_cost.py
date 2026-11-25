"""The micro-skills proposer is charged for its call even when its CLI reports no usage.

Every paid side call is charged to the ledger as overhead. The loop guard's judge and redirect
writer charge the usage their runner recovered, else an estimate (`pricing.
runner_effective_or_approx`: the prompt at 1x, the reply at 5x). The proposer charged only
recovered usage, so a call whose CLI reported none (Cursor's never does; others when the usage
block is missing) cost the ledger nothing."""
from fixtures import write_claude_transcript
from tokeneconomizer import pricing
from tokeneconomizer.loopguard import judge
from tokeneconomizer.microskills import GENERIC
from tokeneconomizer.microskills import distiller as dist

REPLY = '{"extractors": [], "facts": []}'


def _distiller(tmp_path, monkeypatch, runner):
    monkeypatch.setattr(dist, "PROPOSE_EVERY", 1)
    return dist.Distiller.load("acme/tool", str(tmp_path / "state"), runner=runner,
                               vocab=GENERIC)


def _session(tmp_path, n):
    return write_claude_transcript(str(tmp_path / f"s{n}.jsonl"), [
        ("Bash", {"command": "make build"}, "built"),
        ("Bash", {"command": "make test"}, "ok  ./... 0.01s")])


def _propose_once(tmp_path, monkeypatch, usage):
    prompts = []

    def runner(prompt, model):
        prompts.append(prompt)
        return REPLY, usage
    d = _distiller(tmp_path, monkeypatch, runner)
    d.update(_session(tmp_path, 0), iid="s0")
    assert len(prompts) == 1
    return d, prompts[0]


def test_a_call_with_no_usage_is_charged_the_estimate(tmp_path, monkeypatch):
    d, prompt = _propose_once(tmp_path, monkeypatch, None)
    want = pricing.approx_tokens(prompt) + 5 * pricing.approx_tokens(REPLY)
    assert d.opt_tokens == want > 0


def test_it_is_the_estimate_the_judge_charges_for_the_same_call(tmp_path, monkeypatch):
    d, prompt = _propose_once(tmp_path, monkeypatch, None)

    def runner(p, m):
        return REPLY, None
    assert d.opt_tokens == pricing.runner_effective_or_approx(runner, prompt, REPLY, None)
    v = judge.llm_stuck_judge("turns=30 repeated_identical_failures=4", runner)
    assert v["_tokens"] == pricing.runner_effective_or_approx(
        runner, judge.STUCK_JUDGE_SYSTEM + "\n\nRecent process digest:\n"
        "turns=30 repeated_identical_failures=4\n\nReturn the JSON verdict:", REPLY, None)


def test_recovered_usage_is_charged_as_before(tmp_path, monkeypatch):
    assert _propose_once(tmp_path, monkeypatch, 321.5)[0].opt_tokens == 321.5
    assert _propose_once(tmp_path / "local", monkeypatch, 0)[0].opt_tokens == 0   # on-device


def test_a_failed_call_is_still_not_charged(tmp_path, monkeypatch):
    def runner(prompt, model):
        raise RuntimeError("no reply")
    d = _distiller(tmp_path, monkeypatch, runner)
    d.update(_session(tmp_path, 0), iid="s0")
    assert d.opt_tokens == 0
