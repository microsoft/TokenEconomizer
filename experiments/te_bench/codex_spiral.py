#!/usr/bin/env python3
"""Codex-native spiral detection and same-thread recovery.

This module is deliberately separate from :mod:`spiral`.  The historical spiral
implementation supports several agents and restarts a stopped rollout with a fresh
CLI invocation.  Codex exposes stronger signals and a native session-resume command:

* ``command_execution.exit_code`` makes successful verification unambiguous;
* ``thread.started.thread_id`` can be passed to ``codex exec resume``;
* file-change events contain every changed path, not just a generic edit tool name.

The policies below use only those deterministic, locally observable signals.  They have
no paid judge and never terminally abort a task; every rule is a nudge on the *same* Codex
thread.  Two profiles ship (`profile_policy`):

* ``post-green`` (`SHIPPED_POLICY`): one nudge to finalize, once the task's own success
  check has passed unfiltered, every named pytest scope that failed unfiltered has since
  passed, and the trajectory has run past the resolved-task tool envelope;
* ``defensive`` (`DEFENSIVE_POLICY`, what the Codex lane runs unless an arm says
  otherwise, `CODEX_DEFAULT_PROFILE`): the same, plus every deterministic spiral signature
  the controller knows -- repeated failing verification, a repeated command, blocked
  retrieval, a wide or churning patch that keeps failing, a passing check re-run again and
  again, a run past twice the envelope -- at most two nudges per task.

The bare dataclass defaults (`DEFAULT_POLICY`) are a single trigger: after the envelope, a
patch spanning at least 15 distinct files has failed verification after its latest edit.
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import asdict, dataclass, replace
import json
import re
import shlex
from typing import Iterable, Optional


_TEST_RUN_RE = re.compile(
    r"(?i)(?:^|[;&|]\s*|\n\s*|[\"'])"
    # Leading `K=V` assignments, and rtk's Codex-mode prefix (`rtk pytest`, `rtk proxy pytest`,
    # `rtk K=V pytest`): the agent is TOLD to type it on every command, and a detector that
    # did not absorb it would see no test run on `codex-trimech-rtk` -- no trustworthy green,
    # no finalize, every failure signature blind. Kept in step with trace.RTK_PREFIX_RE.
    r"(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+|rtk\s+(?:proxy\s+)?)*"
    r"(?:python(?:\d+(?:\.\d+)?)?\s+-m\s+(?:pytest|unittest|django\s+test)|"
    r"pytest|py\.test|"
    r"(?:python(?:\d+(?:\.\d+)?)?\s+)?(?:\./)?bin/test|"
    r"(?:python(?:\d+(?:\.\d+)?)?\s+)?(?:\./)?(?:tests/)?runtests\.py|"
    r"tox(?:\s|$)|nox(?:\s|$))"
)
_ASSERT_RE = re.compile(
    r"(?i)(?:\bassert\b|assert_|np\.testing|testing\.assert|"
    r"raise_if_dask_computes)"
)
_BLOCKED_RE = re.compile(
    r"(?i)(?:gold-isolation|network (?:is )?unreachable|"
    r"temporary failure in name resolution|could not resolve host|"
    r"connection refused|request to .* blocked)"
)
_FAILURE_ID_RE = re.compile(
    r"(?im)^(?:FAILED|ERROR)\s+([^\s]+)|^_{3,}\s*(.*?)\s*_{3,}$"
)
_FAILURE_LINE_RE = re.compile(
    r"(?i)(?:assertionerror|traceback|\bfailed\b|\berror:|exception)"
)
_RESEARCH_COMMAND_RE = re.compile(
    r"(?i)(?:\brg\b|\bgrep\b|\bsed\s+-n\b|\bfind\b|\bcurl\b|\bwget\b|"
    r"\bgit\s+(?:log|show|blame|branch|tag|fsck)\b|\bnl\s+-ba\b|"
    r"\bpip\s+cache\b|python(?:\d+(?:\.\d+)?)?\s+-\s*<<)"
)
_FINALIZATION_COMMAND_RE = re.compile(
    r"(?i)(?:git\s+(?:diff|status)(?:\s|$)|git\s+diff\s+--check|"
    r"compileall|py_compile|black\s+--check|ruff\s+check|pre-commit\s+run)"
)
# A passing test command whose green must NOT be trusted as a full success signal:
# it narrowed the accepted set (ran a subset, deselected, or re-ran only failures).
# Kept in sync with codex_injection_v3.PytestAcceptanceDetector._FILTERED so the
# spiral post-green gate and the injection post-green hint share ONE definition of a
# trustworthy green.  This is the pytest plug-in's notion of "filtered"; a different
# domain ships its own predicate and the finalize rule/nudge text stay unchanged.
_FILTERED_GREEN_RE = re.compile(
    r"(?i)(?:\s-k\b|--deselect\b|\bdeselect\b|\s-m\s+['\"]"
    r"|--lf\b|--last-failed\b|--ff\b|--failed-first\b)"
)


def green_is_trustworthy(command: str) -> bool:
    """True if a passing test command was a FULL (unfiltered) run, so its green can
    gate post-green finalization.  Narrowed runs (`-k`/`--deselect`/`--lf`/...) are
    not trustworthy: the accepted set they passed is a subset."""
    return not bool(_FILTERED_GREEN_RE.search(command or ""))


# A pytest path/node-id token: an explicit test-file path (optionally a ``::nodeid``).
# Used to attribute a pytest run to the concrete scope(s) it accepted or failed, so a
# green on ONE file cannot mask an unresolved unfiltered failure on ANOTHER.  A bare
# pytest run with no path is the whole-suite scope (``_ALL_SCOPE``).
_TEST_PATH_RE = re.compile(r"(?<![\w/.-])((?:[\w./-]*/)?[\w.-]*test[\w.-]*\.py)")
_ALL_SCOPE = "__ALL__"


def verification_scopes(command: str) -> Optional[set[str]]:
    """Scopes a pytest command accepts/exercises, or ``None`` if it is not a pytest run.

    Returns basenames of the test files named on the command line (e.g.
    ``test_concat.py``), or ``{_ALL_SCOPE}`` for a bare whole-suite pytest run.  Inline
    ``python - <<`` reproducers return ``None`` -- they still drive the green/fail
    counters via :func:`is_verification_command`, but they are not authoritative about
    which *named* test scope passed, so they neither open nor clear outstanding failures.
    """
    command = command or ""
    if not is_test_command(command):
        return None
    paths = {match.group(1).split("::", 1)[0].rsplit("/", 1)[-1]
             for match in _TEST_PATH_RE.finditer(command)}
    paths = {p for p in paths if p}
    return paths or {_ALL_SCOPE}


@dataclass(frozen=True)
class CodexEvent:
    """One event relevant to the Codex spiral controller."""

    kind: str
    command: str = ""
    output: str = ""
    exit_code: Optional[int] = None
    paths: tuple[str, ...] = ()
    thread_id: str = ""


@dataclass(frozen=True)
class CodexSpiralPolicy:
    """Conservative policy parameters, expressed in completed Codex tool events."""

    min_tool_turns: int = 30
    min_failed_verifications: int = 2
    repeated_failure_count: int = 2
    scatter_files: int = 15
    churn_edits: int = 3
    recent_window: int = 12
    repeated_command_count: int = 3
    blocked_attempts: int = 2
    no_progress_turns: int = 8
    post_green_research_turns: int = 4
    scope_checkpoint_files: int = 8
    recovery_failure_grace_turns: int = 0
    recovery_enabled: bool = True
    post_green_enabled: bool = False
    scope_checkpoint_enabled: bool = False
    repeated_failure_enabled: bool = False
    wide_failure_enabled: bool = True
    churn_failure_enabled: bool = False
    repeated_command_enabled: bool = False
    blocked_retrieval_enabled: bool = False
    # Adaptive effort routing starts only after a recent resolved-task window.  If a
    # medium-effort task reaches that learned envelope without a current green result,
    # checkpoint the exact thread and resume it at high effort instead of accepting a
    # quality-risky cheap tail.
    effort_rescue_enabled: bool = False
    # Crossing the envelope on a fresh edit is progress, not a spiral.  Let the medium
    # segment spend a few tool events verifying that edit before paying to rebuild and
    # resume the thread at high effort.  Long localization with no edit is unaffected.
    effort_rescue_progress_grace_turns: int = 3
    resume_effort: str = ""
    nudge_budget: int = 1
    # ---- post-green finalize (codex-native-spiral-v2) ---------------------------
    # The dominant, universal Codex cost sink is NOT thrashing-while-failing (the
    # >=15-file wide_failure region is nearly empty on real traces): it is reaching a
    # trustworthy green early and then wandering -- re-verifying, re-deriving the change
    # from external history, style/lint fishing.  This rule fires ONE same-thread nudge
    # to finalize when the task's own success check has passed UNFILTERED and the
    # trajectory has already run past the learned resolved-task envelope.  It is gated on
    # a trustworthy green, so it can never interrupt an unfinished task or trust a
    # narrowed pass (a filtered green covers only a subset of the accepted set).
    post_green_finalize_enabled: bool = False
    # Grace after the trustworthy green before nudging: never interrupt the accept step
    # itself; only redirect the wandering that follows it.
    post_green_finalize_grace_turns: int = 3
    # An unfiltered green on ONE test file does not prove the patch is done when ANOTHER
    # scope still has an outstanding unfiltered failure.  Two shapes of it:
    #   * a test file failed unfiltered and was re-run only FILTERED, while a different
    #     file then passed unfiltered;
    #   * the full-suite `pytest` failed unfiltered after the last edit and has not passed
    #     since, while a single file then passed.
    # When enabled, finalize additionally requires that NO named pytest scope (and not the
    # whole-suite scope) is carrying an outstanding unfiltered failure -- i.e. every scope
    # that ever failed unfiltered has since been re-run unfiltered and passed.
    post_green_finalize_require_cleared_failures: bool = False
    # ---- defensive rules (2026-09-12) ---------------------------------------------
    # Green re-verification loop: after an edit the task's own check has passed, and
    # then passed again `reverify_loop_count` more times with no edit in between.
    # Re-running a passing check adds no information; it is the one post-green pattern
    # the terra traces contain that is waste by construction.
    reverify_loop_enabled: bool = False
    reverify_loop_count: int = 3
    # Runaway guard: the trajectory has run past `runaway_multiplier` times the learned
    # resolved-task envelope (and past `runaway_floor` events), whatever its state. No
    # baseline task on gpt-5.6-terra reached 2x the envelope, so this is a backstop for a
    # future model or repository, not a lever.
    runaway_enabled: bool = False
    runaway_multiplier: float = 2.0
    runaway_floor: int = 30

    def at_threshold(self, turns: float | int) -> "CodexSpiralPolicy":
        return replace(self, min_tool_turns=max(1, int(round(turns))))


DEFAULT_POLICY = CodexSpiralPolicy()

# The single shipped Codex spiral policy: "post-green finalize, cleared-scopes".
#
# The dominant Codex cost sink is NOT thrashing-while-failing (the >=15-file wide-failure
# region is nearly empty on real traces) but reaching a trustworthy green early and then
# WANDERING -- re-verifying, re-deriving the change from history, style/lint fishing. This
# policy fires ONE same-thread nudge to finalize once the task's own success check has
# passed UNFILTERED and the trajectory has run past the learned resolved-task envelope. It
# is trustworthy-green gated (never trusts a narrowed `-k`/`--lf` pass) and additionally
# requires that every named pytest scope that ever failed unfiltered has since been re-run
# unfiltered and passed -- so a green on ONE file cannot mask an outstanding unfiltered
# failure on ANOTHER (without it, a failure on another file that has not been retired
# reads as a finished patch). The wide-failure/recovery rules are disabled so this measures
# the post-green lever alone. It never terminally aborts a task.
PROFILE_VERSION = "codex-native-spiral-post-green-cleared-scopes"

SHIPPED_POLICY = replace(
    DEFAULT_POLICY,
    post_green_finalize_enabled=True,
    post_green_finalize_require_cleared_failures=True,
    wide_failure_enabled=False,
    recovery_enabled=False)


def shipped_policy(threshold: float | int) -> CodexSpiralPolicy:
    """The single live Codex spiral policy at the repository's task-frozen threshold."""
    return SHIPPED_POLICY.at_threshold(threshold)


# The DEFENSIVE profile (2026-09-12): the shipped post-green finalize plus every
# deterministic spiral signature the controller knows, each a same-thread nudge and none
# an abort, with a budget of two distinct nudges per task. It is not a cost lever -- on the
# gpt-5.6-terra baselines the failure-loop region is empty, tails after a green are one
# event long and nothing runs past 2x the envelope, so it fires on a few percent of tasks
# and its expected cost effect is about zero. It is a net under trajectories that DO
# spiral: repeated failing verification, the same command over and over, blocked
# retrieval, a very wide or churning patch that keeps failing, a passing check re-run
# again and again, or a run past twice the resolved envelope. Reasoning effort is never
# touched.
DEFENSIVE_PROFILE_VERSION = "codex-native-spiral-defensive-v1"
DEFENSIVE_POLICY = replace(
    SHIPPED_POLICY,
    recovery_enabled=True,
    repeated_failure_enabled=True,
    wide_failure_enabled=True,
    churn_failure_enabled=True,
    repeated_command_enabled=True,
    blocked_retrieval_enabled=True,
    reverify_loop_enabled=True,
    runaway_enabled=True,
    nudge_budget=2)


def defensive_policy(threshold: float | int) -> CodexSpiralPolicy:
    """The defensive profile at the repository's task-frozen threshold."""
    return DEFENSIVE_POLICY.at_threshold(threshold)


def profile_policy(profile: str, threshold: float | int) -> CodexSpiralPolicy:
    """`post-green` (the shipped default) or `defensive`, at a threshold."""
    if profile == "defensive":
        return defensive_policy(threshold)
    return shipped_policy(threshold)


def profile_version(profile: str) -> str:
    return DEFENSIVE_PROFILE_VERSION if profile == "defensive" else PROFILE_VERSION


# Which profile the Codex lane runs when an arm does not say otherwise -- the twin of
# `codex_injection.CODEX_DEFAULT_PROFILE`, and settled the same way. `post-green` is the
# SHIPPED policy and remains the dataclass default (so a config that predates this constant
# still means what it meant). `defensive` is what the arms named for the mechanism carry,
# and what `codex-trimech` co-activates. The shipped policy stays reachable, and named, as
# `codex-spiral-postgreen`.
CODEX_DEFAULT_PROFILE = "defensive"


def _normalize(text: str, limit: int = 180) -> str:
    text = (text or "").lower()
    text = re.sub(r"0x[0-9a-f]+", "<hex>", text)
    text = re.sub(r"/tmp/\S+", "/tmp/<tmp>", text)
    text = re.sub(r"\d+", "<n>", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _command_fingerprint(command: str) -> str:
    # Codex normally reports `/bin/bash -lc '<actual command>'`.  Fingerprinting the
    # wrapper (or deleting its quoted body) makes every shell action look identical.
    # Unwrap only the known shell form and retain the inner action; numeric/tmp-path
    # normalization still coalesces incidental variations in genuinely repeated calls.
    command = command or ""
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = []
    if (len(parts) >= 3 and parts[0].rsplit("/", 1)[-1] in ("bash", "sh")
            and parts[1] in ("-c", "-lc")):
        command = parts[2]
    return _normalize(command, limit=320)


def is_test_command(command: str) -> bool:
    return bool(_TEST_RUN_RE.search(command or ""))


def is_verification_command(command: str) -> bool:
    command = command or ""
    if is_test_command(command):
        return True
    is_python_heredoc = "python - <<" in command or "python3 - <<" in command
    return bool(is_python_heredoc and _ASSERT_RE.search(command))


def is_research_command(command: str) -> bool:
    """Whether a post-green command reopens localization rather than wrapping up."""
    command = command or ""
    if is_verification_command(command) or _FINALIZATION_COMMAND_RE.search(command):
        return False
    return bool(_RESEARCH_COMMAND_RE.search(command))


def failure_signatures(output: str) -> set[str]:
    """Extract stable failure identities without retaining source/test contents."""
    found = []
    for match in _FAILURE_ID_RE.finditer(output or ""):
        value = next((group for group in match.groups() if group), None)
        if value:
            found.append(_normalize(value))
    if not found:
        lines = [line.strip() for line in (output or "").splitlines()
                 if _FAILURE_LINE_RE.search(line)]
        found.extend(_normalize(line) for line in lines[:2])
    return {value for value in found if value}


# Patches applied THROUGH THE SHELL. On some model providers the CLI stops emitting
# `file_change` items altogether: every edit arrives as a `command_execution` whose command
# is `apply_patch <<'PATCH' ... *** Begin Patch ... *** Update File: <p>`. A controller that
# only reads `file_change` therefore never sees an edit there, never registers a trustworthy
# green, and never fires. The paths are read from the patch header lines, and the
# edit counts when the apply succeeded (the CLI's own "Success." marker, or exit 0).
_PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File:\s*(.+?)\s*$", re.M)
_GIT_APPLY_RE = re.compile(r"\bgit\s+apply\b")
_GIT_APPLY_PATH_RE = re.compile(r"^\+\+\+ b/(.+?)\s*$", re.M)
_PATCH_SUCCESS_RE = re.compile(r"(?i)\bSuccess\.\s*(?:Updated|Applied)")


def patch_paths(command: str) -> tuple[str, ...]:
    """Paths a shell-applied patch command touches, () when it is not one."""
    command = command or ""
    if "*** Begin Patch" in command:
        found = _PATCH_FILE_RE.findall(command)
    elif _GIT_APPLY_RE.search(command):
        found = _GIT_APPLY_PATH_RE.findall(command)
    else:
        return ()
    return tuple(dict.fromkeys(p.strip() for p in found if p.strip()))


def shell_patch_applied(command: str, output: str, exit_code) -> bool:
    """True when a shell-applied patch command actually changed files."""
    if not patch_paths(command):
        return False
    if _PATCH_SUCCESS_RE.search(output or ""):
        return True
    try:
        return int(exit_code) == 0
    except (TypeError, ValueError):
        return False


def parse_events(line: str) -> list[CodexEvent]:
    """Parse one ``codex exec --json`` line into controller events, in causal order.

    A native `file_change` item is one edit event. A `command_execution` is one command
    event -- preceded by an edit event when the command itself applied a patch through the
    shell, so a verification chained after the patch in the same command is seen as
    following the edit, exactly as it does."""
    try:
        event = json.loads(line)
    except Exception:
        return []
    if event.get("type") == "thread.started":
        return [CodexEvent("thread", thread_id=event.get("thread_id") or "")]
    if event.get("type") != "item.completed":
        return []
    item = event.get("item") or {}
    kind = item.get("type")
    if kind == "command_execution":
        command = item.get("command") or ""
        output = item.get("aggregated_output") or ""
        exit_code = item.get("exit_code")
        try:
            exit_code = int(exit_code) if exit_code is not None else None
        except (TypeError, ValueError):
            pass
        out = []
        if shell_patch_applied(command, output, exit_code):
            out.append(CodexEvent("edit", paths=patch_paths(command)))
        out.append(CodexEvent("command", command=command, output=output,
                              exit_code=exit_code))
        return out
    if kind == "file_change":
        paths = tuple(
            change.get("path") for change in (item.get("changes") or [])
            if isinstance(change, dict) and change.get("path")
        )
        return [CodexEvent("edit", paths=paths)]
    return []


def iter_events(lines: Iterable[str], *, first_thread_only: bool = False):
    """Yield parsed events, optionally excluding suffixes made by an old restart."""
    threads = 0
    for line in lines:
        for event in parse_events(line):
            if event.kind == "thread":
                threads += 1
                if first_thread_only and threads > 1:
                    return
            yield event


class CodexSpiralState:
    """Incremental Codex-only process state used identically live and offline."""

    def __init__(self, recent_window: int = DEFAULT_POLICY.recent_window):
        self.turns = 0
        self.command_turns = 0
        self.edit_events = 0
        self.edited_paths = Counter()
        self.last_edit_turn = 0
        self.last_green_turn = 0
        self.last_progress_turn = 0
        self.last_failed_verification_turn = 0
        self.verification_runs = 0
        self.post_green_research = 0
        self.failed_verifications_since_green = 0
        self.failed_verifications_after_edit = 0
        self.failure_sigs_since_green = Counter()
        self.recent_commands = deque(maxlen=recent_window)
        self.recent_blocked = deque(maxlen=recent_window)
        self.thread_id = ""
        self.last_decision_reason = ""
        # Turn of the first TRUSTWORTHY (unfiltered) green after an edit, and whether the
        # current green is trustworthy.  Gates post-green finalization: only a full,
        # unfiltered pass may license "you are done, finalize".
        self.first_trustworthy_green_turn = 0
        self.last_green_trustworthy = False
        # Named pytest scopes whose MOST RECENT unfiltered run failed and has not since
        # been re-run unfiltered and passed.  Whole-suite failures use ``_ALL_SCOPE``.
        # Only pytest runs (not inline reproducers) open or clear scopes.  A non-empty set
        # means "somewhere an unfiltered test is still red" -- post-green finalize must not
        # trust a green on a different scope while it is non-empty.
        self.outstanding_failure_scopes: set[str] = set()
        # Green verification runs after the FIRST green that followed the latest edit,
        # with no edit in between -- the "re-verification loop" counter. Reset on edit.
        self.green_reruns_since_edit = 0

    @property
    def distinct_edit_files(self) -> int:
        return len(self.edited_paths)

    @property
    def edit_churn(self) -> int:
        return max(self.edited_paths.values(), default=0)

    @property
    def green_after_edit(self) -> bool:
        return bool(self.last_edit_turn and self.last_green_turn > self.last_edit_turn)

    @property
    def currently_green(self) -> bool:
        return bool(self.last_green_turn > max(
            self.last_edit_turn, self.last_failed_verification_turn))

    @property
    def repeated_failure_count(self) -> int:
        return max(self.failure_sigs_since_green.values(), default=0)

    def update(self, event: CodexEvent) -> None:
        if event.kind == "thread":
            if event.thread_id:
                self.thread_id = event.thread_id
            return
        if event.kind not in ("command", "edit"):
            return
        self.turns += 1
        if event.kind == "edit":
            self.edit_events += 1
            self.last_edit_turn = self.turns
            self.last_progress_turn = self.turns
            self.failed_verifications_after_edit = 0
            self.post_green_research = 0
            self.green_reruns_since_edit = 0
            for path in event.paths:
                self.edited_paths[path] += 1
            self.recent_commands.append("<edit>")
            self.recent_blocked.append(False)
            return

        self.command_turns += 1
        command = event.command or ""
        self.recent_commands.append(_command_fingerprint(command))
        self.recent_blocked.append(bool(_BLOCKED_RE.search(event.output or "")))
        if not is_verification_command(command):
            if self.currently_green and is_research_command(command):
                self.post_green_research += 1
            return
        self.verification_runs += 1
        filtered = not green_is_trustworthy(command)
        scopes = verification_scopes(command)
        if event.exit_code == 0:
            # A green targeted test/reproducer supersedes earlier failures: the failure
            # counters below restart from zero.
            if self.last_edit_turn and self.last_green_turn > self.last_edit_turn:
                # already green after this edit: this run re-verifies a passing state
                self.green_reruns_since_edit += 1
            self.last_green_turn = self.turns
            self.last_progress_turn = self.turns
            self.failed_verifications_since_green = 0
            self.failed_verifications_after_edit = 0
            self.failure_sigs_since_green.clear()
            self.post_green_research = 0
            # Clear outstanding unfiltered failures this UNFILTERED pass actually retired.
            # A filtered green retires nothing (it narrowed the accepted set); an inline
            # reproducer (scopes is None) is not authoritative about a named scope.  A
            # whole-suite unfiltered green clears everything.
            if not filtered and scopes is not None:
                if _ALL_SCOPE in scopes:
                    self.outstanding_failure_scopes.clear()
                else:
                    # A NARROW green retires only its own scope(s).  It must NOT clear a
                    # whole-suite (`_ALL_SCOPE`) failure: a green on one file says nothing
                    # about the rest of the suite, so it would mask an unresolved full-suite
                    # failure.  Only a whole-suite unfiltered green clears `_ALL_SCOPE`.
                    self.outstanding_failure_scopes -= scopes
            # Record whether THIS green was a full unfiltered pass.  Only a trustworthy
            # green (and only when it follows an edit) can license post-green finalize.
            trustworthy = bool(self.last_edit_turn) and not filtered
            self.last_green_trustworthy = trustworthy
            if trustworthy and not self.first_trustworthy_green_turn:
                self.first_trustworthy_green_turn = self.turns
            return
        if event.exit_code is None:
            return
        self.last_failed_verification_turn = self.turns
        self.post_green_research = 0
        self.failed_verifications_since_green += 1
        if self.last_edit_turn:
            self.failed_verifications_after_edit += 1
        # An UNFILTERED pytest failure marks its scope(s) outstanding until re-greened
        # unfiltered.  Filtered failures and inline reproducers do not (a filtered run's
        # verdict is about a subset; a reproducer names no scope).
        if not filtered and scopes is not None:
            self.outstanding_failure_scopes |= scopes
        for signature in failure_signatures(event.output):
            self.failure_sigs_since_green[signature] += 1

    def decision(self, policy: CodexSpiralPolicy = DEFAULT_POLICY) -> Optional[str]:
        """Return a deterministic nudge reason, or ``None`` to continue untouched."""
        if self.turns < policy.min_tool_turns:
            return None
        # Post-green finalize (codex-native-spiral-v2): the task's own success check has
        # passed UNFILTERED after an edit, yet the trajectory has run past the learned
        # resolved-task envelope (enforced by min_tool_turns above) and kept going.  Fire
        # one same-thread nudge to finalize.  Gated on a TRUSTWORTHY current green, so it
        # never interrupts an unfinished task and never trusts a narrowed pass; a grace
        # window keeps it from interrupting the accept step itself.
        if policy.post_green_finalize_enabled:
            grace = max(0, int(policy.post_green_finalize_grace_turns))
            blocked = (policy.post_green_finalize_require_cleared_failures
                       and self.outstanding_failure_scopes)
            # `blocked`: somewhere an unfiltered test is still red on a scope this green
            # did not retire -- do not finalize on that ground. The rules below may still
            # apply.
            if (not blocked and self.currently_green and self.last_green_trustworthy
                    and self.first_trustworthy_green_turn
                    and self.turns - self.first_trustworthy_green_turn >= grace):
                return "validated-patch-should-finalize"
        # Defensive rules. Both are checked BEFORE the
        # edited-paths gate: a run past twice the envelope with no edit is a localization
        # spiral, not a healthy trajectory.
        if (policy.reverify_loop_enabled and self.currently_green
                and self.green_reruns_since_edit >= policy.reverify_loop_count):
            return "green-reverification-loop"
        if policy.runaway_enabled and self.turns >= max(
                int(policy.runaway_floor),
                int(round(policy.runaway_multiplier * policy.min_tool_turns))):
            return "beyond-twice-the-resolved-envelope"
        if policy.effort_rescue_enabled and not self.currently_green:
            grace = max(0, int(policy.effort_rescue_progress_grace_turns))
            if (self.last_progress_turn
                    and self.turns - self.last_progress_turn < grace):
                return None
            # This rule intentionally also covers long localization with no edit: that
            # is precisely where continuing indefinitely at reduced effort is riskiest.
            return "medium-effort-envelope-rescue"
        if not self.edited_paths:
            return None
        # A verified patch can still accumulate a costly Codex-specific tail when the
        # model reopens localization instead of finalizing.  Redirect only after several
        # unmistakably research-oriented commands; ordinary diff/status/lint/test closure
        # is deliberately excluded from this counter.
        if policy.post_green_enabled and self.currently_green:
            if self.post_green_research >= policy.post_green_research_turns:
                return "validated-patch-reopened-research"
            return None
        # Large multi-file ports are a characteristic Codex cost sink.  This is not a
        # "you are wrong" nudge: it is a one-shot checkpoint asking the same thread to
        # verify the accumulated patch before expanding it further.  It can therefore
        # fire before a failure, while the recovery rules below remain failure-gated.
        if (policy.scope_checkpoint_enabled
                and self.distinct_edit_files >= policy.scope_checkpoint_files):
            return "wide-edit-scope-needs-verification"
        if not policy.recovery_enabled:
            return None
        failures = self.failed_verifications_since_green
        after_edit = self.failed_verifications_after_edit
        latest_patch_failed = self.last_failed_verification_turn > self.last_edit_turn
        if not latest_patch_failed:
            return None
        # Experimental rules may require a quiet grace window after the newest failure.
        # Live v1 uses zero here because its prompt is a non-terminal checkpoint reserved
        # for the much narrower >=15-file condition, not a generic failure intervention.
        if (self.turns - self.last_failed_verification_turn
                < policy.recovery_failure_grace_turns):
            return None

        if (policy.repeated_failure_enabled
                and failures >= policy.min_failed_verifications
                and self.repeated_failure_count >= policy.repeated_failure_count):
            return "repeated-failing-verification"
        if (policy.wide_failure_enabled
                and self.distinct_edit_files >= policy.scatter_files
                and after_edit >= policy.min_failed_verifications):
            return "very-wide-patch-still-failing"
        if (policy.churn_failure_enabled
                and self.edit_churn >= policy.churn_edits
                and after_edit >= policy.min_failed_verifications
                and self.repeated_failure_count >= policy.repeated_failure_count):
            return "edit-churn-still-failing"

        no_progress = self.turns - self.last_progress_turn
        commands = [value for value in self.recent_commands if value != "<edit>"]
        same_command = max(Counter(commands).values(), default=0)
        if (policy.repeated_command_enabled
                and same_command >= policy.repeated_command_count
                and no_progress >= policy.no_progress_turns
                and failures):
            return "repeated-command-without-progress"
        if (policy.blocked_retrieval_enabled
                and sum(self.recent_blocked) >= policy.blocked_attempts
                and no_progress >= policy.no_progress_turns
                and failures):
            return "blocked-retrieval-without-progress"
        return None

    def snapshot(self) -> dict:
        return {
            "tool_turns": self.turns,
            "command_turns": self.command_turns,
            "edit_events": self.edit_events,
            "distinct_edit_files": self.distinct_edit_files,
            "edit_churn": self.edit_churn,
            "last_edit_turn": self.last_edit_turn,
            "last_green_turn": self.last_green_turn,
            "last_failed_verification_turn": self.last_failed_verification_turn,
            "failed_verifications_since_green": self.failed_verifications_since_green,
            "failed_verifications_after_edit": self.failed_verifications_after_edit,
            "repeated_failure_count": self.repeated_failure_count,
            "green_after_edit": self.green_after_edit,
            "currently_green": self.currently_green,
            "post_green_research": self.post_green_research,
            "verification_runs": self.verification_runs,
            "first_trustworthy_green_turn": self.first_trustworthy_green_turn,
            "last_green_trustworthy": self.last_green_trustworthy,
            "outstanding_failure_scopes": sorted(self.outstanding_failure_scopes),
            "green_reruns_since_edit": self.green_reruns_since_edit,
        }


def replay_events(events: Iterable[CodexEvent], policy: CodexSpiralPolicy = DEFAULT_POLICY):
    """Return ``(first_decision, final_state)`` for an offline trajectory."""
    state = CodexSpiralState(policy.recent_window)
    decision = None
    for event in events:
        state.update(event)
        if decision is None:
            reason = state.decision(policy)
            if reason:
                decision = {
                    "turn": state.turns,
                    "reason": reason,
                    "snapshot": state.snapshot(),
                }
    return decision, state


def _percentile(values: list[int], pct: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = (pct / 100.0) * (len(ordered) - 1)
    lower = int(position)
    fraction = position - lower
    if lower + 1 == len(ordered):
        return float(ordered[-1])
    return ordered[lower] + fraction * (ordered[lower + 1] - ordered[lower])


class CodexEnvelopeTracker:
    """Online resolved-task envelope measured in the same tool-event unit as live state."""

    def __init__(self, *, cold_start: int = 24, floor: int = 20,
                 min_samples: int = 5, percentile: float = 90.0,
                 margin: float = 1.0):
        self.cold_start = cold_start
        self.floor = floor
        self.min_samples = min_samples
        self.percentile = percentile
        self.margin = margin
        self.resolved_tool_turns: list[int] = []

    @property
    def threshold(self) -> float:
        if len(self.resolved_tool_turns) < self.min_samples:
            return float(self.cold_start)
        learned = _percentile(self.resolved_tool_turns, self.percentile)
        return float(max(self.floor, (learned or 0) * self.margin))

    def update(self, tool_turns: int, resolved: bool) -> None:
        if resolved and tool_turns:
            self.resolved_tool_turns.append(int(tool_turns))

    def snapshot(self) -> dict:
        return {
            "threshold": self.threshold,
            "resolved_samples": len(self.resolved_tool_turns),
            "resolved_tool_turns": list(self.resolved_tool_turns),
        }


def nudge_text(reason: str) -> str:
    if reason == "validated-patch-should-finalize":
        # Deliberately DOMAIN-NEUTRAL: no `git diff`, no "test suite", no pytest.  The
        # firing gate (trustworthy unfiltered green + envelope) is precise and lives in
        # the detector; the model-visible text is pure finalize-instead-of-wander
        # guidance that reads the same for a test suite, a build, a linter, or a domain
        # validator.  It never narrows the success check.
        return (
            "Automated process checkpoint: the task's own success check has already "
            "passed after your latest change, and this trajectory has run longer than "
            "usual for a completed task. Preserve the current working state and continue "
            "this same session -- do not restart your investigation, re-derive the change "
            "from scratch, or reconstruct it from external history. Review what you have "
            "already changed, run only the narrowest remaining check needed for "
            "confidence, and finish once it still passes. Reopen the work only if that "
            "check surfaces a concrete, current failure."
        )
    if reason == "green-reverification-loop":
        return (
            "Automated process checkpoint: the task's own success check has now passed "
            "several times in a row with no change in between, so running it again will "
            "not add information. Preserve the current working state and continue this "
            "same session. If a concrete step remains, do the smallest one and check it "
            "once; otherwise finish now."
        )
    if reason == "beyond-twice-the-resolved-envelope":
        return (
            "Automated process checkpoint: this trajectory has run more than twice as long "
            "as completed tasks in this repository usually do. Continue this same session "
            "and preserve the working tree. State in one line the single most likely "
            "remaining cause, inspect the current diff and the latest concrete failure, "
            "then commit to one targeted change and one narrow verification. Do not "
            "restart localization or broaden the search."
        )
    if reason == "medium-effort-envelope-rescue":
        return (
            "Automated effort checkpoint: this task has crossed the recent resolved-task "
            "tool envelope at medium reasoning effort. Continue this exact thread and "
            "preserve the working tree; reasoning effort is now high. Inspect the current "
            "diff and strongest local evidence, commit to the most likely remaining "
            "hypothesis, and run the narrowest verification that can confirm it. Do not "
            "restart localization or discard a justified partial patch."
        )
    if reason == "very-wide-patch-still-failing":
        return (
            "Automated process checkpoint: the current patch spans at least fifteen "
            "files, and verification after the latest edit failed. Preserve the current "
            "working tree and continue this same task. Before expanding the patch, inspect "
            "`git diff --stat` and the latest failure, then run the narrowest diagnostic "
            "that can distinguish a necessary cross-cutting change from an over-broad "
            "port. Keep the breadth if local evidence requires it; otherwise reduce scope "
            "and fix the concrete failure. Before finishing, re-read the issue and verify "
            "every affected public entry point."
        )
    if reason == "wide-edit-scope-needs-verification":
        return (
            "Automated process checkpoint: the current patch now spans many files without "
            "a successful verification after the latest edit. Preserve the current working "
            "tree and do not restart localization. Pause further scope expansion, inspect "
            "`git diff --stat`, and run the narrowest relevant verification now. Use that "
            "result to decide whether every touched file is necessary; continue the same "
            "implementation if the breadth is justified."
        )
    if reason == "validated-patch-reopened-research":
        return (
            "Automated process checkpoint: a verification passed after the latest edit, but "
            "the session has returned to repository research. Preserve the validated patch "
            "and do not restart localization. Inspect `git diff`, run only the narrowest "
            "remaining final check needed for confidence, and finish if it stays green. "
            "Reopen the implementation only if that check exposes a concrete failure."
        )
    focus = {
        "repeated-failing-verification":
            "The same verification failure is recurring after attempted fixes.",
        "scattered-edits-still-failing":
            "The patch has spread across many files and verification is still failing.",
        "edit-churn-still-failing":
            "Repeated edits have not changed the verification outcome.",
        "repeated-command-without-progress":
            "The same command pattern is recurring without a successful verification.",
        "blocked-retrieval-without-progress":
            "Blocked external retrieval is recurring without advancing the local fix.",
    }.get(reason, "The recent process is repeating without a successful verification.")
    return (
        "Automated process check: " + focus + " Continue this same task and preserve the "
        "current working tree, but switch to a materially different hypothesis. Inspect "
        "`git diff` and the latest failing verification first; prefer local repository "
        "evidence over retrying blocked external retrieval. Do not restart localization "
        "from scratch. Once a targeted verification passes after the latest edit, finish "
        "with the narrowest sufficient final check."
    )


class CodexResumeGuard:
    """Live one-shot detector that continues only through native Codex resume."""

    def __init__(self, policy: CodexSpiralPolicy = DEFAULT_POLICY, *, verbose: bool = False):
        self.policy = policy
        self.verbose = verbose
        self.state = CodexSpiralState(policy.recent_window)
        self.nudges_done = 0
        self.pending_reason: Optional[str] = None
        self.fired_reasons: set[str] = set()   # one nudge per reason per task
        self.events: list[dict] = []

    def monitor(self, line: str) -> bool:
        events = parse_events(line)
        if not events:
            return False
        for event in events:
            self.state.update(event)
        event = events[-1]
        if event.kind == "thread" or self.nudges_done >= self.policy.nudge_budget:
            return False
        reason = self.state.decision(self.policy)
        # A missing thread id must never degrade into the harmful fresh-session path.
        # A reason that already produced a nudge on this task does not get a second one:
        # the budget buys DIFFERENT interventions, never the same one twice.
        if not reason or not self.state.thread_id or reason in self.fired_reasons:
            return False
        self.fired_reasons.add(reason)
        self.pending_reason = reason
        record = {
            "action": "resume-nudge",
            "reason": reason,
            "thread_id": self.state.thread_id,
            "snapshot": self.state.snapshot(),
        }
        self.events.append(record)
        if self.verbose:
            print(f"  [codex-spiral] RESUME-NUDGE at tool turn {self.state.turns} "
                  f"({reason}) {record['snapshot']}")
        return True

    @property
    def segment(self) -> int:
        """Segments this guard has opened beyond the first, i.e. nudges issued.

        The SHARED task loop (`te_harness.swebench` run_task) logs `guard.segment` right
        after a continuation, and the generic `spiral.NudgeGuard` exposes it -- so a Codex
        guard must expose it too, or the loop raises on its FIRST nudge. `nudges_done`
        carries the same count and is incremented in `continuation()` before the log line
        reads it, so the two agree by construction.
        `tests/test_spiral.py` pins the whole attribute contract, not just this one name.
        """
        return self.nudges_done

    def continuation(self):
        """Return the explicit resume request consumed by ``run_agent.run_agent``."""
        if not self.pending_reason or not self.state.thread_id:
            return None
        reason = self.pending_reason
        self.pending_reason = None
        self.nudges_done += 1
        request = {
            "mode": "resume",
            "session_id": self.state.thread_id,
            "prompt": nudge_text(reason),
        }
        if self.policy.resume_effort:
            request["effort"] = self.policy.resume_effort
        return request

    def telemetry(self) -> dict:
        return {
            "policy": asdict(self.policy),
            "nudges": self.nudges_done,
            "events": list(self.events),
            "snapshot": self.state.snapshot(),
            "thread_id_captured": bool(self.state.thread_id),
            "paid_judge_effective_tokens": 0,
            "resume_effort": self.policy.resume_effort or None,
        }
