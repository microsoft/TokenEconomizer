---
name: "savings"
description: >
  Show what tool pruning leaves out of this repository's sessions, its last measurement, and how many recorded sessions ran with it. Use when the user asks what TokenEconomizer's tool pruning does here, or how much it has saved.
---

# TokenEconomizer: savings

Run this command in the repository's root directory:

```
"${TOKENECONOMIZER_HOME:-$HOME/.tokeneconomizer}/bin/te" --host codex --cwd "$(pwd)" savings
```

Show its output verbatim in a code block and add nothing else. If it says the list has not been measured, end by asking the user whether to measure it; run the same command with `toolprune measure --applied` in place of `savings` only if they say yes, then run `savings` again.

`savings` reads only TokenEconomizer's own state (under `~/.tokeneconomizer`) and makes no model call.
`toolprune measure --applied` renders the request twice, locally, to compare sizes.
