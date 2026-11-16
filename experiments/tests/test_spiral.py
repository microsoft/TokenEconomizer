"""Spiral: generic envelope guard + Codex-native post-green policy."""
from te_bench.spiral_stage import SpiralStage
from te_bench import codex_spiral as cs
from tokeneconomizer.config import SpiralConfig


def test_shipped_codex_policy_flags():
    pol = cs.shipped_policy(30)
    assert pol.post_green_finalize_enabled is True
    assert pol.post_green_finalize_require_cleared_failures is True
    assert pol.wide_failure_enabled is False
    assert pol.recovery_enabled is False
    assert pol.min_tool_turns == 30           # at_threshold set


def test_green_is_trustworthy():
    assert cs.green_is_trustworthy("python -m pytest tests/test_x.py") is True
    assert cs.green_is_trustworthy("pytest -k only_this") is False
    assert cs.green_is_trustworthy("pytest --last-failed") is False


def test_generic_guard_run_kwargs():
    cfg = SpiralConfig(guard=True, judge=False, nudge=True)
    stage = SpiralStage(cfg, max_turns=80, agent="claude")
    rk = stage.run_kwargs({"instance_id": "w-1"})
    assert rk["spiral_guard"] is True
    assert "spiral_thr" in rk and "abort_envelope" in rk


def test_codex_spiral_run_kwargs_policy():
    cfg = SpiralConfig(codex_spiral=True)
    stage = SpiralStage(cfg, max_turns=80, agent="codex")
    rk = stage.run_kwargs({"instance_id": "x-1"})
    assert "codex_spiral_policy" in rk       # None until the tracker warms, but present


def test_generic_guard_after_task_advances_envelope():
    cfg = SpiralConfig(guard=True, nudge=True)
    stage = SpiralStage(cfg, max_turns=80, agent="claude")
    srow = {"resolved": True, "num_turns": 12}
    stage.after_task({"instance_id": "w-1"},
                     {"resolved": True, "num_turns": 12, "aborted": False}, "/tmp", srow)
    assert srow["aborted"] is False
    assert "spiral_nudges" in srow


def test_codex_spiral_after_task_telemetry(tmp_path):
    cfg = SpiralConfig(codex_spiral=True)
    stage = SpiralStage(cfg, max_turns=80, agent="codex")
    srow = {"resolved": True, "num_turns": 30}
    stage.after_task({"instance_id": "x-1"},
                     {"resolved": True, "num_turns": 30, "codex_spiral_nudges": 0},
                     str(tmp_path), srow)
    assert srow["codex_spiral_active"] is True
    assert srow["codex_spiral_profile"] == cs.PROFILE_VERSION


# ---------------------------------------------------------------------------------------
# Shell-applied patches (2026-09-12). On some model providers the CLI emits no
# file_change items: every edit is `apply_patch <<'PATCH'` inside a command_execution. The
# controller must see those as edits or it never registers a trustworthy green and never
# fires.
# ---------------------------------------------------------------------------------------
import json as _json
from te_bench import codex_spiral as _cs

_PATCH_CMD = ("/bin/bash -lc \"apply_patch <<'PATCH'\n*** Begin Patch\n*** Update File: "
              "django/core/validators.py\n@@\n-old\n+new\n*** End Patch\nPATCH\n\n"
              "git diff --check\"")


def _line(item):
    return _json.dumps({"type": "item.completed", "item": item})


def test_shell_applied_patch_is_an_edit_event_with_its_paths():
    events = _cs.parse_events(_line({"type": "command_execution", "command": _PATCH_CMD,
                                     "aggregated_output": "Success. Updated the following files:\nM django/core/validators.py",
                                     "exit_code": 0}))
    assert [e.kind for e in events] == ["edit", "command"]
    assert events[0].paths == ("django/core/validators.py",)
    # a patch that failed to apply is not an edit
    failed = _cs.parse_events(_line({"type": "command_execution", "command": _PATCH_CMD,
                                     "aggregated_output": "apply_patch: error: Failed to find context", "exit_code": 1}))
    assert [e.kind for e in failed] == ["command"]
    # an ordinary command stays one event
    plain = _cs.parse_events(_line({"type": "command_execution", "command": "ls", "aggregated_output": "", "exit_code": 0}))
    assert [e.kind for e in plain] == ["command"]
    # string exit codes (defensive) are normalised
    assert _cs.parse_events(_line({"type": "command_execution", "command": "pytest -q", "aggregated_output": "1 passed", "exit_code": "0"}))[0].exit_code == 0


def test_shipped_policy_can_fire_on_a_shell_patched_trajectory():
    lines = [_json.dumps({"type": "thread.started", "thread_id": "t-1"})]
    for i in range(4):
        lines.append(_line({"type": "command_execution", "command": f"rg pattern{i} src", "aggregated_output": "x", "exit_code": 0}))
    lines.append(_line({"type": "command_execution", "command": _PATCH_CMD, "aggregated_output": "Success. Updated", "exit_code": 0}))
    lines.append(_line({"type": "command_execution", "command": "python -m pytest tests/test_validators.py -q", "aggregated_output": "3 passed", "exit_code": 0}))
    for i in range(8):
        lines.append(_line({"type": "command_execution", "command": f"git log -p -{i+1} -- django", "aggregated_output": "history", "exit_code": 0}))
    decision, state = _cs.replay_events(_cs.iter_events(lines), _cs.shipped_policy(6))
    assert state.last_edit_turn > 0 and state.first_trustworthy_green_turn > state.last_edit_turn
    assert decision and decision["reason"] == "validated-patch-should-finalize"
    # and the live guard sees the same trajectory the same way
    guard = _cs.CodexResumeGuard(_cs.shipped_policy(6))
    fired = [guard.monitor(l) for l in lines]
    assert any(fired) and guard.continuation()["session_id"] == "t-1"


def test_trajectory_seam_surfaces_shell_patches_as_apply_patch_calls(tmp_path):
    from tokeneconomizer import trace
    from tokeneconomizer.microskills.lifecycle import edited_pyfiles
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join([
        _line({"type": "command_execution", "command": _PATCH_CMD, "aggregated_output": "Success. Updated", "exit_code": 0}),
        _line({"type": "command_execution", "command": "pytest -q", "aggregated_output": "ok", "exit_code": 0}),
    ]) + "\n")
    calls = trace.load_trajectory(str(p))
    assert [c[0] for c in calls] == ["apply_patch", "shell", "shell"]
    assert calls[0][1]["paths"] == ["django/core/validators.py"] and calls[0][1]["via"] == "shell"
    assert edited_pyfiles([(c[0], c[1], c[2]) for c in calls]) == {"validators.py"}


# ---------------------------------------------------------------------------------------
# The defensive profile (2026-09-12): finalize plus every deterministic spiral signature,
# two DISTINCT same-thread nudges per task, never an abort, reasoning effort untouched.
# ---------------------------------------------------------------------------------------
def _cmd(command, exit_code=0, output=""):
    return _line({"type": "command_execution", "command": command, "aggregated_output": output, "exit_code": exit_code})


def test_green_reverification_loop_fires_only_after_repeated_greens_without_an_edit():
    lines = [_json.dumps({"type": "thread.started", "thread_id": "t-2"})]
    lines += [_cmd(f"rg x{i}") for i in range(6)]
    lines.append(_cmd(_PATCH_CMD, 0, "Success. Updated"))
    lines.append(_cmd("pytest tests/test_a.py -q", 0, "1 passed"))          # the accept
    for _ in range(2):
        lines.append(_cmd("pytest tests/test_a.py -q", 0, "1 passed"))      # two re-runs: not yet
    decision, state = _cs.replay_events(_cs.iter_events(lines), _cs.defensive_policy(8))
    assert decision is None or decision["reason"] != "green-reverification-loop"
    lines.append(_cmd("pytest tests/test_a.py -q", 0, "1 passed"))          # third re-run
    decision, state = _cs.replay_events(_cs.iter_events(lines), _cs.defensive_policy(8))
    assert state.green_reruns_since_edit == 3
    # the post-green finalize (grace 3 after the accept) reaches this point first; both are
    # finalize-class nudges and the guard allows one of each, so check the rule directly
    # with finalize disabled:
    from dataclasses import replace as _replace
    d2, _ = _cs.replay_events(_cs.iter_events(lines), _replace(_cs.defensive_policy(8), post_green_finalize_enabled=False))
    assert d2 and d2["reason"] == "green-reverification-loop"
    # an edit resets the counter
    lines.append(_cmd(_PATCH_CMD, 0, "Success. Updated"))
    _, state = _cs.replay_events(_cs.iter_events(lines), _cs.defensive_policy(8))
    assert state.green_reruns_since_edit == 0
    # the shipped profile does not know this rule
    d3, _ = _cs.replay_events(_cs.iter_events(lines[:-1]), _replace(_cs.shipped_policy(8), post_green_finalize_enabled=False))
    assert d3 is None


def test_runaway_guard_fires_at_twice_the_envelope_even_with_no_edit():
    lines = [_json.dumps({"type": "thread.started", "thread_id": "t-3"})]
    lines += [_cmd(f"rg needle{i} src") for i in range(40)]                 # localization forever
    decision, _ = _cs.replay_events(_cs.iter_events(lines), _cs.defensive_policy(16))
    assert decision and decision["reason"] == "beyond-twice-the-resolved-envelope"
    assert decision["turn"] == 32                                           # 2 x 16, above the floor of 30
    shipped, _ = _cs.replay_events(_cs.iter_events(lines), _cs.shipped_policy(16))
    assert shipped is None                                                  # the shipped profile never fires here
    # below the floor the multiplier does not apply
    early, _ = _cs.replay_events(_cs.iter_events(lines[:25]), _cs.defensive_policy(8))
    assert early is None


def test_defensive_guard_spends_its_budget_on_distinct_reasons_only():
    guard = _cs.CodexResumeGuard(_cs.defensive_policy(16))
    lines = [_json.dumps({"type": "thread.started", "thread_id": "t-4"})]
    lines += [_cmd(f"rg needle{i} src") for i in range(40)]
    fired = [guard.monitor(l) for l in lines]
    assert sum(fired) == 1 and guard.continuation()["prompt"].startswith("Automated process checkpoint")
    # the same reason again does not fire, even with budget left
    assert not any(guard.monitor(_cmd(f"rg more{i}")) for i in range(5))
    assert guard.policy.nudge_budget == 2 and guard.nudges_done == 1
    assert _cs.nudge_text("green-reverification-loop") and _cs.nudge_text("beyond-twice-the-resolved-envelope")


def test_defensive_profile_is_selectable_and_unpaid(tmp_path):
    from te_bench.arms import ARMS, ARM_ORDER
    from tokeneconomizer.config import needs_optimizer, SpiralConfig
    c = ARMS["codex-spiral-defensive"].cfg()
    assert c.spiral.codex_spiral and c.spiral.codex_spiral_profile == "defensive"
    assert needs_optimizer(c) is False and "codex-spiral-defensive" in ARM_ORDER
    # Since 2026-09-12 the plain name carries it and the old name is an alias; the shipped
    # post-green policy keeps its own arm.
    assert ARMS["codex-spiral"].cfg().spiral == c.spiral
    assert ARMS["codex-spiral-postgreen"].cfg().spiral.codex_spiral_profile == "post-green"
    import pytest
    with pytest.raises(ValueError):
        SpiralConfig(codex_spiral=True, codex_spiral_profile="bogus")
    from te_bench.spiral_stage import SpiralStage
    stage = SpiralStage(c.spiral, max_turns=80, agent="codex")
    pol = stage.run_kwargs({"instance_id": "x"})["codex_spiral_policy"]
    assert pol.reverify_loop_enabled and pol.runaway_enabled and pol.nudge_budget == 2
    assert pol.post_green_finalize_enabled and pol.post_green_finalize_require_cleared_failures
    assert not pol.effort_rescue_enabled and pol.resume_effort == ""


def test_both_guards_expose_everything_the_shared_task_loop_reads():
    """The Codex guard is driven by the SAME `run_task` loop as the generic one.

    The loop logs `guard.segment` on every nudge (`_log(... guard.segment)`), so the guard
    must expose it, and every other attribute the loop reads. Rather than pin one name,
    read the attributes the loop actually touches out of its source and require both guard
    classes to carry them.
    """
    import inspect, re
    from te_harness import swebench as sw
    from te_bench import nudge_guard as generic

    src = inspect.getsource(sw.ClaudeSwebenchRunner.run_task)
    names = set(re.findall(r"(?<![\w.])guard\.([a-z_][a-z0-9_]*)", src))
    names -= {"continuation"}                      # reached through _guard_continuation
    assert "segment" in names, "the loop no longer logs guard.segment; update this test"
    codex_guard = _cs.CodexResumeGuard(_cs.shipped_policy(10))
    for name in sorted(names):
        assert hasattr(codex_guard, name), f"CodexResumeGuard is missing guard.{name}"
    # segment tracks issued nudges, exactly as the generic guard's does
    assert codex_guard.segment == 0
    lines = [_json.dumps({"type": "thread.started", "thread_id": "t-5"})]
    lines += [_cmd(f"rg needle{i} src") for i in range(40)]
    guard = _cs.CodexResumeGuard(_cs.defensive_policy(16))
    for l in lines:
        if guard.monitor(l):
            break
    assert guard.segment == 0 and guard.continuation() is not None and guard.segment == 1
    assert guard.segment == guard.nudges_done


def test_the_codex_envelope_is_advanced_in_the_unit_its_trigger_compares(tmp_path):
    """`CodexEnvelopeTracker` is documented in tool events and the guard's trigger compares
    `state.turns` (command + edit). The row's `num_turns` is, for Codex, agent STEPS
    (messages + tool calls) and runs ~1.45x larger, so an envelope learned from it sits
    ~45% above the count it is compared to and few tasks ever reach the gate. The stage
    must feed the tracker the trigger's own unit.
    """
    from tokeneconomizer.config import SpiralConfig
    from te_bench.spiral_stage import SpiralStage
    cfg = SpiralConfig(codex_spiral=True, codex_spiral_profile="defensive")
    stage = SpiralStage(cfg, max_turns=80, agent="codex")
    srow = {}
    stage.after_task({"instance_id": "x-1"}, {"resolved": True, "num_turns": 29,
                                              "codex_spiral_tool_turns": 20}, str(tmp_path), srow)
    assert stage.codex_tracker.resolved_tool_turns == [20]          # the trigger's unit
    assert srow["codex_spiral_envelope_unit"] == "tool_events"
    assert srow["codex_spiral_tool_turns"] == 20
    # a row from before the field falls back, and says so
    stage.after_task({"instance_id": "x-2"}, {"resolved": True, "num_turns": 31}, str(tmp_path), srow)
    assert stage.codex_tracker.resolved_tool_turns == [20, 31]
    assert srow["codex_spiral_envelope_unit"] == "steps_fallback"
    # and the runner records the guard's own count for the stage to read
    import inspect
    src = inspect.getsource(sw_codex_guard_row := __import__("te_harness.swebench", fromlist=["x"]).CodexSwebenchRunner._guard_row)
    assert "codex_spiral_tool_turns" in src and "tool_turns" in src


def test_the_resume_invocation_is_one_the_installed_cli_accepts():
    """The spiral's whole mechanism is `codex exec resume <session> -` on the same thread.
    Its option surface is SMALLER than `exec`'s (no -C, no --color), and a flag this CLI
    build does not know would fail only at the moment the guard fires -- i.e. once per
    hundred tasks, deep into a paid run. Check the argv against the CLI's own help."""
    import shutil, subprocess, os, re, pytest
    from te_harness import codex
    argv = codex.build_resume_argv("01a09187-c54c-79a1-a40a-fe8ba406a935", hook=True,
                                   effort="high")
    assert argv[1:4] == ["exec", "resume", "01a09187-c54c-79a1-a40a-fe8ba406a935"]
    assert argv[-1] == "-" and "-C" not in argv and "--color" not in argv
    binary = os.environ.get("TE_CODEX_BIN") or shutil.which("codex")
    if not binary:
        pytest.skip("codex-cli not installed")
    out = subprocess.run([binary, "exec", "resume", "--help"], capture_output=True,
                         text=True, timeout=120)
    text = (out.stdout or "") + (out.stderr or "")
    assert "[SESSION_ID]" in text                      # the positional this argv relies on
    for flag in [a for a in argv if a.startswith("--")]:
        assert flag in text, f"`codex exec resume` does not accept {flag}"


def test_the_codex_verification_detector_sees_through_rtks_command_prefix():
    """`codex-trimech-rtk` runs the spiral on an agent told to type `rtk pytest`: the
    verification detector must read the prefixed command as it reads the bare one."""
    for bare in ("python -m pytest tests/ -q", "pytest -q", "bin/test sympy/x.py",
                 "K=1 pytest -q", "pip install foo", "git status"):
        for pre in ("rtk ", "rtk proxy ", "rtk K=2 "):
            assert cs.is_verification_command(pre + bare) == cs.is_verification_command(bare)
    # a compound command keeps its second segment visible too
    assert cs.is_verification_command("cd x && rtk pytest -q")
    assert cs.is_verification_command("rtk pytest -q")
    assert not cs.is_verification_command("rtk git status")
