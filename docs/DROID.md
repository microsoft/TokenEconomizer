# TokenEconomizer on Factory Droid

Droid support is experimental. It was built and tested against Droid `0.233.0` on macOS; no
other version has been tested.

## What works, and what does not

| Mechanism | Droid | Why |
|---|---|---|
| Micro-skills (repository facts at session start) | yes | `SessionStart` supports `hookSpecificOutput.additionalContext` |
| History import (learn from earlier sessions) | yes | `~/.factory/sessions/<workspace>/<id>.jsonl` has the tool calls |
| Session cost / effective tokens | yes | `<id>.settings.json` holds `tokenUsage` beside the log |
| Loop guard (redirect a failing loop) | partially | `PostToolUse` supports `additionalContext`; the judge needs `droid exec`, which is off by default |
| Tool pruning | no | Droid's TUI has no persistent per-tool setting; `droid exec --only-tools` has no interactive equivalent (Factory issue #65) |

The loop guard still learns its envelope from live turns that end normally, still redirects on
its rule signals, and still sends the "still stuck" notice through the model's reply, because
Droid documents no `systemMessage`. The model judge is what needs a side call, and is therefore
off by default.

## Why a session may learn nothing

Micro-skills reports what it has learned per repository. A directory that is not a git
repository is keyed by its own path, so starting every session in a folder that holds many
unrelated projects mixes evidence from all of them, and a fact from one is contradicted by
another. Start Droid in the project's own folder when you want it to learn that project.

The free layer (`microskills=envfacts`) learns only from repeated environment failures: a command
that fails the same way several sessions in a row and a working alternative. It needs 3
confirmations and 4 sessions in which the fact was exercised before it injects anything. A
repository whose sessions never run tests, builds or failing commands will produce no fact, by
design. The paid layer (`microskills=envfacts+distiller`, or `on`) asks the model for repository
knowledge every few sessions, and needs a side call.

## Install

```sh
git clone https://github.com/microsoft/TokenEconomizer.git
cd TokenEconomizer
python scripts/build_plugin.py --droid
droid plugin marketplace add "$PWD/dist/tokeneconomizer-droid"
droid plugin install tokeneconomizer@tokeneconomizer-droid --scope user
```

The bundle is a native Droid plugin: `.factory-plugin/plugin.json` at its root, hooks in
`hooks/hooks.json` in Droid's top-level shape, commands in `commands/`, and the Python package
in `lib/` (Droid copies the plugin into its cache, so a symlink would break). Start a new Droid
session after installing: Droid snapshots hooks at startup.

Check it in the repository:

```sh
~/.tokeneconomizer/bin/te --host droid --cwd "$PWD" status
```

## Import the sessions you already ran

A plugin installed today knows nothing about yesterday's sessions. Droid's own log directory is
read by the same history import the other hosts use:

```sh
~/.tokeneconomizer/bin/te --host droid --cwd "$PWD" history import
```

The import runs only into a repository with nothing folded yet, folds oldest first, skips
subagent sessions (`callingSessionId`), sessions with no prompt and sessions still being written,
and makes no model call. `history_import=on` lets the first session start in a repository do it
by itself.

Their usage is recorded, but `te status` counts imported sessions apart from the plugin's own
(`+N from earlier history`), because they ran before it: the totals in `sessions ... effective
tokens` cover the sessions that end after the plugin is installed.

## Settings

The plugin's settings live in `~/.tokeneconomizer/config.json`:

```sh
te --host droid config set microskills=envfacts   # learn facts, no model call
te --host droid config set microskills=on         # and let a model propose more, every k sessions
te --host droid config set droid_sidecall=on      # allow the plugin's own model calls
te --host droid config set loopguard=nudge        # the judge stays off until side calls are on
te --host droid config set toolprune=off          # the only honest value on Droid
```

`droid_sidecall` is the switch for the calls described below; `TE_DROID_SIDECALL=1` in the
environment of the session does the same, for a Droid started from a terminal.

`TE_DISABLE=1` in a session's environment turns every hook into a no-op. `te --host droid doctor`
reports the Droid version, the session directory, side-call state and recent errors.

## Side calls are off by default

Micro-skills' paid proposer and the loop guard's judge normally call the agent's own CLI. On
Droid that is `droid exec`, which writes a session log of its own under `~/.factory/sessions`.
Droid has no documented flags for a pure completion (no tools, no hooks, no log), so the adapter
does not make that call unless it is allowed, by `config set droid_sidecall=on` or by
`TE_DROID_SIDECALL=1` in the session's environment. The setting is the one that also works from
the Factory desktop app, where a session's environment cannot be chosen. With the calls off,
`microskills=envfacts` still learns, and the loop guard keeps its rule-based backstop.

When it is on, the call runs at Droid's default read-only autonomy in a throwaway directory, and
its cost is read from the usage Droid records for it.

## What is verified, and what is not

- Verified locally: the session reader, prompt counting, the tool-name mapping, folding a
  session, the history import, usage and pricing, the hook's own JSON output, and the loop
  guard's redirect and notice paths. Each has a test in `tests/test_droid.py`.
- Documented by Factory and relied on here: `SessionStart` appends
  `hookSpecificOutput.additionalContext`; `UserPromptSubmit` and `PostToolUse` accept
  `additionalContext`; there is no `systemMessage` and no `PostToolUseFailure`.
- **Verified live on 2026-11-05**, on Droid 0.233.0: a new session (`bb18c4fc`) received the
  793-character block as `hookSpecificOutput.additionalContext`, and the model repeated it
  verbatim. The hook's JSON, Droid appending it to the model's context, and the model repeating
  it have now all been observed end to end. The block was three facts at the time; a fourth was
  added by the next fold.

Droid's session and hook formats are internal. The adapter fails open: any error answers `{}` and
writes one line to `~/.tokeneconomizer/errors.log`, so a Droid update can at worst stop the plugin
from learning, never break a session.

## Uninstall and rollback

```sh
droid plugin uninstall tokeneconomizer@tokeneconomizer-droid --scope user
droid plugin marketplace remove tokeneconomizer-droid
rm -rf ~/.tokeneconomizer
```

The plugin writes nothing to `~/.factory/settings.json`, `~/.factory/hooks.json` or any
repository file: its hooks come from the plugin directory, its state from `~/.tokeneconomizer`.
Deleting that directory forgets everything it learned.

## Compatibility

Tested on Droid `0.233.0` (macOS), including the live injection of a learned block on
2026-11-05. After a Droid update, run one session and check `te --host droid doctor` and
`~/.tokeneconomizer/errors.log`. If Droid changes the session log shape,
`te --host droid history import` will import nothing, which is the safe failure.
