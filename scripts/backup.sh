#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p backups
backup_path="$(pwd)/backups/mammotion-$(date -u +%Y%m%dT%H%M%SZ).tar.gz"
# Stop application writes so SQLite WAL and snapshot files share one consistent point in time.
docker compose stop console
trap 'docker compose start console' EXIT
sudo tar --exclude='./backups' -czf "$backup_path" -C data .
sudo chown "$(id -u):$(id -g)" "$backup_path"
chmod 600 "$backup_path"
echo "Backup created: $backup_path (credentials excluded; protect .env separately)."
