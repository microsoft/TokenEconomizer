"""What the loop guard reads off a trajectory: a per-call fold of its failure-loop signals.

`SpiralState` is updated once per completed tool call and keeps only counts and a short,
leakage-safe action log: edits per file, files edited, error outputs, verification runs, and how
often a failure repeats one seen before (`stagnation`, the signal that separates a loop from a
hard but productive grind). `completed_calls` turns one streamed line of any agent's output into
the calls it completes, so the same fold runs live and over a saved log.
"""
import hashlib
import json
import re

from ..trace import load_trajectory as _load_trajectory

# An output that RAN code to verify behavior (the test/repro step of the edit->test
# loop).
TEST_RUN_RE = re.compile(
    r"(runtests(\.py)?|\bpytest\b|py\.test|manage\.py\s+test|python\s+-m\s+unittest|"
    r"\btox\b|\bnox\b|\btrial\b|python\d?\s+-c\b)", re.IGNORECASE)
ERR_RE = re.compile(
    r"(Traceback \(most recent call last\)|\b\w*Error\b|\bFAILED\b|\bassert\b|"
    r"Exception|errors during collection|"
    # Other ecosystems' failing-test summaries, which name no error: TAP (`node --test`)
    # `# fail 2` and `not ok 3`, mocha's and rspec's counts, JUnit's, and Go's `--- FAIL: T`
    # and `FAIL<tab>pkg` (not unittest's `FAIL: test_x`, which would change the line a Python
    # failure is known by, and so which failures count as repeats).
    r"^# fail [1-9]|^not ok \d|\b[1-9]\d* (?:failing|failures?)\b|Failures: [1-9]|"
    r"^(?-i:--- FAIL: |FAIL\t|FAIL$))", re.IGNORECASE | re.MULTILINE)
_EDIT_TOOLS = ("edit", "str_replace", "create")
# Tools whose output is the content of files, not the outcome of running anything: a test file
# full of `assert` read back is not a failure, nor is reading it twice a repeated one.
_CONTENT_TOOLS = ("view", "grep", "glob")

# Normalize agent-specific tool names to the copilot-style canonical names the folding
# logic below keys on, so ONE code path handles both the Copilot stream schema and Claude
# Code's stream-json (which names tools Bash/Read/Edit/Write/Grep/Glob/MultiEdit). Unknown
# names pass through unchanged.
_TOOL_CANON = {
    "bash": "bash", "shell": "bash",
    "read": "view", "view": "view",
    "grep": "grep",
    "glob": "glob",
    "edit": "edit", "multiedit": "edit", "str_replace": "edit",
    "apply_patch": "edit",
    "write": "create", "create": "create",
}


def canon_tool(name):
    return _TOOL_CANON.get((name or "").lower(), name or "?")


def completed_calls(line, pend):
    """Schema-agnostic fold of ONE streamed JSONL line into completed (name, args, result)
    tool calls, so the live guards work for BOTH the Copilot stream
    (tool.execution_start/complete) AND Claude Code's stream-json (assistant.tool_use /
    user.tool_result). `pend` is a caller-owned dict {callId -> {name,args}} carried across
    lines to pair a start/tool_use with its later complete/tool_result. Yields a list of
    completed calls finished on THIS line (0, 1, or — for a Claude user msg batching
    several tool_results — more). Tool names are canonicalized so downstream keying is
    uniform; unpaired results fall back to name '?'."""
    try:
        ev = json.loads(line)
    except Exception:
        return []
    if not isinstance(ev, dict):
        return []
    t = ev.get("type")
    out = []
    # ---- Codex exec --json schema ----
    if t == "item.completed":
        item = ev.get("item") or {}
        kind = item.get("type")
        if kind == "command_execution":
            out.append(("bash", {"command": item.get("command") or ""},
                        item.get("aggregated_output") or ""))
        elif kind == "file_change":
            changes = item.get("changes") or []
            paths = [c.get("path") for c in changes
                     if isinstance(c, dict) and c.get("path")]
            out.append(("edit", {"path": paths[0] if paths else "",
                                  "paths": paths, "changes": changes}, ""))
        return out
    # ---- Copilot schema ----
    if t == "tool.execution_start":
        d = ev.get("data", {}) if isinstance(ev.get("data"), dict) else {}
        pend[d.get("toolCallId")] = {"name": d.get("toolName"),
                                     "args": d.get("arguments", {}) or {}}
        return out
    if t == "tool.execution_complete":
        d = ev.get("data", {}) if isinstance(ev.get("data"), dict) else {}
        info = pend.pop(d.get("toolCallId"), {"name": "?", "args": {}})
        res = d.get("result") or {}
        c = res.get("content") if isinstance(res, dict) else ""
        out.append((canon_tool(info["name"]), info.get("args") or {},
                    c if isinstance(c, str) else ""))
        return out
    # ---- Claude Code stream-json schema ----
    if t == "assistant":
        for b in (ev.get("message", {}) or {}).get("content", []) or []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                pend[b.get("id")] = {"name": b.get("name"),
                                     "args": b.get("input", {}) or {}}
        return out
    if t == "user":
        for b in (ev.get("message", {}) or {}).get("content", []) or []:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                info = pend.pop(b.get("tool_use_id"), {"name": "?", "args": {}})
                c = b.get("content")
                if isinstance(c, list):  # content can be a list of text blocks
                    c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
                out.append((canon_tool(info["name"]), info.get("args") or {},
                            c if isinstance(c, str) else ""))
        return out
    return out


def basename(p):
    """The last component of a path in either separator: Claude Code on Windows names the file
    an edit tool changed `C:\\repo\\pkg\\mod.py`, and a shell edit of it `pkg/mod.py`."""
    return p.replace("\\", "/").rstrip("/").split("/")[-1] if p else (p or "")


def _failure_sig(text):
    """A normalized signature of a failing output: drop numbers/addresses/paths so two
    runs that fail the SAME way hash identically (used to detect 'no progress' — the
    same failure recurring after edits). Leakage-safe: a hash, never the content."""
    if not text:
        return None
    first = None
    for ln in text.splitlines():
        if ERR_RE.search(ln):
            first = ln
            break
    if first is None:
        return None
    norm = re.sub(r"0x[0-9a-f]+|\d+|/\S+", "", first)
    return hashlib.md5(norm.strip().encode("utf-8", "replace")).hexdigest()[:10]


class SpiralState:
    """Compact, incremental fold of one trajectory's spiral signals. Cheap to update
    per event so it can run live."""
    __slots__ = ("turns", "edit_counts", "edit_order", "error_outputs",
                 "test_runs", "fail_sigs", "repeated_fail_sigs", "recent")

    def __init__(self):
        self.turns = 0
        self.edit_counts = {}        # basename -> #edits
        self.edit_order = []         # ordered basenames of edit targets
        self.error_outputs = 0       # tool outputs carrying an error/failure marker
        self.test_runs = 0           # code-execution verification steps
        self.fail_sigs = {}          # failure signature -> count
        self.repeated_fail_sigs = 0  # # of failing outputs that REPEAT an earlier sig
        self.recent = []             # leakage-safe rolling log of recent actions (for the
                                     # LLM trace-judge); process-only, never fix content

    # ---- derived signals ----
    @property
    def edit_churn(self):
        return max(self.edit_counts.values()) if self.edit_counts else 0

    @property
    def distinct_edit_files(self):
        return len(self.edit_counts)

    @property
    def stagnation(self):
        """Fraction of failing outputs that repeat an earlier identical failure (the
        agent edited but the SAME failure came back = no progress). 0 when no fails."""
        total = sum(self.fail_sigs.values())
        return (self.repeated_fail_sigs / total) if total else 0.0

    def update(self, tool_name, args, result_text, failed=None):
        """Fold one completed tool call into the state. `failed` is what the agent reported
        about the call -- True for a non-zero exit or a tool error, False for success, None when
        it did not say -- and decides whether its output can be a failing one: a call known to
        have succeeded is not, a content tool only when the tool itself failed, and otherwise
        the output's own markers decide."""
        self.turns += 1
        args = args if isinstance(args, dict) else {}
        tool_name = canon_tool(tool_name)   # accept Copilot- or Claude-named tools
        action = tool_name or "?"
        if tool_name in _EDIT_TOOLS:
            # A patch names every file it changes in `paths`; any other edit names one.
            paths = args.get("paths")
            if not isinstance(paths, (list, tuple)) or not paths:
                paths = [args.get("path") or args.get("filePath") or args.get("file_path")]
            names = list(dict.fromkeys(basename(p) for p in paths if isinstance(p, str)))
            names = [b for b in names if b]
            for b in names:
                self.edit_order.append(b)
                self.edit_counts[b] = self.edit_counts.get(b, 0) + 1
            if names:
                action = f"edit {', '.join(names)}"
        cmd = args.get("command", "") if tool_name == "bash" else ""
        if cmd:
            short = re.sub(r"\s+", " ", cmd.strip())[:90]
            action = f"$ {short}"
            if TEST_RUN_RE.search(cmd):
                self.test_runs += 1
        elif tool_name in ("grep", "glob"):
            action = f"{tool_name} {str(args.get('pattern') or '')[:40]}"
        elif tool_name == "view":
            action = f"view {basename(args.get('path') or args.get('file_path') or '')}"
        fail = None
        can_fail = failed is True if tool_name in _CONTENT_TOOLS else failed is not False
        if result_text and can_fail and ERR_RE.search(result_text):
            self.error_outputs += 1
            sig = _failure_sig(result_text)
            if sig:
                fail = sig
                if sig in self.fail_sigs:
                    self.repeated_fail_sigs += 1
                self.fail_sigs[sig] = self.fail_sigs.get(sig, 0) + 1
        # rolling leakage-safe action log (tool name / command / search / error-SIGNATURE
        # only — no file contents, edit bodies, or view output), capped to recent window.
        tag = f"  [err#{fail}]" if fail else ""
        self.recent.append(f"{self.turns:>3}: {action}{tag}")
        if len(self.recent) > 40:
            self.recent = self.recent[-40:]

    def recent_digest(self, n=24, envelope=None):
        """A compact, LEAKAGE-SAFE digest of the agent's RECENT PROCESS (last n actions)
        for the LLM trace-judge: commands/searches/edited-basenames and error-SIGNATURE
        markers only — never fix content, edit bodies, view output, or the issue text.
        `envelope` (this repo's typical resolved-task length) is passed as CONTEXT so the
        judge can recognize an abnormally-long trajectory as a likely dead-end."""
        head = (f"turns={self.turns} edit_churn={self.edit_churn} "
                f"distinct_edit_files={self.distinct_edit_files} "
                f"error_outputs={self.error_outputs} test_runs={self.test_runs} "
                f"repeated_identical_failures={self.repeated_fail_sigs} "
                f"stagnation={self.stagnation:.2f}")
        if envelope:
            ratio = self.turns / envelope if envelope else 0
            head += (f"\nrepo_context: tasks here usually RESOLVE within ~{envelope:.0f} "
                     f"turns; this trajectory is at {self.turns} turns = {ratio:.1f}x that "
                     f"typical length. Tests are still NOT green. A trajectory far past "
                     f"the typical resolved length with recurring failures is likely a "
                     f"dead-end loop, even if it touches many files.")
        return head + "\nRecent actions (oldest->newest):\n" + "\n".join(self.recent[-n:])

    def snapshot(self):
        return {"turns": self.turns, "edit_churn": self.edit_churn,
                "distinct_edit_files": self.distinct_edit_files,
                "error_outputs": self.error_outputs, "test_runs": self.test_runs,
                "stagnation": round(self.stagnation, 3),
                "repeated_fail_sigs": self.repeated_fail_sigs}


def state_from_trajectory(path, load_trajectory=None):
    """The final `SpiralState` of a saved session log (`trace.load_trajectory` by default)."""
    load_trajectory = load_trajectory or _load_trajectory
    st = SpiralState()
    for name, args, res, ok in load_trajectory(path):
        st.update(name, args, res, failed=None if ok is None else not ok)
    return st
