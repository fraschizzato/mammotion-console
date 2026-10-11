#!/usr/bin/env bash
set -euo pipefail

echo '=== System ==='
cat /etc/os-release | grep -E '^(PRETTY_NAME|VERSION_ID)=' || true
python3 --version || true

echo '=== Home Assistant ==='
systemctl status home-assistant --no-pager 2>/dev/null | head -n 20 || true

echo '=== HA config ==='
ls -ld /home/pi/.homeassistant 2>/dev/null || true

echo '=== Mammotion integration ==='
if [ -f /home/pi/.homeassistant/custom_components/mammotion/manifest.json ]; then
  cat /home/pi/.homeassistant/custom_components/mammotion/manifest.json
fi

echo '=== Port 5000 ==='
ss -lntup | grep ':5000' || echo 'TCP/UDP 5000 appears unused'
