"""`LiveAdapter`: the DomainAdapter for a real repository in a real session.

The mechanisms read their domain only through a DomainAdapter (`tokeneconomizer.domain`), so running
`PrefixStage` and `ToolingStage` on live sessions needs no mechanism edit -- only an adapter whose
answers fit an interactive session instead of a sealed benchmark task:

  * `protected_tools` adds what a PERSON needs to the work surface. The benchmark protected set
    keeps Bash/Read/Edit/...; `toolprune.tools.INAPPLICABLE_TOOLS` then treats AskUserQuestion, plan
    mode and subagents as inapplicable "by construction" for a headless sealed task. A person
    does use them interactively, so those are protected here, and so are the web tools, however
    long they go unused, and whatever the person lists in `toolprune_protect` (names, or
    patterns such as `mcp__headroom__*`); only evidence (loaded but unused for N sessions) can propose anything
    else.
  * `parse_catalog_with_source` reports what the session COULD call. An interactive transcript
    does not record the tool list the way `stream-json`'s init event does, so the catalog is the
    union of a probed snapshot (`te toolprune refresh-catalog`, stored under the state root), the
    deferred tools the transcript lists, and the tools it used. Without a snapshot only deferred
    tools can be proposed -- they cost one listing line each, so that is honest but small.
  * `env_vocabulary` is the generic one (`microskills.GENERIC`): every ecosystem at once, builds
    and checks as well as tests, every edited file, and the proposer's language-neutral digest.
    The repository's markers only pick how a script run through its shebang is spelled
    (`package.json` alone -> `node`). The experiments' narrower vocabularies stay selectable
    (`vocabulary=python-git` / `node-web`).
  * `problem_statements` is empty: there is no issue text to give the per-task tool judge.
"""
import dataclasses
import fnmatch
import json
import os

from .. import trace
from ..loopguard import state_from_trajectory
from ..microskills import GENERIC, NODE_WEB, PYTHON_GIT
from ..toolprune.tools import PROTECTED_TOOLS

# Tools a person relies on in an interactive session, beyond the benchmark work surface.
INTERACTIVE_PROTECTED = frozenset({
    "Bash", "PowerShell", "Read", "Edit", "MultiEdit", "Write", "Grep", "Glob", "LS",
    "NotebookRead",
    "BashOutput", "KillShell", "KillBash",
    "AskUserQuestion", "EnterPlanMode", "ExitPlanMode", "TodoWrite",
    "TaskCreate", "TaskUpdate", "TaskGet", "TaskList", "TaskOutput", "TaskStop",
    "Agent", "Task", "Skill", "ToolSearch", "SlashCommand", "Monitor",
    # The web is never proposed. A run of self-contained tasks can leave it unused for long
    # stretches, and a person's next task may need it; a
    # deny list is easy to accept without reading. Untooling is for tools a repository's work
    # genuinely never needs, and nothing says that of the web.
    "WebFetch", "WebSearch",
})
# The Copilot CLI's own work surface and a person's tools there (1.0.83 names). Only MCP servers
# can be removed persistently on Copilot (`denylist.copilot_server_proposal`), so this matters
# for what the evidence reports, not for what can be written.
COPILOT_PROTECTED = frozenset({
    "bash", "read_bash", "write_bash", "stop_bash", "list_bash", "view", "edit", "create",
    "str_replace", "apply_patch", "custom", "rg", "grep", "glob", "task", "skill",
    "read_agent", "list_agents", "write_agent", "ask_user", "web_fetch", "web_search",
    "report_intent", "update_todo", "sql",
})
# Rarely used, but expensive to lose when a task needs them: proposed only after
# HIGH_RISK_FACTOR times the usual evidence.
HIGH_RISK = frozenset({"NotebookEdit"})
HIGH_RISK_FACTOR = 3


class _Protected(frozenset):
    """The protected names, plus patterns (`mcp__headroom__*`) a name may match instead."""

    def __new__(cls, names, patterns=()):
        obj = super().__new__(cls, names)
        obj.patterns = tuple(patterns)
        return obj

    def __contains__(self, name):
        return super().__contains__(name) or any(fnmatch.fnmatchcase(str(name), p)
                                                 for p in self.patterns)

_PY_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Pipfile",
               "tox.ini", "environment.yml")
_NODE_MARKERS = ("package.json",)


def detect_vocabulary(repo_root, preference="auto"):
    if preference == "node-web":
        return NODE_WEB
    if preference == "python-git":
        return PYTHON_GIT
    has = lambda names: any(os.path.exists(os.path.join(repo_root or "", n)) for n in names)
    if has(_NODE_MARKERS) and not has(_PY_MARKERS):
        return dataclasses.replace(GENERIC, canonical_interpreter="node")
    return GENERIC


def catalog_path(state_root, host):
    return os.path.join(state_root, "hosts", host, "catalog.json")


def load_catalog(state_root, host):
    """{tool: description} from the last `te toolprune refresh-catalog` probe, {} if none."""
    try:
        with open(catalog_path(state_root, host), encoding="utf-8") as fh:
            data = json.load(fh)
        return {t: "" for t in data.get("tools", []) if isinstance(t, str)}
    except (OSError, ValueError):
        return {}


class LiveAdapter:
    name = "live"

    def __init__(self, host, repo_root, vocabulary="auto", catalog=None, protect=()):
        self.host = host
        self.repo_root = repo_root
        self.vocabulary = vocabulary
        self.catalog = dict(catalog or {})
        self.protect = tuple(protect)

    # ---- trajectory reading ----
    def load_trajectory(self, path):
        return trace.load_trajectory(path)

    def tool_usage(self, path):
        return self.host.tool_usage(path)

    def parse_catalog(self, run_dir_path, agent):
        return self.parse_catalog_with_source(run_dir_path, agent)[0]

    def parse_catalog_with_source(self, run_dir_path, agent):
        transcript = os.path.realpath(os.path.join(run_dir_path, "transcript.jsonl"))
        cat = dict(self.catalog)
        observed = set(self.tool_usage(transcript))
        if self.host.name == "claude":
            observed |= trace.claude_deferred_tools(transcript)
        elif self.host.name == "copilot":
            # Some builds record the request's tool list in `session.usage_checkpoint`.
            observed |= set(trace.parse_tool_names_copilot(
                open(transcript, encoding="utf-8", errors="replace").read()
                if os.path.exists(transcript) else ""))
        for name in observed:
            cat.setdefault(name, "")
        if not cat:
            return {}, None
        return cat, ("probe+observed" if self.catalog else "observed")

    # ---- tool pruning ----
    def protected_tools(self):
        extra = COPILOT_PROTECTED if self.host.name == "copilot" else frozenset()
        return _Protected(PROTECTED_TOOLS | INTERACTIVE_PROTECTED | extra, self.protect)

    def inapplicable_tools(self):
        return ()                       # nothing is inapplicable by construction here

    def hardcoded_exclusions(self, used_tools):
        return []

    # ---- loop guard ----
    def spiral_snapshot(self, path):
        return state_from_trajectory(path).snapshot()

    # ---- per-task judge inputs ----
    def problem_statements(self):
        return {}

    def env_vocabulary(self):
        return detect_vocabulary(self.repo_root, self.vocabulary)
