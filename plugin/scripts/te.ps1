param([switch]$Hook)

$env:PYTHONUTF8 = '1'
$OutputEncoding = [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding
$mode = if ($Hook) { 'hook' } else { 'cli' }
$payload = if ($Hook) {
    if ($MyInvocation.ExpectingInput) { $input -join "`n" } else { [Console]::In.ReadToEnd() }
}
$candidates = @(
    foreach ($name in @($env:TE_PYTHON, 'python3', 'python', 'py')) {
        if ($name) {
            Get-Command $name -CommandType Application -All -ErrorAction SilentlyContinue
        }
    }
)
$python = $candidates | Where-Object { $_.Source -notlike '*\WindowsApps\*' } | Select-Object -First 1
if (-not $python) {
    foreach ($candidate in $candidates) {
        & $candidate.Source -c pass *> $null
        if ($LASTEXITCODE -eq 0) { $python = $candidate; break }
    }
}
if (-not $python) {
    $message = 'te: no Python on PATH (set TE_PYTHON)'
    if ($Hook) {
        $root = if ($env:TOKENECONOMIZER_HOME) { $env:TOKENECONOMIZER_HOME } else {
            Join-Path $HOME '.tokeneconomizer'
        }
        try {
            [IO.Directory]::CreateDirectory($root) | Out-Null
            [IO.File]::AppendAllText((Join-Path $root 'errors.log'), "$message`n")
        } catch [IO.IOException] {
            [Console]::Error.WriteLine($_.Exception.Message)
        } catch [UnauthorizedAccessException] {
            [Console]::Error.WriteLine($_.Exception.Message)
        }
        '{}'
        exit 0
    }
    [Console]::Error.WriteLine($message)
    exit 1
}
$pythonArgs = @()
if ($python.Name -eq 'py.exe' -or $python.Name -eq 'py') { $pythonArgs += '-3' }
$pythonArgs += @((Join-Path $PSScriptRoot 'te_bootstrap.py'), $mode)
if ($Hook) {
    $payload | & $python.Source @pythonArgs @args
} else {
    & $python.Source @pythonArgs @args
}
exit $LASTEXITCODE
