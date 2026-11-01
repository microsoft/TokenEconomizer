"""`CodexUntoolProfile`: tool pruning on Codex, where the unit is a capability package.

Codex ships a broad default surface (collaboration sub-agents, web, skills, image/media,
request-user-input, deferred wait, planning, ...) whose schemas and guidance are re-sent on
every request. This controller removes the packages a repository's sessions do not use:

  * A fresh repository starts on the COMPLETE contract (its first session sees everything).
  * After each completed session, at most two independently-observed packages are demoted,
    using ONLY earlier sessions' evidence. A single completed use PERMANENTLY protects a
    package.
  * Higher-risk packages (image/media, skills) need more loaded-but-unused exposures before
    demotion than collaboration or web.
  * Narrow task-text hints and sparse single-package probes restore one package before a
    session without restoring the rest of the contract (fail-open recovery).
  * Deferred `wait` is CORE, never demoted: in recorded traces, pruning it made agents poll
    through the shell instead (dozens of polling commands). The mask is frozen within a session.

What a mask can reach depends on the Codex build: on codex-cli 0.153.1, config toggles remove
`collaboration` (the agent tools and two collaboration messages), `goals` (inside the code-mode
`exec` description), `image_media` and `skills` (the bundled skills' listing). `web` has a
toggle as well; `request_user_input` has none; `planning` is not in that contract at all.

The controller computes the logical capability MASK; mapping a capability to Codex config or
request rewrites is the caller's job (`plugin.denylist` writes a project's `.codex/config.toml`).
"""
import copy
import hashlib
import json
import os
import re
from collections import Counter
from tokeneconomizer.names import repo_filename


# ===========================================================================
# Transcript capability detection (which Codex packages a trace item exercises)
# ===========================================================================
CAPABILITY_DESCRIPTIONS = {
    "collaboration": (
        "Spawn, message, wait for, or close sub-agents. The benchmark runs one sealed "
        "agent process and provides no useful parallel-agent workflow."),
    "goals": (
        "Create, inspect, and update long-lived product goals. These are orchestration "
        "metadata rather than repository read/edit/test operations."),
    "image_media": (
        "View or transport image/audio/generated-media blocks. SWE-bench repair tasks "
        "operate on a checked-out source repository and text test output."),
    "interactive_guidance": (
        "Persona, writing-style, commentary-channel and answer-formatting guidance for an "
        "interactive chat session. A detached, single-turn run has no user reading "
        "commentary and its final message is not graded."),
    "planning": (
        "Maintain a structured todo/update_plan list. This can help some long repairs but "
        "is optional because the agent can reason and report progress without the tool."),
    "web": (
        "Search/open the public web and query online services."),
    "request_user_input": (
        "Pause for interactive human input. Runs are detached, autonomous, and have no "
        "human available inside a task."),
}

CAPABILITIES = tuple(sorted(CAPABILITY_DESCRIPTIONS))

_ITEM_TYPES_BY_CAPABILITY = {
    "collaboration": frozenset((
        "spawn_agent", "send_input", "send_message", "followup_task",
        "resume_agent", "wait_agent", "close_agent", "interrupt_agent", "list_agents")),
    "goals": frozenset(("create_goal", "get_goal", "update_goal")),
    "image_media": frozenset((
        "view_image", "image", "audio", "generated_image", "image_generation")),
    "planning": frozenset(("update_plan", "todo_list")),
    "web": frozenset(("web", "web_search", "web_fetch", "web__run")),
    "request_user_input": frozenset(("request_user_input",)),
}

_NAME_TO_CAPABILITY = {
    name: capability
    for capability, names in _ITEM_TYPES_BY_CAPABILITY.items()
    for name in names
}
_NESTED_TOOL_RE = re.compile(r"\btools\.([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def _item_capabilities(item):
    """Return capability groups represented by one Codex transcript item."""
    if not isinstance(item, dict):
        return set()
    found = set()
    for key in ("type", "name", "tool_name"):
        value = str(item.get(key) or "")
        capability = _NAME_TO_CAPABILITY.get(value)
        if capability:
            found.add(capability)
    # Newer Codex builds package multi-agent operations as one collaboration item; the
    # concrete action is in ``tool`` (e.g. tool="wait"), not name/tool_name. Treat the
    # package type itself as collaboration (mapping a bare tool="wait" globally would
    # misclassify the unrelated deferred-wait tool).
    if str(item.get("type") or "") == "collab_tool_call":
        found.add("collaboration")
    # Some builds expose a unified custom `exec` item rather than normalizing the nested
    # helper into web_search/todo_list/etc. Inspect only custom-tool payloads; shell text
    # may legitimately contain strings like "update_plan" and must not be false evidence.
    kind = str(item.get("type") or "")
    name = str(item.get("name") or "")
    if kind in ("custom_tool_call", "function_call", "tool_call") or name == "exec":
        for key in ("input", "arguments"):
            raw = item.get(key)
            if isinstance(raw, dict):
                raw = json.dumps(raw, sort_keys=True)
            if not isinstance(raw, str):
                continue
            for nested_name in _NESTED_TOOL_RE.findall(raw):
                capability = _NAME_TO_CAPABILITY.get(nested_name)
                if capability:
                    found.add(capability)
    return found


# ===========================================================================
# Narrow issue-text hints (fail-open protection for image/skill packages)
# ===========================================================================
_IMAGE_HINT = re.compile(
    r"\b(?:screenshot|png|jpe?g|gif|svg|audio|video)\b|"
    r"\b(?:image|figure)\s+(?:file|output|render|display)\b",
    re.I,
)
_SKILL_HINT = re.compile(
    r"\b(?:OpenAI|ChatGPT|Codex|Responses API)\b|"
    r"\b(?:generate|create|edit|transform)\s+(?:an?\s+)?"
    r"(?:image|photo|illustration|texture|sprite|mockup)\b|"
    r"\b(?:create|update|install|scaffold)\s+(?:an?\s+)?"
    r"(?:Codex\s+)?(?:skill|plugin)\b|"
    r"\bSKILL\.md\b|\.codex-plugin\b",
    re.I,
)
_SKILL_USE = re.compile(
    r"(?:\bSKILL\.md\b|[/\\]skills[/\\]|\bskills\.read\s*\()",
    re.I,
)
_FAILED_STATUSES = frozenset(("failed", "error", "cancelled"))

_HINT_BASE_RELAY = (
    "deferred_wait", "image_media", "request_user_input", "skills", "web")


def task_hints(problem_statement):
    """Return the packages a task's issue text protects from demotion (image/skills)."""
    text = str(problem_statement or "")
    protected = []
    if _IMAGE_HINT.search(text):
        protected.append("image_media")
    if _SKILL_HINT.search(text):
        protected.append("skills")
    return protected


# ===========================================================================
# Trace audit: attempted / completed / failed capability uses in a Codex trace
# ===========================================================================
_AUDIT_CAPABILITIES = (
    "collaboration", "deferred_wait", "goals", "image_media", "interactive_guidance",
    "request_user_input", "skills", "web")


def _wire_text(item):
    values = []
    if not isinstance(item, dict):
        return ""
    for key in ("input", "arguments", "command", "text"):
        value = item.get(key)
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, dict):
            values.append(json.dumps(value, sort_keys=True))
    return "\n".join(values)


def _audit_capabilities(item):
    found = set(_item_capabilities(item))
    if not isinstance(item, dict):
        return found
    kind = str(item.get("type") or "")
    name = str(item.get("name") or item.get("tool_name") or "")
    if name == "wait" or kind == "wait":
        found.add("deferred_wait")
    if _SKILL_USE.search(_wire_text(item)):
        found.add("skills")
    return found


def inspect_usage(transcript_path):
    """Audit capability leakage in a Codex trace, de-duplicated by transcript item id."""
    capabilities = _AUDIT_CAPABILITIES
    attempted = {capability: set() for capability in capabilities}
    completed = {capability: set() for capability in capabilities}
    failed = {capability: set() for capability in capabilities}
    tools = Counter()
    seen_tools = set()
    reconnects = 0
    try:
        handle = open(transcript_path, encoding="utf-8", errors="replace")
    except OSError:
        handle = None
    if handle is not None:
        with handle:
            for line_number, line in enumerate(handle, 1):
                try:
                    event = json.loads(line)
                except Exception:
                    continue
                phase = str(event.get("type") or "")
                if phase == "error" and "reconnecting" in str(
                        event.get("message") or event.get("error") or "").lower():
                    reconnects += 1
                if phase not in ("item.started", "item.completed"):
                    continue
                item = event.get("item") or {}
                represented = _audit_capabilities(item) & set(capabilities)
                if not represented:
                    continue
                item_id = str(item.get("id") or item.get("call_id") or line_number)
                status = str(item.get("status") or "").lower()
                label = str(
                    item.get("name") or item.get("tool_name")
                    or item.get("tool") or item.get("type") or "unknown")
                if item_id not in seen_tools:
                    tools[label] += 1
                    seen_tools.add(item_id)
                for capability in represented:
                    attempted[capability].add(item_id)
                    if phase == "item.completed":
                        if status in _FAILED_STATUSES:
                            failed[capability].add(item_id)
                        else:
                            completed[capability].add(item_id)
    return {
        "attempted": {c: len(attempted[c]) for c in capabilities},
        "completed": {c: len(completed[c]) for c in capabilities},
        "failed": {c: len(failed[c]) for c in capabilities},
        "tools": dict(sorted(tools.items())),
        "reconnects": reconnects,
    }


# ===========================================================================
# The recent-experience controller.
#
# `_EVIDENCE_CANDIDATES` is the full set of packages whose exposures the state machine
# TRACKS (incl. deferred_wait, so its protection status is still recorded). The public
# controller (CodexUntoolProfile) treats deferred_wait as CORE and only ever DEMOTES the
# `CANDIDATE_CAPABILITIES` subset -- the shipped indirect-use-core behavior.
# ===========================================================================
_EVIDENCE_CANDIDATES = (
    "collaboration", "web", "goals", "interactive_guidance", "skills", "request_user_input",
    "image_media", "deferred_wait")
CORE_CAPABILITIES = ("deferred_wait",)
CANDIDATE_CAPABILITIES = tuple(
    c for c in _EVIDENCE_CANDIDATES if c not in CORE_CAPABILITIES)

MIN_LOADED_EXPOSURES = {
    "collaboration": 1,
    "web": 1,
    # Product-goal metadata (create/get/update_goal): orchestration state like
    # collaboration, structurally inapplicable to a sealed single-task repair, ~0.7k tokens
    # of the code-mode exec description. 0 uses in 243 gpt-5.6-terra baseline tasks.
    "goals": 1,
    # Chat-session guidance in the base prompt (persona, commentary channel, formatting):
    # structurally inapplicable to a detached run, like collaboration. Removed through a
    # lean copy of the base prompt when the harness has a capture for the CLI build.
    "interactive_guidance": 1,
    "skills": 2,
    "request_user_input": 2,
    "image_media": 3,
}
MAX_DEMOTIONS_PER_TASK = 2
RECENT_WINDOW = 3

# Native config carries a supported toggle for these; web/image are also relay-pruned
# because current Codex builds serialize their real declarations inside the unified exec
# package (their native toggles are harmless defense in depth).
# `skills`: the CLI bundles system skills into $CODEX_HOME at startup and lists
# them in a <skills_instructions> developer message (~0.64k tokens); disabling each bundled
# skill through `[[skills.config]] enabled = false` removes the whole block. Its relay id is
# kept for harnesses that prune by request rewrite.
CONFIG_CAPABILITIES = frozenset(("collaboration", "web", "goals", "skills", "image_media",
                                 "interactive_guidance"))
_RELAY_CAPABILITIES = {
    "collaboration": (),
    "web": ("web",),
    "goals": (),
    "interactive_guidance": (),
    "skills": ("skills",),
    "request_user_input": ("request_user_input",),
    "image_media": ("image_media",),
    "deferred_wait": ("deferred_wait",),
}

FIRST_PROBE_AT = 24
INITIAL_PROBE_GAP = 32
PROBE_PRIORITY = (
    "image_media", "skills", "request_user_input", "web", "collaboration", "goals",
    "interactive_guidance")

PROFILE_VERSION = "codex-recent-experience-untool-indirect-use-core"
RETAINED_CAPABILITIES = ("apply_patch", "exec_command", "planning", "write_stdin")


def relay_capabilities(logical_capabilities):
    """Expand logical packages into relay rewrite capability ids."""
    expanded = []
    for capability in logical_capabilities or ():
        expanded.extend(_RELAY_CAPABILITIES.get(capability, ()))
    return sorted(set(expanded))


def _contract_fingerprint():
    return hashlib.sha256(json.dumps({
        "profile": PROFILE_VERSION,
        "evidence_candidates": _EVIDENCE_CANDIDATES,
        "candidates": CANDIDATE_CAPABILITIES,
        "core_capabilities": CORE_CAPABILITIES,
        "min_loaded_exposures": MIN_LOADED_EXPOSURES,
        "max_demotions_per_task": MAX_DEMOTIONS_PER_TASK,
        "recent_window": RECENT_WINDOW,
        "config_capabilities": sorted(CONFIG_CAPABILITIES),
        "relay_capabilities": _RELAY_CAPABILITIES,
        "first_probe_at": FIRST_PROBE_AT,
        "initial_probe_gap": INITIAL_PROBE_GAP,
        "probe_priority": PROBE_PRIORITY,
    }, sort_keys=True).encode("utf-8")).hexdigest()


CONTRACT_FINGERPRINT = _contract_fingerprint()


class CodexUntoolProfile:
    """Persistent, repository-scoped, causal full-to-pruned Codex untool controller.

    Usage per task (the ToolingStage drives this):
        p = CodexUntoolProfile(repo, results_root, fresh=...)
        sel = p.begin_task(iid, problem_statement=...)   # frozen capability mask
        ... run the task with sel['config_capabilities'] / sel['relay_capabilities'] ...
        rec = p.observe(inspect_usage(transcript), resolved=...)  # fold into state
    """

    def __init__(self, repo, results_root, *, fresh=False):
        self.repo = repo
        state_dir = os.path.join(
            os.path.abspath(results_root), "interface_state",
            repo_filename(repo))
        self.path = os.path.join(state_dir, "codex_untool.json")
        self._pending = None
        self.state = self._new_state()
        if not fresh:
            self._load()

    # ---- state ------------------------------------------------------------
    def _new_state(self):
        zero = {c: 0 for c in _EVIDENCE_CANDIDATES}
        return {
            "version": PROFILE_VERSION,
            "contract_fingerprint": CONTRACT_FINGERPRINT,
            "repo": self.repo,
            "tasks_completed": 0,
            "current_pruned": [],
            "ever_completed": [],
            "core_capabilities": list(CORE_CAPABILITIES),
            "attempted_total": dict(zero),
            "completed_total": dict(zero),
            "failed_total": dict(zero),
            "loaded_exposures": dict(zero),
            "incomplete_attempt_exposures": dict(zero),
            "zero_attempt_exposures": dict(zero),
            "recent_loaded_outcomes": {c: [] for c in _EVIDENCE_CANDIDATES},
            "next_probe_at": FIRST_PROBE_AT,
            "probe_gap": INITIAL_PROBE_GAP,
            "probe_cursor": 0,
            "transitions": [],
            "history": [],
        }

    def _load(self):
        try:
            loaded = json.load(open(self.path, encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        if (loaded.get("version") != PROFILE_VERSION
                or loaded.get("contract_fingerprint") != CONTRACT_FINGERPRINT
                or loaded.get("repo") != self.repo):
            return
        state = self._new_state()
        state.update(loaded)
        for field in ("attempted_total", "completed_total", "failed_total",
                      "loaded_exposures", "incomplete_attempt_exposures",
                      "zero_attempt_exposures"):
            merged = {c: 0 for c in _EVIDENCE_CANDIDATES}
            merged.update({c: int(v or 0)
                           for c, v in (state.get(field) or {}).items()
                           if c in _EVIDENCE_CANDIDATES})
            state[field] = merged
        recent = {c: [] for c in _EVIDENCE_CANDIDATES}
        for capability, outcomes in (
                state.get("recent_loaded_outcomes") or {}).items():
            if capability not in recent:
                continue
            recent[capability] = [
                str(v) for v in (outcomes or ())
                if v in ("completed", "incomplete_attempt", "unused")
            ][-RECENT_WINDOW:]
        state["recent_loaded_outcomes"] = recent
        state["current_pruned"] = sorted(
            set(state.get("current_pruned") or ()) & set(CANDIDATE_CAPABILITIES))
        state["ever_completed"] = sorted(
            set(state.get("ever_completed") or ()) & set(_EVIDENCE_CANDIDATES))
        state["core_capabilities"] = list(CORE_CAPABILITIES)
        self.state = state

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        temporary = self.path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(self.state, handle, indent=2, sort_keys=True)
        os.replace(temporary, self.path)

    # ---- probe / demotion policy -----------------------------------------
    def _choose_probe(self, applied_pruned, hints):
        completed = int(self.state.get("tasks_completed", 0) or 0)
        if completed < int(self.state.get("next_probe_at", FIRST_PROBE_AT)):
            return None
        eligible = set(applied_pruned) - set(hints)
        if not eligible:
            return None
        start = int(self.state.get("probe_cursor", 0) or 0)
        for offset in range(len(PROBE_PRIORITY)):
            capability = PROBE_PRIORITY[(start + offset) % len(PROBE_PRIORITY)]
            if capability in eligible:
                return capability
        return None

    def _eligible_demotions(self, pruned, protected):
        """Deferred_wait is CORE -> only CANDIDATE_CAPABILITIES are ever demotable."""
        eligible = []
        recent = self.state.get("recent_loaded_outcomes") or {}
        for capability in CANDIDATE_CAPABILITIES:
            if capability in pruned or capability in protected:
                continue
            needed = MIN_LOADED_EXPOSURES[capability]
            outcomes = list(recent.get(capability) or ())
            if len(outcomes) >= needed and "completed" not in outcomes[-needed:]:
                eligible.append(capability)
        return eligible[:MAX_DEMOTIONS_PER_TASK]

    # ---- per-task selection ----------------------------------------------
    def begin_task(self, task_id, *, problem_statement=""):
        """Freeze a mask using ONLY state from already-completed tasks."""
        completed = int(self.state.get("tasks_completed", 0) or 0)
        hints = task_hints(problem_statement)
        logical = set(self.state.get("current_pruned") or ()) - set(hints)
        probe = self._choose_probe(logical, hints)
        if probe:
            logical.discard(probe)
        logical = sorted(logical)
        # deferred_wait is core: it must never appear in a pruned mask.
        assert not (set(logical) & set(CORE_CAPABILITIES)), \
            "attempted to prune an indirect-use core package"
        if completed == 0:
            reason = "observe-complete-contract"
        elif probe:
            reason = f"single-package-probe:{probe}"
        elif hints:
            reason = "task-hint:" + ",".join(sorted(hints))
        else:
            reason = "recent-experience-indirect-use-core-mask"
        retained = sorted(set(RETAINED_CAPABILITIES) | set(CORE_CAPABILITIES))
        self._pending = {
            "task_id": str(task_id),
            "profile": PROFILE_VERSION,
            "audit": completed == 0,
            "reason": reason,
            "logical_capabilities": logical,
            "config_capabilities": sorted(set(logical) & CONFIG_CAPABILITIES),
            "relay_capabilities": relay_capabilities(logical),
            "task_hints": sorted(hints),
            "probe_capability": probe,
            "probe_reason": "evidence-aging" if probe else None,
            "tasks_completed_before": completed,
            "core_capabilities": list(CORE_CAPABILITIES),
            "retained_capabilities": retained,
        }
        return copy.deepcopy(self._pending)

    def _transition(self, kind, capabilities, reason, task_id):
        if not capabilities:
            return
        transitions = list(self.state.get("transitions") or ())
        transitions.append({
            "after_tasks": int(self.state.get("tasks_completed", 0) or 0),
            "task_id": task_id,
            "kind": kind,
            "capabilities": sorted(capabilities),
            "reason": reason,
        })
        self.state["transitions"] = transitions[-128:]

    # ---- fold one completed trace into state (affects the NEXT task only) --
    def observe(self, usage, *, resolved=None):
        pending = self._pending
        if pending is None:
            raise RuntimeError("observe() called without begin_task()")
        attempted = usage.get("attempted") or {}
        completed = usage.get("completed") or {}
        failed = usage.get("failed") or {}
        logical_pruned = set(pending.get("logical_capabilities") or ())
        loaded = set(_EVIDENCE_CANDIDATES) - logical_pruned
        current_pruned = set(self.state.get("current_pruned") or ())
        protected = set(self.state.get("ever_completed") or ())
        completed_now = set()

        for capability in _EVIDENCE_CANDIDATES:
            attempted_n = int(attempted.get(capability, 0) or 0)
            completed_n = int(completed.get(capability, 0) or 0)
            failed_n = int(failed.get(capability, 0) or 0)
            self.state["attempted_total"][capability] += attempted_n
            self.state["completed_total"][capability] += completed_n
            self.state["failed_total"][capability] += failed_n
            if completed_n > 0:
                protected.add(capability)
                current_pruned.discard(capability)
                completed_now.add(capability)
            if capability not in loaded:
                continue
            self.state["loaded_exposures"][capability] += 1
            if completed_n > 0:
                outcome = "completed"
            elif attempted_n > 0:
                outcome = "incomplete_attempt"
                self.state["incomplete_attempt_exposures"][capability] += 1
            else:
                outcome = "unused"
                self.state["zero_attempt_exposures"][capability] += 1
            outcomes = list(self.state["recent_loaded_outcomes"].get(capability) or ())
            outcomes.append(outcome)
            self.state["recent_loaded_outcomes"][capability] = outcomes[-RECENT_WINDOW:]

        probe = pending.get("probe_capability")
        if probe:
            gap = max(1, int(self.state.get(
                "probe_gap", INITIAL_PROBE_GAP) or INITIAL_PROBE_GAP))
            self.state["next_probe_at"] = int(
                self.state.get("next_probe_at", FIRST_PROBE_AT)) + gap
            self.state["probe_gap"] = gap * 2
            self.state["probe_cursor"] = (
                (PROBE_PRIORITY.index(probe) + 1) % len(PROBE_PRIORITY))
            if probe not in protected:
                current_pruned.add(probe)
                self._transition("probe-reprune", [probe],
                                 "targeted-probe-produced-no-completed-use",
                                 pending.get("task_id"))
            else:
                self._transition("probe-protect", [probe],
                                 "targeted-probe-produced-completed-use",
                                 pending.get("task_id"))

        newly_pruned = self._eligible_demotions(current_pruned, protected)
        current_pruned.update(newly_pruned)
        current_pruned.difference_update(protected)
        current_pruned &= set(CANDIDATE_CAPABILITIES)   # core never pruned
        before = int(self.state.get("tasks_completed", 0) or 0)
        self.state["tasks_completed"] = before + 1
        self.state["current_pruned"] = sorted(current_pruned)
        self.state["ever_completed"] = sorted(protected)
        self._transition("gradual-demotion", newly_pruned,
                         "recent-loaded-exposures-with-zero-completed-use",
                         pending.get("task_id"))
        self._transition("completed-protect", completed_now,
                         "completed-use-permanently-protected",
                         pending.get("task_id"))

        record = {
            "task_id": pending.get("task_id"),
            "audit": bool(pending.get("audit")),
            "reason": pending.get("reason"),
            "logical_capabilities": sorted(logical_pruned),
            "config_capabilities": list(pending.get("config_capabilities") or ()),
            "relay_capabilities": list(pending.get("relay_capabilities") or ()),
            "task_hints": list(pending.get("task_hints") or ()),
            "probe_capability": probe,
            "probe_reason": pending.get("probe_reason"),
            "resolved": resolved,
            "loaded_capabilities": sorted(loaded),
            "attempted": {c: int(attempted.get(c, 0) or 0)
                          for c in _EVIDENCE_CANDIDATES},
            "completed": {c: int(completed.get(c, 0) or 0)
                          for c in _EVIDENCE_CANDIDATES},
            "failed": {c: int(failed.get(c, 0) or 0)
                       for c in _EVIDENCE_CANDIDATES},
            "newly_pruned": sorted(newly_pruned),
            "mask_after": list(self.state["current_pruned"]),
        }
        history = list(self.state.get("history") or ())
        history.append(record)
        self.state["history"] = history[-256:]
        self.save()
        self._pending = None
        return record

    def debug_state(self):
        state = copy.deepcopy(self.state)
        state["next_logical_mask"] = list(state.get("current_pruned") or ())
        state["next_relay_mask"] = relay_capabilities(state.get("current_pruned") or ())
        state["core_capabilities"] = list(CORE_CAPABILITIES)
        state["state_path"] = self.path
        return state
