# TokenEconomizer on ZCode

ZCode (Z.ai) support is experimental. It was written against ZCode `0.17.1` on macOS from its
hook code, its command-line help and the schema of its session store. It has not yet been run
under a live ZCode session with this plugin's hook file: the "Verify it" steps below do that.

## What works, and what does not

| Mechanism | ZCode | Why |
|---|---|---|
| Micro-skills (repository facts at session start) | yes, unverified live | `SessionStart` and `UserPromptSubmit` take `additionalContext`; ZCode adds it to the conversation as a `hook_context` entry |
| Session cost / effective tokens | yes | `model_usage` in the store; the cached part is taken out of `input_tokens`, which counts it |
| Loop guard (redirect a failing loop) | partially | `PostToolUse` takes `additionalContext`; the judge needs `zcode -p`, which is off by default |
| History import (earlier sessions) | no | the store holds them, but no importer reads it yet |
| Tool pruning | no | ZCode has no per-tool setting a hook can write |

## How a session is read

ZCode keeps every session in one SQLite file, `<ZCODE_STORAGE_DIR or ~/.zcode>/cli/db/db.sqlite`
(tables `session`, `message`, `part`, `tool_usage`, `model_usage`). The hook's own
`transcript_path` is no log: ZCode writes the one prompt or answer of the event to a temporary
file and deletes it when the hook returns. The fold therefore opens the store read-only,
copies one session's tool calls, exit codes and token usage into a file of the plugin's
(`<state>/zcode-sessions/<session id>.json`), and reads that. The copy has no words: a prompt is
counted and not kept, and what the model said or reasoned is not read.

ZCode has no `SessionEnd`. A session is folded by the next session start in its repository once
it has been quiet, as on the Antigravity CLI.

## How ZCode is told from Claude Code

ZCode's payload has `session_id`, `tool_name`, `tool_input` and the other Claude names, so
nothing in them tells it apart. Grok Build also sends the camelCase `hookEventName`. What only
ZCode sends is the transcript: both `transcript_path` and `transcriptPath` hold
`<tmpdir>/zcode-hook-<random>/transcript.jsonl`, and `detect` takes a payload for ZCode only when
both are that, and equal. The environment is not a mark: `ZCODE_SESSION_ID` is set for ZCode's
hooks and shell, and an agent started from that shell inherits it, so it only chooses the host of
a `te` command run in a shell (`default_host`), after every other agent's variable. The hook file
passes `--host zcode`, so detection is the fallback.

## Install

ZCode reads workspace hooks from `zcode.json` at the root of a repository (its schema is
`{"hooks": {"enabled": true, "events": {...}}}`) and runs a workspace hook only after the person
trusts it (`zcode hooks trust review`). `plugin/hooks/zcode-hooks.json` holds the five entries;
add them to the `events` of the repository's `zcode.json`, after any entry that guards a tool,
and point the command at the clone if it is not in `~/.tokeneconomizer/src`.

## Verify it

1. Start `zcode` in a repository where the entries are trusted. Run
   `~/.tokeneconomizer/src/plugin/scripts/te --host zcode status`: a session is listed after its
   first hook.
2. After a session that ran a shell command, run `te --host zcode fold` (or start another session
   in the same repository after a quiet period). `te --host zcode status` shows the folded
   session with its prompts and tokens.
3. To see that a learned fact reaches the model, ask ZCode to repeat what its session start added.

## Side calls

`te` asks the model for facts and for a verdict on a stuck turn through `zcode -p`, in a throwaway
directory, in `plan` mode (its `-p` default is `yolo`), with the acting tools removed by
`--disallowed-tools`. That option is a deny list, and a tool an MCP server adds is not on it;
the call also writes a session into the store and reports no usage. So it is off until
`te --host zcode config set zcode_sidecall=on` (or `TE_ZCODE_SIDECALL=1`). `zcode -p` has no
`--model` flag, so there is no `zcode_paid_model`: the call uses the model in ZCode's settings.
What `zcode -p` prints has not been checked against a real model: the plugin takes its stdout.

## Known limits

- Not run end to end under ZCode; the reader is tested on a synthetic store whose JSON keys were
  read off a real one.
- ZCode's `hook_context` entries after a resumed session were not traced, so `keeps_start_context`
  keeps its default (the context is not added again on resume).
- A subagent's own hook calls carry its session id; its rows are counted in the parent's usage,
  and a session forked from another is a session of its own.
- `model_usage.cache_creation_input_tokens` was 0 in every row seen, so cache writes are not
  priced.
