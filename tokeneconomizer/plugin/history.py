"""Importing a repository's sessions from before the plugin, from the agent's own session logs.

The plugin learns from sessions it sees end. Claude Code keeps every session's log
(`~/.claude/projects/<folder>/<session id>.jsonl`, or under `$CLAUDE_CONFIG_DIR`), so the
sessions a repository ran before the plugin was installed can be folded too, once, and learning
starts at install rather than some sessions later. `te history import` does it; with
`history_import` on (it is off by default), the first session start in a repository starts it
by itself, in a detached process.

Which logs are this repository's: each log's recorded directory is keyed with `repo_identity`,
as a live session's is. Where that directory no longer exists (a moved checkout, a renamed
home), Claude Code has moved the log to the folder it derives from the directory's new path, so
the log's folder name is matched EXACTLY against the one Claude Code derives from the
repository's root (`trace.claude_project_folder`): never a prefix, or `widget-shell` would be
taken for `widget`. Left out: a log written in the last `fold.STALE_S` (its session may still be
running), one already folded or recorded by the plugin's own hooks, one with no prompt, one over
`MAX_BYTES`, one older than `history_days`, and a one-prompt SDK run (`claude -p`, a script),
whose restricted tools and long autonomous turns say little about the person's own sessions.
The newest `history_max_sessions` are kept, and folded oldest first.

Time order is the point of most of what follows. Folding treats each session as the newest so
far: a fact recent sessions no longer support would come back, and tools in use lately would
look long unused, if older sessions were folded after newer ones. So:

  * an import runs only into a store with nothing folded yet. A repository that already has
    folded sessions records `{"state": "skipped"}` and never tries again; `te history import`
    there refuses, with the reason;
  * the import holds the repository's one fold lock for its whole run, and while `history.json`
    says `running` or `partial` no live fold folds (`fold.import_blocks`, checked before the
    lock is taken, so a session's end never waits on the import): the session stays pending,
    and the import folds the pending sessions, oldest first, once it is done;
  * the plan -- the ordered session ids and paths -- is saved, and a run stopped by its time
    budget (`BUDGET_S`) is `partial` and resumes the same plan in the same order. The run
    refreshes a heartbeat; one gone stale (`HEARTBEAT_STALE_S`: the process died) lets the next
    session start restart it, so live folding cannot stay blocked. A session whose fold fails is
    counted as skipped and not tried again, and so is one the import's process died on
    `MAX_ATTEMPTS` times (`attempt`).

An imported session is folded to learn from and nothing else (`fold.fold_locked(imported=True)`):
scored against the tools its own log shows, never today's catalog; no model call; no deny rule
held, written or given back; its ledger record and `folded.json` entry say `imported`, and `te
status` counts it apart from the sessions the plugin ran with. Tool pruning keeps the plugin's own
sessions' evidence apart too, and `toolprune=auto` writes by itself only what that alone is
enough for (`fold._fold_toolprune`). Each imported session's prompt-turns are replayed through
the loop guard with no envelope and no judge -- it can neither redirect nor stop anything -- and
the turns that ended green seed the repository's envelope (`replay_envelope`).

`history.json` (beside `folded.json`):

    {"state": "running" | "partial" | "done" | "skipped" | "stopped", "reason",
     "plan": [{"id", "path", "started", "ended"}], "next", "imported", "skipped": {why: n},
     "over_limit", "attempt": {"id", "n"}, "started", "heartbeat", "finished", "noticed"}
"""
import os
import time
import traceback

from .. import loopguard, trace
from . import fold
from .hosts import _EXIT_RE, RESULT_CAP, claude_success_code

STATE = fold.HISTORY
LOG = "history.log"
BUDGET_S = 1800                    # one run's time budget; a longer import resumes next time
HEARTBEAT_STALE_S = 900            # a running import this quiet has died
MAX_BYTES = 256 * 1024 * 1024      # a log larger than this is not read
HISTORY_LOCK_S = 1.0               # a session start's wait for `history.json` (then next time)
MAX_ATTEMPTS = 2                   # runs that died on one session before it is left out
_clock = time.monotonic            # the run's budget clock


def _record_error(root):
    try:
        os.makedirs(root, exist_ok=True)
        with open(os.path.join(root, "errors.log"), "a", encoding="utf-8") as fh:
            fh.write(f"--- {time.strftime('%Y-%m-%dT%H:%M:%S')} pid={os.getpid()} (history)\n")
            fh.write(traceback.format_exc())
    except Exception:
        pass


def read_state(ctx):
    """`history.json` as a dict; None when there is none, or it is not a JSON object."""
    doc = ctx.store.read(STATE)
    return doc if isinstance(doc, dict) else None


def _num(v, default=0):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def _stale(doc, now):
    return now - _num(doc.get("heartbeat")) >= HEARTBEAT_STALE_S


def _ledger_size(store):
    try:
        return os.path.getsize(store.path("ledger.jsonl"))
    except OSError:
        return 0


def _session_records(store):
    """The ledger's records of folded sessions. Not the loop guard's judge's `overhead`
    records: it charges those while a session runs, before anything is folded."""
    if _ledger_size(store) == 0:
        return []
    return [r for r in store.read_lines("ledger.jsonl")
            if isinstance(r, dict) and r.get("kind") == "session"]


def _nothing_folded(store):
    """Has nothing been folded into this store: no `folded.json` entry, no session record?"""
    return not (store.read("folded.json") or {}) and not _session_records(store)


def _live_folds(store):
    """Has a session other than an imported one been folded here?"""
    folded = store.read("folded.json") or {}
    if any(not (isinstance(v, dict) and v.get("imported")) for v in folded.values()):
        return True
    return any(not r.get("imported") for r in _session_records(store))


def _folded_by_import(store, sid):
    """Is `sid` in `folded.json` as a session this repository's import folded?"""
    entry = (store.read("folded.json") or {}).get(sid)
    return isinstance(entry, dict) and bool(entry.get("imported"))


def _refusal(ctx, resuming):
    """Why an import may not run here now (one line), or None. A resumed import may go on while
    every fold so far is its own."""
    if resuming:
        if _live_folds(ctx.store):
            return (f"{ctx.repo.label} has sessions folded since this import started; its "
                    f"earlier sessions would be learned after them")
        return None
    if not _nothing_folded(ctx.store):
        return (f"{ctx.repo.label} already has folded sessions; earlier ones would be learned "
                f"after them, so its history is read only before the first session is folded")
    return None


# ---- which logs ------------------------------------------------------------------------------
def _identity(cwd, env, cache):
    if cwd not in cache:
        from .paths import repo_identity
        try:
            cache[cwd] = repo_identity(cwd, env).key
        except (OSError, ValueError):
            cache[cwd] = None
    return cache[cwd]


def is_this_repository(ctx, folder, cwd, cache=None):
    """Is a log filed under `folder` that names `cwd` a session of this repository? Its recorded
    directory decides, keyed as a live session's is; only where that directory is gone, an
    exact match of the folder with the one Claude Code derives from the repository's root."""
    cache = {} if cache is None else cache
    if cwd and os.path.isdir(cwd):
        return _identity(cwd, ctx.env, cache) == ctx.repo.key
    root = ctx.repo.root
    names = {trace.claude_project_folder(root), trace.claude_project_folder(os.path.realpath(root))}
    return folder in names


def _headless(head, prompts):
    """A one-prompt SDK run: `claude -p`, or a script through the SDK. A person who talks to
    Claude Code through an SDK front end gives it several prompts, and that session is kept."""
    return prompts <= 1 and str(head.get("entrypoint") or "").lower().startswith("sdk")


def plan(ctx, now=None):
    """What an import would fold here, and why the rest is left out; reads, never writes.

    Returns {"sessions": [{"id", "path", "started", "ended"}] oldest first, "skipped": {why: n},
    "over_limit": n}."""
    now = time.time() if now is None else now
    cfg = ctx.config
    folded = ctx.store.read("folded.json") or {}
    skipped, found, cache = {}, [], {}

    def skip(why):
        skipped[why] = skipped.get(why, 0) + 1
    for log in ctx.host.history(ctx.env, cfg.history_dirs):
        head = trace.claude_log_head(log["path"])
        if not is_this_repository(ctx, log["folder"], head.get("cwd"), cache):
            continue
        sid = trace.claude_safe_session_id(log["session_id"])
        mtime, size = log["mtime"], log["size"]
        if sid is None:
            skip("unsafe_id")
        elif mtime > now - fold.STALE_S:
            skip("recent")                         # may be running
        elif mtime < now - cfg.history_days * 86400:
            skip("too_old")
        elif size > MAX_BYTES:
            skip("too_large")
        elif sid in folded:
            skip("already_folded")
        elif os.path.exists(ctx.store.path("sessions", f"{sid}.json")):
            skip("recorded_live")
        else:
            prompts = trace.claude_prompt_count(log["path"])
            if prompts <= 0:
                skip("no_prompt")
            elif _headless(head, prompts):
                skip("headless")
            else:
                found.append({"id": sid, "path": log["path"],
                              "started": head.get("started") or mtime, "ended": mtime})
    found.sort(key=lambda s: (s["started"], s["ended"], s["id"]))
    over = max(0, len(found) - cfg.history_max_sessions)
    return {"sessions": found[over:], "skipped": skipped, "over_limit": over}


def _still_eligible(ctx, item):
    """Why a planned session is no longer to be imported (a resumed plan), or None."""
    sid = trace.claude_safe_session_id(item.get("id")) if isinstance(item, dict) else None
    path = item.get("path") if isinstance(item, dict) else None
    if sid is None or not isinstance(path, str) or os.path.basename(path) != f"{sid}.jsonl":
        return "unsafe_id"
    if sid in (ctx.store.read("folded.json") or {}):
        return "already_folded"
    if os.path.exists(ctx.store.path("sessions", f"{sid}.json")):
        return "recorded_live"
    try:
        if os.path.getsize(path) > MAX_BYTES:
            return "too_large"
    except OSError:
        return "gone"
    return None


# ---- the loop guard's envelope ---------------------------------------------------------------
def replay_envelope(ctx, transcript_path):
    """Seed the loop guard's envelope from an imported session, as its Stop hooks would have.

    Each prompt-turn is folded call by call into a fresh `TurnGuard` with no envelope and no
    judge, so nothing can redirect or stop it, and the result text and exit code it gets are the
    ones a live hook would have (a failed call's "Exit code N", a successful shell call's 0 where
    `claude_success_code` gives it, else none). A turn feeds the envelope, as a live one does,
    when it stopped normally: not interrupted by the person, and, for the session's last turn,
    answered (a log cut off mid-turn did not stop normally).
    Returns the number of turns recorded."""
    cfg = ctx.config
    if cfg.loopguard == "off":
        return 0
    level, overrides = cfg.loopguard_aggr, cfg.loopguard_thresholds
    thr = loopguard.base_thresholds(level, overrides)
    recorded = 0
    turns = trace.claude_prompt_turns(transcript_path)
    turn = next(turns, None)
    while turn is not None:
        following = next(turns, None)
        guard = loopguard.TurnGuard()
        for name, args, result, ok, _cwd in trace.claude_calls(turn):
            result = (result or "")[:RESULT_CAP]
            if ok:
                code = claude_success_code(name, args)
            else:
                m = _EXIT_RE.search(result[:400])
                code = int(m.group(1)) if m else None
            guard.fold(name, args if isinstance(args, dict) else {}, result, code,
                       envelope=None, thr=thr, judge=None)
        normal = not trace.claude_turn_interrupted(turn) and (
            following is not None or trace.claude_turn_answered(turn))
        if guard.cum_turns and normal:
            loopguard.record_turn(ctx.store, guard, level=level, overrides=overrides)
            recorded += 1
        turn = following
    return recorded


# ---- the run ---------------------------------------------------------------------------------
def _update(ctx, change, timeout=10.0):
    """Read-modify-write `history.json` under its own lock; returns the new doc, or None when
    the lock was busy. `change(doc)` returns False to write nothing."""
    with ctx.store.locked("history", timeout=timeout) as got:
        if not got:
            return None
        doc = read_state(ctx) or {}
        if change(doc) is False:
            return doc
        ctx.store.write(STATE, doc)
        return doc


def _save(ctx, doc):
    with ctx.store.locked("history", timeout=10.0):
        ctx.store.write(STATE, doc)


def _say(log, text):
    if log is not None:
        try:
            log(text)
        except Exception:
            pass


def run(ctx, auto=False, now=None, budget_s=BUDGET_S, log=None):
    """Import this repository's earlier sessions, holding the fold lock throughout.

    `auto`: started by a session start, which imports only into a store with nothing folded and
    records a skip otherwise; by hand (`te history import`), a refusal changes nothing that is
    recorded but a store never imported into. Returns {"state", "imported", "skipped",
    "reason"}: state "busy" when the fold lock stayed taken, "refused" when the import may not
    run here."""
    now = time.time() if now is None else now
    if not getattr(ctx.host, "keeps_history", False):
        return {"state": "refused", "reason": f"{ctx.host.name} keeps no session history "
                                              f"the plugin reads"}
    with ctx.store.locked("fold", timeout=fold.FOLD_LOCK_WAIT_S) as got:
        if not got:
            return {"state": "busy", "reason": "another fold holds this repository's lock"}
        doc = read_state(ctx) or {}
        state = doc.get("state")
        blocking = state in fold.HISTORY_BLOCKING
        # A plan stopped part-way resumes in order: by itself while it blocks live folds, and
        # by hand after `history_import=off` stopped it, if nothing was folded live since.
        resuming = ((blocking or (not auto and state == "stopped"))
                    and isinstance(doc.get("plan"), list))
        if auto and not blocking:
            return {"state": state or "none", "reason": "nothing to resume"}
        if auto and not ctx.config.history_import:
            doc.update(state="stopped", reason="history_import is off", at=time.time())
            _save(ctx, doc)
            return {"state": "stopped", "reason": doc["reason"]}
        why = _refusal(ctx, resuming)
        if why:
            if state is None or blocking:       # recorded, so that it never runs by itself
                _save(ctx, dict({k: v for k, v in doc.items() if k != "plan"},
                                state="skipped", reason=why, at=time.time()))
            return {"state": "refused" if not auto else "skipped", "reason": why}
        try:
            return _run_locked(ctx, doc, resuming, now, budget_s, log)
        except Exception:
            # Not left `running`: live folds would wait for a run that fails again each time.
            _record_error(ctx.root)
            doc = read_state(ctx) or {}
            doc.update(state="stopped", reason="the import failed (see errors.log)",
                       at=time.time())
            _save(ctx, doc)
            return {"state": "error", "reason": doc["reason"]}


def _run_locked(ctx, doc, resuming, now, budget_s, log):
    """`run` once it may go on, under the fold lock: plan (or take up the saved plan), fold
    each session in order, save the place after each, then fold the sessions left pending."""
    if not resuming:
        # Live folds wait from now on, while the plan is made, not only once it is.
        _save(ctx, {k: v for k, v in dict(doc, state="running",
                                          heartbeat=time.time()).items() if k != "plan"})
        p = plan(ctx, now)
        doc = {"state": "running", "plan": p["sessions"], "next": 0, "imported": 0,
               "skipped": p["skipped"], "over_limit": p["over_limit"],
               "planned": len(p["sessions"]), "started": time.time(), "noticed": False}
        _say(log, f"{len(p['sessions'])} earlier session(s) of {ctx.repo.label} to read")
    doc.update(state="running", pid=os.getpid(), heartbeat=time.time())
    doc["runs"] = int(_num(doc.get("runs"))) + 1
    _save(ctx, doc)
    skipped = doc["skipped"] = dict(doc["skipped"] if isinstance(doc.get("skipped"), dict)
                                    else {})
    items = doc["plan"]
    deadline = _clock() + max(0.0, float(budget_s))
    i = int(_num(doc.get("next")))
    while i < len(items):
        if _clock() >= deadline:
            break
        _import_one(ctx, doc, items[i], skipped, log)
        i += 1
        doc.pop("attempt", None)
        doc.update(next=i, heartbeat=time.time())
        _save(ctx, doc)
    if i < len(items):
        doc["state"] = "partial"            # the next session start resumes the plan
    else:
        doc.update(state="done", finished=time.time())
        doc.pop("plan", None)
    _save(ctx, doc)
    if doc["state"] == "done":
        # The sessions that ended while the import ran, in the order they ran.
        for sid in fold.pending_sessions(ctx):
            try:
                fold.fold_locked(ctx, sid)
            except Exception:
                _record_error(ctx.root)
    return {"state": doc["state"], "imported": doc.get("imported", 0),
            "skipped": dict(skipped), "over_limit": doc.get("over_limit", 0),
            "left": len(items) - i, "reason": None}


def _import_one(ctx, doc, item, skipped, log):
    """Fold one planned session, under the fold lock. `doc["attempt"]`, saved before the fold
    and dropped with the place saved after it, tells a restarted run what the last one was
    doing when its process died: a session it died on `MAX_ATTEMPTS` times is left out (an
    error), so that no log can block live folds for good; one it folded before it could save
    its place is counted, and its turns replayed into the envelope."""
    sid = item.get("id") if isinstance(item, dict) else None
    att = doc.get("attempt") if isinstance(doc.get("attempt"), dict) else {}
    tries = int(_num(att.get("n"))) if sid is not None and att.get("id") == sid else 0
    why = _still_eligible(ctx, item)
    if why == "already_folded" and _folded_by_import(ctx.store, sid):
        # This import folded it, and its process died before it saved its place.
        doc["imported"] = int(_num(doc.get("imported"))) + 1
        if tries < MAX_ATTEMPTS:
            doc["attempt"] = {"id": sid, "n": tries + 1}
            _save(ctx, doc)
            try:
                replay_envelope(ctx, item["path"])
            except Exception:
                _record_error(ctx.root)
        return
    if why:
        skipped[why] = skipped.get(why, 0) + 1
        return
    if tries >= MAX_ATTEMPTS:
        skipped["error"] = skipped.get("error", 0) + 1      # the process died on it each time
        return
    doc["attempt"] = {"id": sid, "n": tries + 1}
    _save(ctx, doc)
    try:
        rec = fold.fold_locked(ctx, sid, item["path"], imported=True,
                               span=(item.get("started"), item.get("ended")))
        if rec is not None and not rec.get("error"):
            doc["imported"] = int(_num(doc.get("imported"))) + 1
            try:
                replay_envelope(ctx, item["path"])
            except Exception:
                _record_error(ctx.root)
            _say(log, f"read {sid}")
        elif rec is not None:
            skipped["unreadable"] = skipped.get("unreadable", 0) + 1
    except Exception:
        _record_error(ctx.root)
        skipped["error"] = skipped.get("error", 0) + 1


# ---- what a session start does ---------------------------------------------------------------
def maybe_start(ctx, now=None):
    """At a session start: start the import, resume one stopped by its budget, or restart one
    whose process died -- in a detached `te history import` (inline when TE_FOLD_SYNC=1, for
    tests). Reads only `history.json`, and `folded.json` and the ledger (when it is not empty)
    when there is no `history.json` yet. A store with folded sessions records the skip, once."""
    if not getattr(ctx.host, "keeps_history", False):
        return None
    now = time.time() if now is None else now
    exists = os.path.exists(ctx.store.path(STATE))
    if exists and read_state(ctx) is None:
        return None                     # unreadable: left alone (and it blocks no fold)
    if not exists and not ctx.config.history_import:
        return None
    start = {}

    def decide(doc):
        state = doc.get("state")
        if not doc:
            if not ctx.config.history_import:
                return False
            if not _nothing_folded(ctx.store):
                doc.update(state="skipped", at=now,
                           reason=f"{ctx.repo.label} already had folded sessions")
                return None
            doc.update(state="running", started=now, heartbeat=now, noticed=False)
        elif state == "partial" or (state == "running" and _stale(doc, now)):
            if not ctx.config.history_import:
                doc.update(state="stopped", reason="history_import is off", at=now)
                return None
            doc["heartbeat"] = now      # so that another session start does not start one too
        else:
            return False
        start["go"] = True
        return None
    if _update(ctx, decide, timeout=HISTORY_LOCK_S) is None or not start:
        return None
    if ctx.env.get("TE_FOLD_SYNC") == "1":
        return run(ctx, auto=True)
    return ctx.spawn(["history", "import", "--auto"], LOG)


def notice(ctx):
    """Once, after an import finished: the line saying it read the repository's history."""
    doc = read_state(ctx)
    if not doc or doc.get("state") != "done" or doc.get("noticed"):
        return None
    out = {}

    def mark(d):
        if d.get("state") != "done" or d.get("noticed"):
            return False
        d["noticed"] = True
        out["n"] = int(_num(d.get("imported")))
        return None
    if _update(ctx, mark, timeout=HISTORY_LOCK_S) is None or not out.get("n"):
        return None
    return (f"TokenEconomizer read {out['n']} earlier session(s) of {ctx.repo.label} from "
            f"Claude Code's history.")


def summary(ctx):
    """`history.json` without its plan, for `te status` and `te history`."""
    doc = read_state(ctx)
    if doc is None:
        return None
    out = {k: v for k, v in doc.items() if k != "plan"}
    if isinstance(doc.get("plan"), list):
        out["left"] = max(0, len(doc["plan"]) - int(_num(doc.get("next"))))
    return out


def describe(doc):
    """One line for `te status`: what the import did here."""
    if not doc:
        return None
    state = doc.get("state")
    n = int(_num(doc.get("imported")))
    if state == "done":
        return f"read {n} earlier session(s) from Claude Code's history"
    if state in fold.HISTORY_BLOCKING:
        return (f"reading Claude Code's history: {n} session(s) so far, {doc.get('left', '?')} "
                f"to go; sessions ending meanwhile are folded after it")
    if state == "skipped":
        return f"not read ({doc.get('reason') or 'skipped'})"
    if state == "stopped":
        return f"stopped after {n} session(s) ({doc.get('reason') or 'stopped'})"
    return None
