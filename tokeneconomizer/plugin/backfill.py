"""Learning a repository's history from the session logs that already exist at install time.

Every mechanism learns from the sessions the plugin's hooks watch, so a repository worked in
for months starts empty at install: tool pruning has no evidence, the facts shape only from the
first folded sessions, and the loop guard has no envelope. But the agents keep their own
transcripts, and those carry what the folds need -- the tool calls and their results, the
prompts, the usage. So the first session of a repository here starts a background read of the
agent's own log directory (`te backfill`), most recent session first:

  * Claude Code -- `$CLAUDE_CONFIG_DIR/projects/*/*.jsonl` (one folder per project path, the
    folder name a lossy encoding of it, so a session's repository is decided by the `cwd` its
    own log records, never by the folder name);
  * Codex -- `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl`;
  * Copilot -- `$COPILOT_HOME/session-state/<id>/events.jsonl`.

OpenCode's and Pi's stores are not read (their layouts are not yet pinned by a test here);
their folds start from the plugin's first session, as on every host before this.

Only logs whose first recorded directory belongs to this repository are taken, and only logs
quiet for `BACKFILL_MIN_AGE_S`: the session that started the backfill is still being written. A
session already in `folded.json` is skipped, and every session read is then marked folded, so
the fold its own SessionEnd hands off does not learn from it twice.

The learning is the folds' own code -- `fold._fold_microskills`, `fold._fold_toolprune` -- plus
`loopguard.record_turn` for each of a session's prompt-turns, so the envelope learns from a
history the same way it learns from watched turns. Backfilled sessions never wake the paid
proposer or judge (`microskills` keeps its free layer, the distiller folds without a model), so
a long history costs no model calls. Most recent first, capped at `backfill_sessions`, keeps
what the next sessions will actually meet.

`<store>/backfill.json` records that it ran; the run is detached (`te backfill`) and takes the
fold lock, so it never overlaps a fold. It fails open: an unreadable log is skipped and a
failing mechanism recorded, never raised.
"""
import calendar
import glob
import json
import os
import re
import shutil
import tempfile
import time

from .. import loopguard, trace
from . import accounting
from .paths import repo_identity

# A log's last write this recent belongs to a session still running: leave it to that session's
# own fold (`te backfill --force` reads it anyway).
BACKFILL_MIN_AGE_S = 300
LOCK_WAIT_S = 60
STATE = "backfill.json"

_CWD_RE = re.compile(r'"cwd"\s*:\s*("(?:[^"\\]|\\.)*")')


def _home(env, var, name):
    """The agent's config root, or "" when the environment names no home.

    The home comes from the environment the hooks ran in, never from `expanduser`, so a hook
    given an environment without one (a test's, a sandbox's) reads no logs at all."""
    env = os.environ if env is None else env
    home = env.get(var) or env.get("HOME") or env.get("USERPROFILE") or ""
    return os.path.join(home, name) if home else ""


def claude_home(env=None):
    return _home(env, "CLAUDE_CONFIG_DIR", ".claude")


def codex_home(env=None):
    return _home(env, "CODEX_HOME", ".codex")


def copilot_home(env=None):
    return _home(env, "COPILOT_HOME", ".copilot")


def transcript_cwd(path):
    """The first `cwd` a session log records (every agent's log writes one early), or None."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            head = fh.read(262144)
    except OSError:
        return None
    m = _CWD_RE.search(head)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except ValueError:
        return None


def _inside_repo(cwd, root):
    """Is `cwd` the repository root or under it? (Real paths, so a symlinked home still works.)"""
    if not cwd or not root:
        return False
    try:
        p, r = os.path.realpath(cwd), os.path.realpath(root)
    except OSError:
        return False
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def logs_for(host, env=None):
    """[(path, session_id)] for every log of `host` on this machine."""
    if host == "claude":
        home = claude_home(env)
        paths = ([p for p in glob.glob(os.path.join(home, "projects", "*", "*.jsonl"))
                  if os.path.basename(os.path.dirname(p)) != "subagents"] if home else [])
        return [(p, os.path.splitext(os.path.basename(p))[0]) for p in paths]
    if host == "codex":
        home = codex_home(env)
        paths = (glob.glob(os.path.join(home, "sessions", "*", "*", "*", "rollout-*.jsonl"))
                 if home else [])
        return [(p, os.path.splitext(os.path.basename(p))[0]) for p in paths]
    if host == "copilot":
        home = copilot_home(env)
        paths = (glob.glob(os.path.join(home, "session-state", "*", "events.jsonl"))
                 if home else [])
        return [(p, os.path.basename(os.path.dirname(p))) for p in paths]
    return []


def _same_repo(cwd, repo, env, cache):
    """Did a session that started in `cwd` run in `repo`? The key, as a live session's pin
    decides it -- a subdirectory of the repository belongs to it, and a nested checkout
    belongs to itself."""
    if not _inside_repo(cwd, repo.root):
        return False
    if cwd not in cache:
        try:
            cache[cwd] = repo_identity(cwd, env).key
        except OSError:
            cache[cwd] = None
    return cache[cwd] == repo.key


def sessions_for(repo, host, env=None, min_age_s=BACKFILL_MIN_AGE_S):
    """[(path, session_id)] for the logs of `host` that ran in `repo`, newest first.

    A log counts when the first directory it records belongs to this repository, and its last
    write is at least `min_age_s` old (one still being written is the caller's own session, or
    a session whose fold will come)."""
    now, cache, out = time.time(), {}, []
    for path, sid in logs_for(host, env):
        mtime = _mtime(path)
        if mtime is None or now - mtime < min_age_s:
            continue
        if _same_repo(transcript_cwd(path), repo, env, cache):
            out.append((mtime, path, sid))
    out.sort(key=lambda t: t[0], reverse=True)
    return [(p, sid) for _m, p, sid in out]


def codex_sessions_for(repo, env=None, min_age_s=BACKFILL_MIN_AGE_S):
    """[(path, session_id)] for the Codex rollouts that ran in `repo`, newest first."""
    now, cache, out = time.time(), {}, []
    for path, sid in logs_for("codex", env):
        mtime = _mtime(path)
        if mtime is None or now - mtime < min_age_s:
            continue
        try:
            meta = trace.codex_session_meta(path)
        except (OSError, ValueError):
            continue
        if _same_repo((meta or {}).get("cwd"), repo, env, cache):
            out.append((mtime, path, sid))
    out.sort(key=lambda t: t[0], reverse=True)
    return [(p, sid) for _m, p, sid in out]


# ---- folding one session ---------------------------------------------------------------------
def _trace(ctx, path, workdir):
    """The log a fold reads, converted as a live fold converts it (`host.mechanism_trace`)."""
    return ctx.host.mechanism_trace(path, workdir)


def _started(path):
    """When the session began: its first entry's timestamp, else its log's mtime."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"timestamp"' not in line:
                    continue
                try:
                    ts = json.loads(line).get("timestamp")
                except ValueError:
                    continue
                if isinstance(ts, str):
                    try:      # the log's stamps are UTC (`...Z`), as every agent writes them
                        return calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
                    except ValueError:
                        continue
    except OSError:
        pass
    return _mtime(path)


def _fold_turns(ctx, trace_path):
    """Feed the envelope each prompt-turn of a saved log that ended green, as the live guard
    does one at a time. Returns {"turns", "recorded"}."""
    turns = _prompt_turn_calls(trace_path)
    recorded = 0
    for calls in turns:
        g = loopguard.TurnGuard()
        for name, args, res, ok in calls:
            g.fold(name, args, res, None if ok is None else (0 if ok else 1))
        if g.cum_turns and g.resolved_proxy(True):
            loopguard.record_turn(ctx.store, g, level=ctx.config.loopguard_aggr,
                                  overrides=ctx.config.loopguard_thresholds)
            recorded += 1
    return {"turns": len(turns), "recorded": recorded}


def _prompt_turn_calls(path):
    """[[(name, args, result, ok)]] -- one list per prompt-turn of a Claude-format log.

    Claude Code stamps a `promptId` on each entry (2.1.278+), so turns are exact where it is
    present. An entry without one (an older log, or one the agent does not stamp) starts a new
    turn when it is a `user` text entry, as `trace.claude_prompt_count` counts prompts."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = [ln for ln in fh if ln.strip()]
    except OSError:
        return []
    groups, by_prompt, current = [], {}, None
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict) or ev.get("type", "assistant") not in ("user", "assistant"):
            continue
        msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
        pid = ev.get("promptId") or msg.get("promptId")
        if isinstance(pid, str) and pid:
            current = pid
            by_prompt.setdefault(pid, []).append(line)
        elif _is_text_prompt(ev) or current is None:
            # No stamp: the person's own text starts the turn (the first entry always does).
            current = object()
            by_prompt[current] = [line]
        else:
            by_prompt[current].append(line)
    groups = list(by_prompt.values())
    out = []
    for group in groups:
        calls = trace.claude_calls(group)
        if calls:
            out.append([c[:4] for c in calls])
    return out


def _is_text_prompt(ev):
    if ev.get("type") != "user" or ev.get("isMeta") or ev.get("isSidechain"):
        return False
    content = (ev.get("message") or {}).get("content")
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return False
        content = " ".join(b.get("text") or "" for b in content
                           if isinstance(b, dict) and b.get("type") == "text")
    return isinstance(content, str) and bool(content.strip()) and not content.startswith(
        ("<local-command", "[Request interrupted"))


def _fold_one(ctx, adapter, session_id, path):
    """Run the folds' mechanisms over one finished session; its ledger record."""
    from . import fold as fold_mod

    prompts = None
    try:
        prompts = ctx.host.prompt_count(path)
    except (OSError, ValueError):
        pass
    record = {"kind": "session", "backfill": True, "session_id": session_id,
              "host": ctx.host.name, "repo": ctx.repo.label, "folded_at": time.time(),
              "started": _started(path), "ended": _mtime(path), "prompts": prompts}
    workdir = tempfile.mkdtemp(prefix="te-backfill-")
    try:
        trace_path = _trace(ctx, path, workdir)
        if not trace_path:
            record["error"] = "log unreadable"
            return record
        run_dir = fold_mod._run_dir(workdir, trace_path)
        record["injection"] = fold_mod._fold_microskills(
            ctx, adapter, run_dir, session_id, allow_sidecalls=False)
        if prompts != 0:
            record["untool"] = fold_mod._fold_toolprune(
                ctx, adapter, run_dir, session_id, transcript=path, workdir=workdir,
                prompts=prompts)
        # The envelope is learned from the person's prompt-turns: Claude Code's `promptId`
        # marks them exactly, and a log without it splits on the text entries
        # `trace.claude_prompt_count` counts. A converted Codex rollout is an exec stream,
        # whose boundaries are not the person's turns.
        if ctx.config.loopguard != "off" and ctx.host.name == "claude":
            record["loopguard"] = _fold_turns(ctx, path)
        usage = ctx.host.session_usage(path)
        eff, priced_by = accounting.effective(usage)
        record.update({k: usage.get(k) for k in (
            "model", "requests", "input_tokens", "cache_read_input_tokens",
            "cache_creation_input_tokens", "output_tokens")})
        record["effective_tokens"], record["priced_by"] = eff, priced_by
        record["turns"] = prompts
        return record
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ---- the run ---------------------------------------------------------------------------------
def should_run(ctx):
    """Has this repository's state for this agent never been backfilled? (One read.)"""
    return ctx.config.backfill != "off" and not (ctx.store.read(STATE) or {}).get("done")


def run(ctx, limit=None, force=False):
    """Backfill this repository from the agent's own logs; the state written to `backfill.json`.

    `limit` caps the sessions folded, newest first (None: `backfill_sessions` from the config).
    `force` reads logs still being written. A `backfill_sessions` of 0 folds the whole history."""
    from . import fold as fold_mod
    cfg = ctx.config
    if cfg.backfill == "off":
        return {"done": True, "skipped": "backfill=off"}
    if ctx.host.name not in ("claude", "codex", "copilot"):
        # Their stores are not pinned by a reader here; those agents learn from this session on.
        state = {"done": True, "skipped": f"no log reader for {ctx.host.name}",
                 "repo": ctx.repo.label, "host": ctx.host.name}
        ctx.store.write(STATE, state)
        return state
    min_age = 0 if force else BACKFILL_MIN_AGE_S
    # A Codex rollout's repository is in its own `session_meta`, not in a `cwd` line.
    found = (codex_sessions_for(ctx.repo, ctx.env, min_age_s=min_age)
             if ctx.host.name == "codex"
             else sessions_for(ctx.repo, ctx.host.name, ctx.env, min_age_s=min_age))
    folded = ctx.store.read("folded.json") or {}
    found = [(p, sid) for p, sid in found if sid not in folded]
    cap = cfg.backfill_sessions if limit is None else limit
    if cap and cap > 0:
        found = found[:cap]
    from .adapter import LiveAdapter, load_catalog
    adapter = LiveAdapter(ctx.host, ctx.repo.root, vocabulary=cfg.vocabulary,
                          catalog=load_catalog(ctx.root, ctx.host.name),
                          protect=cfg.toolprune_protect)
    started = time.time()
    recs, errors = [], []
    for path, sid in found:
        # The locks are per session, not per run: a live session's own fold (which waits on
        # the fold lock) is never held off for the length of a long history.
        with ctx.store.locked("fold", timeout=LOCK_WAIT_S) as got:
            if not got:
                errors.append({"session_id": sid, "error": "fold in progress; skipped"})
                continue
            with ctx.store.locked("mechanisms"):
                folded = ctx.store.read("folded.json") or {}
                if sid in folded:          # folded while this run was queued
                    continue
                try:
                    rec = _fold_one(ctx, adapter, sid, path)
                except Exception as e:     # one bad log is no reason to lose the rest
                    errors.append({"session_id": sid, "error": f"{type(e).__name__}: {e}"})
                    continue
                recs.append(rec)
                ctx.store.append("ledger.jsonl", rec)
                # Marked folded, so the session's own SessionEnd (or the next start's sweep)
                # does not learn from it twice.
                folded[sid] = {"at": time.time(), "transcript": path, "backfill": True}
                ctx.store.write("folded.json", folded)
    state = {"done": True, "at": time.time(), "secs": round(time.time() - started, 1),
             "sessions": len(recs), "found": len(found), "errors": errors[-20:],
             "limit": cap, "repo": ctx.repo.label, "host": ctx.host.name}
    ctx.store.write(STATE, state)
    if recs and cfg.toolprune == "auto" and not cfg.shadow:
        fold_mod.schedule_pending(ctx)
    return state


def spawn(ctx):
    """Start the backfill detached (inline under `TE_FOLD_SYNC=1`, for tests)."""
    if ctx.env.get("TE_FOLD_SYNC") == "1":
        return run(ctx)
    return ctx.spawn(["backfill"], "backfill.log")
