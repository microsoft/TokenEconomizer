---
name: "status"
description: >
  Show what TokenEconomizer has learned for this repository, what it has done, and what this repository's sessions cost. Use when the user asks about TokenEconomizer, its status, its savings or overhead, or what it learned here.
---

# TokenEconomizer: status

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
& $launcher.python -X utf8 $launcher.bootstrap cli --host codex --cwd (Get-Location).Path status
```

## Bash or sh

```sh
"${TOKENECONOMIZER_HOME:-$HOME/.tokeneconomizer}/bin/te" --host codex --cwd "$(pwd)" status
```

Show the output verbatim in a code block. Add nothing unless it contains a warning; then explain the warning in one sentence.

The command reads and writes only TokenEconomizer's own state (under `~/.tokeneconomizer`); it
makes no model call. If it reports that the launcher does not exist, the plugin has not run in a session yet:
ask the user to start a new Codex session in this repository first.
