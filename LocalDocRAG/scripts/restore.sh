#!/usr/bin/env bash
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
[[ $# == 2 && "$2" == --confirm-overwrite ]] || { echo 'Usage: bash scripts/restore.sh /backup --confirm-overwrite. Current data is overwritten; a safety backup is made first.'; exit 1; }
backup=$(cd -- "$1" && pwd)
[[ -f "$backup/manifest.json" ]] || { echo 'Missing backup manifest'; exit 1; }
bash scripts/backup.sh "$backup.pre-restore-$(date +%Y%m%d-%H%M%S)"
"${compose[@]}" stop web api
if ! "${compose[@]}" run --rm --no-deps --user 0 -v "$backup:/backup:ro" api python -m app.maintenance restore /backup --confirm-overwrite;then
  echo 'Restore failed. Services remain stopped; use the pre-restore safety backup.';exit 1
fi
"${compose[@]}" start api web
