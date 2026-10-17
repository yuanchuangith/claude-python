#!/usr/bin/env bash
set -euo pipefail
RAG_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd -- "$RAG_ROOT"
mode=$(cat .runtime-mode 2>/dev/null || printf cpu)
[[ "$mode" == cpu || "$mode" == gpu ]] || { echo 'Invalid runtime mode'; exit 1; }
compose=(docker compose -f compose.yaml -f "compose.$mode.yaml")
