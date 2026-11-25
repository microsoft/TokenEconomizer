# TokenEconomizer on Kimi Code

The Kimi Code 2.6 adapter observes the tool trajectory and token usage. Hook output is empty,
including on errors. Model side calls are disabled for this host.

## Tested contract

Kimi Code 2.6.2 was run for two turns in one isolated session with a scripted loopback model.
A failed Bash command and a successful Read produced both a call and a result. The hook payload
identifies the client as `kimi_code_cli`; the session ID includes the `session_` prefix. The payload
does not include the log path. The adapter finds that session's only log at
`$KIMI_CODE_HOME/sessions/wd_*/session_<uuid>/agents/main/wire.jsonl`. The default data directory
is `~/.kimi-code`.

`turn.prompt` records the user prompt. `context.append_loop_event` carries `tool.call` and
`tool.result`. A `usage.record` with `usageScope: "turn"` reports the tokens for one model step.
The reader counts uncached input, cache reads, cache creation and output separately.

Kimi included an exit-zero `UserPromptSubmit` hook's stdout in a model request. It did not do this
for `SessionStart`. The adapter emits neither for observation. Print mode exposed acting tools
to the model, so the adapter does not start a side call. The two print runs did not emit
`Stop` or `SessionEnd`. Run `te --host kimi fold --session <id>` to fold a session that did not emit an end.

## Enable for an observation run

Set TokenEconomizer to shadow mode. In Kimi Code's own `config.toml`, add entries for
`SessionStart`, `UserPromptSubmit`, `PostToolUse` and `PostToolUseFailure`. Keep any existing
authorization hooks first. Use the absolute path to the pinned checkout's
`plugin/scripts/te-hook`:

```toml
[[hooks]]
event = "SessionStart"
command = "/path/to/TokenEconomizer/plugin/scripts/te-hook --host kimi >/dev/null"
timeout = 5

[[hooks]]
event = "UserPromptSubmit"
command = "/path/to/TokenEconomizer/plugin/scripts/te-hook --host kimi >/dev/null"
timeout = 5

[[hooks]]
event = "PostToolUse"
command = "/path/to/TokenEconomizer/plugin/scripts/te-hook --host kimi >/dev/null"
timeout = 5

[[hooks]]
event = "PostToolUseFailure"
command = "/path/to/TokenEconomizer/plugin/scripts/te-hook --host kimi >/dev/null"
timeout = 5
```

The fixture in `tests/fixtures/kimi-2.6.2-wire.jsonl` contains only the observations needed by
the tests, from the synthetic project. No provider account or real session is needed to run
the tests.
