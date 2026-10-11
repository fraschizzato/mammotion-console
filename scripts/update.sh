#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
bash scripts/backup.sh
docker compose build --pull
docker compose up -d --wait
echo 'Update complete. Check /api/health and the Map Library.'
