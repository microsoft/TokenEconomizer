"""The loop guard's stuck rule and abort cascade (`rules.is_stuck`, `rules.assess_abort`).

`is_stuck` is the strict heuristic: run a while, AND churn on one file or scatter over several,
AND sustained errors, AND repeated identical failures. `assess_abort` runs, in order: a hard
backstop past `backstop_mult` x the envelope that no judge can veto; with a judge, a free gate
(enough turns, and a repeated failure, many errors, or a turn past the envelope) before the paid
call, then the verdict (stuck with enough confidence aborts; productive or unsure spares); with
no judge, the heuristic plus the envelope. Expected outcomes follow the default thresholds."""
import pytest

from tokeneconomizer.loopguard import DEFAULT_THRESHOLDS, SpiralState, assess_abort, is_stuck


def _state(turns=30, edits=None, errors=8, failures=5, repeats=2):
    st = SpiralState()
    st.turns, st.error_outputs = turns, errors
    st.edit_counts = dict(edits if edits is not None else {"a.py": 5})
    st.fail_sigs = {f"sig{i}": 1 for i in range(failures)}
    st.repeated_fail_sigs = repeats                  # stagnation = repeats / failures
    return st


class _Judge:
    def __init__(self, verdict=None, error=None):
        self.verdict, self.error, self.calls = verdict, error, 0

    def __call__(self, digest):
        self.calls += 1
        if self.error:
            raise self.error
        return self.verdict


# ---- is_stuck ---------------------------------------------------------------------------------
def test_churn_errors_and_repeats_together_are_stuck():
    assert is_stuck(_state())
    assert is_stuck(_state(edits={"a.py": 1, "b.py": 1, "c.py": 1}))   # scatter instead of churn


def test_the_thresholds_are_inclusive():
    assert is_stuck(_state(turns=20, edits={"a.py": 4}, errors=6, failures=5, repeats=1))


@pytest.mark.parametrize("change", [
    dict(turns=19),                                  # not run long enough
    dict(edits={"a.py": 3, "b.py": 1}),              # neither churn (4) nor scatter (3)
    dict(errors=5),                                  # errors not sustained
    dict(failures=10, repeats=1),                    # stagnation 0.10 < 0.20
])
def test_any_one_condition_missing_is_not_stuck(change):
    assert not is_stuck(_state(**change))


# ---- no judge: the heuristic and the envelope -------------------------------------------------
def test_without_a_judge_a_stuck_turn_aborts_only_past_the_envelope():
    assert assess_abort(_state(errors=2)) == (False, "not-stuck")
    assert assess_abort(_state()) == (False, "stuck-but-within-envelope")
    assert assess_abort(_state(), envelope=40) == (False, "stuck-but-within-envelope")
    assert assess_abort(_state(turns=30), envelope=24) == (True, "envelope:30>=24")


# ---- the backstop -----------------------------------------------------------------------------
def test_the_backstop_aborts_before_the_judge_is_asked():
    judge = _Judge({"stuck": False, "confidence": 1.0})
    abort, reason = assess_abort(_state(turns=30), envelope=10, judge=judge)   # backstop 25
    assert abort and reason.startswith("backstop:30>=25")
    assert judge.calls == 0


def test_a_cap_aware_backstop_wins_over_the_multiple_of_the_envelope():
    thr = dict(DEFAULT_THRESHOLDS, backstop_turns=28)
    assert assess_abort(_state(turns=30), envelope=100, thr=thr)[0] is True
    assert assess_abort(_state(turns=27), envelope=100, thr=thr) == (
        False, "stuck-but-within-envelope")


def test_past_the_backstop_a_turn_that_is_not_stuck_goes_on():
    judge = _Judge({"stuck": False, "confidence": 0.9})
    assert assess_abort(_state(turns=60, errors=2), envelope=10, judge=judge) == (
        False, "judge-productive")


# ---- with a judge: the gate, then the verdict -------------------------------------------------
def test_a_closed_gate_spends_nothing():
    judge = _Judge({"stuck": True, "confidence": 1.0})
    assert assess_abort(_state(turns=17), judge=judge) == (False, "gate-closed")
    assert assess_abort(_state(repeats=0, errors=7), judge=judge) == (False, "gate-closed")
    assert judge.calls == 0


@pytest.mark.parametrize("state", [_state(turns=18, repeats=1, errors=0, failures=1),
                                   _state(turns=18, repeats=0, errors=8)])
def test_a_repeated_failure_or_many_errors_open_the_gate(state):
    judge = _Judge(None)
    assert assess_abort(state, judge=judge) == (False, "judge-throttled")
    assert judge.calls == 1


def test_a_turn_past_the_envelope_opens_the_gate_unless_that_is_turned_off():
    quiet = _state(turns=30, repeats=0, errors=2)
    judge = _Judge({"stuck": True, "confidence": 0.8, "reason": "same failure"})
    assert assess_abort(quiet, envelope=25, judge=judge)[0] is True
    off = dict(DEFAULT_THRESHOLDS, gate_over_envelope=False)
    assert assess_abort(quiet, envelope=25, judge=judge, thr=off) == (False, "gate-closed")


@pytest.mark.parametrize("verdict, result", [
    ({"stuck": True, "confidence": 0.9, "reason": "loops"}, True),
    ({"stuck": True, "confidence": 0.7, "reason": "loops"}, True),     # at the threshold
    ({"stuck": True, "confidence": 0.69}, "judge-ambiguous-spared"),
    ({"stuck": None, "confidence": 0.9}, "judge-ambiguous-spared"),
    ({"stuck": False, "confidence": 0.9}, "judge-productive"),
])
def test_only_a_confident_stuck_verdict_aborts(verdict, result):
    abort, reason = assess_abort(_state(), judge=_Judge(verdict))
    if result is True:
        assert abort and reason.startswith(f"judge-stuck@{verdict['confidence']:.2f}:loops")
    else:
        assert (abort, reason) == (False, result)


def test_a_productive_verdict_spares_a_turn_past_the_envelope():
    judge = _Judge({"stuck": False, "confidence": 0.9})
    assert assess_abort(_state(turns=30), envelope=20, judge=judge) == (False, "judge-productive")


def test_no_verdict_or_a_failed_judge_spares_the_turn():
    assert assess_abort(_state(), judge=_Judge(None)) == (False, "judge-throttled")
    assert assess_abort(_state(), judge=_Judge(error=RuntimeError("timeout"))) == (
        False, "judge-throttled")
