"""`TurnGuard`: the loop guard for one prompt-turn, one tool call at a time.

Each tool call arrives as a separate hook process, so the guard's state is on disk between
calls (`to_dict` / `TurnGuard(data=...)`). Per call it folds the result into a `SpiralState`,
checks the cumulative backstop, and asks `rules.assess_abort` (gate -> optional paid judge ->
envelope fallback) whether the turn is stuck; the first stuck verdict is answered with a
redirect, a later one with a stop verdict (which the plugin delivers as a notice to the person,
never as a stop). After a redirect the per-segment state starts afresh, which is the agent's fair
window to recover.

  * The unit is a PROMPT-TURN (one user prompt to the agent's stop): in interactive use a person
    already splits work into short turns, and a whole session folded as one trajectory looks
    stuck merely by being long.
  * The redirect is delivered mid-turn, attached to the tool result.
  * There is no grader, so the per-repository envelope learns from turns that ENDED GREEN: the
    agent stopped on its own, the guard did not stop it, and no verification after its last edit
    failed (`resolved_proxy`).
"""
import os
import re
import shlex
import time

from .rules import DEFAULT_THRESHOLDS, assess_abort, is_stuck, nudge_text
from .shell_edits import (_split_heredocs, fold_shell_edits, shell_edit_targets,
                          shell_revert_targets)
from .state import ERR_RE, TEST_RUN_RE, SpiralState, canon_tool

_SLOTS = SpiralState.__slots__

# What counts as running the project's own checks, across ecosystems. Broader than
# `state.TEST_RUN_RE` (the Python vocabulary the guard's thresholds were tuned on); used ONLY
# for the ended-green proxy, never to change what `SpiralState` counts. A runner
# is often named by path (`python tests/runtests.py`, `.venv/bin/pytest`, `./node_modules/.bin/
# jest`): without the optional directories those turns showed no verification at all, so a turn
# whose last test run failed still counted as ended green and fed the envelope.
VERIFY_RE = re.compile(
    r"(?i)(?:^|[\s;&|(])(?:(?:~?/)?(?:[\w.\-]+/)*python\d?(?:\.\d+)?\s+)?"
    r"(?:~?/)?(?:[\w.\-]+/)*(?:pytest|py\.test|tox|nox|python\d?(?:\.\d+)?\s+-m\s+(?:pytest|unittest)|"
    r"manage\.py\s+test|runtests(?:\.py)?|jest|vitest|mocha|ava|playwright\s+test|"
    r"(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test|go\s+test|cargo\s+(?:test|nextest)|"
    r"(?:mvn|mvnw|\./mvnw)\s+(?:\S+\s+)*test|(?:gradle|gradlew|\./gradlew)\s+(?:\S+\s+)*test|"
    r"make\s+(?:test|check)|ctest|rspec|phpunit|dotnet\s+test|swift\s+test|mix\s+test|"
    r"deno\s+test|node\s+--test|bazel\s+test|(?:\./)?bin/test)(?![\w.\-/])")

# A command line's exit status is its LAST command's, so it may not be the test run's: `npm test
# 2>&1 | tail -n 8` exits with `tail`'s status, `pytest | grep FAILED` with `grep`'s, `python -m
# unittest; echo "exit=$?"` and `npm test || true` with 0, and `pytest -q; git diff` with
# `git`'s. For such a test run the runner's own summary line decides (`runner_verdict`). Without
# one, a run whose status was replaced (a pipe, `||`, a report of the status) is read by its
# output's markers, as a call whose status is unknown is; a run followed by other commands keeps
# the status the agent reported, since their output is in the result too.
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
_QUOTED_RE = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")
_SEP_RE = re.compile(r"(&&|\|\||\|&|(?<!>)\||;|\n)")    # `|`, but not the `>|` redirect
# A runner asked for its help, version or test list, not to run the tests.
_NOT_A_RUN_RE = re.compile(r"\s(?:-h|--help|--version|--collect-only|--co|--list\S*)(?!\S)")
# What may precede the command itself: `FOO=1 timeout 60 npm test`, and a launcher that runs
# it in the project's environment, `uv run pytest`, `npx jest`, `bundle exec rspec`.
_PREFIX_RE = re.compile(r"^\s*(?:(?:\w+=\S*|time|nice|nohup|command|env|"
                        r"timeout(?:\s+(?:-\S+|[A-Z]+|\d\S*))+|"
                        r"(?:(?:uv|poetry|pipenv|pdm|hatch|rye)\s+run|npx|bunx|"
                        r"(?:npm|pnpm|yarn)\s+(?:exec|dlx)|bundle\s+exec|xvfb-run)"
                        r"(?:\s+-\S+)*|"
                        # `dotenv -e .env.local -- npx vitest run`: dotenv-cli's options, then `--`
                        r"dotenv(?:\s+(?!--(?:\s|$))\S+)*\s+--)\s+)*")
# What only reports on the run before it: `echo "exit=$?"`, `true`, `exit 0`.
_REPORT_RE = re.compile(r"^\s*(?:echo|printf|true|:|exit)(?:\s|$)")
_RANK = {"own": 0, "hidden": 1, "mixed": 2}


def _pieces(cmd):
    """`cmd` as its commands and the separators between them (`[cmd, sep, cmd, ...]`), with
    quoted text masked, and whether it sets `pipefail`. A command recorded as `bash -lc
    '<script>'` (Codex) is split by its script."""
    try:
        parts = shlex.split(cmd)
    except ValueError:
        parts = []
    if (len(parts) >= 3 and os.path.basename(parts[0]) in _SHELLS
            and parts[1].startswith("-") and "c" in parts[1]):
        cmd = parts[2]
    pieces = _SEP_RE.split(re.sub(r"[(){}]", " ", _QUOTED_RE.sub("Q", cmd)))
    while len(pieces) > 1 and not pieces[-1].strip():      # a trailing `;` or newline
        del pieces[-2:]
    return pieces, "pipefail" in cmd


# Commands whose own failure does not matter to the loop guard: they read (their output is
# content, not an outcome), move about, set up or report. A later command may take over the exit
# status from one of them without hiding a failure of the work.
_QUIET_HEADS = frozenset({
    "cat", "head", "tail", "less", "more", "wc", "sort", "uniq", "cut", "tr", "nl", "column",
    "ls", "tree", "pwd", "stat", "file", "du", "which", "type", "whereis", "grep", "egrep",
    "fgrep", "rg", "ag", "jq", "find", "echo", "printf", "true", ":", "cd", "pushd", "popd",
    "export", "source", ".", "set", "sed", "git"})
_GIT_READS = frozenset({"show", "diff", "log", "status", "grep", "ls-files", "blame",
                        "rev-parse", "branch"})


def _quiet(stage):
    words = _PREFIX_RE.sub("", stage.strip() + " ").split()     # `SP=/tmp/x;` alone sets up
    if not words:
        return True
    head = os.path.basename(words[0])
    if head not in _QUIET_HEADS:
        return False
    if head == "git":
        return len(words) > 1 and words[1] in _GIT_READS
    if head == "sed":                                  # `sed -i` edits
        return not any(re.match(r"^(?:-[a-zA-Z]*i|--in-place)", w) for w in words[1:])
    if head == "find":
        return not {"-delete", "-exec", "-execdir", "-ok"} & set(words[1:])
    return True


def exit_status_covers(cmd):
    """Does `cmd`'s exit status say whether the work it ran failed? Yes for one command, an `&&`
    chain or a pipe under `pipefail`, and when every command whose status a later one replaces
    (through `|`, `||`, `;` or a new line) only reads, moves about, sets up or reports
    (`grep -rn Error src | head`, `cd web; npx vitest run`). Not when one that does work could
    have failed unseen: `python repro.py 2>&1 | tail -20` exits with `tail`'s status. A heredoc's
    lines are the input of the command that reads them, not commands of their own."""
    if not isinstance(cmd, str) or not cmd.strip():
        return False
    pieces, pipefail = _pieces(_split_heredocs(cmd)[0])
    for i in range(0, len(pieces) - 1, 2):              # every command but the last
        sep = pieces[i + 1]
        if sep == "&&" or (pipefail and sep in ("|", "|&")):
            continue
        if not _quiet(pieces[i]):
            return False
    return True


def status_of_tests(cmd):
    """What `cmd`'s exit status says of the test runs in it: None when it runs none; "own" when
    it is theirs (a run is last, or followed only by `&&`, or piped under `pipefail`); "hidden"
    when a pipe, `||` or a mere report of the status replaced it; "mixed" when other commands
    ran after, their output in the result too. A command recorded as `bash -lc '<script>'`
    (Codex) is judged by its script."""
    if not isinstance(cmd, str):
        return None
    pieces, pipefail = _pieces(cmd)
    found = None
    for i in range(0, len(pieces), 2):
        stage = pieces[i]
        if not VERIFY_RE.match(_PREFIX_RE.sub("", stage)) or _NOT_A_RUN_RE.search(stage):
            continue
        status = "own"
        for sep, then in zip(pieces[i + 1::2], pieces[i + 2::2]):
            if sep == "&&" or (pipefail and sep in ("|", "|&")):
                continue
            if sep in ("|", "|&", "||"):
                status = "hidden"
                break
            if not _REPORT_RE.match(then):
                status = "mixed"
                break
            status = "hidden"
        if found is None or _RANK[status] > _RANK[found]:
            found = status
    return found


# A test runner's summary of its run, which holds whatever the command line's exit status.
_RUN_FAILED_RE = re.compile("|".join((
    r"\b[1-9]\d* (?:failed|errors?)\b.*\bin \d+(?:\.\d+)?s\b",    # pytest
    r"^FAILED \((?:failures|errors)=",                             # unittest, Django
    r"^tests finished:.*\b[1-9]\d* (?:failed|exceptions?)\b",      # sympy's bin/test
    r"^# fail [1-9]",                                               # TAP (`node --test`)
    r"^Tests:.*\b[1-9]\d* failed\b",                               # jest
    r"^\s*Tests\s+[1-9]\d* failed\b",                               # vitest
    r"^\s*[1-9]\d* failing\b",                                      # mocha
    r"^\s*[1-9]\d* failed\s*$",                                      # Playwright
    r"^(?:--- FAIL: |FAIL\t|FAIL$)",                                # go
    r"^test result: FAILED",                                        # cargo
    r"\b\d+ examples?, [1-9]\d* failures?\b",                       # rspec
    r"Tests run: \d+, Failures: (?:[1-9]|0, Errors: [1-9])",        # JUnit (Maven, Gradle)
    r"BUILD FAIL(?:URE|ED)")), re.MULTILINE)
_RUN_PASSED_RE = re.compile("|".join((
    r"\b\d+ passed\b.*\bin \d+(?:\.\d+)?s\b",
    r"^OK(?: \(.*\))?\s*$",
    r"^tests finished: \d+ passed",
    r"^# fail 0\b",
    r"^Tests:\s+\d+ passed",
    r"^\s*Tests\s+\d+ passed",
    r"^\s*\d+ passing\b",
    r"^\s*\d+ passed \(\d+(?:\.\d+)?(?:ms|s|m|h)\)",              # Playwright
    r"^ok\s+\S+\s+(?:\d+(?:\.\d+)?s|\(cached\))",
    r"^test result: ok\.",
    r"\b\d+ examples?, 0 failures\b",
    r"BUILD SUCCESS")), re.MULTILINE)


def runner_verdict(text):
    """True when a test runner's summary in `text` says its run failed, False when one says it
    passed (and none failed), None when `text` holds no summary."""
    if not text:
        return None
    if _RUN_FAILED_RE.search(text):
        return True
    return False if _RUN_PASSED_RE.search(text) else None


_EDIT = {"edit", "create"}


def state_to_dict(st):
    return {k: getattr(st, k) for k in _SLOTS}


def state_from_dict(d):
    st = SpiralState()
    for k in _SLOTS:
        if k in (d or {}):
            setattr(st, k, d[k])
    return st


class TurnGuard:
    """The loop guard for one prompt-turn, persisted between hook calls via `to_dict`."""

    JUDGE_BASE_GAP = 5
    JUDGE_MAX_GAP = 20
    # A background verdict that never came back (its job died) stops blocking the next one
    # after this long; a side call is capped at 180 s.
    JUDGE_STALE_S = 300

    def __init__(self, turn_key="", data=None):
        d = data or {}
        self.turn_key = d.get("turn_key", turn_key)
        self.st = state_from_dict(d.get("st"))          # this segment; reset after a nudge
        self.whole = state_from_dict(d.get("whole"))    # the whole turn; never reset
        self.cum_turns = int(d.get("cum_turns", 0))
        self.segment = int(d.get("segment", 0))
        self.nudges_done = int(d.get("nudges_done", 0))
        self.aborted = bool(d.get("aborted", False))
        self.abort_delivered = bool(d.get("abort_delivered", False))
        self.events = list(d.get("events", []))
        self.judge_last = int(d.get("judge_last", -999))
        self.judge_gap = int(d.get("judge_gap", self.JUDGE_BASE_GAP))
        self.judge_calls = int(d.get("judge_calls", 0))
        self.judge_tokens = float(d.get("judge_tokens", 0) or 0)
        self.writer_tokens = float(d.get("writer_tokens", 0) or 0)
        self.last_edit = int(d.get("last_edit", 0))
        self.last_verify = int(d.get("last_verify", 0))
        self.last_verify_failed = bool(d.get("last_verify_failed", False))
        self.edits = int(d.get("edits", 0))
        self.verifications = int(d.get("verifications", 0))
        self.judge_pending = d.get("judge_pending") or None   # {"id", "since", "segment"}
        self.nudge_ready = d.get("nudge_ready") or None       # a redirect the judge wrote

    def to_dict(self):
        return {"turn_key": self.turn_key, "st": state_to_dict(self.st),
                "whole": state_to_dict(self.whole),
                "cum_turns": self.cum_turns, "segment": self.segment,
                "nudges_done": self.nudges_done, "aborted": self.aborted,
                "abort_delivered": self.abort_delivered, "events": self.events[-20:],
                "judge_last": self.judge_last, "judge_gap": self.judge_gap,
                "judge_calls": self.judge_calls, "judge_tokens": self.judge_tokens,
                "writer_tokens": self.writer_tokens, "last_edit": self.last_edit,
                "last_verify": self.last_verify, "last_verify_failed": self.last_verify_failed,
                "edits": self.edits, "verifications": self.verifications,
                "judge_pending": self.judge_pending, "nudge_ready": self.nudge_ready}

    # ---- outcome tracking for the ended-green proxy ----------------------------------
    def _track(self, name, args, result, exit_code, shell_edits=()):
        canon = canon_tool(name)
        if canon in _EDIT:
            self.last_edit = self.cum_turns
            self.edits += 1
            return
        cmd = (args or {}).get("command") if canon == "bash" else None
        verifies = status_of_tests(cmd) is not None
        if shell_edits:
            # `sed -i ... && pytest`: the edit comes first, so the run verifies it.
            self.last_edit = self.cum_turns - (1 if verifies else 0)
            self.edits += 1
        if verifies:
            self.last_verify = self.cum_turns
            self.verifications += 1
            if exit_code is not None:
                self.last_verify_failed = exit_code != 0
            else:                         # no status, or one another command gave (a pipe)
                self.last_verify_failed = bool(ERR_RE.search(result or ""))

    def resolved_proxy(self, stopped_normally=True):
        """Did this turn END GREEN? The stand-in for the grader the envelope cannot have.

        Green unless a verification after the last edit failed. Requiring a PASSING check after
        the last edit was tried on a continuous Django stream and made the envelope longer, not
        shorter: it drops the short turns that edit without running anything, while the long
        failing turns end with the agent's own checks passing either way."""
        if not stopped_normally or self.aborted or self.cum_turns == 0:
            return False
        return not (self.last_verify_failed and self.last_verify > self.last_edit)

    # ---- what the judge is told -------------------------------------------------------
    # The digest says "Tests are still NOT green." whenever it has an envelope: on a benchmark
    # the guard only ever watches an unfinished task. Here the guard knows how the
    # turn's last verification went, so the judge -- and the redirect it writes -- is told that.
    _NOT_GREEN = "Tests are still NOT green."

    def verification_state(self):
        if not self.verifications:
            return "No test or verification run has been seen in this turn."
        state = "failed" if self.last_verify_failed else "passed"
        since = " There have been edits since." if self.last_edit > self.last_verify else ""
        return f"The last verification run {state}.{since}"

    def live_digest(self, digest):
        return (digest or "").replace(self._NOT_GREEN, self.verification_state())

    # ---- the judge, rate-limited with backoff exactly as NudgeGuard._sampled_judge ----
    def _backoff(self, v):
        if v and v.get("stuck") is False:
            self.judge_gap = min(self.JUDGE_MAX_GAP, self.judge_gap + self.JUDGE_BASE_GAP)
        else:
            self.judge_gap = self.JUDGE_BASE_GAP

    def _sampled(self, judge):
        def call(digest):
            if self.st.turns - self.judge_last < self.judge_gap:
                return None
            self.judge_last = self.st.turns
            self.judge_calls += 1
            try:
                v = judge(self.live_digest(digest))
            except Exception:
                v = None
            if v:
                self.judge_tokens += v.get("_tokens") or 0
            self._backoff(v)
            return v
        return call

    def _background(self, judge):
        """`_sampled` for a judge that runs in the background (`plugin.judge.AsyncJudge`):
        a verdict is taken whenever one has come back, a new job starts under the same rate
        limit, and while one is out the judge answers nothing -- which `assess_abort` reads as
        throttled, the same as a call the rate limit skipped."""
        def call(digest):
            found, v = judge.poll(self)
            if found:
                self.judge_pending = None
                if v:
                    self.nudge_ready = (v.get("_nudge") or "").strip() or None
                self._backoff(v)
                return v or None
            if self.judge_pending:
                if time.time() - float(self.judge_pending.get("since") or 0) < self.JUDGE_STALE_S:
                    return None
                self.judge_pending = None                 # its job never answered
            if self.st.turns - self.judge_last < self.judge_gap:
                return None
            self.judge_last = self.st.turns
            self.judge_calls += 1
            job = judge.start(self, self.live_digest(digest))
            if job:
                self.judge_pending = {"id": job, "since": time.time(), "segment": self.segment}
            return None
        return call

    # ---- one completed tool call ------------------------------------------------------
    def fold(self, name, args, result, exit_code=None, *, envelope=None, thr=None,
             judge=None, nudge_budget=1):
        """Fold one call; return None, ("nudge", reason) or ("abort", reason).

        The body is `NudgeGuard.monitor` for a single completed call."""
        thr = thr or DEFAULT_THRESHOLDS
        cmd = (args or {}).get("command") if canon_tool(name) == "bash" else None
        shell_edits = shell_edit_targets(cmd) if cmd is not None else []
        if shell_edits:
            # A file put back with `git checkout --` or `git restore` is an edit only when this
            # turn edited it: putting back what a dev server keeps regenerating is not churn.
            reverted = set(shell_revert_targets(cmd))
            shell_edits = [b for b in shell_edits
                           if b not in reverted or self.whole.edit_counts.get(b)]
        status = status_of_tests(cmd)
        if status in ("hidden", "mixed"):
            verdict = runner_verdict(result)
            if verdict is not None:
                exit_code = int(verdict)
            elif status == "hidden":
                exit_code = None
        failed = None if exit_code is None else exit_code != 0
        self.st.update(name, args or {}, result or "", failed)
        self.whole.update(name, args or {}, result or "", failed)
        if status is not None and not TEST_RUN_RE.search(cmd):
            # `SpiralState` counts the Python runners its thresholds were tuned on; the turn's
            # other test runs (`npx vitest`, `go test`) are verifications too, in what the
            # redirect and the judge are told. No rule the guard decides by reads this count.
            self.st.test_runs += 1
            self.whole.test_runs += 1
        fold_shell_edits(self.st, shell_edits)
        fold_shell_edits(self.whole, shell_edits)
        self.cum_turns += 1
        self._track(name, args, result, exit_code, shell_edits)
        if self.aborted:
            return None

        backstop = thr.get("backstop_turns")
        if backstop is None and envelope is not None:
            backstop = envelope * thr["backstop_mult"]
        if (backstop is not None and self.cum_turns >= backstop
                and is_stuck(self.st, thr)):
            # The ladder is nudge, THEN stop. On a benchmark the envelope was ~30 calls, so
            # its 2.5x backstop lay well past the stuck rule's 20-call floor and the nudge came
            # first. An interactive repository's envelope is ~4-10 calls: its backstop falls
            # INSIDE that floor, so the moment a turn first counts as stuck the backstop has
            # tripped too, and it would pre-empt every nudge. An unspent nudge goes first;
            # after it, a fresh segment must be stuck again before the backstop stops it.
            reason = f"cum-backstop:{self.cum_turns}>={backstop:.0f}t"
            if self.nudges_done < nudge_budget:
                return self._stop("nudge", reason, envelope)
            return self._stop("abort", reason, envelope)

        wired = None
        if judge is not None:
            wired = self._background(judge) if hasattr(judge, "poll") else self._sampled(judge)
        abort, reason = assess_abort(self.st, envelope=envelope, judge=wired, thr=thr)
        if not abort:
            if (thr.get("nudge_on_ambiguous") and reason == "judge-ambiguous-spared"
                    and self.nudges_done < nudge_budget):
                return self._stop("nudge", "ambiguous-past-envelope", envelope)
            return None
        if self.nudges_done < nudge_budget:
            return self._stop("nudge", reason, envelope)
        return self._stop("abort", reason, envelope)

    def _stop(self, action, reason, envelope):
        self.events.append({"segment": self.segment, "action": action,
                            "turn_in_segment": self.st.turns, "cum_turns": self.cum_turns,
                            "envelope": envelope, "reason": reason,
                            "snapshot": self.st.snapshot()})
        if action == "abort":
            self.aborted = True
        return action, reason

    def nudge_body(self, envelope=None, writer=None):
        """The redirect text: the one a background judge already wrote with its verdict, else
        an LLM-written one when a writer is wired, else the count template
        (`nudge_text`), which mirrors only the agent's own actions."""
        if self.nudge_ready:
            text, self.nudge_ready = self.nudge_ready, None
            return text
        if writer is not None:
            try:
                out = writer(self.live_digest(self.st.recent_digest(envelope=envelope)))
            except Exception:
                out = None
            if out:
                self.writer_tokens += out.get("_tokens") or 0
                text = (out.get("text") or "").strip()
                if text:
                    return text
        return nudge_text(self.st)

    def after_nudge(self):
        """Start the post-nudge segment with a fresh state: `NudgeGuard.continuation`'s reset,
        which is the agent's fair re-assessment window."""
        self.nudges_done += 1
        self.segment += 1
        self.st = SpiralState()
        self.judge_last = -999
        self.judge_gap = self.JUDGE_BASE_GAP
        self.judge_pending = None          # a verdict on the old segment is about the old one
        self.nudge_ready = None

    def summary(self, stopped_normally=True):
        return {"turn_key": self.turn_key, "tool_calls": self.cum_turns,
                "nudges": self.nudges_done, "aborted": self.aborted,
                "resolved_proxy": self.resolved_proxy(stopped_normally),
                "edits": self.edits, "verifications": self.verifications,
                "judge_tokens": self.judge_tokens, "writer_tokens": self.writer_tokens}
