"""AgentRunner: the seam between the economizer and the task-execution harness.

The economizer is harness-agnostic. It never launches Docker, calls an LLM API, or shells
out to a coding-agent CLI itself -- that heavy, environment-specific machinery lives BELOW
this interface. The economizer needs only two capabilities from a runner:

  1. an OPTIMIZER call -- a small paid LLM request used by the mechanisms' between-task
     proposer / tool-judge / spiral-judge / nudge-writer. This is the ``runner(prompt,
     model) -> (text, effective_tokens)`` callable the mechanisms already expect.

  2. (optionally) a TASK EXECUTOR -- run one benchmark instance under a chosen arm and the
     economizer's per-task decisions (injected prefix, tool-exclusion set / capability
     mask, spiral params), returning a normalized row + the path to the saved transcript.

A concrete runner (Docker + claude/codex/copilot CLI + official gold grading) ships in the
repo's `te_harness` package -- the one the published rows were produced with;
its image and dataset paths are env-var placeholders. Supply a different one with
``--runner your_pkg.harness:make_runner``; nothing here changes, because the economizer
never learns which harness it is talking to.

`te_harness` is a SIBLING package, never imported from anywhere under `tokeneconomizer`, and
excluded from the distributed wheel. This package depends only on the protocol below, so
`import tokeneconomizer` cannot pull in Docker or an LLM SDK -- a property of the package
boundary, not of anyone's discipline in respecting it.

The row/transcript contract the mechanisms read:
  * transcript.jsonl written under ``run_dir_path`` (trace-reading seam consumes it);
  * row: a dict with at least the token-breakdown fields pricing.effective_tokens reads
    (input_tokens/cache_read_input_tokens/cache_creation_input_tokens/output_tokens or
    tokens_total), plus ``resolved``, ``num_turns``, and any mechanism telemetry fields
    the runner surfaces (aborted / spiral_* / codex_* / output_compression, ...).
"""
from typing import Optional, Protocol, Tuple, runtime_checkable


@runtime_checkable
class AgentRunner(Protocol):
    """Everything the economizer needs from the task-execution harness."""

    def optimizer_call(self, prompt: str, model: str = "agent") -> Tuple[str, Optional[float]]:
        """Run one small paid LLM request (the between-task proposer / judge / nudge).
        Returns (raw_text, effective_tokens). effective_tokens may be None if unknown."""
        ...

    def run_task(self, task: dict, *, arm: str, injected: Optional[str],
                 exclude_tools, run_dir_path: str, **run_kwargs) -> Tuple[dict, str]:
        """Execute one benchmark instance under `arm` with the economizer's per-task
        decisions applied. Returns (row, transcript_run_dir). Optional: a runner used only
        for optimizer calls need not implement this."""
        ...

    # ---- optional: stream-level lifecycle ------------------------------------------
    # Most harnesses own nothing wider than a single task and can ignore these. A harness
    # that owns the whole stream implements them, and the driver calls them around the
    # task loop (`hasattr`-guarded, so they stay optional): Web-Bench's evaluator, for
    # example, drives a project's 20 sequentially dependent tasks itself, so it must be
    # told the stream before the first task and shut down after the last.
    def begin_stream(self, tasks: list, **info) -> None:
        """Called once with the full task stream before the first run_task. `info` carries
        the driver's context (results_root, arm, repo, seed)."""
        ...

    def end_stream(self) -> None:
        """Called once after the last run_task, including when the loop raised."""
        ...


def as_optimizer_callable(runner):
    """Adapt an AgentRunner (or a bare callable) into the ``(prompt, model) ->
    (text, eff_tokens)`` callable the mechanisms expect. Returns None for None so the
    LLM-free paths (disabled proposer/judge) stay no-ops."""
    if runner is None:
        return None
    if hasattr(runner, "optimizer_call"):
        return lambda prompt, model="agent": runner.optimizer_call(prompt, model)
    if callable(runner):
        return runner
    raise TypeError(f"cannot use {runner!r} as an optimizer runner")
