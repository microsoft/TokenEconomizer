---
description: Review the tools TokenEconomizer left out of this repository's sessions, give them back, or apply a waiting proposal
argument-hint: "[show | apply [TOOL ...] | restore [TOOL ...] | refresh-catalog | measure]"
---
The arguments I typed after the command are: "$ARGUMENTS" (it shows the proposal when there are none).
Run the command for this shell, then show me its output verbatim in a code block.

In Copilot CLI on Windows, use the PowerShell tool:

```powershell
$root = if ($env:TOKENECONOMIZER_HOME) { $env:TOKENECONOMIZER_HOME } else { Join-Path $HOME '.tokeneconomizer' }
$meta = Join-Path $root 'bin\te.json'
$OutputEncoding = [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding
if (Test-Path -LiteralPath $meta) {
  $launcher = Get-Content -LiteralPath $meta -Raw -ErrorAction Stop | ConvertFrom-Json
  & $launcher.python -X utf8 $launcher.bootstrap cli --host copilot --cwd (Get-Location).Path toolprune $ARGUMENTS
} elseif ($env:COPILOT_PLUGIN_ROOT -and (Test-Path -LiteralPath (Join-Path $env:COPILOT_PLUGIN_ROOT 'scripts\te_bootstrap.py'))) {
  $py = if ($env:TE_PYTHON) { $env:TE_PYTHON } else { 'python' }
  & $py -X utf8 (Join-Path $env:COPILOT_PLUGIN_ROOT 'scripts\te_bootstrap.py') cli --host copilot --cwd (Get-Location).Path toolprune $ARGUMENTS
} else {
  $err = Join-Path $root 'errors.log'
  $detail = if (Test-Path -LiteralPath $err) { (Get-Content -LiteralPath $err -Tail 3) -join "`n" } else { "start a new chat session or install from a local clone / bundle so the hook writes $meta" }
  [Console]::Error.WriteLine("TokenEconomizer is not initialized yet ($meta missing): $detail")
  cmd /c exit 1
}
```

Otherwise, run this command with the Bash tool:

```
te="${CLAUDE_PLUGIN_ROOT}/scripts/te"; [ -x "$te" ] || te="${TOKENECONOMIZER_HOME:-$HOME/.tokeneconomizer}/bin/te"; "$te" --cwd "$(pwd)" toolprune $ARGUMENTS
```

It applies or restores the deny list only when I typed `apply` or `restore` in those arguments; never add them yourself. If it shows a proposal waiting for `toolprune apply` (under `toolprune=propose`, or one the plugin could not write), end with one line telling me how to apply it (`/tokeneconomizer:toolprune apply`) and that it takes effect from the next session. If it shows a pending list, say that it is written by itself once this repository's sessions have been idle, or now with `/tokeneconomizer:toolprune apply`.
