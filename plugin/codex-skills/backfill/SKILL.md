---
name: "backfill"
description: >
  TokenEconomizer: learn this repository's history from the agent's own session logs that predate the plugin -- fold earlier Claude Code, Codex or Copilot sessions in this repository so the facts, the tool evidence and the loop guard's envelope start from them. Use when the user asks to backfill, to learn from existing sessions or history, or wonders why nothing was learned before the plugin was installed.
---

# TokenEconomizer: backfill

Run this command in the repository's root directory:

```
"${TOKENECONOMIZER_HOME:-$HOME/.tokeneconomizer}/bin/te" --host codex --cwd "$(pwd)" backfill ARGS
```

Use no ARGS unless the user asked for `--limit N` (cap the sessions read, newest first) or
`--force` (also read logs still being written). Show the output verbatim in a code block.

It makes no model call and reads only logs already on this machine; it writes TokenEconomizer's
own state under `~/.tokeneconomizer`, outside the workspace, which the sandbox keeps read-only:
run it with `sandbox_permissions` set to `"require_escalated"` and a one-line `justification`, so
that Codex asks the user to approve it.

Say in one line how many sessions it read. If it reports `sessions: 0` because logs were still
being written, say the sessions still open are folded by their own end or the next session start.
If it reports `skipped` with `fold in progress`, another fold is running: run it again shortly.
