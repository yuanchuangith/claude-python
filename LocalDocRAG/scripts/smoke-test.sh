#!/usr/bin/env bash
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
"${compose[@]}" exec -T api python scripts/smoke_test.py --base http://127.0.0.1:8000 "$@"
