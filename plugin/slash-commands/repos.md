---
description: List every repository TokenEconomizer keeps state for, or merge one repository's state into another
argument-hint: "[list | migrate | merge KEY [INTO]]"
---
The arguments I typed after the command are: "$ARGUMENTS" (it lists the repositories when there are none).
Run the command for this shell, then show me its output verbatim in a code block.

In Copilot CLI on Windows, use the PowerShell tool:

```powershell
$root = if ($env:TOKENECONOMIZER_HOME) { $env:TOKENECONOMIZER_HOME } else { Join-Path $HOME '.tokeneconomizer' }
& (Join-Path $root 'bin\te.ps1') --host copilot --cwd (Get-Location).Path repos $ARGUMENTS
```

Otherwise, run this command with the Bash tool:

```
te="${CLAUDE_PLUGIN_ROOT}/scripts/te"; [ -x "$te" ] || te="${TOKENECONOMIZER_HOME:-$HOME/.tokeneconomizer}/bin/te"; "$te" --cwd "$(pwd)" repos $ARGUMENTS
```

It moves state only when I typed `migrate` or `merge` in those arguments; never add them yourself. If the list shows state recorded by an earlier version, say in one sentence that it is sorted out at the next session start, or now with `/tokeneconomizer:repos migrate`.
