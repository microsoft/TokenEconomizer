"""Learning from the sessions that ended before the plugin was installed.

A person who installs the plugin already has months of Claude Code transcripts under
`~/.claude/projects/<encoded-cwd>/<session-id>.jsonl` (or `$CLAUDE_CONFIG_DIR/projects`). On a
repository's first use they are folded in, oldest first, exactly as if the plugin had seen them
end -- except that the fold makes no model call and writes no deny list. What the free layers
learn (facts, tool-pruning evidence, the loop guard's envelope) then applies to live sessions as
usual.

The scan never runs in the SessionStart hook. `start` only looks at the state file and a marker
and hands the work to a detached `te backfill`; `plan` is the only code that reads history, and
it writes nothing. A run stops at its time budget and records `partial`, so the next session
start resumes where it stopped -- a session already in `folded.json` is skipped.

State is `<state root>/backfill.json`, one entry per host:

    {host: {"state": "done" | "partial" | "running", "started", "finished",
            "repos": {key: {"label", "sessions", "skipped"}}, "noticed": bool}}

`backfill.log` holds the run's progress lines. The marker `.backfilling-<host>` keeps
concurrent session starts from launching several runs.
"""
import json
import os
import re
import time
import traceback

from .. import loopguard, trace
from ..trace.claude import _is_prompt
from ..trace.jsonl import lines as _lines
from . import fold, repos
from .config import load_config
from .context import LiveContext
from .hosts import Host
from .paths import repo_identity, state_root
from .store import Store

DAYS = 365
MAX_PER_REPO = 200
MAX_BYTES = 256 * 1024 * 1024
REPLAY_MAX_BYTES = 64 * 1024 * 1024
BUDGET_S = 1800
MARKER_S = 600
PLAN_PROGRESS_EVERY = 25
STATE = "backfill.json"
LOG = "backfill.log"


def _root(env):
    return state_root(env)


def _read_state(root):
    return Store(root).read(STATE) or {}


def _host_state(root, host):
    entry = _read_state(root).get(host)
    return entry if isinstance(entry, dict) else {}


def _update(root, host, change):
    """Read-modify-write one host's entry under the backfill lock."""
    st = Store(root)
    with st.locked("backfill", timeout=10.0) as got:
        if not got:
            return None
        doc = st.read(STATE) or {}
        entry = doc.get(host) if isinstance(doc.get(host), dict) else {}
        change(entry)
        doc[host] = entry
        st.write(STATE, doc)
    return entry


def _record_error(root):
    try:
        os.makedirs(root, exist_ok=True)
        with open(os.path.join(root, "errors.log"), "a", encoding="utf-8") as fh:
            fh.write(f"--- {time.strftime('%Y-%m-%dT%H:%M:%S')} pid={os.getpid()} (backfill)\n")
            fh.write(traceback.format_exc())
    except Exception:
        pass


def _log(root, message, log=None):
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}"
    try:
        os.makedirs(root, exist_ok=True)
        with open(os.path.join(root, LOG), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    if log is not None:
        try:
            log(message)
        except Exception:
            pass


def _marker(root, host):
    return os.path.join(root, f".backfilling-{host}")


def _refresh_marker(root, host):
    """Keep this host's run marker fresh, creating it if a manual run never did. A run longer
    than `MARKER_S` must not be joined by a second one from another session start."""
    path = _marker(root, host)
    try:
        os.utime(path, None)
        return
    except OSError:
        pass
    try:
        os.makedirs(root, exist_ok=True)
        with open(path, "a", encoding="utf-8"):
            pass
        os.utime(path, None)
    except OSError:
        pass


def _host_name(host):
    return getattr(host, "name", str(host))


def settings(env=None):
    """The backfill's global settings: `config.json` at the state root, then the environment.
    A repository's own settings do not apply here: history is read before a repository context
    exists, and `backfill=off` is handled when the run reaches that repository."""
    env = os.environ if env is None else env
    cfg = load_config(state_root(env), env=env)
    return {"days": cfg.backfill_days,
            "max_per_repo": cfg.backfill_max_per_repo,
            "dirs": cfg.backfill_dirs,
            "headless": cfg.backfill_headless == "on"}


def _when_bool(value):
    if isinstance(value, str):
        return value.strip().lower() in ("on", "true", "yes", "1", "enable", "enabled")
    return bool(value)


def _folded_ids(host, env):
    """Every session id this host has already folded or recorded, in any repository's store."""
    host_name = _host_name(host)
    out = set()
    base = repos.repos_dir(state_root(env))
    try:
        keys = os.listdir(base)
    except OSError:
        return out
    for key in keys:
        st = Store(os.path.join(base, key, host_name))
        folded = st.read("folded.json") or {}
        if isinstance(folded, dict):
            out.update(folded)
        try:
            names = os.listdir(st.path("sessions"))
        except OSError:
            continue
        out.update(name[:-5] for name in names if name.endswith(".json"))
    return out


_WSL_RE = re.compile(r"^(\\\\wsl(?:\$|\.localhost)?\\[^\\/]+)", re.IGNORECASE)


def translate_cwd(cwd, source_dir):
    """Translate a POSIX path recorded in a transcript read from a WSL history folder into the
    path the Windows side can see. A history dir outside WSL, or a cwd already local, is
    returned unchanged."""
    if not isinstance(cwd, str) or not cwd.startswith("/"):
        return cwd
    src = str(source_dir or "").replace("/", "\\")
    match = _WSL_RE.match(src)
    if not match:
        return cwd
    prefix = match.group(1)
    mnt = re.match(r"^/mnt/([A-Za-z])(?:/(.*))?$", cwd)
    if mnt:
        letter = mnt.group(1).upper()
        rest = (mnt.group(2) or "").replace("/", "\\")
        return f"{letter}:\\{rest}" if rest else f"{letter}:\\"
    return prefix + "\\" + cwd.lstrip("/").replace("/", "\\")


def _is_headless(path, prompts):
    """An automated run: started through the SDK (`claude -p`, a daemon, a probe) and given one
    prompt. A person who talks to Claude Code through an SDK front end (a chat bot, say) gives it
    several, and that session is kept. In the recorded histories this was checked against, the
    two separated cleanly: nearly every automated SDK session ran exactly one prompt."""
    return prompts <= 1 and trace.claude_entrypoint(path).lower().startswith("sdk")


def plan(host, env, now, days=None, max_per_repo=None, exclude=(), on_progress=None,
         headless=None):
    """What a backfill would fold, grouped by repository; writes nothing.

    Each repository's entries are the finished sessions of the last `days` whose first directory
    still exists, whose log has a prompt and is not headless, too large, fresh, or already
    folded. The most recent `max_per_repo` are kept, oldest first. `on_progress` is called every
    `PLAN_PROGRESS_EVERY` history files while planning, so a long scan can keep the run marker
    fresh. `days`, `max_per_repo` and `headless` default to the global settings."""
    env = os.environ if env is None else env
    now = time.time() if now is None else now
    if days is None or max_per_repo is None or headless is None:
        opts = settings(env)
        if days is None:
            days = opts["days"]
        if max_per_repo is None:
            max_per_repo = opts["max_per_repo"]
        if headless is None:
            headless = opts["headless"]
    days = int(days)
    max_per_repo = int(max_per_repo)
    headless = _when_bool(headless)
    excluded = {str(x) for x in exclude or ()}
    folded_ids = _folded_ids(host, env)
    groups = {}
    seen = 0
    for entry in host.history(env):
        if len(entry) == 4:
            sid, path, mtime, source_dir = entry
        else:                           # a host from before history carried its source folder
            sid, path, mtime = entry
            source_dir = None
        sid = str(sid)
        mtime = float(mtime)
        seen += 1
        if on_progress is not None and seen % PLAN_PROGRESS_EVERY == 0:
            on_progress()
        reason = None
        if mtime > now - fold.STALE_S:
            reason = "fresh"
        elif mtime < now - days * 86400:
            reason = "too_old"
        elif sid in excluded:
            reason = "excluded"
        elif sid in folded_ids:
            reason = "already_folded"
        else:
            try:
                size = os.path.getsize(path)
            except OSError:
                size = None
            if size is None:
                reason = "unreadable"
            elif size > MAX_BYTES:
                reason = "too_large"
        cwd = repos.transcript_cwd(path)
        if cwd:
            cwd = translate_cwd(cwd, source_dir)
        repo = None
        if cwd and os.path.isdir(cwd):
            try:
                repo = repo_identity(cwd, env)
            except (OSError, ValueError):
                repo = None
        if reason is None and repo is None:
            reason = "missing_cwd"
        prompts = trace.claude_prompt_count(path) if reason is None else None
        if reason is None and prompts <= 0:
            reason = "no_prompts"
        if reason is None and not headless and _is_headless(path, prompts):
            reason = "headless"
        if repo is None:                 # nothing says which repository; no group to report it in
            continue
        group = groups.setdefault(repo.key, {
            "key": repo.key, "label": repo.label, "cwd": repo.root,
            "sessions": [], "skipped": {}})
        if reason:
            group["skipped"][reason] = group["skipped"].get(reason, 0) + 1
        else:
            group["sessions"].append((sid, path, mtime))
            group["cwd"] = repo.root
    out = []
    for group in groups.values():
        kept = sorted(group["sessions"], key=lambda s: (s[2], s[0]), reverse=True)[:max_per_repo]
        dropped = len(group["sessions"]) - len(kept)
        if dropped:
            group["skipped"]["max_per_repo"] = dropped
        group["sessions"] = sorted(kept, key=lambda s: (s[2], s[0]))
        out.append(group)
    return sorted(out, key=lambda g: g["key"])


def _ordered(groups, current_cwd, env):
    """The planned repositories, the one containing `current_cwd` first."""
    current = None
    if current_cwd and os.path.isdir(current_cwd):
        try:
            current = repo_identity(current_cwd, env).key
        except (OSError, ValueError):
            current = None
    return sorted(groups, key=lambda g: (g["key"] != current, g["key"]))


def _live_session(ctx, sid, path):
    """Did a session that was only history at plan time go live before its fold? A session
    record, or a transcript that moved inside the quiet window, belongs to the live path."""
    if os.path.exists(ctx.store.path("sessions", f"{sid}.json")):
        return True
    try:
        return os.path.getmtime(path) > time.time() - fold.STALE_S
    except OSError:
        return False


def run(host, env, *, now=None, days=None, max_per_repo=None, budget_s=BUDGET_S,
        exclude=(), current_cwd=None, log=None, headless=None):
    """Fold the planned history in, oldest first; writes `backfill.json` and `backfill.log`.

    The one repository containing `current_cwd` goes first. A fold here gathers evidence but
    makes no paid call and applies nothing (see `fold.fold_session(backfill=True)`), and each
    folded session's envelope is replayed from its transcript. A transcript that cannot be
    folded is logged and counted under its repository's `skipped: {"error": n}`, and the run
    goes on. `days`, `max_per_repo` and `headless` default to the global settings. Stops at
    `budget_s` with state `partial`; the next start resumes."""
    env = os.environ if env is None else env
    root = _root(env)
    host_name = _host_name(host)
    now = time.time() if now is None else now

    def started(entry):
        entry.setdefault("started", now)
        entry["state"] = "running"
        entry["finished"] = None
        entry.setdefault("repos", {})
        entry.setdefault("noticed", False)
    _update(root, host_name, started)
    try:
        groups = plan(host, env, now=now, days=days, max_per_repo=max_per_repo,
                      exclude=exclude, headless=headless,
                      on_progress=lambda: _refresh_marker(root, host_name))
        previous = _host_state(root, host_name).get("repos") or {}
        state = {k: dict(v) for k, v in previous.items() if isinstance(v, dict)}
        for group in groups:            # record every planned repository, folded or not
            entry = state.setdefault(group["key"],
                                     {"label": group["label"], "sessions": 0, "skipped": {}})
            entry["label"] = group["label"]
            entry["skipped"] = dict(group["skipped"])
        deadline = time.monotonic() + max(0.0, float(budget_s))
        partial = False
        budget_hit = False
        folded = 0
        for group in _ordered(groups, current_cwd, env):
            if not group["sessions"]:
                _log(root, f"{group['label']}: nothing new", log)
                continue
            try:
                ctx = LiveContext.build(host, group["cwd"], env)
            except Exception:
                _record_error(root)
                entry = state[group["key"]]
                entry["skipped"]["error"] = int(entry["skipped"].get("error") or 0) + 1
                _log(root, f"{group['label']}: could not build its context", log)
                continue
            if not ctx.config.enabled or ctx.config.backfill == "off":
                _log(root, f"{group['label']}: backfill is off here", log)
                continue
            entry = state[group["key"]]
            noted = False
            for sid, path, _mtime in group["sessions"]:
                if time.monotonic() >= deadline:
                    budget_hit = True
                    partial = True
                    break
                if _live_session(ctx, sid, path):
                    entry["skipped"]["fresh"] = int(entry["skipped"].get("fresh") or 0) + 1
                    _log(root, f"{group['label']}: skipped {sid}, it went live", log)
                    continue
                _refresh_marker(root, host_name)
                try:
                    record = fold.fold_session(ctx, sid, path, backfill=True)
                except Exception:
                    _record_error(root)
                    entry["skipped"]["error"] = int(entry["skipped"].get("error") or 0) + 1
                    _log(root, f"{group['label']}: could not fold {sid}", log)
                    continue
                if record is None:
                    # Already folded by a racing run is fine; a busy fold lock is not.
                    if sid not in (ctx.store.read("folded.json") or {}):
                        partial = True
                    continue
                if not noted:
                    repos.note_repo(ctx.root, ctx.repo)
                    noted = True
                try:
                    replay_envelope(ctx, path)
                except Exception:
                    _record_error(root)
                entry["sessions"] = int(entry.get("sessions") or 0) + 1
                folded += 1
                _log(root, f"{group['label']}: folded {sid}", log)
            if budget_hit:
                break
        final = "partial" if partial else "done"

        def finish(entry):
            entry.setdefault("started", now)
            entry["state"] = final
            entry["finished"] = time.time()
            entry["repos"] = state
            entry.setdefault("noticed", False)
        _update(root, host_name, finish)
        _log(root, f"{host_name}: {final}, {folded} session(s) folded", log)
        return {"state": final, "sessions": folded, "repos": state}
    finally:
        try:
            os.remove(_marker(root, host_name))
        except OSError:
            pass


def start(ctx, exclude=()):
    """Launch a backfill from SessionStart, cheaply. Never scans history itself.

    Returns at once when backfill is off, when this host's run is `done`, or when a run was
    started less than `MARKER_S` ago. Under `TE_FOLD_SYNC=1` it runs inline, for tests."""
    if not ctx.config.enabled or ctx.config.backfill == "off":
        return None
    if getattr(ctx.host.__class__, "history", None) is Host.history:
        return None                     # this agent keeps no history the plugin can read
    if _host_state(ctx.root, ctx.host.name).get("state") == "done":
        return None
    marker = _marker(ctx.root, ctx.host.name)
    try:
        if time.time() - os.path.getmtime(marker) < MARKER_S:
            return None
    except OSError:
        pass
    try:
        os.makedirs(ctx.root, exist_ok=True)
        with open(marker, "w", encoding="utf-8"):
            pass
    except OSError:
        return None
    if ctx.env.get("TE_FOLD_SYNC") == "1":
        run(ctx.host, ctx.env, exclude=exclude, current_cwd=ctx.repo.root)
        return True
    return ctx.spawn(["backfill", "--quiet"] +
                     [a for sid in exclude for a in ("--exclude", str(sid))], LOG)


def notice(root, host, command):
    """Once per host, after a run has finished: what history it learned from (else None)."""
    entry = _host_state(root, host)
    if entry.get("state") != "done" or entry.get("noticed"):
        return None
    repos = entry.get("repos") or {}
    if sum(int((r or {}).get("sessions") or 0) for r in repos.values()) <= 0:
        return None                     # nothing was learned from history: nothing to say
    with Store(root).locked("backfill", timeout=10.0) as got:
        if not got:
            return None
        doc = Store(root).read(STATE) or {}
        current = doc.get(host) if isinstance(doc.get(host), dict) else {}
        if current.get("state") != "done" or current.get("noticed"):
            return None
        current["noticed"] = True
        doc[host] = current
        Store(root).write(STATE, doc)
    repos = current.get("repos") or {}
    learned = [r for r in repos.values() if int((r or {}).get("sessions") or 0) > 0]
    sessions = sum(int((r or {}).get("sessions") or 0) for r in learned)
    count = len(learned)
    return (f"TokenEconomizer: learned from {sessions} earlier Claude Code session(s) in "
            f"{count} repositories (see `{command}`); `backfill=off` turns this off.")


# ---- seeding the envelope from a session's prompt-turns ------------------------------------
def _turn_lines(lines):
    """The transcript split at each prompt (`trace.claude._is_prompt`); earlier lines are
    file snapshots and other setup, not turns. A subagent's sidechain entries are not the main
    conversation and are left out."""
    turns, current = [], None
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            ev = None
        if isinstance(ev, dict) and _is_prompt(ev):
            current = [line]
            turns.append(current)
        elif current is not None:
            if isinstance(ev, dict) and ev.get("isSidechain"):
                continue
            current.append(line)
    return turns


def _text_of(ev):
    content = (ev.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text") or "" for b in content
                        if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _interrupted(lines):
    """Did the person interrupt this turn? Claude Code writes a user entry starting
    `[Request interrupted by user`."""
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict) and ev.get("type") == "user" and \
                _text_of(ev).startswith("[Request interrupted by user"):
            return True
    return False


def _ends_with_assistant_text(lines):
    """Does the log end with text the assistant wrote (rather than a tool result, or a
    tool_use with no answer)?"""
    for line in reversed(lines):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict) or ev.get("type") not in trace.claude.CONVERSATION:
            continue
        if ev.get("type") != "assistant":
            return False
        return bool(_text_of(ev).strip())
    return False


def replay_envelope(ctx, transcript_path):
    """Seed the loop guard's per-repository envelope from a finished transcript.

    Each prompt-turn is folded call by call into a fresh `TurnGuard` -- with no envelope and no
    judge, so the guard cannot abort -- and its outcome feeds `record_turn` like a live turn.
    A turn is stopped normally unless it was interrupted, or it is the transcript's last and the
    log ends without a final assistant text. A transcript over `REPLAY_MAX_BYTES` is left alone
    (it still folded). Returns the number of turns recorded."""
    if ctx.config.loopguard == "off":
        return 0
    try:
        if os.path.getsize(transcript_path) > REPLAY_MAX_BYTES:
            return 0
    except OSError:
        pass
    level, overrides = ctx.config.loopguard_aggr, ctx.config.loopguard_thresholds
    thr = loopguard.base_thresholds(level, overrides)
    lines = list(_lines(transcript_path))
    turns = _turn_lines(lines)
    recorded = 0
    for turn in turns:
        guard = loopguard.TurnGuard()
        for name, args, result, ok, _cwd in trace.claude_calls(turn):
            guard.fold(name, args, result,
                       None if ok is None else (0 if ok else 1),
                       envelope=None, thr=thr, judge=None)
        if not guard.cum_turns:
            continue
        stopped_normally = True
        if _interrupted(turn):
            stopped_normally = False
        elif turn is turns[-1] and not _ends_with_assistant_text(turn):
            stopped_normally = False
        loopguard.record_turn(ctx.store, guard, level=level,
                              stopped_normally=stopped_normally, overrides=overrides)
        recorded += 1
    return recorded
