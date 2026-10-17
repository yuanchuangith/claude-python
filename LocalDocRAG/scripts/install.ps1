param([ValidateSet('auto','gpu','cpu')][string]$Mode = $(if ($env:GPU_MODE) { $env:GPU_MODE } else { 'auto' }))
. "$PSScriptRoot/common.ps1"
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw 'Docker is missing. Install Docker Desktop with WSL2: https://docs.docker.com/desktop/setup/install/windows-install/' }
Assert-RagDockerReady
$composeVersion = & docker compose version --short
if ($LASTEXITCODE -ne 0 -or [int]($composeVersion.TrimStart('v').Split('.')[0]) -lt 2) { throw 'Docker Compose v2+ is required.' }
if (-not (Test-Path -LiteralPath '.env')) { Copy-Item -LiteralPath '.env.example' -Destination '.env' }
$config = @{}
Get-Content -LiteralPath '.env' | ForEach-Object { if ($_ -match '^([A-Z_]+)=(.*)$') { $config[$matches[1]] = $matches[2].Trim() } }
$ragPort = if ($config.APP_PORT) { [int]$config.APP_PORT } else { 7860 }
if ($config.APP_BIND -and $config.APP_BIND -ne '127.0.0.1') { throw 'APP_BIND must remain 127.0.0.1; V1 has no LAN authentication.' }
$existing = & docker compose -f compose.yaml ps -q web
$listen = Get-NetTCPConnection -LocalPort $ragPort -State Listen -ErrorAction SilentlyContinue
if ($listen -and -not $existing) { throw "Port $ragPort is occupied. Change APP_PORT in .env; the existing program will not be stopped." }
$drive = (Get-Item -LiteralPath $RagRoot).PSDrive
if ($drive.Free -lt 15GB) { throw 'At least 15 GB free disk space is required for images/models. Also check Docker Desktop virtual disk space.' }
if ($Mode -ne 'cpu') {
    & docker run --rm --gpus all --entrypoint nvidia-smi ollama/ollama:0.13.5 --query-gpu=name,memory.total --format=csv,noheader
    if ($LASTEXITCODE -ne 0) {
        if ($Mode -eq 'gpu') { throw 'Docker GPU is unavailable. Enable WSL2 GPU/NVIDIA Container Toolkit or rerun start.bat -Mode cpu.' }
        $answer = Read-Host 'Docker GPU check failed. Use explicit CPU mode (slower generation)? Type CPU to continue'
        if ($answer -ne 'CPU') { throw 'GPU check failed; CPU fallback was not selected.' }
        $Mode = 'cpu'
    } else { $Mode = 'gpu' }
}
Set-Content -LiteralPath '.runtime-mode' -Value $Mode -Encoding ascii
$compose = Get-RagCompose
Invoke-RagDocker @compose config --quiet
Invoke-RagDocker @compose up -d --build
$ready = $false
for ($attempt=0; $attempt -lt 90; $attempt++) {
    try { $health = Invoke-RestMethod -Uri "http://127.0.0.1:$ragPort/api/health" -TimeoutSec 5; if ($health.status -eq 'ok') { $ready=$true; break } } catch {}
    Start-Sleep -Seconds 2
}
if (-not $ready) { & docker @compose logs --tail 35; throw 'Services did not become ready; run scripts/doctor.ps1.' }
foreach ($model in @($config.OLLAMA_EMBED_MODEL,$config.OLLAMA_VISION_MODEL,$config.OLLAMA_CHAT_MODEL)) {
    & docker @compose exec -T ollama ollama show $model *> $null
    if ($LASTEXITCODE -ne 0) {
        $downloaded=$false
        for ($retry=0; $retry -lt 3; $retry++) { & docker @compose exec -T ollama ollama pull $model; if ($LASTEXITCODE -eq 0) { $downloaded=$true; break } }
        if (-not $downloaded) { throw "Model download failed: $model. Re-run this entry to continue; existing data is preserved." }
    }
}
Invoke-RagDocker @compose exec -T api python -m app.maintenance init
Invoke-RagDocker @compose exec -T api python scripts/smoke_test.py --base http://127.0.0.1:8000 --minimal
Write-Host "LocalDocRAG 已启动`nWeb: http://127.0.0.1:$ragPort`n模式: $Mode (inference verified)`nEmbedding: $($config.OLLAMA_EMBED_MODEL)`nVision: $($config.OLLAMA_VISION_MODEL)`nChat: $($config.OLLAMA_CHAT_MODEL)`n数据存储: Docker 持久卷"
