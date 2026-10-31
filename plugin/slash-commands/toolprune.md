---
description: Review the tools TokenEconomizer left out of this repository's sessions, give them back, or apply a waiting proposal
argument-hint: "[show | apply [TOOL ...] | restore [TOOL ...] | refresh-catalog | measure [--applied]]"
---
The arguments I typed after the command are: "$ARGUMENTS" (it shows the proposal when there are none).
Run this exact command with the Bash tool, then show me its output verbatim in a code block:

```
te="${CLAUDE_PLUGIN_ROOT}/scripts/te"; [ -x "$te" ] || te="${TOKENECONOMIZER_HOME:-$HOME/.tokeneconomizer}/bin/te"; "$te" --cwd "$(pwd)" toolprune $ARGUMENTS
```

It applies or restores the deny list only when I typed `apply` or `restore` in those arguments; never add them yourself. If it shows a proposal waiting for `toolprune apply` (under `toolprune=propose`, or one the plugin could not write), end with one line telling me how to apply it (`/tokeneconomizer:toolprune apply`) and that it takes effect from the next session. If it shows a pending list, say that it is written by itself once this repository's sessions have been idle, or now with `/tokeneconomizer:toolprune apply`.
