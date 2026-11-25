"""The per-repository envelope: a percentile of how long turns that ended green ran, and the
detector thresholds learned from the same turns.

These pin the statistic and its safety rules, with values worked out by hand: a linearly
interpolated percentile (numpy's default), no envelope before enough samples, a lone outlier that
barely moves it, only ended-green turns feeding it, learned signal gates never below the level's
floor, turn-scale gates clamped to the level's ceiling, and a backstop kept under a turn cap."""
import contextlib
import random

import pytest

from tokeneconomizer import loopguard as guard
from tokeneconomizer.loopguard import envelope


def _tracker(turns, level="default", snaps=None, **kw):
    t = guard.EnvelopeTracker(level=level, **kw)
    for i, n in enumerate(turns):
        t.update(n, True, snapshot=(snaps[i] if snaps else None))
    return t


class _Store:
    """The two calls the envelope makes on a repository's state store, in memory."""
    def __init__(self):
        self.docs = {}

    def read(self, name, default=None):
        return self.docs.get(name, default)

    def write(self, name, obj):
        self.docs[name] = obj

    @contextlib.contextmanager
    def locked(self, name="state", timeout=10.0):
        yield


# ---- the statistic ---------------------------------------------------------------------------
@pytest.mark.parametrize("pct, want", [(90, 9.1), (50, 5.5), (0, 1.0), (100, 10.0)])
def test_the_envelope_is_a_linearly_interpolated_percentile(pct, want):
    turns = list(range(1, 11))
    random.Random(7).shuffle(turns)                  # the order turns arrive in does not matter
    t = _tracker(turns, margin=1.0, pct=pct, min_samples=1)
    assert t.envelope == pytest.approx(want)


def test_one_sample_is_its_own_percentile():
    assert _tracker([7], margin=1.0, min_samples=1).envelope == pytest.approx(7.0)


def test_the_default_envelope_is_the_p90_times_the_margin_once_five_turns_ended_green():
    assert _tracker([10, 12, 14, 16]).envelope is None
    # p90 of 10, 12, 14, 16, 18 is 16 + 0.6 * 2 = 17.2; the default margin is 1.25
    assert _tracker([10, 12, 14, 16, 18]).envelope == pytest.approx(17.2 * 1.25)


def test_a_lone_very_long_turn_barely_moves_the_envelope():
    turns = [20, 21, 22, 23, 24, 25, 26, 28, 30, 115]
    # p90 = 30 + 0.1 * (115 - 30) = 38.5, where a max-based envelope would sit at 115
    assert _tracker(turns).envelope == pytest.approx(38.5 * 1.25)


def test_only_a_turn_that_ended_green_feeds_it():
    t = guard.EnvelopeTracker()
    t.update(40, False, snapshot={"edit_churn": 9})
    t.update(0, True, snapshot={"edit_churn": 9})
    assert t.resolved_turns == [] and t.resolved_snaps == []


def test_a_frozen_envelope_holds_until_enough_turns_ended_green():
    t = guard.EnvelopeTracker(frozen_envelope=30.0)
    for n in (10, 12, 14, 16):
        t.update(n, True)
        assert t.envelope == 30.0
    t.update(18, True)
    assert t.envelope == pytest.approx(17.2 * 1.25)


# ---- the thresholds learned on top of the level ----------------------------------------------
def test_before_warm_up_the_thresholds_are_the_levels_own():
    for level in ("conservative", "default", "aggressive"):
        assert _tracker([10, 12], level=level).thresholds() == guard.scaled_thresholds(level)


def test_turn_scale_gates_follow_the_turns_within_the_levels_bounds():
    short = _tracker([10] * 5).thresholds()          # 0.6 * p90 = 6
    assert (short["gate_min_turns"], short["min_turns"]) == (6, 8)   # floors 6 and 8
    long = _tracker([100] * 5).thresholds()          # 0.6 * p90 = 60
    base = guard.DEFAULT_THRESHOLDS
    assert (long["gate_min_turns"], long["min_turns"]) == (base["gate_min_turns"],
                                                            base["min_turns"])


def test_learned_signal_gates_never_go_below_the_levels_floor():
    calm = {"edit_churn": 1, "distinct_edit_files": 1, "error_outputs": 0, "stagnation": 0.0}
    for level in ("conservative", "default", "aggressive"):
        base = guard.scaled_thresholds(level)
        thr = _tracker([20] * 5, level=level, snaps=[calm] * 5).thresholds()
        for key in ("edit_churn", "distinct_edit_files", "error_outputs", "gate_error_outputs",
                    "stagnation"):
            assert thr[key] == base[key], (level, key)


def test_a_churny_repository_raises_the_signal_gates_above_its_p90():
    busy = {"edit_churn": 9, "distinct_edit_files": 4, "error_outputs": 12, "stagnation": 0.5}
    thr = _tracker([20] * 5, snaps=[busy] * 5).thresholds()
    assert (thr["edit_churn"], thr["distinct_edit_files"], thr["error_outputs"]) == (10, 5, 13)
    assert thr["gate_error_outputs"] == 13           # never below the learned error gate
    assert thr["stagnation"] == pytest.approx(0.5)


def test_the_backstop_stays_reachable_under_a_turn_cap():
    t = _tracker([40] * 5)                           # envelope 40 * 1.25 = 50
    assert t.thresholds()["backstop_turns"] == pytest.approx(50 * 2.5)
    assert t.thresholds(max_turns=40)["backstop_turns"] == pytest.approx(0.9 * 40)


# ---- storage ---------------------------------------------------------------------------------
def test_the_stored_envelope_keeps_the_most_recent_turns():
    store = _Store()
    t = _tracker(range(1, envelope.ENVELOPE_HISTORY + 101), margin=1.0)
    guard.save_envelope(store, t)
    kept = store.read("envelope.json")["resolved_turns"]
    assert len(kept) == envelope.ENVELOPE_HISTORY and kept[0] == 101 and kept[-1] == 600
    assert guard.load_envelope(store).resolved_turns == kept


def test_a_turn_whose_last_check_failed_after_its_last_edit_is_not_recorded():
    store = _Store()
    red = guard.TurnGuard(turn_key="t1")
    red.fold("Edit", {"file_path": "/r/a.py", "old_string": "x", "new_string": "y"}, "ok")
    red.fold("Bash", {"command": "pytest -q"}, "FAILED tests/test_a.py", 1)
    assert guard.record_turn(store, red).resolved_turns == []
    green = guard.TurnGuard(turn_key="t2")
    green.fold("Edit", {"file_path": "/r/a.py", "old_string": "x", "new_string": "y"}, "ok")
    green.fold("Bash", {"command": "pytest -q"}, "1 passed", 0)
    tracker = guard.record_turn(store, green)
    assert tracker.resolved_turns == [2] and tracker.resolved_snaps[0]["turns"] == 2
    assert store.read("envelope.json")["resolved_turns"] == [2]
