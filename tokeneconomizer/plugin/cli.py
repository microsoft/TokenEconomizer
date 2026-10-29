"""`te` -- inspect and steer what TokenEconomizer does in your sessions.

    te status                         what it has learned for this repository, and what it did
    te microskills                    the facts it injects, and the ones still forming
    te toolprune show                 the proposed deny list, and the evidence behind it
    te toolprune apply [ITEM ...]     write the proposal (or the named items) to the repository's
                                      settings: tools (Claude Code), capabilities (Codex) or
                                      MCP servers (Copilot)
    te toolprune restore [ITEM ...]   remove entries this plugin added
    te toolprune refresh-catalog      record the tool list a fresh session loads (Claude, Copilot)
    te toolprune measure              measure the request with and without the proposal
    te toolprune apply-pending        write a pending `auto` deny list once the repository has
                                      been idle long enough (the fold starts it, detached)
    te config show                    each mechanism on or off, its settings, the guard's thresholds
    te config set KEY=VALUE ...       e.g. `loopguard=off`, `microskills=on`,
                                      `loopguard.min_turns=12` (this repository; `--global` for
                                      every repository)
    te config unset KEY ...           back to the next source down (the global file, then default)
    te repomap [show | refresh]       the repository map (`repomap=on`): what is cached, or
                                      make it now
    te repos                          every repository with state here: how it was identified,
                                      its agents and sessions, the checkouts seen
    te repos migrate                  send state recorded under earlier keys where it belongs
                                      now (runs by itself, once, after an upgrade)
    te repos merge KEY [INTO]         merge the state directory KEY into INTO (default: this
                                      repository's), for state whose checkouts are gone
    te doctor                         check the installation
    te --host copilot probe           real Copilot sessions in a throwaway repository: does a
                                      learned fact change what the model does, and what does the
                                      deny list save (makes model calls; see plugin/probe.py)

`te facts` and `te untool` are the earlier names of `microskills` and `toolprune`, and still work.
`--cwd` picks the repository (default: the current directory) and `--host` the agent
(claude | codex | copilot | pi; default: `TE_HOST`, else the agent whose shell runs `te`, else
claude).
"""
import argparse
import glob
import json
import os
import sys
import time

from .. import loopguard
from . import accounting, backfill, denylist, fold, hosts, paths, repomap, repos, sidecall, toolcheck
from .config import canonical, unset_config, write_config
from .context import LiveContext
from .paths import state_root


def _ctx(args, env=None, session_id=None):
    host = hosts.HOSTS[args.host]()
    return LiveContext.build(host, args.cwd, env, session_id=session_id)


def _unwritable(path):
    """Why the plugin cannot write its state at `path`, or None when it can. A command that
    changes something checks first: a sandboxed agent (Codex's default sandbox makes everything
    outside the workspace read-only) must not get half a change -- a deny list written to the
    repository's settings, and no record that the plugin added it, which `restore` needs."""
    probe = os.path.join(path, f".write-test-{os.getpid()}")
    try:
        os.makedirs(path, exist_ok=True)
        open(probe, "w", encoding="utf-8").close()
        os.remove(probe)
        return None
    except OSError as e:
        return e.strerror or str(e)


def _refuse_unwritable(path):
    """Print why a change cannot be made here, and return the exit code; 0 when it can."""
    why = _unwritable(path)
    if why is None:
        return 0
    print(f"not changed: TokenEconomizer cannot write its state in {path} ({why}). Inside a "
          f"sandboxed agent, run this command with permission to write outside the workspace "
          f"(Codex asks you to approve it), or run it from a terminal as "
          f"`~/.tokeneconomizer/bin/te {' '.join(sys.argv[1:]) or '...'}`.")
    return 1


def _session_ctx(args, session_id):
    """The context of a recorded session: the repository it is pinned to, when that repository
    holds its record (a fold handed off at SessionEnd), else the one `--cwd` names. Only a
    session's own hooks pin it; this never does."""
    ctx = _ctx(args)
    if session_id and repos.pinned(ctx.root, ctx.host.name, session_id) is not None:
        pinned = _ctx(args, session_id=session_id)
        if os.path.exists(pinned.store.path("sessions", f"{session_id}.json")):
            return pinned
    return ctx


def _session_note(ctx, env=None):
    """A line when this command runs inside an agent session recorded under another repository
    (the one it started in), else None."""
    env = os.environ if env is None else env
    sid = ctx.host.env_session_id(env)
    pin = repos.pinned(ctx.root, ctx.host.name, sid) if sid else None
    if pin is None or pin.key == ctx.repo.key:
        return None
    return (f"this {ctx.host.name} session started in {pin.root} and is recorded under "
            f"{pin.label} (`te --cwd {pin.root} status`)")


# ---- status / microskills ------------------------------------------------------------------
def cmd_status(args):
    ctx = _ctx(args)
    cfg = ctx.config
    out = {"repo": ctx.repo.label, "repo_root": ctx.repo.root, "repo_key": ctx.repo.key,
           "identified_by": ctx.repo.how, "host": ctx.host.name,
           "state": ctx.store.base, "config": cfg.as_dict(), "warnings": cfg.warnings,
           "session_note": _session_note(ctx)}
    inj = ctx.store.read("injection.json") or {}
    unt = ctx.store.read("untool.json") or {}
    env_tracker = loopguard.load_envelope(ctx.store, cfg.loopguard_aggr,
                                          cfg.loopguard_thresholds)
    ledger = accounting.summarize(ctx.store.read_lines("ledger.jsonl"))
    out["backfill"] = backfill.summary(ctx)
    out.update({
        "microskills": {"profile": cfg.microskills,
                        "injected_lines": sum(1 for ln in (inj.get("rendered") or "").splitlines()
                                              if ln.startswith("- ")),
                        "sessions_folded": inj.get("sessions", 0)},
        "toolprune": {"mode": cfg.toolprune, "sessions": unt.get("sessions", 0),
                      "prompts": unt.get("prompts", 0),
                      "catalog_source": unt.get("catalog_source"),
                      "proposal": unt.get("proposal", []),
                      "applied": denylist.applied_for(ctx.store, ctx.repo.root,
                                                      ctx.host.name)},
        "loopguard": {"mode": cfg.loopguard, "envelope": env_tracker.envelope,
                      "ended_green_turns": len(env_tracker.resolved_turns)},
        "ledger": ledger})
    if args.json:
        print(json.dumps(out, indent=2, default=str))
        return 0
    print(f"TokenEconomizer -- {out['repo']} ({ctx.host.name})")
    print(f"  repository   {ctx.repo.root}, {repos.describe(ctx.repo)}")
    print(f"  state        {out['state']}")
    print(f"  config       microskills={cfg.microskills} toolprune={cfg.toolprune} "
          f"loopguard={cfg.loopguard} repomap={cfg.repomap} mode={cfg.mode} "
          f"shadow={cfg.shadow}")
    if cfg.repomap != "off":
        rm = repomap.read_state(ctx)
        why = rm.get("error") or "made after the first session"
        print("  repomap      " + (f"{rm.get('chars', 0) // 4} tokens cached for "
                                    f"{(rm.get('commit') or '?')[:10]}" if rm.get("map") else
                                    f"none yet ({why})"))
    for w in cfg.warnings:
        print(f"  warning      {w}")
    if out["session_note"]:
        print(f"  note         {out['session_note']}")
    print(f"  sessions     {ledger['sessions']} folded, "
          f"{accounting.fmt_tokens(ledger['effective_tokens'])} effective tokens, plugin "
          f"overhead {accounting.fmt_tokens(ledger['overhead_tokens'])} "
          f"({ledger['overhead_frac']:.1%})")
    if out["backfill"]:
        print(f"  history      {out['backfill']['folded']} earlier session(s) learned from "
              f"(of {out['backfill']['found']} found; `te backfill` reads again)")
    i = out["microskills"]
    print(f"  microskills  {i['injected_lines']} fact(s) injected at session start "
          f"({i['sessions_folded']} sessions learned from)")
    u = out["toolprune"]
    if not denylist.supports(ctx.host.name):
        print("  toolprune    not available on this host")
    else:
        src = u["catalog_source"] or "none yet"
        print(f"  toolprune    {u['sessions']} sessions ({u['prompts']} prompts) observed, "
              f"catalog: {src}; "
              f"proposed {denylist.unit(ctx.host.name, plural=True)}: "
              f"{', '.join(u['proposal']) or '-'}; applied: {', '.join(u['applied']) or '-'}")
    sp = out["loopguard"]
    env_s = f"{sp['envelope']:.0f} tool calls" if sp["envelope"] else "not learned yet"
    print(f"  loopguard    envelope {env_s} ({sp['ended_green_turns']} ended-green turns); "
          f"{ledger['nudges']} redirect(s), {ledger['aborts']} still-stuck notice(s) over "
          f"{ledger['turns']} turns")
    return 0


def cmd_microskills(args):
    ctx = _ctx(args)
    inj = ctx.store.read("injection.json") or {}
    if ctx.config.microskills == "off":
        print(f"micro-skills is off here (the default). `config set microskills=on` turns it on: "
              f"facts are then learned from finished sessions, and a model proposes more every "
              f"{ctx.config.microskills_propose_every} sessions, through this agent")
        if not inj:
            return 0
    text = inj.get("rendered") or ""
    print(text.rstrip() or "(nothing injected yet: no fact has cleared its firing gate)")
    facts = inj.get("facts") or {}
    rows = [(k, v) for k, v in facts.items() if isinstance(v, dict) and "state" in v]
    if rows:
        print("\nfacts tracked:")
        for k, v in sorted(rows):
            print(f"  {v.get('state', '?'):9s} fire={v.get('fire', '?'):>5s} "
                  f"{'INJECTED' if v.get('inject') else '        '} {k}")
    return 0


# ---- toolprune -----------------------------------------------------------------------------
def _toolprune_show(ctx, unt):
    host = ctx.host.name
    print(f"sessions observed: {unt.get('sessions', 0)} ({unt.get('prompts', 0)} prompts)  "
          f"catalog: {unt.get('catalog_source') or 'none yet'}")
    print(f"applied ({denylist.unit(host, plural=True)} this plugin added to "
          f"{denylist.settings_path(ctx.repo.root, host)}): "
          f"{', '.join(denylist.applied_for(ctx.store, ctx.repo.root, host)) or '-'}")
    print(f"proposed: {', '.join(_proposal(ctx, unt)) or '-'}")
    pending = [t for t in (unt.get("auto_pending") or {}).get("items") or []
               if t in _proposal(ctx, unt)]
    if pending and ctx.config.toolprune == "auto":
        print(f"pending: written once this repository's sessions have been idle "
              f"{ctx.config.toolprune_quiet_minutes} minutes, so that no conversation you "
              f"resume re-reads its cached prompt (`toolprune apply` writes it now)")
    kept = denylist.kept_for(ctx.store, ctx.repo.root, host)
    if kept:
        print(f"kept, as you gave them back (`toolprune apply NAME` removes one again): "
              f"{', '.join(kept)}")
    if host == "codex":
        caps = unt.get("capabilities") or {}
        if caps:
            print("capabilities (removable from a project's .codex/config.toml):")
            for c, e in sorted(caps.items()):
                print(f"  {c:14s} loaded in {e['loaded_sessions']:3d} sessions "
                      f"({e.get('loaded_prompts', e['loaded_sessions']):3d} prompts), unused in "
                      f"{e['unused_sessions']:3d}{'  USED (kept)' if e['used'] else ''}")
        trusted = denylist.codex_project_trusted(ctx.repo.root)
        if trusted is False:
            print("note: Codex reads a project's .codex/config.toml only in a TRUSTED project; "
                  "trust this one in Codex first, or an applied proposal does nothing.")
        return 0
    if host == "copilot":
        servers = unt.get("servers") or {}
        if servers:
            print("MCP servers (Copilot can only remove a whole server):")
            for name, e in sorted(servers.items()):
                state = ("USED (kept)" if e["used"] else
                         f"unused for {e.get('unused_sessions', 0)} sessions "
                         f"({e.get('unused_prompts', 0)} prompts)")
                print(f"  {name:24s} {e['tools']:3d} tools, {state}")
    streaks = unt.get("streaks") or {}
    if streaks and host == "claude":
        cfg = ctx.config
        prompts = unt.get("prompt_streaks") or {}
        print(f"how long each tool has gone unused (a tool never used is left out after "
              f"{cfg.toolprune_evidence} prompts over {cfg.toolprune_min_sessions}+ sessions, "
              f"or after {cfg.toolprune_sessions} sessions):")
        for t, n in sorted(streaks.items(), key=lambda kv: (-kv[1], -prompts.get(kv[0], 0)))[:30]:
            used = "used" if t in set(unt.get("used_tools", [])) else ""
            print(f"  {n:4d} sessions {prompts.get(t, n):5d} prompts  {t} {used}")
    if not unt.get("catalog_source") or unt.get("catalog_source") == "observed":
        if os.path.exists(os.path.join(ctx.root, "hosts", host, "catalog.json")):
            # Recorded, but a repository reads the catalog when its next session is folded.
            print("note: the catalog `toolprune refresh-catalog` recorded joins this repository's "
                  "evidence from its next session.")
        else:
            print(f"note: without `te --host {host} toolprune refresh-catalog` only "
                  + ("deferred and observed" if host == "claude" else "observed")
                  + " tools are known, which leaves the always-loaded tools out of the evidence.")
    return 0


def _wait_and_apply(args):
    """`toolprune apply-pending`, which the fold leaves running, detached: wait until the pending
    `auto` deny list can be written without costing a warm prompt cache (`fold.apply_pending`),
    looking again as sessions come and go, then write it. One waiter per repository and agent:
    a second finds the first one's heartbeat fresh and leaves."""
    ctx = _ctx(args)
    beat = ctx.store.path("toolprune-waiter")
    try:
        if time.time() - os.path.getmtime(beat) < 2 * fold.WAITER_POLL_S:
            return 0
    except OSError:
        pass
    ctx.store.ensure()
    with open(beat, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))
    try:
        while True:
            wait = fold.apply_pending(ctx)
            if wait is None:
                return 0
            time.sleep(min(max(wait, 1.0), fold.WAITER_POLL_S))
            os.utime(beat)
            ctx = _ctx(args)            # the settings may have changed meanwhile
    finally:
        try:
            os.remove(beat)
        except OSError:
            pass


def _proposal(ctx, unt):
    """The proposal, less what this checkout already applied or gave back: the clones of one
    repository share its evidence, and one of them may have applied the list before another."""
    mine = set(denylist.applied_for(ctx.store, ctx.repo.root, ctx.host.name))
    mine |= set(denylist.kept_for(ctx.store, ctx.repo.root, ctx.host.name))
    return [t for t in unt.get("proposal", []) if t not in mine]


def cmd_toolprune(args):
    ctx = _ctx(args)
    host = ctx.host.name
    if not denylist.supports(host):
        print(f"tool pruning is not available on {host} (it declares only a handful of tools)")
        return 1
    unt = ctx.store.read("untool.json") or {}
    action = args.action
    if action == "show":
        return _toolprune_show(ctx, unt)
    if action in ("apply", "restore") and _refuse_unwritable(ctx.store.base):
        return 1
    if action == "apply":
        tools = args.tools or _proposal(ctx, unt)
        if not tools:
            print("nothing to apply")
            return 0
        if host == "codex":
            bad = [t for t in tools if t not in denylist.CODEX_TOGGLES]
            if bad:
                print(f"not a removable Codex capability: {', '.join(bad)} (choose from "
                      f"{', '.join(denylist.CODEX_LIVE_CAPABILITIES)})")
                return 2
        try:
            added = denylist.apply(ctx.repo.root, tools, ctx.store, host=host)
        except RuntimeError as e:
            print(f"not applied: {e}")
            return 1
        # Named by the person: no longer one they gave back, whatever `restore` said before.
        denylist.set_kept(ctx.store, ctx.repo.root, host, remove=tools)
        print(f"added: {', '.join(added) or '(already present)'}\n"
              f"in {denylist.settings_path(ctx.repo.root, host)} -- takes effect from the next "
              f"session.")
        if host == "copilot" and added:
            if denylist.copilot_folder_trusted(ctx.repo.root) is False:
                print("warning: Copilot reads a repository's .github/copilot settings only in a "
                      "trusted folder, and this one is not. Trust it (Copilot asks when you "
                      "start it here), or the change does nothing.")
            left = toolcheck.verify_copilot(ctx.repo.root,
                                            denylist.applied_for(ctx.store, ctx.repo.root, host))
            if left is not None:
                print("checked with a local capture: " + ", ".join(
                    f"{k} {'removed' if not v else f'still loaded ({v} tools)'}"
                    for k, v in sorted(left.items())))
        if host == "codex" and added:
            if denylist.codex_project_trusted(ctx.repo.root) is False:
                print("warning: this project is not trusted in Codex, which ignores a "
                      "project's .codex/config.toml until it is. Trust it (Codex asks when "
                      "you start it here), or the change does nothing.")
            state = toolcheck.verify_codex(ctx.repo.root,
                                           denylist.applied_for(ctx.store, ctx.repo.root, host))
            if state:
                print("codex features list: " + ", ".join(
                    f"{k}={'on' if v else 'off'}" for k, v in sorted(state.items())))
        return 0
    if action == "restore":
        try:
            removed = denylist.restore(ctx.repo.root, ctx.store, args.tools or None, host=host)
        except RuntimeError as e:
            print(f"not restored: {e}")
            return 1
        # Given back by the person: `toolprune: auto` must not take them away again at the next
        # fold, and a proposal does not ask for them again.
        denylist.set_kept(ctx.store, ctx.repo.root, host, add=removed)
        print(f"removed: {', '.join(removed) or '-'} (from the next session)"
              + (f"; kept from now on (`toolprune apply {removed[0]}` removes one again)"
                 if removed else ""))
        return 0
    if action == "apply-pending":
        return _wait_and_apply(args)
    if action == "refresh-catalog":
        if host == "codex":
            print("Codex needs no catalog: its removable capabilities are a fixed set "
                  f"({', '.join(denylist.CODEX_LIVE_CAPABILITIES)}), read from each session.")
            return 0
        path, tools = toolcheck.refresh_catalog(state_root(), host=host)
        print(f"{len(tools)} tools recorded in {path}:\n  " + ", ".join(tools))
        return 0
    if action == "measure":
        tools = args.tools or _proposal(ctx, unt) or denylist.applied_for(
            ctx.store, ctx.repo.root, host)
        if not tools:
            print("no proposal to measure")
            return 1
        res = toolcheck.measure(tools, host=host)
        print(json.dumps(res, indent=2))
        return 0
    return 2


# ---- repomap ------------------------------------------------------------------------------
def cmd_repomap(args):
    ctx = _ctx(args)
    if args.action == "refresh":
        st = repomap.refresh(ctx)
    else:
        st = repomap.read_state(ctx)
    if not st.get("map"):
        print(f"no map cached for {ctx.repo.label}" + (f": {st['error']}" if st.get("error")
                                                      else ""))
        if ctx.config.repomap == "off":
            print("the map is off; turn it on with `te config set repomap=on`")
        return 1 if args.action == "refresh" else 0
    current = st.get("commit") == repomap.head_commit(ctx.repo.root)
    print(f"{ctx.repo.label}: ~{st['chars'] // 4} tokens (budget {st.get('tokens_budget')}), "
          f"commit {(st.get('commit') or '?')[:10]}{'' if current else ' (an older commit)'}, "
          f"made in {st.get('secs')}s with {st.get('python')}")
    if st.get("error"):
        print(f"last refresh failed: {st['error']}")
    print(repomap.HEADER + st["map"][:2000] + ("\n..." if len(st["map"]) > 2000 else ""))
    return 0


def _rtk_hook():
    """Where rtk's PreToolUse hook is registered for Claude Code or Copilot, if anywhere."""
    found = []
    for path in (os.path.expanduser("~/.claude/settings.json"),
                 *glob.glob(os.path.expanduser("~/.copilot/hooks/*.json"))):
        try:
            if "rtk" in open(path, encoding="utf-8").read():
                found.append(path)
        except OSError:
            pass
    return found


# ---- config / doctor / fold ----------------------------------------------------------------
def _config_view(cfg):
    """The settings as a person reads them: each mechanism on or off, its settings, and the
    guard's thresholds with the numbers a `loopguard.<name>` setting would replace."""
    d = cfg.as_dict()
    pinned = d.pop("loopguard_thresholds")
    return {
        "enabled": d.pop("enabled"),
        "microskills": {"state": "off" if cfg.microskills == "off" else "on",
                        "microskills": d.pop("microskills"),
                        "microskills_max_facts": d.pop("microskills_max_facts"),
                        "microskills_max_tokens": d.pop("microskills_max_tokens"),
                        "microskills_propose_every": d.pop("microskills_propose_every")},
        "toolprune": {"state": "off" if cfg.toolprune == "off" else "on",
                      "toolprune": d.pop("toolprune"),
                      "toolprune_evidence": d.pop("toolprune_evidence"),
                      "toolprune_min_sessions": d.pop("toolprune_min_sessions"),
                      "toolprune_sessions": d.pop("toolprune_sessions"),
                      "toolprune_quiet_minutes": d.pop("toolprune_quiet_minutes")},
        "loopguard": {
            "state": "off" if cfg.loopguard == "off" else "on",
            "loopguard": d.pop("loopguard"), "loopguard_aggr": d.pop("loopguard_aggr"),
            "loopguard_nudges": d.pop("loopguard_nudges"),
            "thresholds": loopguard.base_thresholds(cfg.loopguard_aggr, pinned),
            "pinned": sorted(pinned)},
        "other": d,
        "warnings": cfg.warnings,
    }


def cmd_config(args):
    ctx = _ctx(args)
    if args.action == "show":
        print(json.dumps(_config_view(ctx.config), indent=2))
        return 0
    path = (os.path.join(ctx.root, "config.json") if args.globally
            else os.path.join(os.path.dirname(ctx.store.base), "config.json"))
    if _refuse_unwritable(os.path.dirname(path)):
        return 1
    if args.action == "unset":
        try:
            cur = unset_config(path, [k.strip() for k in args.pairs])
        except ValueError as e:
            print(f"rejected: {e}")
            return 2
        print(f"{path}: {json.dumps(cur, sort_keys=True)}")
        return 0
    updates = {}
    for kv in args.pairs:
        if "=" not in kv:
            print(f"expected KEY=VALUE, got {kv!r}")
            return 2
        k, v = kv.split("=", 1)
        updates[k.strip()] = v.strip()
    if not updates:
        print("nothing to set: give KEY=VALUE (see `te config show` for the keys)")
        return 2
    try:
        cur = write_config(path, updates)
    except ValueError as e:
        print(f"rejected: {e}")
        return 2
    print(f"{path}: {json.dumps(cur, sort_keys=True)}")
    print("applies from the next hook call; a new fact-size cap applies once the next session "
          "has been folded")
    if {canonical(k) for k in updates} & {"microskills", "loopguard"} and sidecall.resume(
            ctx.host.name, ctx.root):
        print(f"the plugin's model calls through `{ctx.host.name} -p` will be tried again")
    if "distiller" in str(cur.get("microskills", "")):
        every = cur.get("microskills_propose_every") or ctx.config.microskills_propose_every
        print(f"note: micro-skills calls a model through this agent every {every} sessions to "
              "propose facts, charged to the session's cost; `microskills=envfacts` learns "
              "without model calls")
    if cur.get("loopguard") == "stop":
        print("note: `stop` acts as `nudge`: the plugin never stops a turn; a turn still stuck "
              "after the redirect gets a notice saying how to stop it")
    return 0


def cmd_fold(args):
    # A session is folded into the repository it is pinned to, wherever `--cwd` points.
    ctx = _session_ctx(args, (args.session or [None])[0])
    sids = args.session or fold.pending_sessions(ctx)
    for sid in sids:
        rec = fold.fold_session(ctx, sid)
        if rec is not None and args.verbose:
            print(json.dumps(rec, default=str))
    return 0


def cmd_backfill(args):
    """Fold this repository's earlier sessions from the agent's own logs (see `backfill`)."""
    ctx = _ctx(args)
    if backfill.history_dir(ctx.host.name, ctx.env) is None:
        print(f"{ctx.host.name}: no session history is read for this agent (Claude Code only)")
        return 0
    limit = args.limit if args.limit is not None else None
    sid = ctx.host.env_session_id(ctx.env)
    r = backfill.run(ctx, exclude=(sid,) if sid else (), limit=limit)
    if not getattr(args, "quiet", False):
        print(f"{ctx.repo.label}: found {r['found']} earlier session(s), folded {r['folded']}")
    return 0


def cmd_judge(args):
    """The detached half of the stuck judge (see plugin/judge.py); not for people."""
    from . import judge
    judge.run_job(_session_ctx(args, args.session), args.session, args.job)
    return 0


# ---- repos ----------------------------------------------------------------------------------
def _when(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "-"


def _print_moves(done):
    """One line per move a migration made; returns how many there were."""
    n = 0
    for r in done or ():
        for m in r.get("moves") or ():
            n += 1
            what = "all of its state" if r.get("into") == m["into"] else f"{m['moved']} session(s)"
            print(f"  {r['key']} -> {m['into']} ({m['host']}): {what}"
                  + (f", {m['refold']} to be folded there again" if m.get("refold") else ""))
    return n


def cmd_repos(args):
    root = state_root()
    if args.action in ("migrate", "merge") and _refuse_unwritable(root):
        return 1
    if args.action == "migrate":
        done = repos.run_migration(root)
        if done is None:
            print("another migration holds the state root; try again in a minute")
            return 1
        if not args.quiet:
            if not _print_moves(done):
                print("nothing to move: every session is filed under the repository it ran in")
        return 0
    if args.action == "merge":
        if not args.keys or len(args.keys) > 2:
            print("usage: te repos merge KEY [INTO] (keys as `te repos` lists them)")
            return 2
        src = args.keys[0]
        if not os.path.isdir(os.path.join(repos.repos_dir(root), src)):
            print(f"no state directory {src!r} in {repos.repos_dir(root)}")
            return 2
        into = args.keys[1] if len(args.keys) > 1 else _ctx(args).repo
        into_key = into if isinstance(into, str) else into.key
        if into_key == src:
            print(f"{src} is this repository's own state; name the one to merge into it")
            return 2
        res = repos.merge(root, src, into)
        if res is None:
            print("the state root or one of the two directories is busy; try again in a minute")
            return 1
        print(f"merged {src} into {into_key}: " + (", ".join(
            f"{h} {c['moved']} session(s)" + (f" ({c['refold']} to be folded again)"
                                             if c["refold"] else "")
            for h, c in sorted(res.items())) or "no sessions"))
        print(f"the old directory is kept under {os.path.join(root, 'merged')}")
        if not isinstance(into, str):
            repos.refold_moved([{"moves": [dict(c, host=h, cwd=into.root)
                                           for h, c in res.items()]}])
        return 0
    current = _ctx(args).repo
    rows, merged = repos.listing(root, current=current)
    if args.json:
        print(json.dumps({"repos": rows, "merged": merged}, indent=2, default=str))
        return 0
    if not rows:
        print(f"no state yet in {repos.repos_dir(root)}")
    for row in rows:
        print(f"{row['label']}  [{row['key']}]" + ("  <- this directory" if row["current"]
                                                  else ""))
        print("  identified   " + (repos.how_words(row["how"], row["remote"]) if row["how"]
                                   else "recorded by an earlier version"))
        print("  sessions     " + (", ".join(f"{h} {n}" for h, n in row["agents"].items())
                                   or "none") + f"; last {_when(row['last'])}")
        if row["checkouts"]:
            print("  checkouts    " + ", ".join(row["checkouts"]))
        elif row["cwds"]:
            print("  ran in       " + ", ".join(row["cwds"][:3])
                  + (f" (+{len(row['cwds']) - 3})" if len(row["cwds"]) > 3 else ""))
        for n in row["notes"]:
            print(f"  note         {n}")
    if merged:
        print(f"\n{merged} earlier state director{'y' if merged == 1 else 'ies'} merged into "
              f"the above, kept under {os.path.join(root, 'merged')}")
    return 0


def cmd_probe(args):
    if args.host != "copilot":
        print("the probe runs on Copilot only so far: `te --host copilot probe`")
        return 2
    from . import probe
    return probe.run(sessions=args.sessions, model=args.model, keep=args.keep)


def cmd_doctor(args):
    ok = sys.version_info >= (3, 10)
    print(f"python      {sys.version.split()[0]} {'ok' if ok else '(needs >= 3.10)'}")
    root = state_root()
    try:
        os.makedirs(root, exist_ok=True)
        probe = os.path.join(root, ".write-test")
        open(probe, "w", encoding="utf-8").close()
        os.remove(probe)
        print(f"state root  {root} (writable)")
    except OSError as e:
        print(f"state root  {root} NOT writable: {e}")
        ok = False
    import shutil as _sh
    # Only the agents installed here: a Codex-only machine has no Claude Code catalog to probe.
    agents = [h for h in ("claude", "codex", "copilot", "pi") if _sh.which(h)]
    print(f"agents      {', '.join(agents) or 'none of claude, codex, copilot, pi on PATH'}")
    for h in agents:
        pause = sidecall.paused(h, root)
        if pause:
            print(f"{h:11s} model calls paused since "
                  f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(pause['at']))}: `{h} -p` said "
                  f"it is not signed in; `config set microskills=on` (or `loopguard=on`) "
                  f"tries again")
    if "claude" in agents:
        cat = os.path.join(root, "hosts", "claude", "catalog.json")
        print(f"claude      catalog {'present' if os.path.exists(cat) else 'not probed'}")
    if "codex" in agents:
        trusted = denylist.codex_project_trusted(args.cwd)
        if trusted is not None:
            print(f"codex       this project is {'trusted' if trusted else 'NOT trusted'} "
                  f"(an untrusted project's .codex/config.toml is ignored)")
    if "copilot" in agents:
        from urllib.parse import urlparse
        from .sidecall import copilot_model
        cat = os.path.join(root, "hosts", "copilot", "catalog.json")
        print(f"copilot     catalog {'present' if os.path.exists(cat) else 'not probed'}")
        base = os.environ.get("COPILOT_PROVIDER_BASE_URL")
        route = (f"BYOK {os.environ.get('COPILOT_PROVIDER_TYPE') or 'openai'} at "
                 f"{urlparse(base).netloc or base}" if base else "Copilot subscription")
        print(f"copilot     {route}; side calls and `probe` use model "
              f"{copilot_model() or '(Copilot default)'}")
        if base:
            print("copilot     note: Copilot does not pass COPILOT_PROVIDER_* to the plugin's "
                  "hooks, so its model calls (micro-skills' proposer, the loop guard's judge) "
                  "cannot use this provider")
        ctrust = denylist.copilot_folder_trusted(args.cwd)
        if ctrust is not None:
            print(f"copilot     this folder is {'trusted' if ctrust else 'NOT trusted'} (an "
                  f"untrusted folder's .github/copilot settings are ignored)")
    if "pi" in agents:
        settings = os.path.join(os.environ.get("PI_CODING_AGENT_DIR") or os.path.join(
            os.path.expanduser("~"), ".pi", "agent"), "settings.json")
        try:
            pkgs = json.load(open(settings, encoding="utf-8")).get("packages") or []
        except (OSError, ValueError, AttributeError):
            pkgs = []
        mine = [p for p in pkgs if "tokeneconomizer" in json.dumps(p).lower()]
        print(f"pi          package {'installed: ' + str(mine[0]) if mine else 'not in ' + settings}")
    ctx = _ctx(args)
    print(f"repository  {ctx.repo.root}: {repos.describe(ctx.repo)}; state {ctx.repo.key}")
    home_git = os.path.join(paths.home_dir(), ".git")
    for path in repos.ignored_git_entries(args.cwd):
        if os.path.realpath(path) != home_git:          # the home line below says it
            print(f"repository  ignored {path}: not a git repository (no HEAD, or a dangling "
                  f"gitdir)")
    note = repos.home_git_note()
    if note:
        print(f"home        {note}")
    if repos.needs_migration(root):
        print("state       recorded by an earlier version and not yet migrated: `te repos "
              "migrate` (runs by itself at the next session start)")
    py = repomap.find_python(ctx)
    print(f"repomap     {ctx.config.repomap}; " + (f"aider found ({py})" if py else
          "no Python with aider found (pip install aider-chat, or set repomap_python)"))
    rtk, hooks = _sh.which("rtk"), _rtk_hook()
    if rtk or hooks:
        print(f"rtk         {rtk or 'not on PATH'}; hook in "
              f"{', '.join(hooks) or 'no settings file'}. Compatible. Note: `rtk pytest` prints "
              f"nothing when pytest is missing, so the plugin cannot learn that fact from it")
    log = os.path.join(root, "errors.log")
    if os.path.exists(log):
        tail = open(log, encoding="utf-8", errors="replace").read().splitlines()[-6:]
        print("errors.log (tail):\n  " + "\n  ".join(tail))
    else:
        print("errors.log  none")
    return 0 if ok else 1


def build_parser():
    ap = argparse.ArgumentParser(prog="te", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cwd", default=os.getcwd())
    ap.add_argument("--host", default=None, choices=sorted(hosts.HOSTS))
    # The same two options are accepted after the subcommand too (`te fold --host codex ...`).
    # SUPPRESS keeps a subcommand that omits them from overwriting the top-level values.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--cwd", default=argparse.SUPPRESS)
    common.add_argument("--host", default=argparse.SUPPRESS, choices=sorted(hosts.HOSTS))
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, *aliases):
        return sub.add_parser(name, aliases=list(aliases), parents=[common])

    p = add("status")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)
    add("microskills", "facts").set_defaults(fn=cmd_microskills)
    p = add("toolprune", "untool")
    p.add_argument("action", nargs="?", default="show",
                   choices=("show", "apply", "restore", "refresh-catalog", "measure",
                            "apply-pending"))
    p.add_argument("tools", nargs="*")
    p.set_defaults(fn=cmd_toolprune)
    p = add("config")
    p.add_argument("action", nargs="?", default="show", choices=("show", "set", "unset"))
    p.add_argument("pairs", nargs="*")
    p.add_argument("--global", dest="globally", action="store_true")
    p.set_defaults(fn=cmd_config)
    p = add("fold")
    p.add_argument("--session", action="append")
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(fn=cmd_fold)
    p = add("backfill")
    p.add_argument("--limit", type=int, default=None,
                   help="newest sessions to read (default: the `backfill_sessions` setting)")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(fn=cmd_backfill)
    p = add("judge")
    p.add_argument("--session", required=True)
    p.add_argument("--job", required=True)
    p.set_defaults(fn=cmd_judge)
    add("doctor").set_defaults(fn=cmd_doctor)
    p = add("repos")
    p.add_argument("action", nargs="?", default="list", choices=("list", "migrate", "merge"))
    p.add_argument("keys", nargs="*")
    p.add_argument("--json", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(fn=cmd_repos)
    p = add("repomap")
    p.add_argument("action", nargs="?", default="show", choices=("show", "refresh"))
    p.set_defaults(fn=cmd_repomap)
    p = add("probe")
    p.add_argument("--sessions", type=int, default=6)
    p.add_argument("--model", default=None)
    p.add_argument("--keep", action="store_true")
    p.set_defaults(fn=cmd_probe)
    return ap


def _recent_host(cwd):
    """The agent whose state for this repository changed last, or None: from a plain terminal,
    a Codex or Pi user means their own agent's state without typing `--host`."""
    from .paths import repo_identity, repo_state_dir
    try:
        repo = repo_identity(cwd)
    except OSError:
        return None
    seen = {}
    for name in hosts.HOSTS:
        d = repo_state_dir(repo, name)
        if os.path.isdir(d):
            seen[name] = max([os.path.getmtime(d)] + [os.path.getmtime(os.path.join(d, f))
                                                      for f in os.listdir(d)])
    return max(seen, key=seen.get) if seen else None


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.host is None:
        args.host = hosts.default_host(recent=_recent_host(args.cwd))
    try:
        return args.fn(args) or 0
    finally:
        from .paths import secure_root
        secure_root()


if __name__ == "__main__":
    sys.exit(main())
