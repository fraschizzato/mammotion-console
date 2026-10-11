#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
backup_path="${1:?Usage: bash scripts/restore.sh /absolute/path/to/backup.tar.gz}"
test -f "$backup_path"
if tar -tzf "$backup_path" | grep -E '(^/|(^|/)\.\.(/|$))'; then
  echo 'Unsafe archive paths'; exit 1
fi
echo 'Restoring requires an empty data directory. Move the existing data aside first; do not delete it.'
test -z "$(find data -mindepth 1 -print -quit 2>/dev/null)" || exit 1
docker compose stop console
mkdir -p data
sudo tar --no-same-owner -xzf "$backup_path" -C data
sudo chown -R 10001:10001 data
docker compose up -d --wait
