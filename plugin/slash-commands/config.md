---
description: Show or change TokenEconomizer's settings - turn each mechanism on or off, or tune it
argument-hint: "[show | set KEY=VALUE ... [--global] | unset KEY ... [--global]]"
---
The arguments I typed after the command are: "$ARGUMENTS" (it shows every setting when there are none).
Run the command for this shell, then show me its output verbatim in a code block.

In Copilot CLI on Windows, use the PowerShell tool:

```powershell
$root = if ($env:TOKENECONOMIZER_HOME) { $env:TOKENECONOMIZER_HOME } else { Join-Path $HOME '.tokeneconomizer' }
$meta = Join-Path $root 'bin\te.json'
$OutputEncoding = [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding
if (Test-Path -LiteralPath $meta) {
  $launcher = Get-Content -LiteralPath $meta -Raw -ErrorAction Stop | ConvertFrom-Json
  & $launcher.python -X utf8 $launcher.bootstrap cli --host copilot --cwd (Get-Location).Path config $ARGUMENTS
} elseif ($env:COPILOT_PLUGIN_ROOT -and (Test-Path -LiteralPath (Join-Path $env:COPILOT_PLUGIN_ROOT 'scripts\te_bootstrap.py'))) {
  $py = if ($env:TE_PYTHON) { $env:TE_PYTHON } else { 'python' }
  & $py -X utf8 (Join-Path $env:COPILOT_PLUGIN_ROOT 'scripts\te_bootstrap.py') cli --host copilot --cwd (Get-Location).Path config $ARGUMENTS
} else {
  $err = Join-Path $root 'errors.log'
  $detail = if (Test-Path -LiteralPath $err) { (Get-Content -LiteralPath $err -Tail 3) -join "`n" } else { "start a new chat session or install from a local clone / bundle so the hook writes $meta" }
  [Console]::Error.WriteLine("TokenEconomizer is not initialized yet ($meta missing): $detail")
  cmd /c exit 1
}
```

Otherwise, run this command with the Bash tool:

```
te="${CLAUDE_PLUGIN_ROOT}/scripts/te"; [ -x "$te" ] || te="${TOKENECONOMIZER_HOME:-$HOME/.tokeneconomizer}/bin/te"; "$te" --cwd "$(pwd)" config $ARGUMENTS
```

It changes a setting only when I typed `set` or `unset` in those arguments; never add them yourself. If a setting was rejected, say why in one sentence.
