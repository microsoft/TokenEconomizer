---
name: "savings"
description: >
  Show, in three lines, how much TokenEconomizer has saved in this repository. Use when the user asks how much TokenEconomizer has saved, or whether it is worth it.
---

# TokenEconomizer: savings

Run this command in the repository's root directory:

```
"${TOKENECONOMIZER_HOME:-$HOME/.tokeneconomizer}/bin/te" --host codex --cwd "$(pwd)" savings
```

If it says "Not measured yet", run the same command with `toolprune measure` in place of `savings`, then run `savings` again. Show the final output of `savings` verbatim in a code block and add nothing else.

`savings` reads only TokenEconomizer's own state (under `~/.tokeneconomizer`) and makes no model call.
`toolprune measure` renders the request twice to compare sizes.
