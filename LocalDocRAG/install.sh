#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
command -v docker >/dev/null || { echo 'Install Docker Engine: https://docs.docker.com/engine/install/'; exit 1; }
server_version=$(docker info --format '{{.ServerVersion}}') || { echo 'Docker daemon unavailable; start Docker before retrying.'; exit 1; }
[[ "$server_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+[^[:space:]]*$ ]] || { echo 'Docker returned no valid server version; fix Docker before retrying.'; exit 1; }
major=$(docker compose version --short | sed 's/^v//' | cut -d. -f1)
[[ "$major" -ge 2 ]] || { echo 'Docker Compose v2+ required'; exit 1; }
[[ -f .env ]] || cp .env.example .env
# Only read explicit simple KEY=value entries, never source user config as shell code.
value() { sed -n "s/^$1=//p" .env | tail -1 | tr -d '\r'; }
port=$(value APP_PORT); port=${port:-7860}
[[ "$port" =~ ^[0-9]+$ && "$port" -gt 0 && "$port" -lt 65536 ]] || { echo 'Invalid APP_PORT'; exit 1; }
[[ $(value APP_BIND) == 127.0.0.1 ]] || { echo 'APP_BIND must remain 127.0.0.1'; exit 1; }
free=$(df -Pk . | awk 'NR==2 {print $4}')
[[ "$free" -gt 15728640 ]] || { echo 'At least 15GB free required; also check Docker virtual disk.'; exit 1; }
existing=$(docker compose -f compose.yaml ps -q web)
if command -v ss >/dev/null && ss -ltn | awk '{print $4}' | grep -Eq ":${port}$" && [[ -z "$existing" ]]; then echo "Port $port is occupied; change APP_PORT in .env"; exit 1; fi
mode=${GPU_MODE:-$(value GPU_MODE)}; mode=${mode:-auto}
[[ "$mode" == auto || "$mode" == cpu || "$mode" == gpu ]] || { echo 'GPU_MODE must be auto/cpu/gpu'; exit 1; }
if [[ "$mode" != cpu ]]; then
  if docker run --rm --gpus all --entrypoint nvidia-smi ollama/ollama:0.13.5 --query-gpu=name,memory.total --format=csv,noheader; then mode=gpu
  else
    [[ "$mode" != gpu ]] || { echo 'Docker GPU unavailable. Install NVIDIA Container Toolkit or use GPU_MODE=cpu bash install.sh'; exit 1; }
    read -r -p 'GPU unavailable. Type CPU to select CPU mode (slower generation): ' answer
    [[ "$answer" == CPU ]] || exit 1
    mode=cpu
  fi
fi
printf '%s\n' "$mode" > .runtime-mode
compose=(docker compose -f compose.yaml -f "compose.$mode.yaml")
"${compose[@]}" config --quiet
"${compose[@]}" up -d --build
ready=0
for ((i=0;i<90;i++)); do
  if "${compose[@]}" exec -T api python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=3)" >/dev/null 2>&1; then ready=1;break;fi
  sleep 2
done
[[ "$ready" == 1 ]] || { "${compose[@]}" logs --tail 35; echo 'Readiness timeout; run scripts/doctor.sh'; exit 1; }
for key in OLLAMA_EMBED_MODEL OLLAMA_VISION_MODEL OLLAMA_CHAT_MODEL; do
  model=$(value "$key")
  [[ -n "$model" ]] || { echo "Missing $key"; exit 1; }
  if ! "${compose[@]}" exec -T ollama ollama show "$model" >/dev/null 2>&1;then
    downloaded=0
    for i in 1 2 3; do if "${compose[@]}" exec -T ollama ollama pull "$model"; then downloaded=1;break;fi;done
    [[ "$downloaded" == 1 ]] || { echo "Model download failed: $model. Rerun to resume."; exit 1; }
  fi
done
"${compose[@]}" exec -T api python -m app.maintenance init
"${compose[@]}" exec -T api python scripts/smoke_test.py --base http://127.0.0.1:8000 --minimal
printf 'LocalDocRAG 已启动\nWeb: http://127.0.0.1:%s\n模式: %s (inference verified)\n数据存储: Docker 持久卷\n' "$port" "$mode"
