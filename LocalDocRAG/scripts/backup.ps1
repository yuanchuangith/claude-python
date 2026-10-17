param([Parameter(Mandatory=$true)][string]$Destination)
. "$PSScriptRoot/common.ps1"
$target = [IO.Path]::GetFullPath($Destination)
New-Item -ItemType Directory -Force -Path $target | Out-Null
if ((Get-ChildItem -LiteralPath $target -Force | Measure-Object).Count -gt 0) { throw 'Backup destination must be empty.' }
$compose = Get-RagCompose
Invoke-RagDocker @compose stop web api
try { Invoke-RagDocker @compose run --rm --no-deps --user 0 -v "${target}:/backup" api python -m app.maintenance backup /backup }
finally { Invoke-RagDocker @compose start api web }
Write-Host 'Backup completed with SQLite online backup and Qdrant snapshots.'
