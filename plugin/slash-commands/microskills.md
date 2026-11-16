---
description: Show the repository facts (micro-skills) TokenEconomizer learned and injects at session start
---
Run the command for this shell, then show me its output verbatim in a code block, with no commentary.

In Copilot CLI on Windows, use the PowerShell tool:

```powershell
$root = if ($env:TOKENECONOMIZER_HOME) { $env:TOKENECONOMIZER_HOME } else { Join-Path $HOME '.tokeneconomizer' }
$launcher = Get-Content -LiteralPath (Join-Path $root 'bin\te.json') -Raw -ErrorAction Stop | ConvertFrom-Json
$OutputEncoding = [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding
& $launcher.python -X utf8 $launcher.bootstrap cli --host copilot --cwd (Get-Location).Path microskills
```

Otherwise, run this command with the Bash tool:

```
te="${CLAUDE_PLUGIN_ROOT}/scripts/te"; [ -x "$te" ] || te="${TOKENECONOMIZER_HOME:-$HOME/.tokeneconomizer}/bin/te"; "$te" --cwd "$(pwd)" microskills
```
