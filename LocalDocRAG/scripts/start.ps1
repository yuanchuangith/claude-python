. "$PSScriptRoot/common.ps1"
$compose = Get-RagCompose
Invoke-RagDocker @compose up -d
