---
description: Show, in three lines, how much TokenEconomizer has saved in this repository
---
Run this exact command with the Bash tool:

```
te="${CLAUDE_PLUGIN_ROOT}/scripts/te"; [ -x "$te" ] || te="${TOKENECONOMIZER_HOME:-$HOME/.tokeneconomizer}/bin/te"; "$te" --cwd "$(pwd)" savings
```

If it says "Not measured yet", run the same command with `toolprune measure` in place of `savings` (it makes two tiny model calls, about 1 cent), then run `savings` again. Show the final output of `savings` verbatim in a code block and add nothing else.
