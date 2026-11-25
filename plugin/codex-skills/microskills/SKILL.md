---
name: "microskills"
description: >
  Show the repository facts (micro-skills) TokenEconomizer learned and injects at the start of each session here, and the ones still forming. Use when the user asks what TokenEconomizer learned or injects, or about its facts or micro-skills.
---

# TokenEconomizer: microskills

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
& $launcher.python -X utf8 $launcher.bootstrap cli --host codex --cwd (Get-Location).Path microskills
```

## Bash or sh

```sh
"${TOKENECONOMIZER_HOME:-$HOME/.tokeneconomizer}/bin/te" --host codex --cwd "$(pwd)" microskills
```

Show the output verbatim in a code block, with no commentary.

The command reads and writes only TokenEconomizer's own state (under `~/.tokeneconomizer`); it
makes no model call. If it reports that the launcher does not exist, the plugin has not run in a session yet:
ask the user to start a new Codex session in this repository first.
