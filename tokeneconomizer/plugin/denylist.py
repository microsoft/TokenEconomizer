"""Writing an approved tool-pruning proposal into each agent's own settings, and taking it back.

Tool pruning decides WHICH tools to drop (`toolprune.ToolingStage`, run by `fold`). A plugin
launches nothing, so on each host the lever is a per-repository settings file the agent reads at
session start, and the unit of removal is whatever that file can express:

  * Claude Code -- a TOOL. A bare-name `permissions.deny` rule removes the tool from the
    session's tool list, not merely refusing the call. Verified on 2.1.286: three deny rules
    took a session from 30 tools to 27, the same list `--disallowed-tools` produced, and the
    same rules in a project's `.claude/settings.local.json` took it to 23.
  * Codex -- a CAPABILITY package (`toolprune.codex`), written as config toggles into the
    project's `.codex/config.toml`. Verified on 0.153.1 with `codex debug prompt-input`: the
    collaboration, goals and image toggles take effect from a project-local file -- but ONLY in
    a trusted project; an untrusted project's `.codex/` is ignored, so `apply` checks trust and
    says so. The `skills` package is not offered: its toggle takes effect in the user-level
    config only, and a per-repository decision does not belong there.
  * Copilot CLI -- an MCP SERVER, through `disabledMcpServers` in the repository's
    `.github/copilot/settings.local.json`. Verified on 1.0.83: listing the built-in
    `github-mcp-server` removes its tools from the request -- but, as on Codex, ONLY in a
    trusted folder (Copilot's `trustedFolders`). Copilot has no persistent per-tool filter
    (`--excluded-tools` is a launch flag; `permissions.deny` refuses calls but leaves the schema
    in the request), so a server is proposed only when every one of its tools has gone unused.

Rules common to all three:

  * `toolprune: auto` (the default) writes the file once a proposal forms and the repository's
    sessions have been idle for `toolprune_quiet_minutes` (`fold.apply_pending`), and the next
    session start says so; with `toolprune: propose` only the person's approval (`te toolprune
    apply`) does. Either way it applies from the NEXT session. Verified on Claude Code 2.1.307,
    Codex 0.163.0 and Copilot 1.0.102 (docs/PLUGIN.md §3): a running session keeps the tools
    it started with; a conversation resumed after the change sends the new tools, which
    re-writes its whole history to the prompt cache -- hence the wait.
  * The writer adds and removes exactly the entries it recorded adding (`untool_applied.json`,
    per settings file, since the clones of one repository share its state but not their
    settings); an entry the person wrote is never touched.
  * When the plugin creates the file, it adds the path to `.git/info/exclude`, so a file meant
    to be per-user can never be committed by accident.

Checking what an applied list did, and measuring a request with and without it, is
`toolcheck`'s job.
"""
import json
import os
import re
import shutil
import subprocess
import time

from .. import trace
from . import adapter as live_adapter

SETTINGS_REL = os.path.join(".claude", "settings.local.json")
CODEX_CONFIG_REL = os.path.join(".codex", "config.toml")
COPILOT_SETTINGS_REL = os.path.join(".github", "copilot", "settings.local.json")

_SETTINGS = {"claude": SETTINGS_REL, "codex": CODEX_CONFIG_REL, "copilot": COPILOT_SETTINGS_REL}
_UNITS = {"claude": "tool", "codex": "capability", "copilot": "MCP server"}

# Codex capability -> (table or None for top level, key, TOML value). Each probed project-local
# on 0.153.1 (see the module docstring); `te_harness.codex` holds the launch-time superset.
CODEX_TOGGLES = {
    "collaboration": ((None, "include_collaboration_mode_instructions", "false"),
                      ("agents", "enabled", "false"),
                      ("features", "multi_agent", "false")),
    "goals": (("features", "goals", "false"),),
    "image_media": (("features", "view_image", "false"),),
}
# What the live Codex lever can reach. `web` and `request_user_input` are a person's tools and
# never proposed (as the web is not on Claude Code); `interactive_guidance` rewrites the base
# prompt's guidance for talking to a user, which an interactive session is; `skills` is
# user-level only.
CODEX_LIVE_CAPABILITIES = tuple(sorted(CODEX_TOGGLES))
MARK = "# tokeneconomizer"


def supports(host):
    return host in _SETTINGS


def unit(host, plural=False):
    u = _UNITS.get(host, "tool")
    return (u[:-1] + "ies" if u.endswith("y") else u + "s") if plural else u


def settings_path(repo_root, host="claude"):
    return os.path.join(repo_root, _SETTINGS.get(host, SETTINGS_REL))


def _read_settings(path):
    """The settings object at `path`: {} when there is no file. A file that is there but is not
    a JSON object is refused rather than written over -- it is the person's, and a parse error
    is no reason to replace what they had in it."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise RuntimeError(f"cannot read {path}: {e}")
    if not text.strip():
        return {}
    try:
        obj = json.loads(text)
    except ValueError as e:
        raise RuntimeError(f"refusing to edit {path}: it is not valid JSON ({e}); fix it first")
    if not isinstance(obj, dict):
        raise RuntimeError(f"refusing to edit {path}: it is not a JSON object")
    return obj


def _guard_target(repo_root, path):
    """Refuse a write that a symlink would carry out of the repository.

    The settings paths are the repository's, so a checkout decides what they are: a `.claude`,
    `.codex` or `.github` that is a symlink, or a settings file that is one, would land the
    write somewhere else -- a user-level configuration, another project."""
    root = os.path.realpath(repo_root)
    parent = os.path.realpath(os.path.dirname(path))
    if os.path.islink(path) or os.path.commonpath([root, parent]) != root:
        raise RuntimeError(f"refusing to write {path}: it is, or goes through, a symlink that "
                           "leads out of the repository")


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".te.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def _write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".te.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _exclude_from_git(repo_root, rel=SETTINGS_REL):
    """Append `rel` to `.git/info/exclude` (local-only ignore), idempotently."""
    from .paths import _git_common_dir
    gitdir = _git_common_dir(repo_root)
    if not gitdir:
        return False
    # A `.git` FILE names its git directory, so a checkout could point it anywhere: follow it
    # only to something that is a git directory, and never write through a symlinked `info` or
    # `exclude`.
    if (os.path.isfile(os.path.join(repo_root, ".git"))
            and not os.path.isfile(os.path.join(gitdir, "HEAD"))):
        return False
    path = os.path.join(gitdir, "info", "exclude")
    if os.path.islink(os.path.dirname(path)) or os.path.islink(path):
        return False
    entry = "/" + rel.replace(os.sep, "/")
    try:
        existing = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
        if entry in existing.split():
            return True
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(("" if existing.endswith("\n") or not existing else "\n")
                     + "# added by TokenEconomizer (per-user tool deny list)\n" + entry + "\n")
        return True
    except OSError:
        return False


# What the writer added, per settings file: clones and worktrees of one repository share its
# state (and so its proposal), but each checkout has its own settings file. Up to 0.1.0 the
# record held one file's entries (`{"tools", "path"}`); that shape is still read.
APPLIED = "untool_applied.json"


def _canon(path):
    return os.path.realpath(path) if path else ""


def applied_by_path(doc):
    """{settings file: [entries the plugin added to it]} from an `untool_applied.json`."""
    if isinstance(doc.get("by_path"), dict):
        return {_canon(p): list(t or []) for p, t in doc["by_path"].items()}
    if doc.get("tools"):
        return {_canon(doc.get("path") or ""): list(doc["tools"])}
    return {}


def kept_by_path(doc):
    """{settings file: [entries the person gave back with `restore`]}: never applied again by
    `toolprune: auto`, which would otherwise remove them at the next fold."""
    kept = doc.get("kept")
    return {_canon(p): list(t or []) for p, t in kept.items()} if isinstance(kept, dict) else {}


def write_applied(store, by_path, kept=None):
    doc = store.read(APPLIED) or {}
    kept = kept_by_path(doc) if kept is None else kept
    store.write(APPLIED, {"by_path": {p: sorted(t) for p, t in by_path.items() if t},
                          "kept": {p: sorted(t) for p, t in kept.items() if t},
                          "updated": time.time()})


def kept_for(store, repo_root, host="claude"):
    """What the person gave back in this checkout, which `auto` must leave alone."""
    return list(kept_by_path(store.read(APPLIED) or {}).get(
        _canon(settings_path(repo_root, host))) or [])


def set_kept(store, repo_root, host, add=(), remove=()):
    """Mark entries as given back by the person (`restore`), or no longer (`apply` by name)."""
    doc = store.read(APPLIED) or {}
    kept = kept_by_path(doc)
    path = _canon(settings_path(repo_root, host))
    kept[path] = sorted((set(kept.get(path) or []) | set(add)) - set(remove))
    write_applied(store, applied_by_path(doc), kept)


def applied(store, path=None):
    """What the plugin added to the settings file at `path`; without one, to any checkout's."""
    by = applied_by_path(store.read(APPLIED) or {})
    if path is None:
        return sorted(set().union(*by.values())) if by else []
    return list(by.get(_canon(path)) or by.get("") or [])


def applied_for(store, repo_root, host="claude"):
    """What the plugin added to this checkout's settings file for `host`."""
    return applied(store, settings_path(repo_root, host))


def _record(store, items, path):
    by = applied_by_path(store.read(APPLIED) or {})
    by.pop("", None)
    by[_canon(path)] = sorted(items)
    write_applied(store, by)


# ---- the JSON-list hosts: Claude Code's deny rules, Copilot's disabled MCP servers -------
def _json_list_keys(host):
    return ("permissions", "deny") if host == "claude" else (None, "disabledMcpServers")


def _apply_json_list(repo_root, items, store, host):
    path = settings_path(repo_root, host)
    _guard_target(repo_root, path)
    created = not os.path.exists(path)
    data = _read_settings(path)
    outer, key = _json_list_keys(host)
    holder = data.setdefault(outer, {}) if outer else data
    lst = holder.setdefault(key, [])
    added = [t for t in items if t not in lst]
    if not added:
        return []
    lst.extend(added)
    # Recorded before written, as `restore` writes before it forgets: whichever write fails, the
    # record still covers every entry of the plugin's in the file, so `restore` can take it back.
    _record(store, set(applied(store, path)) | set(added), path)
    write_json(path, data)
    if created:
        _exclude_from_git(repo_root, _SETTINGS[host])
    return added


def _restore_json_list(repo_root, store, items, host):
    mine = set(applied(store, settings_path(repo_root, host)))
    target = mine if not items else mine & set(items)
    if not target:
        return []
    path = settings_path(repo_root, host)
    _guard_target(repo_root, path)
    data = _read_settings(path)
    outer, key = _json_list_keys(host)
    holder = (data.get(outer) or {}) if outer else data
    lst = holder.get(key) or []
    kept = [r for r in lst if r not in target]
    removed = [r for r in lst if r in target]
    if key in holder:
        holder[key] = kept
        if not kept:
            holder.pop(key)
        if outer and not holder:
            data.pop(outer, None)
        write_json(path, data)
    _record(store, mine - target, path)
    return removed


# ---- Codex: marked lines in the project's .codex/config.toml ----------------------------
_HEADER_RE = re.compile(r"^\s*\[\s*([A-Za-z0-9_.\-\"]+)\s*\]\s*(#.*)?$")
_ANY_HEADER_RE = re.compile(r"^\s*\[")


def _sections(lines):
    """[(table or None, start, end)] -- line ranges of the top level and each [table]."""
    out, cur, start = [], None, 0
    for i, line in enumerate(lines):
        if _ANY_HEADER_RE.match(line):
            out.append((cur, start, i))
            m = _HEADER_RE.match(line)
            cur, start = (m.group(1) if m else "[[array]]"), i + 1
    out.append((cur, start, len(lines)))
    return out


def _key_set(lines, table, key):
    """Is `key` already set in `table` -- as `key =` under [table], or as a top-level dotted
    `table.key =`? A key the user set is theirs, whatever its value."""
    for t, a, b in _sections(lines):
        for line in lines[a:b]:
            s = line.strip()
            if table is None and t is None and re.match(rf"{re.escape(key)}\s*=", s):
                return True
            if table is not None and t == table and re.match(rf"{re.escape(key)}\s*=", s):
                return True
            if table is not None and t is None and re.match(
                    rf"{re.escape(table)}\.{re.escape(key)}\s*=", s):
                return True
    return False


def _toml_insert(text, table, key, value, tag):
    """Insert `key = value` into `table` (None: top level), marked with `tag`."""
    lines = text.splitlines()
    if _key_set(lines, table, key):
        return text, False
    # `features = { ... }` -- an inline table cannot be extended, and adding a [features] table
    # beside it would make the file invalid; leave that table to the person who wrote it.
    if table is not None and any(re.match(rf"\s*{re.escape(table)}\s*=", ln) for ln in lines):
        return text, False
    line = f"{key} = {value}  {tag}"
    secs = _sections(lines)
    first_header = next((a - 1 for t, a, b in secs[1:]), len(lines))
    if table is None:
        lines.insert(first_header, line)
    else:
        hdr = next((a for t, a, b in secs if t == table), None)
        dotted = any(re.match(rf"\s*{re.escape(table)}\.\w+\s*=", ln)
                     for ln in lines[:first_header])
        if hdr is not None:
            lines.insert(hdr, line)
        elif dotted:
            lines.insert(first_header, f"{table}.{line}")
        else:
            if lines and lines[-1].strip():
                lines.append("")
            lines += [f"[{table}]  {tag}", line]
    return "\n".join(lines) + "\n", True


def _toml_remove(text, tags):
    """Drop every line carrying one of `tags`; a [table] header the plugin added goes too,
    unless someone has since put a key of their own under it."""
    lines = text.splitlines()
    marked = lambda ln: any(ln.rstrip().endswith(t) for t in tags)
    keep = []
    for i, ln in enumerate(lines):
        if not marked(ln):
            keep.append(ln)
            continue
        if _HEADER_RE.match(ln):
            j, foreign = i + 1, False
            while j < len(lines) and not _ANY_HEADER_RE.match(lines[j]):
                if lines[j].strip() and not marked(lines[j]):
                    foreign = True
                    break
                j += 1
            if foreign:
                keep.append(ln)
    while keep and not keep[-1].strip():
        keep.pop()
    return ("\n".join(keep) + "\n") if keep else ""


def _toml_valid(text):
    try:
        import tomllib
    except ImportError:           # Python 3.10: Codex itself is the check (`verify_codex`)
        return True
    try:
        tomllib.loads(text)
        return True
    except tomllib.TOMLDecodeError:
        return False


def _git_tracked(repo_root, rel):
    """Is `rel` committed in this repository? False when git cannot tell."""
    try:
        out = subprocess.run(["git", "ls-files", "--error-unmatch", rel], cwd=repo_root,
                             capture_output=True, encoding="utf-8", errors="replace", timeout=10)
        return out.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def codex_config_error(repo_root, cli="codex", timeout=30):
    """Codex's own complaint about the configuration it would load here, or None.

    `codex features list` loads the whole layered configuration and calls no model, so a file
    that would break the person's next session fails here first. Silent (None) when the CLI is
    not installed, or the project is untrusted and its file is not loaded at all."""
    if not shutil.which(cli):
        return None
    try:
        out = subprocess.run([cli, "features", "list"], capture_output=True,
                             encoding="utf-8", errors="replace", cwd=repo_root, timeout=timeout,
                             env=dict(os.environ, TE_SIDECALL="1"))
    except (OSError, subprocess.SubprocessError):
        return None
    return None if out.returncode == 0 else ((out.stderr or out.stdout).strip()[:500]
                                             or f"exit {out.returncode}")


def _apply_codex(repo_root, capabilities, store):
    path = settings_path(repo_root, "codex")
    _guard_target(repo_root, path)
    created = not os.path.exists(path)
    if not created and _git_tracked(repo_root, CODEX_CONFIG_REL):
        # Claude Code's and Copilot's files are per-user by name; a project's .codex/config.toml
        # is often the TEAM's, committed. Marked lines in it would ride along in the next commit.
        raise RuntimeError(f"{path} is committed in this repository; the plugin will not edit a "
                           "shared file. Add the toggles by hand, or keep them in your own "
                           "copy of it.")
    original = open(path, encoding="utf-8").read() if not created else ""
    text = original
    added = []
    for cap in capabilities:
        if cap not in CODEX_TOGGLES or cap in applied(store, path):
            continue
        for table, key, value in CODEX_TOGGLES[cap]:
            text, _ = _toml_insert(text, table, key, value, f"{MARK}:{cap}")
        added.append(cap)
    if not added:
        return []
    if not _toml_valid(text):
        raise RuntimeError(f"refusing to write {path}: the result would not parse as TOML")
    _write_text(path, text)
    err = codex_config_error(repo_root)
    if err:
        if created:
            os.remove(path)
        else:
            _write_text(path, original)
        raise RuntimeError(f"Codex rejected the edited {path}, so it was put back: {err}")
    if created:
        _exclude_from_git(repo_root, CODEX_CONFIG_REL)
    _record(store, set(applied(store, path)) | set(added), path)
    return added


def _restore_codex(repo_root, store, capabilities):
    mine = set(applied(store, settings_path(repo_root, "codex")))
    target = mine if not capabilities else mine & set(capabilities)
    if not target:
        return []
    path = settings_path(repo_root, "codex")
    _guard_target(repo_root, path)
    if os.path.exists(path):
        text = _toml_remove(open(path, encoding="utf-8").read(),
                            [f"{MARK}:{c}" for c in target])
        if text.strip():
            _write_text(path, text)
        else:
            os.remove(path)
    _record(store, mine - target, path)
    return sorted(target)


def copilot_trusted_folders(env=None):
    """Copilot's `trustedFolders` (its config.json starts with `//` comment lines), or None."""
    from .hosts import copilot_home
    try:
        text = open(os.path.join(copilot_home(env), "config.json"), encoding="utf-8").read()
        doc = json.loads("\n".join(ln for ln in text.splitlines()
                                   if not ln.lstrip().startswith("//")))
    except (OSError, ValueError):
        return None
    return [f for f in (doc.get("trustedFolders") or []) if isinstance(f, str)]


def copilot_folder_trusted(path, env=None):
    """Is `path` in or under a folder Copilot trusts? None when the config is unreadable."""
    folders = copilot_trusted_folders(env)
    if folders is None:
        return None
    real = os.path.normcase(os.path.realpath(path))
    for f in folders:
        f = os.path.normcase(os.path.realpath(f))
        if real == f or real.startswith(f.rstrip(os.sep) + os.sep):
            return True
    return False


def codex_home(env=None):
    env = os.environ if env is None else env
    return env.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")


def codex_project_trusted(repo_root, env=None):
    """True/False from the user's Codex config: is this checkout a trusted project? A
    project-local `.codex/config.toml` is ignored until it is. None if the config is
    unreadable."""
    try:
        text = open(os.path.join(codex_home(env), "config.toml"), encoding="utf-8").read()
    except OSError:
        return None
    want = {os.path.normcase(p) for p in (os.path.abspath(repo_root), os.path.realpath(repo_root))}
    current = None
    for line in text.splitlines():
        m = re.match(r'^\s*\[projects\.(?:"([^"]+)"|\'([^\']+)\')\]\s*$', line)
        if m:
            current = os.path.normcase(m.group(1) or m.group(2))
            continue
        if _ANY_HEADER_RE.match(line):
            current = None
            continue
        if current in want and re.match(r'^\s*trust_level\s*=\s*"trusted"', line):
            return True
    return False


# ---- dispatch ------------------------------------------------------------------------------
def apply(repo_root, tools, store, host="claude"):
    """Write `tools` (tools, capabilities or MCP servers, by host). Returns those newly added."""
    if host == "codex":
        return _apply_codex(repo_root, tools, store)
    return _apply_json_list(repo_root, tools, store, host)


def restore(repo_root, store, tools=None, host="claude"):
    """Remove entries this plugin added (all of them, or just `tools`). Returns removed."""
    if host == "codex":
        return _restore_codex(repo_root, store, tools)
    return _restore_json_list(repo_root, store, tools, host)


def copilot_server_proposal(raw, catalog, used, servers):
    """MCP servers every one of whose catalog tools is in the evidence proposal `raw`."""
    by_server = {}
    for t in catalog:
        s = trace.mcp_server_of(t, servers)
        if s:
            by_server.setdefault(s, set()).add(t)
    raw = set(raw)
    return sorted(s for s, tools in by_server.items()
                  if tools and tools <= raw and not (tools & set(used)))


# ---- the catalog probe and the measurement ------------------------------------------------
