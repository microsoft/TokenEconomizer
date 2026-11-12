# TokenEconomizer on Kimi Code

Kimi Code 2.x is not supported. It is observe-only: there is no adapter, no reader and no hook
entry, and `tests/test_kimi.py` pins that. This note says why and what is needed.

## What is known (Kimi Code 2.1.1)

- The binary is `~/.kimi-code/bin/kimi`. The Python `kimi_cli` in `~/.kimi/` is the older 1.x.
- A session is `~/.kimi-code/sessions/wd_<name>_<hash>/session_<uuid>/agents/main/wire.jsonl`, one
  JSON record per line with a `type` key. The types seen are `metadata`, `config.update` and
  `tools.set_active_tools`. No record of a prompt, a tool call or a tool result has been seen.
- Hooks are `[[hooks]]` in `~/.kimi-code/config.toml` only. Events include `SessionStart`,
  `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop` and `SessionEnd`. Exit 2 blocks on
  `UserPromptSubmit`, `PreToolUse` and `Stop`; other failures let the call go on. There is no
  `updatedInput`.
- Context: the docs say stdout of an exit-0 hook "may be appended to context". There is no
  `additionalContext` field. This has not been tried.
- Side call: `kimi -p <prompt>` runs under the `auto` permission policy and has no flag that
  removes tools, so a side call could act on text a repository controls.

## What is missing, and why no reader was written

A reader written from the three record types above would have to guess where a prompt and a tool
result are, and would pass its own tests while counting the wrong things. Two pieces of evidence
come first. Each has a skipped test in `tests/test_kimi.py` that becomes a real one.

1. **A multi-turn `wire.jsonl`.** In one Kimi Code session, send two prompts; make one of them
   run a shell command and read a file. Then list the record `type` and the key names of every
   line of that session's `agents/main/wire.jsonl` (types and key names only, no text) and save
   them as `tests/fixtures/kimi_wire_types.json`. A one-line `jq` over the file is enough:
   `jq -c '[.type, (keys)]' wire.jsonl`.
2. **An injection test.** In a scratch directory, add a `UserPromptSubmit` `[[hooks]]` entry to
   `~/.kimi-code/config.toml` whose command prints a made-up sentence and exits 0. Ask Kimi Code
   to repeat the sentence the hook printed. Record whether it can.

If the sample has the three records and the injection works, the adapter is the ZCode one with
a `wire.jsonl` reader (`trace/kimi.py`) instead of the SQLite one, and plain-stdout context. If
the injection fails, Kimi stays observe-only with micro-skills' block and the loop guard's judge
off: nothing can be injected, and its side call cannot be restricted.
