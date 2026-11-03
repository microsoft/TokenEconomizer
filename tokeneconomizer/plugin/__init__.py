"""The plugin runtime: the three mechanisms inside a person's own sessions, through the hooks
each agent already exposes.

The agent launches itself; this package runs only when the agent calls a hook, each call a
fresh, short-lived process. So what is learned lives on disk, and every handler returns fast and
fails open.

  * state        -- `paths`, `store`, `config`, `context`: per-repository, per-agent state that
                    survives hook processes, sessions and plugin updates;
  * agents       -- `hosts`: hook payloads in, hook outputs out, for Claude Code, Codex, Copilot
                    CLI and Pi; `adapter`: the `DomainAdapter` for a real repository;
  * at each hook -- `controller`: inject the learned facts at session start, guard each
                    prompt-turn call by call, and hand a finished session to `fold`;
  * after a session -- `fold`: run `PrefixStage` and `ToolingStage` over the finished session,
                    off the agent's critical path; `denylist` writes an approved proposal into
                    the agent's own settings file; `history`: a repository's sessions from
                    before the plugin, from the agent's own logs, folded once to learn from;
  * model calls  -- `sidecall`, `judge`: LLM calls through the agent's own CLI (the proposer
                    with micro-skills on, the judge with the loop guard on), charged to the
                    session's ledger (`accounting`);
  * commands     -- `hook.main` (what the hooks execute) and `cli.main` (`te`), with
                    `toolcheck`, `repomap` and `probe` behind it.

What differs from a benchmark run is written down where it differs: there is no grader, so the
loop guard's envelope learns from a "turn ended green" proxy; a "task" is a session for the
facts and for pruning but a prompt-turn for the guard; a pruned tool leaves through a deny list
the person approves, from the next session on; a redirect is attached to a tool result mid-turn.

Stdlib only: a hook must start in tens of milliseconds and depend on nothing the person did not
install. See docs/PLUGIN.md.
"""
