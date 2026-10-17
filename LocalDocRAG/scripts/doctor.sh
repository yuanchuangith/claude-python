#!/usr/bin/env bash
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
command -v docker >/dev/null || { echo 'Install Docker Engine + Compose v2'; exit 1; }
docker version --format '{{json .}}'
docker compose version
"${compose[@]}" ps
"${compose[@]}" exec -T api python -c "import urllib.request,json;print(json.dumps(json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/system/status')),ensure_ascii=False,indent=2))"
