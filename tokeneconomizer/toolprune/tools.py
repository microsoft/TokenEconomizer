"""The tool sets tool pruning never touches, and the optional per-task tool judge.

`PROTECTED_TOOLS` is the work surface (shell, read, edit, search) on every agent; nothing prunes
it. `INAPPLICABLE_TOOLS` are tools that cannot help a single headless task by construction
(orchestration, scheduling, session memory, interactive questions). `judge_tools_for_task` is
the paid, instruction-aware judge: it reads the task text and the catalog and says which tools
to load for THIS task, never dropping a protected one.
"""
import json
import re

from ..pricing import approx_tokens as _approx_tokens

INAPPLICABLE_TOOLS = (
    # sub-agent orchestration (a single sealed task has nothing to delegate to)
    "task", "list_agents", "read_agent", "write_agent",
    # session DB / scheduling / skills / CLI self-docs — irrelevant to solving code
    "sql", "session_store_sql", "manage_schedule", "skill",
    "fetch_copilot_cli_documentation",
    # persistent memory (also off by default headless) and its vote tool
    "store_memory", "vote_memory",
    # interactive Q&A: headless can never get an answer
    "ask_user",
    # ---- Claude Code tool names (PascalCase; disjoint from the Copilot names
    # above, so listing both is safe regardless of --agent). These are
    # inapplicable-by-construction for one sealed headless SWE-bench task.
    # Work surface (Bash/Edit/Read/Write/Grep/Glob) is kept.
    # sub-agent orchestration + workflow engine (nothing to delegate to):
    "Agent", "Workflow",
    # task list / scheduling / cron (no session persistence in a single task):
    "TaskCreate", "TaskUpdate", "TaskGet", "TaskList", "TaskOutput", "TaskStop",
    "ScheduleWakeup", "CronCreate", "CronDelete", "CronList",
    # plan-mode + git-worktree session controls (irrelevant to a sealed rollout):
    "EnterPlanMode", "ExitPlanMode", "EnterWorktree", "ExitWorktree",
    # skills and notebooks:
    "Skill", "NotebookEdit",
    # interactive Q&A: headless can never get an answer:
    "AskUserQuestion",
)
# Tools that must always stay available regardless of usage (the real work surface).
PROTECTED_TOOLS = frozenset({
    "bash", "view", "grep", "glob", "edit", "create", "str_replace",
    "read_bash", "stop_bash", "list_bash", "read_file",
    # Codex's complete native work surface (fixed; neither schema is removable).
    "shell", "apply_patch",
    # Claude Code work-surface names:
    "Bash", "PowerShell", "Edit", "Write", "Read", "Grep", "Glob", "MultiEdit",
})


TOOL_JUDGE_DOMAIN = (
    "You configure the toolset for an autonomous coding agent that is about to solve ONE "
    "specific GitHub issue by READING, EDITING, and TESTING source code in a single "
    "repository, HEADLESS and SINGLE-SHOT (no human, one sealed container, no sub-agents / "
    "scheduling / external memory). Every LOADED tool's JSON schema is re-sent on EVERY turn "
    "and costs tokens. Below are the ISSUE text and the currently-available tools. For THIS "
    "specific issue, mark each tool:\n"
    "  LOAD = plausibly useful to read/navigate/edit/run/test code to solve THIS issue\n"
    "  DROP = this specific issue will not need it\n"
    "Be conservative: DROP only when you are confident the tool is irrelevant to THIS issue. "
    "Do NOT assume a tool is needed merely because it exists. Return ONLY a JSON object "
    "{tool_name: {\"load\": true|false, \"reason\": \"<short>\"}}.\n"
)


def judge_tools_for_task(problem_statement, catalog, runner, model="agent",
                         domain=None, max_issue_chars=4000):
    """Per-task, INSTRUCTION-AWARE tool judge. Reads the (redacted) task text plus
    the candidate tool catalog ({name: desc}) and returns ({tool: load_bool}, tokens): which
    tools to LOAD for THIS task. Complements the trace-based rule — the caller unions the
    DROP set with the evidence exclusions (and never drops PROTECTED or ever-used tools).

    Leakage-safe: the caller MUST pass the SAME problem statement the agent receives, never
    a solution or its tests. On any failure returns
    ({}, tokens) so the caller keeps everything loaded (safe). `tokens` is the cache-aware
    EFFECTIVE cost of this one-shot call (fresh input x1 + output x5 when approximated), on
    the same basis as the session's own cost, so it is charged to the same ledger."""
    if runner is None or not catalog or not problem_statement:
        return {}, 0
    brief = domain or TOOL_JUDGE_DOMAIN
    prompt = (brief + "\nISSUE:\n" + problem_statement[:max_issue_chars]
              + "\n\nTOOLS:\n" + "\n".join(f"- {n}: {d}" for n, d in catalog.items())
              + "\n\nReturn the JSON classification:")
    try:
        raw, real = runner(prompt, model)
    except Exception:
        return {}, 0
    raw = raw or ""
    tokens = real if real is not None else (_approx_tokens(prompt) + 5 * _approx_tokens(raw))
    out = {}
    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
            for t, v in obj.items():
                load = v.get("load") if isinstance(v, dict) else v
                out[t] = bool(load)
        except Exception:
            return {}, tokens
    # safety: never DROP a protected work tool, whatever the judge said.
    for t in PROTECTED_TOOLS:
        if t in out:
            out[t] = True
    return out, tokens
