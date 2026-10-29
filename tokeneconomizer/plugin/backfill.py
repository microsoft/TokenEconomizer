"""Learning from the sessions a repository ran before the plugin was installed.

Without this, a fresh install starts from nothing: micro-skills and tool pruning see only the
sessions that end after it. Claude Code keeps every session's log under
`~/.claude/projects/<encoded cwd>/<session id>.jsonl` (`$CLAUDE_CONFIG_DIR` when set), so the
first SessionStart in a repository hands its earlier logs to a detached `te backfill`, which
folds them oldest first through the same `fold.fold_session` a finished session goes through;
the sessions that follow keep learning as before.

  * The encoded directory name is lossy (`/` and `.` both become `-`), so a log's repository is
    read from the `cwd` its own records carry and keyed by `repo_identity`, as a live session is.
  * Only the most recent `backfill_sessions` logs are folded: each fold can be a paid proposer
    tick (one every `microskills_propose_every` sessions), and old sessions say least about how
    the repository works now.
  * A log written to in the last `fold.STALE_S` seconds may be a running session; it is left to
    its own SessionEnd.
  * It runs once per repository and agent (`backfill.json`); a session already folded is never
    folded again, so `te backfill` by hand is safe to repeat.

Only Claude Code's history is read; for the other agents this is a no-op.
"""
import json
import os
import time
from datetime import datetime

from . import fold
from .paths import repo_identity

STATE = "backfill.json"
HEAD_LINES = 200        # records read from a log's start looking for its cwd and first timestamp


def claude_projects_dir(env):
    base = env.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(base, "projects")


def _timestamp(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _head(path):
    """(session id, cwd, first timestamp) from a Claude Code log's first records."""
    sid = cwd = started = None
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= HEAD_LINES or (cwd and started):
                    break
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict) or rec.get("isSidechain"):
                    continue
                sid = sid or rec.get("sessionId")
                cwd = cwd or rec.get("cwd")
                started = started or _timestamp(rec.get("timestamp"))
    except OSError:
        return None
    return sid, cwd, started


def history(ctx, exclude=(), now=None):
    """[(started, session id, log path, last write)] of this repository's earlier Claude Code sessions,
    oldest first, not yet folded, at most `backfill_sessions` of the most recent."""
    if ctx.host.name != "claude":
        return []
    now = now or time.time()
    folded = ctx.store.read("folded.json") or {}
    projects = claude_projects_dir(ctx.env)
    try:
        dirs = [os.path.join(projects, d) for d in os.listdir(projects)]
    except OSError:
        return []
    keys = {}                                   # cwd -> repository key
    out = []
    for d in dirs:
        try:
            names = [n for n in os.listdir(d) if n.endswith(".jsonl")]
        except OSError:
            continue
        for name in names:
            path = os.path.join(d, name)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if now - mtime < fold.STALE_S:
                continue
            head = _head(path)
            if not head or not head[0] or not head[1]:
                continue
            sid, cwd, started = head
            if sid in folded or sid in exclude:
                continue
            if cwd not in keys:
                try:
                    keys[cwd] = repo_identity(cwd, ctx.env).key
                except OSError:
                    keys[cwd] = None
            if keys[cwd] != ctx.repo.key:
                continue
            out.append((started or mtime, sid, path, mtime))
    out.sort()
    return [(s, sid, p, m) for s, sid, p, m in out[-ctx.config.backfill_sessions:]]


def due(ctx):
    """Whether this repository's history has yet to be read (cheap: one file read)."""
    return (ctx.config.backfill and ctx.host.name == "claude"
            and not ctx.store.read(STATE))


def run(ctx, exclude=()):
    """Fold the repository's earlier sessions. Returns the ledger records of those folded."""
    with ctx.store.locked("backfill", timeout=1.0) as got:
        if not got:
            return []
        found = history(ctx, exclude=exclude)
        records = []
        for started, sid, path, ended in found:
            name = os.path.join("sessions", f"{sid}.json")
            if not ctx.store.read(name):
                ctx.store.write(name, {"transcript": path, "started": started, "ended": ended,
                                       "backfilled": True})
            rec = fold.fold_session(ctx, sid, transcript_path=path)
            if rec is not None:
                records.append(rec)
        prior = ctx.store.read(STATE) or {}
        ctx.store.write(STATE, {"at": time.time(), "found": len(found),
                                "folded": int(prior.get("folded", 0)) + len(records)})
        return records


def spawn(ctx, exclude=()):
    """Run the backfill detached (inline when TE_FOLD_SYNC=1, for tests)."""
    if ctx.env.get("TE_FOLD_SYNC") == "1":
        return run(ctx, exclude=exclude)
    return ctx.spawn(["backfill"] + [a for sid in exclude for a in ("--exclude", sid)],
                     "fold.log")
