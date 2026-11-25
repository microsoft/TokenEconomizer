"""The loop guard's two optional paid calls: a stuck judge and a redirect writer.

Both read the same leakage-safe digest of the agent's recent actions
(`SpiralState.recent_digest`: commands, searches, edited file basenames, error signatures --
never file contents or the task text). The judge decides stuck-versus-productive when the free
gate is open; the writer turns the redirect from a count template into one that names the
pattern it sees. Each returns its effective-token cost so the caller can charge it to the
session's ledger, and falls back to the free path on any failure.
"""
import json
import re

from ..pricing import runner_effective_or_approx as _runner_effective_or_approx

STUCK_JUDGE_SYSTEM = (
    "You monitor an AI coding agent that is partway through ONE task in a software repo. "
    "A cheap heuristic flagged its recent process as a possible UNPRODUCTIVE LOOP. Read "
    "the leakage-safe digest of its RECENT actions and judge whether it is genuinely "
    "STUCK — repeating the same kind of edit/test/failure with no progress toward green "
    "tests, unlikely to recover if it keeps going the same way — versus still making "
    "real progress (changing failures, narrowing the problem, exploring new hypotheses).\n\n"
    "Judge ONLY the workflow from the digest; do NOT infer the task's solution or which "
    "lines to change. Favor SPARING (stuck=false) when there is any sign of progress; "
    "only return stuck=true with high confidence for a clear, repetitive dead-end.\n"
    "Return ONLY a JSON object: {\"stuck\": bool, \"confidence\": <0.0-1.0>, "
    "\"reason\": \"<short leakage-safe justification>\"}."
)

STUCK_JUDGE_WITH_NUDGE_SYSTEM = (
    STUCK_JUDGE_SYSTEM[:-1]
    + " When stuck is true, also include \"nudge\": a SHORT 2-4 sentence redirect addressed "
    "to the agent that (1) names the specific repetitive pattern in the digest and (2) "
    "proposes a MATERIALLY DIFFERENT next step (without telling it to stop, naming line "
    "numbers, or referencing unseen identifiers); set \"nudge\" to \"\" when stuck is false.\n"
    "Return ONLY a JSON object: {\"stuck\": bool, \"confidence\": <0.0-1.0>, "
    "\"reason\": \"<short leakage-safe justification>\", \"nudge\": \"<redirect or empty>\"}."
)


def llm_stuck_judge(digest, runner, model="agent", with_nudge=False):
    """The sampled stuck judge. `digest` is a leakage-safe recent-process string
    (`SpiralState.recent_digest()`). Returns a verdict dict
    {stuck: bool|None, confidence: float, reason: str, _tokens: number}; stuck=None on any
    failure (the caller treats None/ambiguous by falling back to the envelope gate).
    When `with_nudge` is true and the model includes a non-empty `nudge` alongside
    `stuck=true`, `_nudge` carries it so callers can skip a second redirect-writer call.
    `_tokens` is the REAL token cost of this paid judge call (input+output+cache, from the
    runner's usage block; a char-based approximation if the runner couldn't recover it) so
    the caller can charge it to the ledger. 0 when no call was made."""
    if runner is None or not digest:
        return {"stuck": None, "confidence": 0.0, "reason": "no-runner", "_tokens": 0}
    system = STUCK_JUDGE_WITH_NUDGE_SYSTEM if with_nudge else STUCK_JUDGE_SYSTEM
    prompt = (system + "\n\nRecent process digest:\n" + digest
              + "\n\nReturn the JSON verdict:")
    try:
        raw, _real = runner(prompt, model)
    except Exception as e:
        return {"stuck": None, "confidence": 0.0,
                "reason": f"runner-error:{e}"[:60], "_tokens": 0}
    raw = raw or ""
    # EFFECTIVE (cache-aware) cost when the runner recovered usage; else approximate on the
    # SAME basis: an uncached one-shot -> prompt is fresh input (weight 1), reply is output
    # (weight 5). Keeps mechanism cost comparable to exec's cache-aware effective tokens.
    _tokens = _runner_effective_or_approx(runner, prompt, raw, _real)
    verdict = {"stuck": None, "confidence": 0.0, "reason": "", "_tokens": _tokens}
    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
            verdict["stuck"] = (bool(obj["stuck"]) if "stuck" in obj
                                and obj["stuck"] is not None else None)
            verdict["confidence"] = max(0.0, min(1.0, float(obj.get("confidence", 0) or 0)))
            verdict["reason"] = str(obj.get("reason", ""))[:160]
            if with_nudge and verdict["stuck"]:
                nudge = str(obj.get("nudge") or "").strip()[:1200]
                if nudge:
                    verdict["_nudge"] = nudge
        except Exception:
            pass
    return verdict


NUDGE_WRITER_SYSTEM = (
    "An AI coding agent is stuck in a REPETITIVE, UNPRODUCTIVE loop partway through one "
    "task in a software repo. You are given a leakage-safe digest of its RECENT ACTIONS "
    "only (tool names, shell commands, searched patterns, edited file BASENAMES, and "
    "error SIGNATURES — never file contents or the task description). Write a SHORT "
    "redirect (2-4 sentences) addressed to the agent that: (1) names the specific "
    "repetitive pattern you actually see in the digest (e.g. re-editing the same file, "
    "re-running the same test to the same failure); (2) proposes a MATERIALLY DIFFERENT "
    "next step (a different file/root-cause to consider, re-reading the failing test's "
    "expectation, verifying a new hypothesis before editing again).\n"
    "HARD CONSTRAINTS: Do NOT tell it to stop, give up, or reduce effort — only redirect. "
    "Do NOT infer the task's solution, name specific line numbers to change, or reference "
    "any code/identifier you cannot see in the digest. Output ONLY the redirect as plain "
    "text — no preamble, no JSON, no headings."
)


def llm_nudge_writer(digest, runner, model="agent"):
    """The sampled redirect writer. `digest` is the same leakage-safe recent-process
    string the judge reads. Returns {"text": str, "_tokens": number}: a targeted redirect and
    the effective token cost of the call, on the judge's basis. On any failure returns
    {"text": "", "_tokens": 0} so the caller falls back to `rules.nudge_text`."""
    if runner is None or not digest:
        return {"text": "", "_tokens": 0}
    prompt = (NUDGE_WRITER_SYSTEM + "\n\nRecent process digest:\n" + digest
              + "\n\nWrite the redirect:")
    try:
        raw, _real = runner(prompt, model)
    except Exception:
        return {"text": "", "_tokens": 0}
    raw = (raw or "").strip()
    if not raw:
        return {"text": "", "_tokens": 0}
    # EFFECTIVE (cache-aware) cost when the runner recovered usage; else approximate on the
    # SAME model-priced basis as llm_stuck_judge.
    _tokens = _runner_effective_or_approx(runner, prompt, raw, _real)
    return {"text": raw[:1200], "_tokens": _tokens}
