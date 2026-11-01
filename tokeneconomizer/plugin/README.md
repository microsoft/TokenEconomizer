# Plugin runtime

What the plugin's hooks and the `te` command run. The agent launches itself; this package runs
only when the agent calls a hook, each call a fresh process, so what is learned lives on disk and
every handler returns fast and fails open.

What happens at each hook, and how each agent was verified to behave, is in
[`docs/PLUGIN.md`](../../docs/PLUGIN.md); `controller.py` implements it.

| Module | |
|---|---|
| `hook.py`, `cli.py` | entry points: the hook dispatcher and `te` |
| `controller.py` | what happens at each hook |
| `hosts.py` | per-agent hook payloads and outputs (Claude Code, Codex, Copilot CLI, Pi, opencode, Cursor) |
| `fold.py` | a finished session through `PrefixStage` and `ToolingStage` |
| `denylist.py` | writing the deny list into each agent's settings (by itself under `auto`, on `toolprune apply` under `propose`), and taking it back |
| `toolcheck.py` | asking the agent CLIs what a session loads, and what a deny list removed |
| `config.py` | settings, their sources, and the earlier names they are still read under |
| `context.py`, `paths.py`, `store.py` | where state lives, which repository a session is in, locked atomic JSON |
| `repos.py` | which repository's state a session belongs to over time: session pins, `te repos`, `repos migrate` and `merge` |
| `adapter.py` | the `DomainAdapter` for a real repository |
| `accounting.py` | the session ledger |
| `sidecall.py`, `judge.py` | the plugin's model calls (the loop guard's judge, micro-skills' proposer) through the agent's own CLI, off the hook's critical path |
| `repomap.py`, `repomap_gen.py` | the opt-in repository map |
| `probe.py` | `te --host copilot probe`: the plugin end to end in real Copilot sessions |

The installable plugin directory -- manifests, hook files and launchers -- is `plugin/` at the
repository root; see `plugin/README.md` there and `docs/PLUGIN.md`.
