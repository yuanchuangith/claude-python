#!/usr/bin/env bash
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
[[ $# == 1 ]] || { echo 'Usage: bash scripts/backup.sh /empty/destination'; exit 1; }
mkdir -p -- "$1"
destination=$(cd -- "$1" && pwd)
[[ -z $(ls -A -- "$destination") ]] || { echo 'Backup destination must be empty'; exit 1; }
"${compose[@]}" stop web api
trap '"${compose[@]}" start api web' EXIT
"${compose[@]}" run --rm --no-deps --user 0 -v "$destination:/backup" api python -m app.maintenance backup /backup
