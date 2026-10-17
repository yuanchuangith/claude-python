param([switch]$Minimal)
. "$PSScriptRoot/common.ps1"
$compose = Get-RagCompose
$options = @(); if ($Minimal) { $options += '--minimal' }
Invoke-RagDocker @compose exec -T api python scripts/smoke_test.py --base http://127.0.0.1:8000 @options
