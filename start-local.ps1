param(
    [ValidateRange(1, 65535)][int]$Port = 8888,
    [switch]$Background
)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv/Scripts/python.exe'
$configPath = Join-Path $projectRoot '.env.local.json'
$runtimeRoot = Join-Path $projectRoot 'logs/local'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Create .venv and install .[actiondesign] before starting.'
}
if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) {
    throw "Port $Port is already in use."
}
New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
if (-not (Test-Path -LiteralPath $configPath)) {
    $origins = foreach ($localPort in @(8888, 3000, 3111, 5173, 8000, 8001, $Port) | Select-Object -Unique) {
        "http://localhost:$localPort"
        "http://127.0.0.1:$localPort"
    }
    [ordered]@{
        ACTIONDESIGN_MCP_PROXY_ENABLED = 'true'
        ACTIONDESIGN_PUBLIC_BROWSER_MCP_CONFIG = 'true'
        ACTIONDESIGN_SERVICE_AUTH_MODE = 'required'
        ACTIONDESIGN_SERVICE_AUTH_TOKEN = ([guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N'))
        ACTIONDESIGN_ADMIN_PASSWORD = ([guid]::NewGuid().ToString('N'))
        ACTIONDESIGN_ADMIN_SESSION_TTL_SECONDS = '86400'
        ACTIONDESIGN_AGENT_ALLOW_ORIGINS = ($origins -join ',')
        ACTIONDESIGN_PROFILE_STORE_PATH = 'logs/local/config/api-profiles.json'
        ACTIONDESIGN_PROFILE_SECRET_STORE_PATH = 'logs/local/config/api-profile-secrets.json'
        ACTIONDESIGN_MANAGED_SECRET_STORE_PATH = 'logs/local/config/managed-secrets.json'
        ACTIONDESIGN_MCP_SERVER_STORE_PATH = 'logs/local/config/mcp-servers.json'
        ACTIONDESIGN_BROWSER_API_KEY_STORE_PATH = 'logs/local/config/browser-api-key.json'
        ACTIONDESIGN_CLIENT_API_KEY_STORE_PATH = 'logs/local/config/client-api-keys.json'
        ACTIONDESIGN_AGENT_LOG_ROOT = 'logs/local/debug'
        ACTIONDESIGN_SYSTEM_LOG_ENABLED = 'true'
        ACTIONDESIGN_SYSTEM_LOG_PATH = 'logs/local/system-logs.sqlite3'
    } | ConvertTo-Json | Set-Content -LiteralPath $configPath -Encoding utf8
}
$config = Get-Content -LiteralPath $configPath -Raw -Encoding utf8 | ConvertFrom-Json
if (-not $Background) {
    & $pythonPath -u (Join-Path $projectRoot 'run_gateway.py') --port $Port
    if ($LASTEXITCODE -ne 0) {
        throw "Gateway exited with code $LASTEXITCODE."
    }
    return
}
$previousEnvironment = @{}
try {
    foreach ($property in $config.PSObject.Properties) {
        $previousEnvironment[$property.Name] = [Environment]::GetEnvironmentVariable($property.Name, 'Process')
        [Environment]::SetEnvironmentVariable($property.Name, [string]$property.Value, 'Process')
    }
    $process = Start-Process -FilePath $pythonPath -ArgumentList @(
        '-u', (Join-Path $projectRoot 'run_gateway.py'), '--port', "$Port"
    ) -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $runtimeRoot 'stdout.log') `
        -RedirectStandardError (Join-Path $runtimeRoot 'stderr.log')
    $process.Id | Set-Content -LiteralPath (Join-Path $runtimeRoot 'gateway.pid')
} finally {
    foreach ($name in $previousEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $previousEnvironment[$name], 'Process')
    }
}
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    $process.Refresh()
    if ($process.HasExited) {
        throw "Gateway exited. See logs/local/stderr.log."
    }
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/actiondesign-agent/health" -TimeoutSec 2
        if ($health.status -eq 'ok' -and $health.mcpProxyEnabled) {
            Write-Output "Backend and MCP gateway ready. PID: $($process.Id)"
            Write-Output "Admin: http://127.0.0.1:$Port/admin/"
            Write-Output 'Local credentials: .env.local.json'
            return
        }
    } catch {
        Start-Sleep -Milliseconds 500
    }
}
throw 'Gateway did not become healthy. See logs/local/stderr.log.'
