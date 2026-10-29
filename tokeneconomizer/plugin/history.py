"""Learning from the sessions a person ran before the plugin was installed.

The fold learns from each session the plugin sees end. The agent keeps its own log of every
earlier session too -- Claude Code under `~/.claude/projects/<folder>/<session id>.jsonl` -- so
the first session start in a repository hands that repository's earlier logs to a detached
`te history import`, which folds them, oldest first, as the fold folds a session the plugin saw
end. Learning then carries on from new sessions as before.

  * Each log is folded once. `folded.json` keys it by session id, as it does every session, so
    a log the fold or an earlier import already took is skipped, and an import run again finds
    only what is new.
  * An import makes no model call: micro-skills' proposer waits for sessions the plugin sees
    end, so turning the plugin on never pays for a whole history at once.
  * Only logs that are done. A log written in the last `fold.STALE_S` seconds belongs to a
    session still running, and a session the plugin has a record of is its hooks' to fold.
  * Only this repository's: a log belongs to the repository the first directory it names
    resolves to now (`repos.session_cwd`), the rule the fold and `te repos migrate` use.
  * `history.json` records that the import ran and what it found, so it runs by itself once
    per repository and agent; `te history import` runs it again (after `history_dirs`
    changes, say). `history_import=off` keeps it from running by itself.

Where the logs are: the agent's default, or `history_dirs`. A directory there may be an agent
home (holding `projects/`), a `projects/` directory, or one project's folder of logs. Claude
Code is the only agent read so far; another needs its layout in `default_dirs` and `logs`.
"""
import glob
import os
import time

from . import fold, repos
from .paths import home_dir, repo_identity

STATE = "history.json"
RESPAWN_S = 1800        # an import started this recently is still running (or will be retried)


def supports(host_name):
    return host_name == "claude"


def default_dirs(host_name, env=None):
    """Where the agent keeps its session logs when `history_dirs` is not set."""
    env = os.environ if env is None else env
    if host_name == "claude":
        home = home_dir(env)
        d = env.get("CLAUDE_CONFIG_DIR") or (os.path.join(home, ".claude") if home else "")
        return [d] if d else []
    return []


def dirs_for(ctx):
    named = [d.strip() for d in (ctx.config.history_dirs or "").split(os.pathsep) if d.strip()]
    return ([os.path.abspath(os.path.expanduser(d)) for d in named] if named
            else default_dirs(ctx.host.name, ctx.env))


def logs(directory):
    """(path, session id) of every top-level session log under `directory`. A sub-agent's log
    (`<id>/subagents/*.jsonl`, or `agent-*.jsonl` beside the sessions in older versions) is part
    of its session's, which the trace readers take in with it."""
    base = os.path.join(directory, "projects") if os.path.isdir(
        os.path.join(directory, "projects")) else directory
    out = []
    for pattern in ("*.jsonl", os.path.join("*", "*.jsonl")):
        for path in glob.glob(os.path.join(glob.escape(base), pattern)):
            name = os.path.basename(path)
            if not name.startswith("agent-") and os.path.isfile(path):
                out.append((path, name[:-len(".jsonl")]))
    return out


def candidates(ctx, dirs=None, exclude=(), now=None):
    """This repository's finished, never-folded session logs: [(path, session id)], oldest
    first."""
    now = now or time.time()
    folded = ctx.store.read("folded.json") or {}
    seen, keys, out = set(), {}, []
    for d in dirs_for(ctx) if dirs is None else dirs:
        for path, sid in logs(d):
            if sid in seen or sid in folded or sid in exclude:
                continue
            seen.add(sid)
            if os.path.exists(ctx.store.path("sessions", f"{sid}.json")):
                continue                # the plugin saw this session: its hooks fold it
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if now - mtime < fold.STALE_S:
                continue                # still running
            cwd = repos.session_cwd({"transcript": path})
            if not cwd:
                continue
            if cwd not in keys:
                try:
                    keys[cwd] = repo_identity(cwd, ctx.env).key
                except Exception:       # a log names whatever directory it ran in
                    keys[cwd] = None
            if keys[cwd] == ctx.repo.key:
                out.append((mtime, path, sid))
    return [(path, sid) for _, path, sid in sorted(out)]


def wanted(ctx):
    """Whether this agent's earlier sessions are imported by themselves here: the setting is
    on, the agent's logs can be read, and a mechanism is on that learns from them."""
    cfg = ctx.config
    return (cfg.history_import == "on" and supports(ctx.host.name)
            and (cfg.microskills != "off" or cfg.toolprune != "off"))


def start(ctx, exclude=(), now=None):
    """At a session start: the first time in this repository, import its earlier sessions in a
    detached `te history import` (inline under TE_FOLD_SYNC=1, for tests). Returns whether an
    import was started."""
    if not wanted(ctx):
        return False
    now = now or time.time()
    st = ctx.store.read(STATE) or {}
    if st.get("done") or now - float(st.get("started") or 0) < RESPAWN_S:
        return False
    ctx.store.write(STATE, dict(st, started=now))
    if ctx.env.get("TE_FOLD_SYNC") == "1":
        run(ctx, exclude=exclude)
        return True
    return ctx.spawn(["history", "import", "--quiet"]
                     + [a for sid in exclude for a in ("--exclude", sid)], "history.log")


def run(ctx, dirs=None, exclude=(), limit=None):
    """Fold this repository's earlier sessions, oldest first: the most recent `limit` of them
    (`history_max_sessions` when None; 0 for all). Returns what `history.json` records."""
    dirs = dirs_for(ctx) if dirs is None else list(dirs)
    found = candidates(ctx, dirs, exclude)
    limit = ctx.config.history_max_sessions if limit is None else limit
    chosen = found[-limit:] if limit else found
    imported = []
    for path, sid in chosen:
        if fold.fold_session(ctx, sid, transcript_path=path, imported=True) is not None:
            imported.append(sid)
    with ctx.store.locked("history"):
        st = ctx.store.read(STATE) or {}
        st.update({"done": time.time(), "dirs": dirs, "found": len(found),
                   "imported": int(st.get("imported", 0)) + len(imported),
                   "last": {"imported": len(imported), "over_limit": len(found) - len(chosen),
                            "unfolded": len(chosen) - len(imported)}})
        ctx.store.write(STATE, st)
    return st


def notice(ctx, seen, command):
    """Once per import that took any session: a sentence saying so (else None). `seen` is the
    session start's notices.json, which the caller writes."""
    st = ctx.store.read(STATE) or {}
    took = (st.get("last") or {}).get("imported", 0)
    if not st.get("done") or not took or seen.get("history") == st["done"]:
        return None
    seen["history"] = st["done"]
    return (f"TokenEconomizer: learned from {took} earlier session(s) of {ctx.repo.label} "
            f"found in {ctx.host.name}'s own session logs (see `{command}`).")
