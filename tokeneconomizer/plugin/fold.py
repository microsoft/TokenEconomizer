"""Folding a finished session into what the plugin knows about its repository.

It runs the mechanisms' own classes over the finished session: `PrefixStage` learns and renders
the micro-skills (the LLM-free facts, plus the paid proposer when allowed), and `ToolingStage`
accrues tool pruning's used-tool and unused-streak evidence.
A session is one "task" of the repository's stream for both, the unit they were built around.

Two adaptations, both forced by the setting:

  * The stage objects lived for a whole stream in one process; here each fold is a fresh
    process. `PrefixStage` already persists its distillers; `ToolingStage`'s streaks were
    in-memory only, so they are saved to `untool.json` and restored before each fold.
  * Folding runs OFF the agent's critical path. SessionEnd hooks are short (Codex caps them at
    three seconds), and parsing a long transcript -- or a paid proposer tick -- takes longer, so
    the hook spawns `te fold` detached and returns at once. A session whose end was never seen
    (a killed terminal) is folded by the next SessionStart once its transcript has gone quiet.

Each session is folded once, at its first end, keyed by session id; a resumed session is not
re-folded, because counting the same trajectory twice would hand the fact lifecycle confirmations
it did not earn.
"""
import os
import shutil
import tempfile
import time

from .. import trace
from . import accounting, denylist, sidecall
from . import adapter as live_adapter
from .adapter import LiveAdapter, load_catalog

STALE_S = 600           # an unfolded session whose transcript is quiet this long is over
# A fold never runs in a hook -- it is a detached process, or `te fold` -- so it can wait for
# another fold to finish, up to one paid side call, and must never proceed without the lock:
# two folds of one session would both find it unfolded and count it twice, in every mechanism's
# state and in the ledger. That happened whenever a session started while the previous one's
# paid fold was still in its side call (the next SessionStart re-folds whatever is not yet
# marked folded). A fold that still cannot take the lock skips: the holder finishes the
# session, or, if it died, the session stays pending and the next SessionStart folds it.
FOLD_LOCK_WAIT_S = sidecall.TIMEOUT_S + 60

# A deny list changes the tools a session sends, and the tools open the cached prompt prefix.
# A running session keeps the tools it started with, but a conversation resumed after the change
# sends the new ones, so its whole history is written to the prompt cache again instead of read
# from it (both verified on Claude Code 2.1.307, Codex 0.163.0 and Copilot 1.0.102). The fold
# runs just after a session ends, when that session's cache is warmest and resuming it is
# likeliest, so `auto` holds a new list as pending and writes it only once every session of the
# repository has been idle longer than a prompt cache lives (`toolprune_quiet_minutes`): then a
# resumed or new session finds the cache cold anyway. A list that has waited a day is written
# regardless, so a repository whose sessions never pause still gets it.
PENDING_MAX_S = 24 * 3600
WAITER_POLL_S = 600     # how often the waiter looks again, and so how fresh its heartbeat is


def _run_dir(workdir, trace_path):
    run = os.path.join(workdir, "run")
    os.makedirs(run, exist_ok=True)
    link = os.path.join(run, "transcript.jsonl")
    try:
        os.symlink(trace_path, link)
    except OSError:
        shutil.copyfile(trace_path, link)
    return run


def _fold_microskills(ctx, adapter, run_dir, session_id, injected=None, paid=True):
    profile = ctx.config.microskills
    if profile == "off":
        return None
    from tokeneconomizer.config import InjectionConfig
    from tokeneconomizer.microskills import PrefixStage
    icfg = InjectionConfig(enabled=True, profile=profile,
                           propose_every=ctx.config.microskills_propose_every,
                           max_facts=ctx.config.microskills_max_facts,
                           max_tokens=ctx.config.microskills_max_tokens)
    # Paused once the host's CLI said it is not signed in (`sidecall.paused`): the free layer
    # still learns. A backfill fold passes `paid=False`: history already written is no reason
    # to call a model now.
    runner = (sidecall.runner_for(ctx.host.name, ctx.config)
              if paid and icfg.paid and not sidecall.paused(ctx.host.name, ctx.root) else None)
    stage = PrefixStage(icfg, ctx.repo.label, ctx.mech_dir, False, runner=runner,
                        optimizer_model="agent", adapter=adapter, agent=ctx.host.name)
    srow = {"resolved": None}
    task = {"instance_id": session_id}
    if injected is not None:
        task["injected_ids"] = injected
    opt_tokens, reason = stage.after_task(task, {}, run_dir, srow)
    rendered = stage.render() or ""
    prior = ctx.store.read("injection.json") or {}
    ctx.store.write("injection.json", {
        "profile": profile, "reason": reason, "rendered": rendered,
        "rendered_ids": stage.injected_ids(),
        "facts": srow.get("manual_facts"), "updated": time.time(),
        "sessions": int(prior.get("sessions", 0)) + 1})
    return {"optimizer_tokens": float(opt_tokens or 0), "rendered_chars": len(rendered)}


def enough_evidence(sessions, prompts, cfg, high_risk=False):
    """Has a never-used tool gone unused long enough to be left out? Over `toolprune_evidence`
    prompts spread across at least `toolprune_min_sessions` sessions, or over
    `toolprune_sessions` sessions however short; a high-risk tool needs HIGH_RISK_FACTOR times
    the prompts and the sessions. Prompts let long sessions count for more; the floor keeps one
    session's prompts -- one piece of work -- from deciding alone (in recorded sessions that is
    what pruned tools a later session needed)."""
    k = live_adapter.HIGH_RISK_FACTOR if high_risk else 1
    return ((sessions >= cfg.toolprune_min_sessions and prompts >= cfg.toolprune_evidence * k)
            or sessions >= cfg.toolprune_sessions * k)


def _fold_toolprune(ctx, adapter, run_dir, session_id, transcript=None, workdir=None,
                    prompts=None, backfill=False):
    """Tool pruning's evidence: the sessions a tool went unused (`ToolingStage`'s streaks) and
    the prompts those sessions ran (`prompt_streaks`), weighed by `enough_evidence`. A list
    applies from a later session, since a running session keeps the tools it started with.
    `prompts` is None where the log does not say, and the session counts as one prompt. A
    `backfill` fold records evidence and the proposal but applies nothing: no deny list is
    held, written or restored, and no pending write is scheduled."""
    if not denylist.supports(ctx.host.name) or ctx.config.toolprune == "off":
        return None
    if ctx.host.name == "codex":
        return _fold_toolprune_codex(ctx, adapter, session_id, transcript, workdir, prompts,
                                     backfill=backfill)
    host = ctx.host.name
    from tokeneconomizer.config import ToolingConfig
    from tokeneconomizer.toolprune import ToolingStage
    st = ctx.store.read("untool.json") or {}
    # The stage proposes never-used tools unused for the session floor; `enough_evidence`
    # decides among them.
    stage = ToolingStage(ToolingConfig(evidence=max(1, ctx.config.toolprune_min_sessions)),
                         host, "agent",
                         adapter=adapter, repo=ctx.repo.label, results_root=ctx.mech_dir,
                         fresh=True)
    stage.used_tools = set(st.get("used_tools", []))
    stage.tool_unused_streaks = {k: int(v) for k, v in (st.get("streaks") or {}).items()}
    # A session from before prompts were counted counts as one prompt.
    prompt_streaks = {k: int(v) for k, v in
                      (st.get("prompt_streaks") or stage.tool_unused_streaks).items()}
    stage.catalog_tools = set(st.get("catalog_tools", []))
    stage.catalog_desc = dict(st.get("catalog_desc") or {})
    applied = denylist.applied_for(ctx.store, ctx.repo.root, ctx.host.name)
    kept = set(denylist.kept_for(ctx.store, ctx.repo.root, ctx.host.name))   # given back
    stage._exclude_tools = list(applied)        # the mask this session actually ran under
    # A tool that has since become protected (the web tools, from this version on) comes back:
    # remove the plugin's OWN deny rule for it. Only rules it recorded adding, never one the
    # person wrote, and only in the direction of giving a tool back.
    protected = adapter.protected_tools()
    restored = []
    if not backfill and any(t in protected for t in applied) and not ctx.config.shadow:
        try:
            restored = denylist.restore(ctx.repo.root, ctx.store,
                                      [t for t in applied if t in protected], host=host)
        except RuntimeError:            # an unreadable or redirected settings file: leave it
            restored = []
    if restored:
        applied = denylist.applied_for(ctx.store, ctx.repo.root, ctx.host.name)
    # The stage warns once per STREAM when no catalog parses -- a harness defect signal there.
    # Here a stage lives for one fold, and a session that called no tool legitimately has no
    # observed catalog; the source (or its absence) is recorded in untool.json instead.
    stage._warned_no_catalog = True
    srow = {}
    before = dict(stage.tool_unused_streaks)
    stage.after_task({"instance_id": session_id}, {"untooled_excluded": applied}, run_dir, srow)
    for t, n in stage.tool_unused_streaks.items():
        if n == 0:
            prompt_streaks[t] = 0                          # used
        elif n == before.get(t, 0) + 1:                    # unused this session
            prompt_streaks[t] = prompt_streaks.get(t, 0) + (1 if prompts is None else prompts)
    sessions = int(st.get("sessions", 0)) + 1
    raw = [t for t in stage.build_tools({"instance_id": f"after-{session_id}"}, sessions) or []
           if enough_evidence(stage.tool_unused_streaks.get(t, 0), prompt_streaks.get(t, 0),
                              ctx.config, high_risk=t in live_adapter.HIGH_RISK)]
    doc = {"used_tools": sorted(stage.used_tools), "streaks": stage.tool_unused_streaks,
           "prompt_streaks": prompt_streaks,
           "catalog_tools": sorted(stage.catalog_tools), "catalog_desc": stage.catalog_desc,
           # A session run under an applied mask parses no catalog (the stage skips it, as the
           # masked list is not the full one); the last source still describes the catalog.
           "catalog_source": srow.get("untool_catalog_source") or st.get("catalog_source"),
           "sessions": sessions, "prompts": int(st.get("prompts", 0)) + (prompts or 0),
           "updated": time.time()}
    if host == "copilot":
        # Copilot's only persistent lever is a whole MCP server (see `denylist`).
        servers = trace.copilot_mcp_servers(ctx.env)
        proposal = [s for s in denylist.copilot_server_proposal(
            raw, stage.catalog_tools, stage.used_tools, servers)
            if s not in applied and s not in kept]
        doc["servers"] = _server_summary(stage, servers, prompt_streaks)
    else:
        proposal = [t for t in raw if t not in applied and t not in kept]
    doc["proposal"] = proposal
    doc["new_in_proposal"] = sorted(set(proposal) - set(st.get("proposal", [])))
    if not backfill and ctx.config.toolprune == "auto" and proposal and not ctx.config.shadow:
        _hold(doc, st, proposal, ctx.repo.root)
    if restored:
        doc["restored_protected"] = restored
    ctx.store.write("untool.json", doc)
    return {"proposal": doc["proposal"], "pending": bool(doc.get("auto_pending")),
            "restored": restored}


def _server_summary(stage, servers, prompt_streaks):
    """{server: {tools, used, unused_sessions, unused_prompts}} -- the Copilot evidence at the
    unit it acts on: a server's least-evidenced tool."""
    out = {}
    for t in stage.catalog_tools:
        s = trace.mcp_server_of(t, servers)
        if not s:
            continue
        e = out.setdefault(s, {"tools": 0, "used": False, "unused_sessions": None,
                               "unused_prompts": None})
        e["tools"] += 1
        e["used"] = e["used"] or t in stage.used_tools
        for key, n in (("unused_sessions", stage.tool_unused_streaks.get(t, 0)),
                       ("unused_prompts", prompt_streaks.get(t, 0))):
            e[key] = int(n) if e[key] is None else min(e[key], int(n))
    return out


def _fold_toolprune_codex(ctx, adapter, session_id, transcript, workdir, prompts=None,
                          backfill=False):
    """Codex: the experiments' capability controller (`codex_untool.CodexUntoolProfile`, driven
    through `ToolingStage`) decides which packages went unused, over a trace that carries the
    model's own `exec` calls (`trace.codex_capability_trace`). Two live adaptations:

      * the session ran under the capabilities the PERSON applied, not the profile's own mask
        (a plugin launches nothing), so the observation is recorded against the applied set;
      * a package is proposed only once the profile has demoted it AND it was loaded, and
        unused, long enough by `enough_evidence` (x3 for image/media, the high-risk package),
        with no use ever -- the profile alone demotes collaboration after one unused task, which
        was right for a sealed benchmark stream and is too quick for a person's sessions. The
        profile counts sessions; the prompts are counted here (`loaded_prompts`)."""
    from tokeneconomizer.config import ToolingConfig
    from tokeneconomizer.toolprune import ToolingStage
    if not transcript or not workdir:
        return None
    cap_dir = os.path.join(workdir, "capabilities")
    os.makedirs(cap_dir, exist_ok=True)
    cap_trace = os.path.join(cap_dir, "trace.jsonl")
    if trace.codex_capability_trace(transcript, cap_trace) is None:
        return None
    run_dir = _run_dir(cap_dir, cap_trace)
    stage = ToolingStage(ToolingConfig(codex_untool=True), "codex", "agent", adapter=adapter,
                         repo=ctx.repo.label, results_root=ctx.mech_dir, fresh=False)
    stage._warned_no_catalog = True
    stage.build_tools({"instance_id": session_id}, 0)
    applied = denylist.applied_for(ctx.store, ctx.repo.root, ctx.host.name)
    kept = set(denylist.kept_for(ctx.store, ctx.repo.root, ctx.host.name))   # given back
    stage.codex_profile._pending["logical_capabilities"] = sorted(applied)
    loaded_before = dict(stage.codex_profile.state.get("loaded_exposures") or {})
    srow = {}
    stage.after_task({"instance_id": session_id}, {}, run_dir, srow)
    state = stage.codex_profile.state
    ever_used = set(state.get("ever_completed") or ())
    loaded = state.get("loaded_exposures") or {}
    prior = ctx.store.read("untool.json") or {}
    # A session from before prompts were counted counts as one prompt.
    loaded_prompts = dict(prior.get("loaded_prompts") or
                          {c: int(n) for c, n in loaded_before.items()})
    for c in denylist.CODEX_LIVE_CAPABILITIES:
        if int(loaded.get(c, 0)) > int(loaded_before.get(c, 0)):     # loaded this session
            loaded_prompts[c] = int(loaded_prompts.get(c, 0)) + (1 if prompts is None
                                                                 else prompts)
    proposal = [c for c in denylist.CODEX_LIVE_CAPABILITIES
                if c in (state.get("current_pruned") or ()) and c not in ever_used
                and c not in applied and c not in kept
                and enough_evidence(int(loaded.get(c, 0)), int(loaded_prompts.get(c, 0)),
                                    ctx.config, high_risk=c in CODEX_HIGH_RISK)]
    doc = {"sessions": int(state.get("tasks_completed") or 0), "catalog_source": "capabilities",
           "prompts": int(prior.get("prompts", 0)) + (prompts or 0),
           "capabilities": {c: {"loaded_sessions": int(loaded.get(c, 0)),
                                "loaded_prompts": int(loaded_prompts.get(c, 0)),
                                "unused_sessions": int((state.get("zero_attempt_exposures")
                                                        or {}).get(c, 0)),
                                "used": c in ever_used}
                            for c in denylist.CODEX_LIVE_CAPABILITIES},
           "loaded_prompts": loaded_prompts,
           "proposal": proposal, "updated": time.time(),
           "new_in_proposal": sorted(set(proposal) - set(prior.get("proposal", [])))}
    if not backfill and ctx.config.toolprune == "auto" and proposal and not ctx.config.shadow:
        _hold(doc, prior, proposal, ctx.repo.root)
    ctx.store.write("untool.json", doc)
    return {"proposal": doc["proposal"], "pending": bool(doc.get("auto_pending")),
            "restored": []}


def _hold(doc, prior, proposal, root):
    """`auto`: keep the proposal as pending for `apply_pending`, from when it first waited. A
    refusal to write the file (a committed or unparseable one) stands until a write succeeds."""
    pend = prior.get("auto_pending") or {}
    doc["auto_pending"] = {"items": list(proposal), "root": root,
                           "since": pend.get("since") or time.time()}
    if prior.get("auto_apply_refused"):
        doc["auto_apply_refused"] = prior["auto_apply_refused"]


def last_activity(ctx):
    """When a session of this repository on this agent last did anything: a hook call (its
    record's write) or a line in its own session log, whichever is later; 0 when none did."""
    latest = 0.0
    sess_dir = ctx.store.path("sessions")
    try:
        names = [n for n in os.listdir(sess_dir) if n.endswith(".json")]
    except OSError:
        return latest
    for name in names:
        try:
            latest = max(latest, os.path.getmtime(os.path.join(sess_dir, name)))
        except OSError:
            continue
        tp = (ctx.store.read(os.path.join("sessions", name)) or {}).get("transcript")
        try:
            latest = max(latest, os.path.getmtime(tp)) if tp else latest
        except OSError:
            pass
    return latest


def apply_pending(ctx, now=None):
    """Write the pending `auto` deny list once the repository has been quiet long enough (see
    PENDING_MAX_S). Returns the seconds to wait before looking again, or None when there is
    nothing left to wait for: written, refused, given back or applied by hand meanwhile, or
    `toolprune` is no longer `auto`."""
    now = now or time.time()
    with ctx.store.locked("mechanisms"):
        unt = ctx.store.read("untool.json") or {}
        pend = unt.get("auto_pending")
        if not pend:
            return None
        root = pend.get("root") or ctx.repo.root
        done = set(denylist.applied_for(ctx.store, root, ctx.host.name))
        done |= set(denylist.kept_for(ctx.store, root, ctx.host.name))
        # A tool protected since the list was held (`toolprune_protect`) is never written.
        protected = LiveAdapter(ctx.host, ctx.repo.root,
                                protect=ctx.config.toolprune_protect).protected_tools()
        items = [t for t in pend.get("items") or [] if t not in done and t not in protected]
        if items and ctx.config.toolprune == "auto" and not ctx.config.shadow:
            idle = now - last_activity(ctx)
            quiet_s = ctx.config.toolprune_quiet_minutes * 60
            waited = now - float(pend.get("since") or now)
            if idle < quiet_s and waited < PENDING_MAX_S:
                return min(quiet_s - idle, PENDING_MAX_S - waited)
            try:
                unt["auto_applied"] = denylist.apply(root, items, ctx.store, host=ctx.host.name)
                unt["proposal"] = [t for t in unt.get("proposal") or [] if t not in items]
                unt.pop("auto_apply_refused", None)
            except RuntimeError as e:      # an unreadable, committed or redirected file
                unt["auto_apply_refused"] = str(e)
        unt.pop("auto_pending", None)
        ctx.store.write("untool.json", unt)
        return None


def schedule_pending(ctx):
    """Look at the pending deny list now and, if it has to wait, leave a detached
    `te toolprune apply-pending` to write it later (only the look, when TE_FOLD_SYNC=1)."""
    wait = apply_pending(ctx)
    if wait is not None and ctx.env.get("TE_FOLD_SYNC") != "1":
        ctx.spawn(["toolprune", "apply-pending"], "fold.log")
    return wait


# Rarely used, but expensive to lose when a session needs it -- the Codex counterpart of
# adapter.HIGH_RISK (NotebookEdit on Claude Code).
CODEX_HIGH_RISK = frozenset({"image_media"})


def fold_session(ctx, session_id, transcript_path=None, backfill=False):
    """Fold one finished session. Returns the ledger record, or None if already folded.

    `backfill` folds a session from before the plugin was installed: the free layers learn and
    gather evidence, but no paid model call is made and nothing is applied or scheduled. The
    record and the `folded.json` entry say `backfill: true`, and a transcript's own first and
    last timestamps stand in for a live session record's `started` and `ended`."""
    sess_name = os.path.join("sessions", f"{session_id}.json")
    with ctx.store.locked("fold", timeout=FOLD_LOCK_WAIT_S) as got:
        if not got:
            return None
        folded = ctx.store.read("folded.json") or {}
        if session_id in folded:
            return None
        sess = ctx.store.read(sess_name) or {}
        transcript = transcript_path or sess.get("transcript")
        record = {"kind": "session", "session_id": session_id, "host": ctx.host.name,
                  "repo": ctx.repo.label, "folded_at": time.time(),
                  "started": sess.get("started"), "ended": sess.get("ended"),
                  "shadow": ctx.config.shadow}
        if backfill:
            record["backfill"] = True
            if transcript and (record["started"] is None or record["ended"] is None):
                first, last = trace.claude_session_span(transcript)
                record["started"] = record["started"] if record["started"] is not None else first
                record["ended"] = record["ended"] if record["ended"] is not None else last
        workdir = tempfile.mkdtemp(prefix="te-fold-")
        try:
            trace_path = ctx.host.mechanism_trace(transcript, workdir) if transcript else None
            try:
                prompts = ctx.host.prompt_count(transcript) if transcript else None
            except (OSError, ValueError):
                prompts = None
            record["prompts"] = prompts
            if not trace_path:
                record["error"] = "transcript unreadable"
            else:
                run_dir = _run_dir(workdir, trace_path)
                adapter = LiveAdapter(ctx.host, ctx.repo.root, vocabulary=ctx.config.vocabulary,
                                      catalog=load_catalog(ctx.root, ctx.host.name),
                                      protect=ctx.config.toolprune_protect)
                with ctx.store.locked("mechanisms"):
                    inj = _fold_microskills(ctx, adapter, run_dir, session_id,
                                            injected=sess.get("injected_ids"), paid=not backfill)
                    # A session that ran no prompt is no evidence about the tools.
                    unt = None if prompts == 0 else _fold_toolprune(
                        ctx, adapter, run_dir, session_id, transcript=transcript,
                        workdir=workdir, prompts=prompts, backfill=backfill)
                record["injection"] = inj
                record["untool"] = unt
                if unt and unt.get("pending") and not backfill:
                    schedule_pending(ctx)
                if not backfill:
                    ctx.host.settle(transcript)
                usage = ctx.host.session_usage(transcript)
                eff, priced_by = accounting.effective(usage)
                record.update({k: usage.get(k) for k in (
                    "model", "requests", "input_tokens", "cache_read_input_tokens",
                    "cache_creation_input_tokens", "output_tokens")})
                record["effective_tokens"], record["priced_by"] = eff, priced_by
            turns = sess.get("turns") or []
            record.update({
                "turns": len(turns),
                "nudges": int(sess.get("nudges", 0)), "aborts": int(sess.get("aborts", 0)),
                "injected_chars": int(sess.get("injected_chars", 0)),
                "untool_applied": denylist.applied_for(ctx.store, ctx.repo.root, ctx.host.name),
                "overhead": {
                    "optimizer_tokens": ((record.get("injection") or {}).get("optimizer_tokens")
                                         or 0),
                    "spiral_judge_tokens": float(sess.get("judge_tokens", 0) or 0),
                    "spiral_nudge_writer_tokens": float(sess.get("writer_tokens", 0) or 0)}})
            ctx.store.append("ledger.jsonl", record)
            entry = {"at": time.time(), "transcript": transcript}
            if backfill:
                entry["backfill"] = True
            folded[session_id] = entry
            ctx.store.write("folded.json", folded)
            if sess:
                sess["folded"] = True
                ctx.store.write(sess_name, sess)
            return record
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


def pending_sessions(ctx, exclude=(), stale_s=STALE_S, now=None):
    """Recorded sessions that ended (or went quiet) without being folded."""
    now = now or time.time()
    folded = ctx.store.read("folded.json") or {}
    out = []
    sess_dir = ctx.store.path("sessions")
    if not os.path.isdir(sess_dir):
        return out
    for name in sorted(os.listdir(sess_dir)):
        if not name.endswith(".json"):
            continue
        sid = name[:-5]
        if sid in folded or sid in exclude:
            continue
        sess = ctx.store.read(os.path.join("sessions", name)) or {}
        tp = sess.get("transcript") or ""
        try:
            quiet = now - os.path.getmtime(tp) >= stale_s
        except OSError:
            quiet = False
        if sess.get("ended") or quiet:
            out.append((float(sess.get("started") or 0), sid))
    # Oldest first: the sessions a migration hands over are folded in the order they ran.
    return [sid for _, sid in sorted(out)]


def spawn_fold(ctx, session_ids):
    """Fold sessions in a detached process (inline when TE_FOLD_SYNC=1, for tests)."""
    if not session_ids:
        return None
    if ctx.env.get("TE_FOLD_SYNC") == "1":
        return [fold_session(ctx, sid) for sid in session_ids]
    return ctx.spawn(["fold"] + [a for sid in session_ids for a in ("--session", sid)],
                     "fold.log")
