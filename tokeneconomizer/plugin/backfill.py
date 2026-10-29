"""Learning from the sessions an agent logged before the plugin was installed.

A fold learns from a finished session; the hooks only see sessions that start after install, so
a new install begins with nothing. Claude Code, though, keeps every session's log under
`~/.claude/projects/`, and each log names the directory it started in. `backfill` finds the logs
of this repository's earlier sessions and folds them, oldest first, the way `fold.pending_sessions`
folds a session whose end was missed; every session after that is learned from as it ends.

  * It runs once per repository and agent, in the background (a detached `te backfill`, started by
    the first SessionStart). `te backfill` runs it again by hand; a session already folded is
    never folded twice, so that only adds what is new.
  * It reads the newest `backfill_sessions` logs of the repository (0 turns it off), and folds
    them oldest first, so the fact lifecycle sees them in the order they ran.
  * A log still being written (quiet for less than `fold.STALE_S`), the current session's, and an
    empty one are left alone: the hooks fold those.
  * Only the free layer learns from history (`fold.fold_session(history=True)`): reading a long
    history makes no model call. The proposer runs on the sessions that end from now on.

Only Claude Code's logs are read; the other agents have no such directory this reads.
"""
import os
import time

from . import fold, repos

MARKER = "backfill.json"
RESPAWN_S = 600         # a backfill started this recently is still running
DEFAULT_SESSIONS = 50


def history_dir(host, env=None):
    """Where `host` keeps its session logs for every project, or None when it is not read."""
    if host != "claude":
        return None
    env = os.environ if env is None else env
    home = env.get("CLAUDE_CONFIG_DIR") or os.path.join(
        env.get("HOME") or env.get("USERPROFILE") or os.path.expanduser("~"), ".claude")
    return os.path.join(home, "projects")


def _logs(root):
    """[(mtime, session id, path)] for every top-level session log under the projects directory
    (a subagent's log lives in a subdirectory of its session and is part of that session)."""
    out = []
    try:
        projects = [e.path for e in os.scandir(root) if e.is_dir()]
    except OSError:
        return out
    for d in projects:
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            if e.is_file() and e.name.endswith(".jsonl"):
                try:
                    st = e.stat()
                except OSError:
                    continue
                if st.st_size:
                    out.append((st.st_mtime, e.name[:-6], e.path))
    return out


def candidates(ctx, exclude=(), now=None, limit=None):
    """[(session id, log path)] of this repository's earlier sessions that are not folded yet,
    oldest first: the newest `limit` of them (default `backfill_sessions`)."""
    root = history_dir(ctx.host.name, ctx.env)
    limit = ctx.config.backfill_sessions if limit is None else limit
    if not root or limit <= 0:
        return []
    now = now or time.time()
    folded = ctx.store.read("folded.json") or {}
    cache, found = {}, []
    # Newest first, so a long history stops being read once `limit` sessions are found.
    for mtime, sid, path in sorted(_logs(root), reverse=True):
        if len(found) >= limit:
            break
        if sid in folded or sid in exclude or now - mtime < fold.STALE_S:
            continue
        repo = repos._resolve(repos._transcript_cwd(path), ctx.env, cache)
        if repo is not None and repo.key == ctx.repo.key:
            found.append((mtime, sid, path))
    return [(sid, path) for _, sid, path in sorted(found)]


def run(ctx, exclude=(), now=None, limit=None, log=None):
    """Fold this repository's earlier sessions. Returns {"found", "folded"}; the marker records
    when it finished, so a session start does not start it again."""
    found = candidates(ctx, exclude=exclude, now=now, limit=limit)
    done = 0
    for sid, path in found:
        if fold.fold_session(ctx, sid, transcript_path=path, history=True) is not None:
            done += 1
        if log:
            log(sid)
    result = {"found": len(found), "folded": done}
    ctx.store.write(MARKER, dict(result, finished=time.time(), limit=(
        ctx.config.backfill_sessions if limit is None else limit)))
    return result


def needed(ctx, now=None):
    """Should this session start begin a backfill: it is on, the agent's logs are read, and none
    has finished or is running for this repository."""
    if ctx.config.backfill_sessions <= 0 or not history_dir(ctx.host.name, ctx.env):
        return False
    m = ctx.store.read(MARKER) or {}
    if m.get("finished"):
        return False
    return (now or time.time()) - float(m.get("started") or 0) >= RESPAWN_S


def start(ctx, exclude=()):
    """Begin a backfill in the background (inline under `TE_FOLD_SYNC=1`, for tests). The
    marker is written first, so concurrent session starts do not pile up."""
    if not needed(ctx):
        return False
    ctx.store.write(MARKER, {"started": time.time()})
    if ctx.env.get("TE_FOLD_SYNC") == "1":
        run(ctx, exclude=exclude)
        return True
    return ctx.spawn(["backfill"], "backfill.log")


def summary(ctx):
    """The marker as `status` reports it: None before a backfill has run."""
    m = ctx.store.read(MARKER)
    return m if isinstance(m, dict) and m.get("finished") else None

