"""`LiveController`: what the plugin does at each hook.

    SessionStart      inject the repository's micro-skills (cached by the last fold), and
                      fold any earlier session that ended without its SessionEnd being seen;
                      with `repomap` on, refresh a stale map in the background
    UserPromptSubmit  open a prompt-turn for the loop guard; on a session's first prompt, add
                      the repository map (`repomap=on`)
    PostToolUse       fold the call into the turn's guard; a redirect when it first says the
                      turn is stuck, then a notice to the person saying how to stop it
    Stop              close the turn; an ended-green turn feeds the repository's envelope
    SessionEnd        hand the session to a detached `te fold`

Every handler returns quickly and fails open. A hook that raises would break the user's session
for a cost optimization, so `hook.main` turns any exception into an empty output, and nothing
here waits on the network: injection reads a cached render, and the only model call a hook can
cause -- the sampled stuck judge -- runs only when the guard's free gate is open, and then in a
detached job (`judge`), never in the hook itself.

Every visible action is announced in one `systemMessage` line, so a person can see why the agent
changed course; `shadow` computes and logs every decision without acting on any of them.
"""
import hashlib
import os
import time

from .. import loopguard
from . import backfill, denylist, fold, repomap, repos, sidecall
from .hosts import merge_outputs
from .paths import ensure_launcher

TURN_HISTORY = 200


class LiveController:
    def __init__(self, ctx, payload):
        self.ctx = ctx
        self.p = payload or {}
        self.host = ctx.host
        self.store = ctx.store
        self.cfg = ctx.config
        self.sid = self.host.session_id(self.p)

    # ---- plumbing -----------------------------------------------------------------------
    def _sess_name(self):
        return os.path.join("sessions", f"{self.sid}.json")

    def _session(self):
        s = self.store.read(self._sess_name())
        if not isinstance(s, dict):
            # Where the session started (its first hook's directory): what `repos` resolves
            # to tell which repository the session belongs to, when its log does not say.
            s = {"session_id": self.sid, "host": self.host.name, "started": time.time(),
                 "cwd": self.host.cwd(self.p, self.ctx.env), "turns": [], "nudges": 0,
                 "aborts": 0, "judge_tokens": 0.0, "writer_tokens": 0.0}
        return s

    def _save(self, s):
        tp = self.host.transcript(self.p)
        if tp:
            s["transcript"] = tp
        s["updated"] = time.time()
        self.store.write(self._sess_name(), s)

    def _cmd(self, name):
        """How a person runs one of the plugin's commands on this host."""
        if self.host.name in ("claude", "copilot"):    # Copilot's TUI runs plugin commands too
            return f"/tokeneconomizer:{name}"
        if self.host.name == "pi":
            return f"/te {name}"                 # registered by the Pi extension
        if self.host.name == "codex":
            return f"$tokeneconomizer:{name}"    # the plugin's Codex skills
        return f"te --host {self.host.name} {name}"

    def _log(self, kind, **fields):
        self.store.append("events.jsonl", dict(
            fields, kind=kind, at=time.time(), session_id=self.sid, shadow=self.cfg.shadow))

    def handle(self):
        # A payload without a session id is not a hook call any host makes; recording it would
        # leave an "unknown" session behind.
        if not self.cfg.enabled or not self.host.session_id(self.p):
            return {}
        self.ctx.settle()
        self.cfg = self.ctx.config
        event = self.p.get("hook_event_name") or ""
        # PostToolUseFailure: Copilot reports a failed tool there, not in PostToolUse.
        handler = {"SessionStart": self.session_start, "UserPromptSubmit": self.user_prompt,
                   "PostToolUse": self.post_tool, "PostToolUseFailure": self.post_tool,
                   "Stop": self.stop, "SessionEnd": self.session_end}.get(event)
        return (handler() or {}) if handler else {}

    # ---- SessionStart -----------------------------------------------------------------
    def session_start(self):
        source = self.p.get("source") or "startup"
        try:
            ensure_launcher(self.ctx.env)
        except OSError:
            pass
        s = self._session()
        s["source"] = source
        s.setdefault("cwd", self.host.cwd(self.p, self.ctx.env))
        outputs = []
        seen = self.store.read("notices.json") or {}
        seen_before = dict(seen)

        if self.cfg.microskills != "off":
            inj = self.store.read("injection.json") or {}
            text = inj.get("rendered") or ""
            # A resumed or forked conversation already carries what the first start injected,
            # where the agent replays it; a cleared or compacted one has lost it. ("new" is
            # Copilot's word for "startup".)
            fresh = source in ("startup", "new", "clear", "compact")
            if not fresh and not self.host.keeps_start_context:
                # The agent rebuilt the conversation without what its first start added: the
                # same text again puts it back as it was (and as its prompt cache has it), so a
                # session that started without facts gets none either.
                text = s.get("injected_text", text if s.get("injected_chars") else "")
            if text and (fresh or not self.host.keeps_start_context):
                n_facts = sum(1 for ln in text.splitlines() if ln.startswith("- "))
                self._log("inject", chars=len(text), facts=n_facts, source=source)
                if not self.cfg.shadow:
                    outputs.append(self.host.context("SessionStart", text))
                    s["injected_chars"] = len(text)
                    s["injected_text"] = text
                    if "rendered_ids" in inj:     # which facts, for this session's fold
                        s["injected_ids"] = list(inj["rendered_ids"])
                    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
                    if seen.get("injection") != digest:
                        outputs.append(self.host.notice(
                            f"TokenEconomizer: added {n_facts} learned fact(s) about "
                            f"{self.ctx.repo.label} to this session's context "
                            f"(review with `{self._cmd('microskills')}`)."))
                        seen["injection"] = digest

        if (self.cfg.toolprune != "off" and denylist.supports(self.host.name)
                and not self.cfg.shadow):
            outputs.extend(self._toolprune_notices(seen))

        pause = sidecall.paused(self.host.name, self.ctx.root)
        calls_a_model = "distiller" in self.cfg.microskills or self.cfg.loopguard != "off"
        if (pause and calls_a_model and not self.cfg.shadow
                and seen.get("sidecall_paused") != pause["at"]):
            byok = (" (with your own model key, Copilot does not pass it to the plugin's hooks)"
                    if self.host.name == "copilot" else "")
            outputs.append(self.host.notice(
                f"TokenEconomizer: the plugin's own model calls go through `{self.host.name} -p`, "
                f"which answered that it is not signed in{byok}, so they are paused; facts that "
                f"need no model call are still learned, and the loop guard redirects only at its "
                f"backstop. `{self._cmd('config')} set microskills=on` (or `loopguard=on`) tries "
                f"again."))
            seen["sidecall_paused"] = pause["at"]

        note = repos.migration_notice(self.ctx.root, self._cmd("repos"))
        if note and not self.cfg.shadow:
            outputs.append(self.host.notice(note))

        if self.cfg.repomap != "off":
            if repomap.stale(self.ctx):
                repomap.spawn_refresh(self.ctx)
            at_start = self.cfg.repomap == "start" or not self.host.prompt_context
            if at_start and source in ("startup", "new", "clear", "compact"):
                outputs.extend(self._inject_map(s, seen, repomap.cached_block(self.ctx),
                                                "SessionStart", "cached"))
                s["repomap_done"] = True

        if seen != seen_before:
            self.store.write("notices.json", seen)
        self._save(s)
        repos.note_repo(self.ctx.root, self.ctx.repo)
        repos.prune_pins(self.ctx.root, self.host.name)
        repos.start_migration(self.ctx)
        # The first session here learns from the agent's own logs of earlier ones (once per
        # repository and agent), so what was learned before install is not lost; then the
        # ordinary sweep of sessions whose end was never seen.
        if backfill.should_run(self.ctx):
            try:
                backfill.spawn(self.ctx)
            except Exception:
                pass
        fold.spawn_fold(self.ctx, fold.pending_sessions(self.ctx, exclude=(self.sid,)))
        return merge_outputs(*outputs)

    def _toolprune_notices(self, seen):
        """The deny list's lines at session start, each said once: what `auto` removed from this
        checkout's sessions (from this one on), and a proposal still waiting -- under `propose`,
        or when `auto` could not write the settings file."""
        out = []
        cmd, unit = self._cmd("toolprune"), denylist.unit(self.host.name)
        unt = self.store.read("untool.json") or {}
        root = self.ctx.repo.root
        mine = sorted(denylist.applied_for(self.store, root, self.host.name))

        def few(items):
            return ", ".join(items[:4]) + ("..." if len(items) > 4 else "")
        if self.cfg.toolprune == "auto" and seen.get("untool_applied", "") != ",".join(mine):
            if mine:
                trusted = (denylist.codex_project_trusted(root) if self.host.name == "codex" else
                           denylist.copilot_folder_trusted(root) if self.host.name == "copilot"
                           else None)
                out.append(self.host.notice(
                    f"TokenEconomizer removed {len(mine)} {unit}(s) this repository's sessions "
                    f"never used ({few(mine)}), from this session on. Review with `{cmd}`; "
                    f"`{cmd} restore` gives them back."
                    + (" (The agent reads that setting only in a trusted folder: trust this "
                       "one.)" if trusted is False else "")))
            seen["untool_applied"] = ",".join(mine)
        # What this checkout already applied, or gave back, is no news here (another clone may
        # not have).
        kept = set(denylist.kept_for(self.store, root, self.host.name))
        prop = [t for t in unt.get("proposal") or [] if t not in mine and t not in kept]
        refused = unt.get("auto_apply_refused") if self.cfg.toolprune == "auto" else None
        # Under `auto`, a list waiting for the repository to go quiet is no news yet: the
        # session start after it is written says so (above). Only a refusal is.
        if self.cfg.toolprune == "auto" and not refused:
            prop = []
        if prop and seen.get("untool") != ",".join(prop):
            out.append(self.host.notice(
                f"TokenEconomizer: {len(prop)} {unit}(s) went unused in this repository's recent "
                f"sessions ({few(prop)}). Review with `{cmd}`; "
                + (f"they could not be applied ({refused}), and `{cmd} apply` tries again."
                   if refused else f"nothing changes until you run `{cmd} apply`.")))
            seen["untool"] = ",".join(prop)
        return out

    def _inject_map(self, s, seen, block, event, how):
        """The outputs that put the repository map in context (and say so, once per repository)."""
        self._log("repomap", chars=len(block), how=how)
        if not block or self.cfg.shadow:
            return []
        s["repomap_chars"] = len(block)
        out = [self.host.context(event, block)]
        if not seen.get("repomap"):
            out.append(self.host.notice(
                f"TokenEconomizer: added a map of {self.ctx.repo.label}'s key files and symbols "
                f"to this session's context (~{len(block) // 4} tokens; `repomap=off` to stop)."))
            seen["repomap"] = True
        return out

    # ---- prompt-turns -------------------------------------------------------------------
    def _close_turn(self, s, stopped_normally):
        data = s.get("turn")
        if not data:
            return None
        g = loopguard.TurnGuard(data=data)
        summary = g.summary(stopped_normally)
        if g.cum_turns and stopped_normally:
            loopguard.record_turn(self.store, g, level=self.cfg.loopguard_aggr,
                              overrides=self.cfg.loopguard_thresholds)
        s["turns"] = (s.get("turns") or [])[-(TURN_HISTORY - 1):] + [summary]
        s["judge_tokens"] = float(s.get("judge_tokens", 0) or 0) + g.judge_tokens
        s["writer_tokens"] = float(s.get("writer_tokens", 0) or 0) + g.writer_tokens
        s["turn"] = None
        return summary

    def user_prompt(self):
        if self.cfg.loopguard == "off" and not (self.cfg.repomap == "on"
                                             and self.host.prompt_context):
            return {}
        s = self._session()
        outputs = []
        if self.cfg.repomap == "on" and self.host.prompt_context and not s.get("repomap_done"):
            # Once per session, on its first prompt: a map ranked toward what the prompt names.
            s["repomap_done"] = True
            seen = self.store.read("notices.json") or {}
            block, how = repomap.for_prompt(self.ctx, self.p.get("prompt") or "")
            outputs = self._inject_map(s, seen, block, "UserPromptSubmit", how)
            if outputs:
                self.store.write("notices.json", seen)
        if self.cfg.loopguard != "off":
            # A turn still open here never reached Stop -- the person interrupted it. Its
            # outcome is unknown, so it is recorded but does not feed the envelope.
            self._close_turn(s, stopped_normally=False)
            s["turn"] = loopguard.TurnGuard(turn_key=self.host.turn_key(self.p)).to_dict()
        self._save(s)
        return merge_outputs(*outputs) if outputs else {}

    def stop(self):
        if self.cfg.loopguard == "off" or self.p.get("stop_hook_active"):
            return {}
        s = self._session()
        summary = self._close_turn(s, stopped_normally=True)
        if summary:
            self._log("turn", **summary)
        self._save(s)
        return {}

    # ---- PostToolUse ---------------------------------------------------------------------
    def _judge(self, thr, envelope):
        """The stuck judge, run in the background (`judge.AsyncJudge`): with the guard on, it
        makes every call to redirect but the backstop's. The hook only starts a job, and a later
        call picks up its verdict with the redirect already written, so the agent never waits.

        None before this repository has an envelope -- the guard does not act on a repository
        whose usual turns it has not learned (the rule fallback never acts without one either).
        While this agent's model calls are paused, a judge that never answers: the turn is
        spared, as a missing verdict is, rather than redirected on the rule alone."""
        from .judge import AsyncJudge, NoVerdict
        if envelope is None:
            return None
        if sidecall.paused(self.host.name, self.ctx.root):
            return NoVerdict()
        return AsyncJudge(self.ctx, self.sid, thr.get("judge_confidence"))

    def post_tool(self):
        if self.cfg.loopguard == "off" or self.host.is_subagent(self.p):
            return {}
        # The answer names the event it answers: Claude Code rejects a PostToolUseFailure answer
        # labelled PostToolUse ("Hook returned incorrect event name", 2.1.306) and drops it whole,
        # redirect and notice -- and a stuck turn's calls are mostly failures.
        event = self.p.get("hook_event_name") or "PostToolUse"
        s = self._session()
        key = self.host.turn_key(self.p)
        data = s.get("turn")
        if data and key and data.get("turn_key") and data["turn_key"] != key:
            self._close_turn(s, stopped_normally=False)     # a turn we never saw end
            data = None
        g = loopguard.TurnGuard(data=data) if data else loopguard.TurnGuard(turn_key=key)
        name, args, text, code = self.host.tool_call(self.p)
        tracker = loopguard.load_envelope(self.store, level=self.cfg.loopguard_aggr,
                                      overrides=self.cfg.loopguard_thresholds)
        envelope = tracker.envelope
        thr = loopguard.thresholds(tracker, self.cfg.loopguard_thresholds)
        # The judge is woken only by a sign of trouble -- a repeated failure, or dense errors --
        # once a turn has run `min_turns` calls; never merely because the turn outran this
        # repository's usual length, which interactive turns do all the time (the experiments'
        # agents ran under a turn cap, hence their envelope gate).
        thr = dict(thr, gate_over_envelope=False,
                   gate_min_turns=max(thr["gate_min_turns"], thr["min_turns"]))
        decision = g.fold(name, args, text, code, envelope=envelope, thr=thr,
                          judge=self._judge(thr, envelope),
                          nudge_budget=self.cfg.loopguard_nudges)
        out = {}
        if decision:
            action, reason = decision
            short = reason.split(":")[0]
            if action == "nudge":
                body = g.nudge_body(envelope)
                g.after_nudge()
                s["nudges"] = int(s.get("nudges", 0)) + 1
                self._log("nudge", reason=reason, tool_calls=g.cum_turns, envelope=envelope)
                if not self.cfg.shadow:
                    out = merge_outputs(
                        self.host.context(event, body),
                        self.host.notice(f"TokenEconomizer nudged this turn ({short}): the "
                                         f"same failure kept recurring."))
            elif action == "abort" and not g.abort_delivered:
                # Still stuck after the redirect. The plugin never stops a turn itself -- the
                # agents' hooks cannot all do it, and the person decides -- so the last rung is
                # a notice that says how to stop it, the same on every agent and in every mode.
                g.abort_delivered = True
                s["aborts"] = int(s.get("aborts", 0)) + 1
                self._log("abort", reason=reason, tool_calls=g.cum_turns, envelope=envelope,
                          delivered="notice")
                if not self.cfg.shadow:
                    headless = self.ctx.is_headless(self.host.transcript(self.p))
                    text = (f"TokenEconomizer: this turn still looks stuck after a redirect "
                            f"({short}, {g.cum_turns} tool calls). {self.host.stop_hint(headless)}")
                    out = self.host.notice(text)
                    if self.host.relays_notices:
                        # Where the agent keeps hook messages out of the person's view, the
                        # model passes the notice on in its reply, which they do see.
                        out = merge_outputs(out, self.host.context(event, self.host.relay(text)))
        s["turn"] = g.to_dict()
        self._save(s)
        return out

    # ---- SessionEnd ---------------------------------------------------------------------
    def session_end(self):
        # An end with no start and no session log is no session to learn from: Codex 0.163.0
        # sends one for a thread that never ran a prompt (`codex debug prompt-input`), and
        # recording it would only leave an unreadable fold in the ledger.
        tp = self.host.transcript(self.p)
        if not os.path.exists(self.store.path(self._sess_name())) and not (
                tp and os.path.exists(tp)):
            return {}
        s = self._session()
        self._close_turn(s, stopped_normally=False)
        s["ended"] = time.time()
        s["end_reason"] = self.p.get("reason")
        self._save(s)
        fold.spawn_fold(self.ctx, [self.sid])
        return {}
