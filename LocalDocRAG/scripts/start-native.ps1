param([ValidateSet('auto','gpu','cpu')][string]$Mode='auto',[int]$Port=7860,[ValidateSet(4096,8192)][int]$ContextLength=4096,[switch]$DependenciesOnly)
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
$runtime=Join-Path $root 'runtime'
New-Item -ItemType Directory -Force -Path (Join-Path $runtime 'logs') | Out-Null
$env:OLLAMA_HOST='127.0.0.1:11434'
$env:OLLAMA_MODELS=Join-Path $runtime 'models'
$env:OLLAMA_NO_CLOUD='1'
$env:OLLAMA_MAX_LOADED_MODELS='1'
$env:OLLAMA_NUM_PARALLEL='1'
$env:OLLAMA_CONTEXT_LENGTH="$ContextLength"
$env:OLLAMA_KEEP_ALIVE='2m'
$env:OLLAMA_FLASH_ATTENTION='1'
$env:GPU_MODE=$Mode
$env:DATA_DIR=Join-Path $root 'data'
$env:OLLAMA_URL='http://127.0.0.1:11434'
# The 2b alias can keep thinking without a final JSON on Ollama 0.13.5.
# Use the explicit instruct variant for this portable Windows runtime.
$env:OLLAMA_VISION_MODEL='qwen3-vl:2b-instruct'
$env:QDRANT_URL='http://127.0.0.1:6333'
if ($Mode -eq 'cpu') { $env:CUDA_VISIBLE_DEVICES='-1' }
function Listening([int]$Number) { return [bool](Get-NetTCPConnection -LocalPort $Number -State Listen -ErrorAction SilentlyContinue) }
$pids=@{}
if (-not (Listening 11434)) {
    $exe=Join-Path $runtime 'ollama-v0.40.5/ollama.exe'
    if (-not (Test-Path -LiteralPath $exe)) { throw 'Run backend Python scripts/setup_native.py first.' }
    $p=Start-Process -FilePath $exe -ArgumentList @('serve') -WorkingDirectory $root -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtime 'logs/ollama.out.log') -RedirectStandardError (Join-Path $runtime 'logs/ollama.err.log')
    $pids.ollama=$p.Id
}
if (-not (Listening 6333)) {
    $exe=Join-Path $runtime 'qdrant/qdrant.exe'
    if (-not (Test-Path -LiteralPath $exe)) { throw 'Run backend Python scripts/setup_native.py first.' }
    $storage=(Join-Path $runtime 'qdrant-data').Replace('\','/')
    $snapshots=(Join-Path $runtime 'qdrant-snapshots').Replace('\','/')
    $config="storage:`n  storage_path: '$storage'`n  snapshots_path: '$snapshots'`nservice:`n  host: 127.0.0.1`n  http_port: 6333`n  grpc_port: 6334`ntelemetry_disabled: true`n"
    $configPath=Join-Path $runtime 'qdrant-native.yaml'
    [IO.File]::WriteAllText($configPath,$config,[Text.UTF8Encoding]::new($false))
    $p=Start-Process -FilePath $exe -ArgumentList @('--config-path',('"'+$configPath+'"')) -WorkingDirectory $root -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtime 'logs/qdrant.out.log') -RedirectStandardError (Join-Path $runtime 'logs/qdrant.err.log')
    $pids.qdrant=$p.Id
}
foreach ($entry in @(@{Port=11434;Url='http://127.0.0.1:11434/api/tags'},@{Port=6333;Url='http://127.0.0.1:6333/collections'})) {
    $ready=$false
    for ($i=0;$i -lt 30;$i++) { try { $null=Invoke-RestMethod -Uri $entry.Url -TimeoutSec 2; $ready=$true; break } catch { Start-Sleep -Milliseconds 500 } }
    if (-not $ready) { throw "Native dependency $($entry.Port) failed. Check runtime/logs." }
}
if (-not $DependenciesOnly -and -not (Listening $Port)) {
    $python=Join-Path $root 'backend/.venv/Scripts/python.exe'
    $p=Start-Process -FilePath $python -ArgumentList @('-m','uvicorn','app.native:app','--host','127.0.0.1','--port',"$Port",'--no-access-log') -WorkingDirectory (Join-Path $root 'backend') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtime 'logs/api.out.log') -RedirectStandardError (Join-Path $runtime 'logs/api.err.log')
    $pids.api=$p.Id
}
$record=Join-Path $runtime 'native-processes.json'
if (Test-Path -LiteralPath $record) { $old=Get-Content -LiteralPath $record -Raw | ConvertFrom-Json; foreach ($prop in $old.PSObject.Properties) { if (-not $pids.ContainsKey($prop.Name)) { $pids[$prop.Name]=$prop.Value } } }
$pids | ConvertTo-Json | Set-Content -LiteralPath $record -Encoding UTF8
Write-Host "Native local dependencies started; model readiness is reported separately. API: http://127.0.0.1:$Port"
