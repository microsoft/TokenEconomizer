---
description: Earlier name of /tokeneconomizer:microskills - show the repository facts TokenEconomizer injects
---
Run the command for this shell, then show me its output verbatim in a code block, with no commentary.

In Copilot CLI on Windows, use the PowerShell tool:

```powershell
$root = if ($env:TOKENECONOMIZER_HOME) { $env:TOKENECONOMIZER_HOME } else { Join-Path $HOME '.tokeneconomizer' }
& (Join-Path $root 'bin\te.ps1') --host copilot --cwd (Get-Location).Path microskills
```

Otherwise, use the Bash tool:

```
te="${CLAUDE_PLUGIN_ROOT}/scripts/te"; [ -x "$te" ] || te="${TOKENECONOMIZER_HOME:-$HOME/.tokeneconomizer}/bin/te"; "$te" --cwd "$(pwd)" microskills
```
