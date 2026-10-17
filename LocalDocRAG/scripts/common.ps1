$ErrorActionPreference = 'Stop'
$RagRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $RagRoot
function Invoke-RagDocker {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    & docker @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Docker failed: $($Arguments[0]) $($Arguments[1]). Check Docker Desktop/WSL2 or run scripts/doctor.ps1." }
}
function Assert-RagDockerReady {
    $failure = 'Docker Engine is unavailable (docker info failed or returned no server version). Start/fix Docker Desktop WSL2, then retry; no services were changed. Run scripts/doctor.ps1 for diagnostics.'
    try {
        $diagnostic = @(& docker info --format '{{.ServerVersion}}' 2>&1)
        $dockerExit = $LASTEXITCODE
    } catch { throw $failure }
    $serverVersion = ($diagnostic | ForEach-Object { "$_" }) -join "`n"
    if ($dockerExit -ne 0 -or $serverVersion.Trim() -notmatch '^\d+\.\d+\.\d+[^\s]*$') {
        throw $failure
    }
    Write-Host "Docker Engine: $($serverVersion.Trim())"
}
function Get-RagCompose {
    $selected = if (Test-Path -LiteralPath '.runtime-mode') { (Get-Content -LiteralPath '.runtime-mode' -Raw).Trim() } else { 'cpu' }
    if ($selected -notin @('gpu', 'cpu')) { throw 'Invalid runtime mode file.' }
    return @('compose','-f','compose.yaml','-f',"compose.$selected.yaml")
}
