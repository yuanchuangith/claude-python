. "$PSScriptRoot/common.ps1"
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Write-Host 'Docker is missing: https://docs.docker.com/desktop/'; exit 1 }
Invoke-RagDocker version --format '{{json .}}'
Invoke-RagDocker compose version
$compose = Get-RagCompose
Invoke-RagDocker @compose ps
Invoke-RagDocker @compose exec -T api python -c "import urllib.request,json; r=json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/system/status'));print(json.dumps(r,ensure_ascii=False,indent=2))"
