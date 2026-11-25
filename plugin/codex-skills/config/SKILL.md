---
name: "config"
description: >
  Show or change TokenEconomizer's settings: turn micro-skills, tool pruning or the loop guard on or off, or tune them, for this repository or (with --global) every repository. Use when the user asks to configure, enable or disable TokenEconomizer or one of its mechanisms.
---

# TokenEconomizer: config

Run the block for your shell from the repository root.

## Windows PowerShell

The extensionless `te` file is a shell script. Use the Python launcher recorded by the hook.

```powershell
$root = if ($env:TOKENECONOMIZER_HOME) { $env:TOKENECONOMIZER_HOME } else { Join-Path $HOME '.tokeneconomizer' }
$meta = Join-Path $root 'bin\te.json'
if (-not (Test-Path -LiteralPath $meta -PathType Leaf)) {
    throw "Missing $meta. Start a new Codex session in this repository to initialize TokenEconomizer."
}
$launcher = Get-Content -LiteralPath $meta -Encoding UTF8 -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
& $launcher.python -X utf8 $launcher.bootstrap cli --host codex --cwd (Get-Location).Path config show
```

## Bash or sh

```sh
"${TOKENECONOMIZER_HOME:-$HOME/.tokeneconomizer}/bin/te" --host codex --cwd "$(pwd)" config show
```

Use `show` unless the user asked to change a setting; then `set KEY=VALUE ...` or `unset KEY ...`, adding `--global` only if they asked for every repository. Show the output verbatim in a code block; if a setting was rejected, say why in one sentence.

Changing a setting (`set`, `unset`) writes TokenEconomizer's own state under `~/.tokeneconomizer`, outside the workspace, which the sandbox keeps read-only: run that command with `sandbox_permissions` set to `"require_escalated"` and a one-line `justification`, so that Codex asks the user to approve it. `show` reads only and needs no approval.

The command reads and writes only TokenEconomizer's own state (under `~/.tokeneconomizer`); it
makes no model call. If it reports that the launcher does not exist, the plugin has not run in a session yet:
ask the user to start a new Codex session in this repository first.
