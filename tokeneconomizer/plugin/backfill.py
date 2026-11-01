"""Folding the sessions a repository already ran, before this plugin ever watched one.

The live path learns from sessions it sees end: a hook hands the export to a detached fold. A
repository with a history has none of that -- the sessions ran before the plugin was installed,
or ended without their hook. opencode keeps them in its own database, so they can be handed over
the same way: an export file (`trace.opencode_db` writes it), a session record, and the same
`fold.fold_session` a live session ends into. Nothing here is a second learning path; it is the
live one, replayed.

Three things keep a replay honest and cheap:

  * a session is folded once -- `folded.json` already keys that, and a session it holds is
    skipped before its export is even written;
  * a session still running is left alone (`ACTIVE_S`): its own end will fold it, and folding it
    now would both cut its trajectory short and re-fold it later under the same id;
  * the replay runs at most once per `MAX_AGE_H` per repository (`backfill.json`), because every
    opencode start would otherwise walk the database again -- `--force` replays regardless.

Micro-skills' proposer is a model call charged to the session. A replay of a hundred sessions
would make twenty of them in a row, one per few sessions, so a replay learns the facts that need
no model (`microskills=envfacts`) unless `paid` says otherwise; the distiller picks the repository
up again on its next live session.
"""
import os
import time

from .. import trace
from . import fold, hosts
from .context import LiveContext
from .paths import state_root

MAX_AGE_H = 6.0        # how long a replay's result stands before another opencode start replays
ACTIVE_S = 300         # a session updated this recently may still be running: leave it alone


def _session_record(session, transcript):
    """The record the hooks write for a live session, for one recovered from the database.
    The directory is spelled as the OS does (opencode's database keeps `C:/a/b`), so a record
    made here and one made by a hook name the same directory the same way."""
    return {"session_id": session["id"], "host": "opencode",
            "cwd": os.path.normpath(session["directory"]),
            "started": session["started"], "ended": session["updated"],
            "transcript": transcript, "source": "import", "turns": [], "nudges": 0,
            "aborts": 0, "judge_tokens": 0.0, "writer_tokens": 0.0,
            "updated": time.time()}


def _fresh(store, force):
    """Whether this repository's last replay is recent enough to stand."""
    if force:
        return False
    last = store.read("backfill.json") or {}
    return (time.time() - float(last.get("at") or 0)) < MAX_AGE_H * 3600


def _pending(conn, ctx, directory, since_ms, exclude, now, force):
    """(session, already folded) for the directory: the ones to replay, oldest first."""
    folded = ctx.store.read("folded.json") or {}
    out, done = [], 0
    for s in trace.opencode_db.sessions(conn, since_ms, directories=[directory],
                                        exclude=exclude):
        if s["id"] in folded:
            done += 1
        elif not force and now - s["updated"] < ACTIVE_S:
            continue                            # probably still running: its own end folds it
        else:
            out.append(s)
    return sorted(out, key=lambda s: s["started"] or 0), done


def _replay(conn, ctx, directory, since_ms, exclude, limit, paid, force, dry_run, out):
    """Fold this directory's old sessions into its state; returns the counts."""
    if not paid:
        # A replay is not the moment to spend a model call per few sessions (see the module).
        ctx.config.microskills = "envfacts"
    sessions, done = _pending(conn, ctx, directory, since_ms, exclude, time.time(), force)
    if limit:
        sessions = sessions[:limit]
    if dry_run:
        return {"directory": directory, "folded": len(sessions), "already": done, "errors": 0,
                "dry_run": True}
    exports = os.path.join(state_root(ctx.env), "opencode-sessions")
    folded, errors = 0, 0
    for s in sessions:
        transcript = os.path.join(exports, f"{s['id']}.json")
        try:
            if trace.opencode_db.write_export(conn, s, transcript) is None:
                continue
            ctx.store.write(os.path.join("sessions", f"{s['id']}.json"),
                            _session_record(s, transcript))
            if fold.fold_session(ctx, s["id"]):
                folded += 1
        except (OSError, ValueError) as e:      # one unreadable session must not stop the rest
            errors += 1
            out(f"  {s['id']}: {e}")
    ctx.store.write("backfill.json", {"at": time.time(), "folded": folded, "sessions": len(sessions),
                                      "directory": directory})
    return {"directory": directory, "folded": folded, "already": done, "errors": errors,
            "dry_run": False}


def run(directories=None, since_days=3.0, limit=None, exclude=(), dry_run=False, paid=False,
        force=False, env=None, out=print):
    """Replay opencode's recorded sessions into this repository's state.

    `directories` is None for every project the database holds, else the projects to replay.
    Returns the per-directory counts, plus what a fresh-enough repository skipped."""
    since_ms = (time.time() - max(0.0, float(since_days)) * 86400) * 1000
    path = trace.opencode_db.db_path(env)
    if not os.path.exists(path):
        return {"db": path, "missing": True, "results": []}
    conn = trace.opencode_db.connect(path)
    try:
        if not trace.opencode_db.has_schema(conn):
            return {"db": path, "unsupported": True, "results": []}
        targets = (directories if directories is not None
                   else trace.opencode_db.directories(conn, since_ms))
        results = []
        for directory in targets:
            host = hosts.HOSTS["opencode"]()
            ctx = LiveContext.build(host, directory, env)
            if _fresh(ctx.store, force):
                results.append({"directory": directory, "skipped": "replayed recently"})
                continue
            results.append(_replay(conn, ctx, directory, since_ms, exclude, limit, paid,
                                   force, dry_run, out))
        return {"db": path, "results": results}
    finally:
        conn.close()
