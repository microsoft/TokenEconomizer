"""The tool catalog an agent loaded for a session: `{tool_name: description}`.

Tool pruning needs the full list a session was offered, not only the tools it called. Each
agent publishes it differently:

  * Claude Code -- the `system` init event of `stream-json` names the loaded tools; their
                   descriptions are a static property of the CLI build, joined from a snapshot
                   (`claude_tool_catalog.json`);
  * Copilot CLI -- its debug log, in one of three shapes depending on the build (below);
  * Codex       -- a fixed default surface (it writes no tool dump).

`catalog_sources` returns every source that produced anything, and `parse_catalog_with_source`
the richest of them with its name, so a caller can record which source won and notice the next
format change from its records instead of from an empty catalog.
"""
import json
import os
import re

# Codex ships a fixed default tool surface (it does not dump JSON tool schemas into a
# debug log). Inlined here -- verbatim from the original CodexDriver.DEFAULT_TOOL_CATALOG
# -- so catalog-size telemetry reflects what is actually loaded (and prunable) without a
# dependency on the Docker/CLI runner layer.
CODEX_DEFAULT_TOOL_CATALOG = {
    "shell": "Run shell commands to inspect, edit, and test the repository.",
    "apply_patch": "Apply source-code file changes.",
    "web_search": "Search the web.",
    "view_image": "Attach a local image to context (unused on text tasks).",
    "update_plan": "Maintain a step plan (scaffolding, unused here).",
    "spawn_agent": "Spawn a collaborating subagent.",
    "send_input": "Send input to a collaborating subagent.",
    "resume_agent": "Resume a collaborating subagent.",
    "wait_agent": "Wait for a collaborating subagent.",
    "close_agent": "Close a collaborating subagent.",
}

# Matches `"name": "<tool>", ... "description": "<text>"` in the (JSON-in-JSON) debug
# log. Allows optional intervening scalar fields (e.g. `"title": null,`) between name and
# description — newer Copilot CLI schemas insert `"title"`, which previously caused every
# tool except the few lacking a title to be missed.
_CATALOG_RE = re.compile(
    r'"name":\s*"([a-z_]+)"\s*,\s*(?:"[a-z_]+":\s*(?:"[^"]*"|null|true|false|[0-9.]+)\s*,\s*)*'
    r'"description":\s*"')

# Recovering the Copilot CLI's catalog. Three sources, in the order the CLI dropped them:
#   1. `_CATALOG_RE` over debug-log tool SCHEMAS ("name" + "description" objects). Gone by
#      1.0.82 -- newer builds write no schemas at all.
#   2. `parse_tool_names_copilot` over the transcript's `session.usage_checkpoint` ->
#      `data.promptCacheBreakState[].models[].tools[]`. Carried 1.0.82; 1.0.83 stopped emitting
#      the event.
#   3. The flat catalog the debug log still writes on both versions --
#
#          "tools": [ "bash:66921ccc", "read_bash:71c40652", ... ]
#
# (1) and (2) are both gone on 1.0.83, and an empty catalog is indistinguishable from "no
# tools loaded", which would leave pruning a no-op. So every source is tried and the richest
# wins (descriptions beat names, more tools beat fewer). The flat form carries names only,
# which is enough for evidence pruning; a tool judge reasoning over descriptions gets nothing
# from it.
_CATALOG_FLAT_BLOCK_RE = re.compile(r'"tools"\s*:\s*\[(.*?)\]', re.DOTALL)
_CATALOG_FLAT_ITEM_RE = re.compile(r'"([a-z][a-z0-9_]*):[0-9a-f]{6,}"')


def parse_tool_catalog_flat(debug_log_text):
    """{tool_name: ""} from the CLI >= 1.0.83 flat `"tools": ["name:fingerprint", ...]`."""
    out = {}
    for block in _CATALOG_FLAT_BLOCK_RE.findall(debug_log_text or ""):
        for name in _CATALOG_FLAT_ITEM_RE.findall(block):
            out.setdefault(name, "")
    return out


def parse_tool_catalog(debug_log_text, max_desc=200):
    """Extract {tool_name: short_description} from a saved copilot debug log. Works on
    the JSON-in-JSON escaped payload (the tool schemas the model is sent). Agent-agnostic
    in spirit: any agent whose log carries name/description tool defs parses the same.

    Schema-only by design: the flat name:fingerprint catalog is a SEPARATE source
    (`parse_tool_catalog_flat`), and `trace.catalog_sources` tries both and picks the
    richest. Folding the fallback in here would make the winning source unreportable --
    a run using the flat catalog would still claim it had parsed schemas."""
    if not debug_log_text:
        return {}
    s = (debug_log_text.replace('\\"', '"').replace('\\n', '\n').replace('\\\\', '\\'))
    out = {}
    for m in _CATALOG_RE.finditer(s):
        name = m.group(1)
        if name in out:
            continue
        desc = s[m.end():m.end() + max_desc + 40].split('\n')[0][:max_desc]
        out[name] = desc
    return out


# Claude Code writes no debug log of tool schemas. Its `stream-json` init event carries the
# loaded tool NAMES; the descriptions are a static property of the CLI build, snapshotted once
# into claude_tool_catalog.json and joined against the live names.
_CLAUDE_CATALOG_SNAPSHOT = os.path.join(os.path.dirname(__file__),
                                        "claude_tool_catalog.json")


def _load_claude_catalog_snapshot(path=None):
    p = path or _CLAUDE_CATALOG_SNAPSHOT
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def parse_tool_catalog_claude(transcript_text, max_desc=200, snapshot=None):
    """Extract {tool_name: short_description} for a Claude Code run. Reads the loaded
    tool NAMES from the transcript's `system` init event and joins them to the
    description snapshot (claude_tool_catalog.json). Names present in the transcript
    but missing from the snapshot get an empty description (the classifier still sees
    the name); snapshot entries whose tool was NOT loaded this run are skipped, so the
    catalog reflects what the agent was actually sent. Returns {} if no system event."""
    if not transcript_text:
        return {}
    snap = snapshot if snapshot is not None else _load_claude_catalog_snapshot()
    names = []
    for line in transcript_text.splitlines():
        line = line.strip()
        if not line or '"type"' not in line or '"system"' not in line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if isinstance(ev, dict) and ev.get("type") == "system" and isinstance(
                ev.get("tools"), list):
            names = ev["tools"]
            break
    return {n: (snap.get(n, "") or "")[:max_desc] for n in names}


def _read(path):
    try:
        return open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return ""


def catalog_sources(run_dir_path, agent):
    """[(source_name, {tool: desc}), ...] for every catalog source that yielded tools."""
    if agent == "codex":
        return [("codex_default", dict(CODEX_DEFAULT_TOOL_CATALOG))]
    if agent == "claude":
        cat = parse_tool_catalog_claude(
            _read(os.path.join(run_dir_path, "transcript.jsonl")))
        return [("claude_transcript", cat)] if cat else []
    dbg = _read(os.path.join(run_dir_path, "copilot_debug.log"))
    tr = _read(os.path.join(run_dir_path, "transcript.jsonl"))
    out = []
    for name, cat in (("schema_debug_log", parse_tool_catalog(dbg)),
                      ("usage_checkpoint", parse_tool_names_copilot(tr)),
                      ("flat_debug_log", parse_tool_catalog_flat(dbg))):
        if cat:
            out.append((name, cat))
    return out


def _catalog_rank(item):
    """Richest-first: prefer a source with descriptions, then one with more tools."""
    _, cat = item
    return (sum(1 for v in cat.values() if v), len(cat))


def parse_catalog_with_source(run_dir_path, agent):
    """({tool: desc}, source_name) -- the richest catalog available for this run dir.

    source_name is None when NO source produced anything. Callers should treat that as a
    defect signal, not as an empty catalog: an empty catalog makes tool pruning a no-op.
    """
    srcs = catalog_sources(run_dir_path, agent)
    if not srcs:
        return {}, None
    name, cat = max(srcs, key=_catalog_rank)
    return cat, name


def parse_catalog(run_dir_path, agent):
    """Agent-agnostic parse of the loaded tool catalog ({name: desc}) from a task's run
    dir -- Copilot debug log or Claude transcript. Returns {} if unavailable.

    Codex has no per-task JSON tool dump, so its known default surface is returned instead
    (CODEX_DEFAULT_TOOL_CATALOG) so catalog-size telemetry is honest.
    """
    return parse_catalog_with_source(run_dir_path, agent)[0]


def parse_tool_names_copilot(transcript_text):
    """{tool_name: ""} for the tools a Copilot run actually loaded.

    The names ride in `session.usage_checkpoint` events under
    data.promptCacheBreakState[].models[].tools[] -- the same list the model was sent, so
    this reflects what the agent really had, not a static default surface.
    """
    names = set()
    for line in (transcript_text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if not isinstance(ev, dict):
            continue
        for pcb in ((ev.get("data") or {}).get("promptCacheBreakState") or []):
            for model in (pcb.get("models") or {}).values():
                for tool in (model.get("tools") or []):
                    name = tool.get("name")
                    if name:
                        names.add(name)
    return {n: "" for n in sorted(names)}
