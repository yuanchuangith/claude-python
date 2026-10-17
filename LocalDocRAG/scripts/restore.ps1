param([Parameter(Mandatory=$true)][string]$Source,[switch]$ConfirmOverwrite)
. "$PSScriptRoot/common.ps1"
if (-not $ConfirmOverwrite) { throw 'Restore overwrites current knowledge. Pass -ConfirmOverwrite after reviewing the backup. A pre-restore backup will be created.' }
$sourcePath = (Resolve-Path -LiteralPath $Source).Path
if (-not (Test-Path -LiteralPath (Join-Path $sourcePath 'manifest.json'))) { throw 'Missing backup manifest.' }
$safety = "$sourcePath.pre-restore-$(Get-Date -Format yyyyMMdd-HHmmss)"
& "$PSScriptRoot/backup.ps1" -Destination $safety
$compose = Get-RagCompose
Invoke-RagDocker @compose stop web api
try { Invoke-RagDocker @compose run --rm --no-deps --user 0 -v "${sourcePath}:/backup:ro" api python -m app.maintenance restore /backup --confirm-overwrite }
catch { Write-Host "Restore failed. Services remain stopped; safety backup is $safety"; throw }
Invoke-RagDocker @compose start api web
