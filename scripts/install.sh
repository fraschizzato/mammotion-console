#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
command -v docker >/dev/null || { echo 'Install Docker Engine and the Compose plugin first. See docs/DEPLOYMENT.md.'; exit 1; }
docker compose version >/dev/null
if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo 'Created .env. Configure HA_BASE_URL and HA_TOKEN, then run this script again.'
  exit 0
fi
mkdir -p data/{db,maps,previews,backups,logs}
sudo chown -R 10001:10001 data
docker compose up -d --build --wait
echo 'Mammotion Console is ready on port 5000 (or PORT in .env).'
