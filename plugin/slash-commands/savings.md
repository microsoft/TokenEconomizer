---
description: Show what tool pruning leaves out of this repository's sessions and its last measurement, in three lines
---
Run this exact command with the Bash tool:

```
te="${CLAUDE_PLUGIN_ROOT}/scripts/te"; [ -x "$te" ] || te="${TOKENECONOMIZER_HOME:-$HOME/.tokeneconomizer}/bin/te"; "$te" --cwd "$(pwd)" savings
```

Show its output verbatim in a code block and add nothing else. If it says the list has not been measured, end by asking me whether to measure it, with the cost the output gives; run `/tokeneconomizer:toolprune measure --applied` only if I say yes, then run `savings` again.
