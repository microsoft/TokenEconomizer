---
description: Show or change TokenEconomizer's settings - turn each mechanism on or off, or tune it
argument-hint: "[show | set KEY=VALUE ... [--global] | unset KEY ... [--global]]"
---
The arguments I typed after the command are: "$ARGUMENTS" (it shows every setting when there are none).
Run the command for this shell, then show me its output verbatim in a code block.

In Copilot CLI on Windows, use the PowerShell tool:

```powershell
$root = if ($env:TOKENECONOMIZER_HOME) { $env:TOKENECONOMIZER_HOME } else { Join-Path $HOME '.tokeneconomizer' }
$launcher = Get-Content -LiteralPath (Join-Path $root 'bin\te.json') -Raw -ErrorAction Stop | ConvertFrom-Json
$OutputEncoding = [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding
& $launcher.python -X utf8 $launcher.bootstrap cli --host copilot --cwd (Get-Location).Path config $ARGUMENTS
```

Otherwise, run this command with the Bash tool:

```
te="${CLAUDE_PLUGIN_ROOT}/scripts/te"; [ -x "$te" ] || te="${TOKENECONOMIZER_HOME:-$HOME/.tokeneconomizer}/bin/te"; "$te" --cwd "$(pwd)" config $ARGUMENTS
```

It changes a setting only when I typed `set` or `unset` in those arguments; never add them yourself. If a setting was rejected, say why in one sentence.
