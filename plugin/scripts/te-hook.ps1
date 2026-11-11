if ($MyInvocation.ExpectingInput) {
    $input | & "$PSScriptRoot\te.ps1" -Hook @args
} else {
    & "$PSScriptRoot\te.ps1" -Hook @args
}
exit $LASTEXITCODE
