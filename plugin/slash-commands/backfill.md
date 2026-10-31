---
description: Learn this repository's history from the agent's own session logs that predate the plugin
argument-hint: "[--limit N] [--force]"
---
The arguments I typed after the command are: "$ARGUMENTS" (it reads every earlier session it can find when there are none).
Run this exact command with the Bash tool, then show me its output verbatim in a code block:

```
te="${CLAUDE_PLUGIN_ROOT}/scripts/te"; [ -x "$te" ] || te="${TOKENECONOMIZER_HOME:-$HOME/.tokeneconomizer}/bin/te"; "$te" --cwd "$(pwd)" backfill $ARGUMENTS
```

It reads Claude Code's own transcripts of earlier sessions in this repository (`~/.claude/projects/`), folds them with the same code a session's end uses, and makes **no model call**. Pass on `--limit N` or `--force` only when I typed them; never add them yourself.

When it finishes, say in one line how many sessions it read, and that they now count toward the facts, the tool evidence and the loop guard's envelope. If `sessions` is 0 because a log was still being written, say that the sessions still open are folded by their own end or the next session start. If the output shows `skipped` with `fold in progress`, say another fold is running and to run the command again shortly.
