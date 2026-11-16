"""The loop guard's judge, off the hook's critical path.

With the guard on, its gate asks an LLM whether a turn is stuck and, when it is, has a second
call write the redirect. Both are side calls through the agent's own CLI (`sidecall`),
and run inside a PostToolUse hook they would hold the agent for tens of seconds. So the hook
only STARTS a job here and returns: `AsyncJudge.start` writes the request and
spawns `te judge` detached; the job runs the judge, and the nudge writer only when the verdict is
stuck, then leaves the verdict -- with the redirect already written -- for the next PostToolUse,
whose `AsyncJudge.poll` hands it to the guard. The cascade the guard runs (`assess_abort`) is
unchanged: while a verdict is pending the judge answers nothing, which it already reads as
"throttled"; only the latency moved off the agent.

What a job spends is charged to the ledger the moment it is spent, as its own `overhead` record
for the session: a verdict that lands after its turn -- or its session -- has ended was still paid
for, and must not drop out of the account.
"""
import time

from . import sidecall

REQUEST = "judge/{sid}.request.json"
RESULT = "judge/{sid}.result.json"


class AsyncJudge:
    """The judge as `TurnGuard` sees it: `start` a job for a digest, `poll` for its verdict."""

    def __init__(self, ctx, session_id, threshold):
        self.ctx = ctx
        self.sid = session_id
        self.threshold = float(threshold or 0)

    def start(self, guard, digest):
        job = f"{guard.turn_key}:{guard.segment}:{guard.judge_calls}:{time.time():.3f}"
        self.ctx.store.write(REQUEST.format(sid=self.sid), {
            "id": job, "session_id": self.sid, "turn_key": guard.turn_key,
            "segment": guard.segment, "digest": digest, "threshold": self.threshold,
            "at": time.time()})
        spawn_job(self.ctx, self.sid, job)
        return job

    def poll(self, guard):
        """(found, verdict) for the guard's pending job; verdict is None when the call failed."""
        pending = guard.judge_pending or {}
        if not pending.get("id"):
            return False, None
        res = self.ctx.store.read(RESULT.format(sid=self.sid)) or {}
        if res.get("id") != pending["id"]:
            return False, None
        return True, res.get("verdict") or None


class NoVerdict:
    """The judge while this agent's model calls are paused: it never answers, so the guard
    spares the turn rather than act on its rule alone, and only the backstop remains."""

    def start(self, guard, digest):
        return None

    def poll(self, guard):
        return False, None


def run_job(ctx, session_id, job_id):
    """The detached half: judge the digest, write the redirect if stuck, charge the ledger."""
    from ..loopguard import judge as loop_judge
    req = ctx.store.read(REQUEST.format(sid=session_id)) or {}
    if req.get("id") != job_id:                       # superseded by a newer request
        return None
    paused = sidecall.paused(ctx.host.name, ctx.root)
    # Each call asks for its own runner: `local_roles` may put the judge on-device and leave
    # the redirect on the agent's CLI, or the other way round. A paused host's CLI is skipped,
    # but a local runner (which needs no login) still answers.
    runner = sidecall.runner_for(ctx.host.name, ctx.config, role="judge") if not paused \
        else sidecall.local_runner("judge", ctx.config)
    writer = sidecall.runner_for(ctx.host.name, ctx.config, role="nudge") if not paused \
        else sidecall.local_runner("nudge", ctx.config)
    verdict, written = None, None
    if runner is not None:
        try:
            verdict = loop_judge.llm_stuck_judge(req.get("digest") or "", runner, model="agent")
        except Exception:
            verdict = None
        if verdict and getattr(runner, "answered_locally", False):
            # The local model's confidences are coarse and high: a stuck verdict counts only at
            # its own calibrated floor (`local_judge_confidence`); below it the turn is spared.
            verdict["local"] = getattr(runner, "local", "local")
            floor = float(getattr(ctx.config, "local_judge_confidence", 0.9) or 0)
            if verdict.get("stuck") and float(verdict.get("confidence") or 0) < floor:
                verdict["stuck"] = False
                verdict["reason"] = ("below local_judge_confidence: "
                                     + str(verdict.get("reason") or ""))[:160]
        conf = float((verdict or {}).get("confidence") or 0)
        if verdict and verdict.get("stuck") and conf >= float(req.get("threshold") or 0):
            try:
                written = (loop_judge.llm_nudge_writer(req.get("digest") or "", writer,
                                                       model="agent")
                           if writer is not None else None)
            except Exception:
                written = None
            if written and (written.get("text") or "").strip():
                verdict["_nudge"] = written["text"].strip()
    spend = {"spiral_judge_tokens": float((verdict or {}).get("_tokens") or 0),
             "spiral_nudge_writer_tokens": float((written or {}).get("_tokens") or 0)}
    if any(spend.values()):
        # Charged before the verdict is published: a fold that runs the instant the verdict
        # lands has already got the record.
        ctx.store.append("ledger.jsonl", {
            "kind": "overhead", "session_id": session_id, "host": ctx.host.name,
            "job": job_id, "at": time.time(), "overhead": spend})
    ctx.store.write(RESULT.format(sid=session_id),
                    {"id": job_id, "verdict": verdict, "at": time.time()})
    return verdict


def spawn_job(ctx, session_id, job_id):
    """Run a judge job detached (inline when TE_JUDGE_SYNC=1, for tests)."""
    if ctx.env.get("TE_JUDGE_SYNC") == "1":
        return run_job(ctx, session_id, job_id)
    return ctx.spawn(["judge", "--session", session_id, "--job", job_id], "judge.log")
