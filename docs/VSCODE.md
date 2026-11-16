# Install TokenEconomizer for GitHub Copilot in VS Code

This guide configures TokenEconomizer for GitHub Copilot Chat in VS Code with **Copilot**
selected as the session target. It installs project hooks on Windows using native Python and
PowerShell. Neither GitHub CLI (`gh`) nor standalone Copilot CLI (`copilot`) is required.

The setup covers session hooks, transcript processing, and context injection. It enables
model-free micro-skills (`envfacts`) and leaves features that require a separate model call or
CLI-specific tool configuration disabled. See [feature scope](#feature-scope) for the limits.

## 1. Prepare VS Code and Copilot

You need:

- Windows, PowerShell 7, and Python 3.10 or newer.
- Git to clone TokenEconomizer, or an existing checkout.
- VS Code with GitHub Copilot Chat enabled and signed in.
- A local project folder where you want to use TokenEconomizer.

If Copilot is not set up yet, open VS Code, hover over the Copilot icon in the status bar,
select **Use AI Features**, and complete sign-in. See the
[official Copilot setup guide](https://code.visualstudio.com/docs/setup/copilot).
Free Copilot access was sufficient for the installation checks; you need remaining usage
for the model you select.

Open your project with **File > Open Folder**, then open **Chat** and start a new chat.
Choose **Copilot** in the **Session Target** control and **Agent** in the agent picker.
The session target and model are separate choices: selecting a model such as Copilot Fusion
does not select the session target. See
[choosing a harness](https://code.visualstudio.com/docs/agents/run/agent-harnesses).

This configuration uses the Copilot hook format. The older **Local** session target has a
different contract and cannot use these instructions unchanged. If **Copilot** is unavailable,
check your editor version and organization settings before continuing.

## 2. Download TokenEconomizer

Skip this step if you already have the repository. Otherwise, run in PowerShell 7, adjusting
the destination to a directory you own:

```powershell
git clone https://github.com/microsoft/TokenEconomizer.git C:/dev/TokenEconomizer
```

Keep this checkout in place: the hooks will reference its Python files by absolute path.
No Python package installation is needed; TokenEconomizer uses the standard library.
When reviewing an unmerged PR, use the checkout of that PR's branch for the following steps.

## 3. Install the hooks in your project

Open a PowerShell 7 terminal. In the block below, set:

- `$teRoot` to your TokenEconomizer checkout.
- `$projectRoot` to the existing project folder you opened in VS Code.

Run the whole block in the same terminal. It creates
`.github/hooks/tokeneconomizer-vscode.json` inside your project and stores this configuration's
state in `.te-vscode-state/`. It leaves other hook files intact and refuses to overwrite its
own file on a repeated installation. Do not also enable another TokenEconomizer hook or
plugin installation for this project, which would run the same events twice.

```powershell
$teRoot = (Resolve-Path 'C:/dev/TokenEconomizer').Path
$projectRoot = (Resolve-Path 'C:/work/my-project').Path
$pythonExe = (Get-Command python -CommandType Application |
    Where-Object Source -NotLike '*WindowsApps*' | Select-Object -First 1).Source
if (-not $pythonExe) { throw 'Set $pythonExe to a real Python 3.10+ executable' }
& $pythonExe --version # Must be a real Python 3.10+ installation.

$bootstrap = Join-Path $teRoot 'plugin/scripts/te_bootstrap.py'
$teState = Join-Path $projectRoot '.te-vscode-state'
$hookDir = Join-Path $projectRoot '.github/hooks'
$hookFile = Join-Path $hookDir 'tokeneconomizer-vscode.json'
if (Test-Path -LiteralPath $hookFile) {
    throw "TokenEconomizer hooks already exist: $hookFile"
}

# Keep this editor configuration's learned state separate from normal CLI sessions.
$previousTeState = $env:TOKENECONOMIZER_HOME
try {
    $env:TOKENECONOMIZER_HOME = $teState
    & $pythonExe $bootstrap cli --host copilot --cwd $projectRoot config set `
        microskills=envfacts toolprune=off loopguard=off repomap=off history_import=off
    if ($LASTEXITCODE -ne 0) { throw 'Could not configure TokenEconomizer' }
} finally {
    $env:TOKENECONOMIZER_HOME = $previousTeState
}

# Quote paths as PowerShell literal strings, including paths containing apostrophes.
$quotedPython = "'" + $pythonExe.Replace("'", "''") + "'"
$quotedBootstrap = "'" + $bootstrap.Replace("'", "''") + "'"
$command = "& $quotedPython $quotedBootstrap hook --host copilot"
$hooks = [ordered]@{}
foreach ($event in @('SessionStart', 'UserPromptSubmit', 'PostToolUse',
                    'PostToolUseFailure', 'Stop', 'SessionEnd')) {
    $hooks[$event] = @(@{
        type = 'command'
        powershell = $command
        timeoutSec = 60
        env = @{ TOKENECONOMIZER_HOME = $teState; PYTHONUTF8 = '1' }
    })
}
New-Item -ItemType Directory -Path $hookDir -Force | Out-Null
@{ version = 1; hooks = $hooks } | ConvertTo-Json -Depth 8 |
    Set-Content -LiteralPath $hookFile -Encoding utf8NoBOM
```

Keep the two generated paths out of commits because they contain local configuration and state.
For a Git project, add these lines to its local Git exclude file, normally `.git/info/exclude`:

```gitignore
/.te-vscode-state/
/.github/hooks/tokeneconomizer-vscode.json
```

The generated hook command invokes Python directly. Git Bash and WSL are not needed.
Leave the system PATH intact: TokenEconomizer's existing Windows background-worker fallback
can invoke Windows PowerShell through WMI.

## 4. Activate the hooks in Copilot Chat

1. In your project's Chat view, confirm that **Copilot** is the selected session target.
2. Open **Chat: Open Customizations** from the Command Palette (`Ctrl+Shift+P`), then **Hooks**.
3. Check that the workspace configuration includes the six events from
   `tokeneconomizer-vscode.json`: `SessionStart`, `UserPromptSubmit`, `PostToolUse`,
   `PostToolUseFailure`, `Stop`, and `SessionEnd`.
4. Review any hook or workspace trust prompts shown by VS Code.
5. Start a fresh Copilot chat so its `SessionStart` hook uses the new configuration.

These steps use VS Code's [workspace hook support](https://code.visualstudio.com/docs/agent-customization/hooks).
There is no `gh auth login`, `copilot login`, or `copilot plugin install` step.
This method configures the runtime through hooks; it does not register the packaged plugin or
its slash commands in the editor.

## 5. Check the installation

Ask Copilot to read an existing file in your project. For example, if it has a `README.md`:

> Use the file-reading tool to read README.md and summarize it in one sentence. Do not edit files or run terminal commands.

Run the following in the same PowerShell terminal used in step 3. If you opened a new terminal,
first restore `$pythonExe`, `$bootstrap`, `$teState`, and `$projectRoot` from that step.

```powershell
$previousTeState = $env:TOKENECONOMIZER_HOME
try {
    $env:TOKENECONOMIZER_HOME = $teState
    & $pythonExe $bootstrap cli --host copilot --cwd $projectRoot status --explain
    & $pythonExe $bootstrap cli --host copilot --cwd $projectRoot config show
    $errorLog = Join-Path $teState 'errors.log'
    if (Test-Path -LiteralPath $errorLog) { Get-Content -LiteralPath $errorLog -Tail 30 }
} finally {
    $env:TOKENECONOMIZER_HOME = $previousTeState
}
```

Check that the configuration shows `microskills=envfacts` and the other mechanisms disabled,
and that a session for your project appears in the isolated state. A status response alone
only confirms that Python runs; the session record confirms that the editor invoked the hooks.

To check completed-session processing, save your work, close that project's VS Code window,
and inspect its state from an external PowerShell terminal. In the verified editor version,
closing the window delivered `SessionEnd`; ending a model turn only delivered `Stop`.
Under `.te-vscode-state/repos/<project-key>/copilot/sessions/`, the session record should have
an `ended` timestamp and eventually `folded: true`. Reopen the project for your next session.

An empty learned-context block after a simple file read is expected. Learning requires
qualifying evidence and may take multiple sessions. Receiving hooks and processing a session
does not by itself demonstrate that a fact was learned or tokens were saved.

## Update or remove the configuration

To update the code, update your TokenEconomizer checkout and start a fresh Copilot session.
If you move the checkout or change Python installations, remove the hook file below and repeat
step 3 with the new paths. Keep `.te-vscode-state/` to retain this configuration's state.

To disable TokenEconomizer in this project, remove only
`.github/hooks/tokeneconomizer-vscode.json` and start a fresh session. You may retain
`.te-vscode-state/` for later use. Other hook files and the normal `~/.tokeneconomizer` state
are separate from this setup.

## Troubleshooting

| Symptom | What to check |
|---|---|
| No TokenEconomizer hooks appear | Open the correct project folder, select the **Copilot** session target, and check the file under `.github/hooks/`. Organization policy can disable hooks. |
| Python cannot be found | Set `$pythonExe` to the full path of an installed Python 3.10+ executable and regenerate the configuration. Avoid Windows Store aliases. |
| Hooks run twice | Check workspace, user, and plugin hook sources for another TokenEconomizer installation. |
| No session record is created | Inspect the editor's hook diagnostics and `.te-vscode-state/errors.log`. Check the absolute paths in the hook file. |
| A completed session is not folded | Check the session's `ended` field and the project's `copilot/fold.log`. Keep system executables on PATH for the Windows background-worker fallback. |
| Copilot logs contain calls but TokenEconomizer finds none | Use a checkout containing the Copilot transcript-detection fix. Older code can misclassify metadata containing `"tool_use"` as a Claude transcript. |
| No learned facts yet | A read-only smoke check may produce no qualifying evidence. Do not seed a marker and count it as learned behavior. |

## Feature scope

The configuration was checked with Windows 11, VS Code 1.146.0, Python 3.13.17, PowerShell
7.6.10, and the **Copilot** session target. The editor showed **Copilot Fusion**; its session
log reported runtime `1.0.108-0` and resolved model `gpt-6-luna`.
Other versions and remote execution environments need their own checks.

| Capability | Scope of this setup |
|---|---|
| Session hooks and transcripts | Start, prompt, successful/failed tools, stop, and end events were observed. Transcripts were parsed and the final session folded automatically. |
| Context injection | A manually prepared marker reached a subsequent model session. Natural fact acquisition still needs separate validation. |
| Model-free micro-skills | `envfacts` is enabled. The file-only checks did not establish its full learning cycle. |
| Model-based proposer and loop guard | Disabled. The existing adapter launches standalone `copilot` for its own model calls; it does not use VS Code's signed-in model API. |
| Tool pruning | Disabled. CLI settings and tool probes have not been verified against the editor. |
| Usage | Completed transcripts supplied token usage after shutdown. TokenEconomizer pricing is not Copilot credit accounting, and this check does not measure savings. |
| Packaged plugin installation | Not covered. This guide installs project hooks directly; marketplace discovery and slash commands require separate verification. |

For hook payload details, see the
[GitHub Copilot hook reference](https://docs.github.com/en/copilot/reference/hooks-reference).
The [older Local hook reference](https://code.visualstudio.com/docs/agents/reference/hooks-reference)
describes a different input/output contract.

## For contributors

Follow [CLAUDE.md](../CLAUDE.md) and the [test workflow](../.github/workflows/tests.yml).
Validate changes to these instructions by running the setup in a separate project, checking
the generated hook configuration, and exercising a real Copilot session. Record the editor,
runtime, model, OS, and Python versions, and distinguish simulated hook inputs from live runs.

The package's Windows baseline is `python -m pytest tests -ra --tb=short`.
The repository also requests `python -m pytest tests experiments/tests`; the benchmark suite
has Unix-specific APIs and shell assumptions, so disclose Windows failures separately.
Run `git diff --check` and keep machine-specific hooks, state, and transcripts out of the PR.
