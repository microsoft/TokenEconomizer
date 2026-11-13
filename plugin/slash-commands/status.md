---
description: Show what TokenEconomizer has learned for this repository and what it has done
---
Run the command for this shell, then show me its output verbatim in a code block. Add nothing unless the output contains a warning, in which case explain the warning in one sentence.

In Copilot CLI on Windows, use the PowerShell tool:

```powershell
$root = if ($env:TOKENECONOMIZER_HOME) { $env:TOKENECONOMIZER_HOME } else { Join-Path $HOME '.tokeneconomizer' }
& (Join-Path $root 'bin\te.ps1') --host copilot --cwd (Get-Location).Path status
```

Otherwise, run this command with the Bash tool:

```
te="${CLAUDE_PLUGIN_ROOT}/scripts/te"; [ -x "$te" ] || te="${TOKENECONOMIZER_HOME:-$HOME/.tokeneconomizer}/bin/te"; "$te" --cwd "$(pwd)" status
```
