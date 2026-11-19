"""Every agent's tool calls as one ordered list of `(name, args, result_text, success)`.

`load_trajectory` reads a saved session log of any supported agent -- a Claude Code transcript
or `stream-json`, a Copilot CLI event log, a Codex `exec --json` stream -- and returns the same
tuples, so a mechanism never needs to know which agent wrote the file. A Codex rollout and a Pi
session are converted into one of these first (`codex.codex_rollout_to_exec`,
`pi.pi_session_to_claude`).
"""
import fnmatch
import os
import re

from .claude import claude_calls
from .codex import codex_exec_calls
from .copilot import copilot_event_calls, copilot_session_cwd

# rtk in its Codex mode changes the COMMAND LINE rather than hooking it: the agent is told to
# type `rtk pytest`, `rtk git status`, `rtk proxy <cmd>`. Every classifier that reads a
# command's first token -- the micro-skill vocabulary, a test-run detector -- would otherwise
# stop seeing a test run, a fetch or a repository query. One definition, so the classifiers
# cannot disagree about what counts as the wrapper. `rtk proxy` is rtk's own passthrough (run
# unfiltered, still tracked); a bare `rtk` with no command is left alone. `RTK_WORDS` is the
# wrapper alone, for a classifier that passes over it among other prefixes (`loopguard.turn`).
RTK_WORDS = r"rtk(?:\s+proxy)?"
RTK_PREFIX_RE = re.compile(r"^" + RTK_WORDS + r"\s+(?=\S)")

# The tools whose `command` argument is shell text, lower-cased: Claude Code's Bash, and its
# PowerShell tool on Windows, Codex's shell, the Copilot CLI's stop_bash.
SHELL_TOOLS = frozenset({"bash", "powershell", "shell", "stop_bash"})

# Roles a person can give a tool the mechanisms do not know by name (the `tool_roles`
# setting), such as an MCP server's shell or edit tool, and the built-in tool each role reads
# as. The plugin sets them once per process from its configuration (`set_tool_roles`).
TOOL_ROLES = {"shell": "bash", "edit": "edit", "read": "read"}
_tool_roles = ()


def parse_tool_role(entry):
    """`"mcp__serena__replace_content=edit"` -> `("mcp__serena__replace_content", "edit")`;
    None when the entry names no tool or no known role. A tool may be a glob (`mcp__x__*`)."""
    pattern, sep, role = str(entry).rpartition("=")
    pattern, role = pattern.strip().lower(), role.strip().lower()
    return (pattern, role) if sep and pattern and role in TOOL_ROLES else None


def set_tool_roles(entries):
    """Use these `tool_roles` entries from now on in this process; malformed ones are skipped."""
    global _tool_roles
    _tool_roles = tuple(r for r in map(parse_tool_role, entries or ()) if r)


def tool_kind(name):
    """A tool's name as the mechanisms read it: lower-cased, or, for a tool the person gave a
    role, the built-in tool of that role (`bash`, `edit`, `read`). The first matching entry
    wins."""
    low = (name or "").lower()
    for pattern, role in _tool_roles:
        if fnmatch.fnmatchcase(low, pattern):
            return TOOL_ROLES[role]
    return low


def strip_rtk_prefix(command):
    """`rtk pytest -q` -> `pytest -q`; anything else unchanged."""
    return RTK_PREFIX_RE.sub("", (command or "").lstrip(), count=1)


def _is_codex_exec(lines):
    return any('"type":"item.completed"' in line.replace(" ", "") for line in lines)


def load_trajectory(path):
    """Ordered [(tool_name, args, result_text, success)] over a saved session log.

    The schema is sniffed from the lines: Codex emits completed items, Claude Code emits message
    envelopes carrying `tool_use` blocks, and anything else is read as Copilot events."""
    if not os.path.exists(path):
        return []
    lines = [l.strip() for l in open(path, encoding="utf-8", errors="replace") if l.strip()]
    if _is_codex_exec(lines):
        return codex_exec_calls(lines)
    # Copilot can also say "tool_use" in metadata (for example a model's finish reason).
    # Recognize actual Claude conversation blocks, not strings anywhere in the event log.
    claude = claude_calls(lines)
    if claude:
        return [c[:4] for c in claude]
    return copilot_event_calls(lines)


def call_cwds(path):
    """The directory each call of `load_trajectory(path)` ran in, aligned with it; None where
    the log does not say. Claude Code's shell keeps its directory between calls, and its
    transcript records it on every entry: after `cd tests && ...`, a bare `python runtests.py`
    runs in `tests/`. Codex runs each command afresh. Copilot's log records only where the
    session started, and that is where its calls are read as starting: models there type
    `cd <that directory> && ...` before every command, which is then the plain form."""
    calls = load_trajectory(path)
    if not calls or not os.path.exists(path):
        return [None] * len(calls)
    lines = [l.strip() for l in open(path, encoding="utf-8", errors="replace") if l.strip()]
    if _is_codex_exec(lines):
        return [None] * len(calls)
    cwds = [c[4] for c in claude_calls(lines)]
    if len(cwds) == len(calls):
        return cwds
    return [copilot_session_cwd(lines)] * len(calls)


def tool_usage(path):
    """{tool_name: calls} over a saved session log."""
    counts = {}
    for name, _args, _res, _ok in load_trajectory(path):
        if name:
            counts[name] = counts.get(name, 0) + 1
    return counts
