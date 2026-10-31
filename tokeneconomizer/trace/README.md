# Trace

The one place that knows each agent's session-log format. Every mechanism reads a finished
session as an ordered list of `(tool_name, args, result_text, success)` calls through
`load_trajectory`, whichever agent wrote it.

| Module | |
|---|---|
| `calls.py` | `load_trajectory` (format sniffed per file), `call_cwds`, `tool_usage`, `strip_rtk_prefix` |
| `catalog.py` | the tool list a session loaded, from whichever source the agent publishes |
| `claude.py` | Claude Code transcripts: calls with the shell's directory, usage by message id, subagents, deferred tools |
| `codex.py` | `codex exec --json` streams; a session's rollout file, converted to that stream; usage |
| `copilot.py` | Copilot CLI event logs: calls, usage, prompt-turns, MCP servers |
| `cursor.py` | Cursor Agent hook events, normalized to a Claude-shaped trajectory; usage is unavailable |
| `pi.py` | Pi's session tree: the active branch, written as a Claude transcript; usage |
| `jsonl.py` | line and record readers shared by the above |

A new agent needs a reader here and nothing else: once its log reads as `(name, args, result,
ok)` calls, every mechanism handles it.
