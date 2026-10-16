# Debian 12 deployment

## Host prerequisites

The production image includes Python 3.13; the Debian host does not need a Python or Node upgrade. Install Docker Engine and the Compose plugin using [Docker's Debian instructions](https://docs.docker.com/engine/install/debian/). Verify `docker version` and `docker compose version`. Choose a LAN-reachable HA URL; `localhost` inside the container refers to the console container, not HA.

On the target VM:

```sh
cd /opt/mammotion-console
cp .env.example .env
chmod 600 .env
nano .env
# HA_BASE_URL=http://<HA LAN address>:8123
# HA_TOKEN=<long-lived token created in the HA user profile>
bash scripts/install.sh
docker compose ps
curl --fail http://127.0.0.1:5000/api/health
```

Browse `http://ip:5000` from the LAN. The address is deployment context, never hard-coded into the application. `PORT=5000` is the default.
Use your actual interface address in `BIND_ADDRESS` if you want to bind only the LAN interface.

The process runs as UID/GID **10001**, with a read-only root filesystem, dropped Linux capabilities, no privilege escalation, and a writable `/data` volume. `scripts/install.sh` sets ownership on `./data`. One Uvicorn worker owns the HA WebSocket and capture loop. Do not add workers: use a single instance against this data volume.

## Shared display calibration

Set `MAP_DISPLAY_ROTATION_DEGREES`, `MAP_DISPLAY_OFFSET_EAST_M` and
`MAP_DISPLAY_OFFSET_NORTH_M` in `.env` when the base-map display needs adjustment.
Optionally set both `MAP_DISPLAY_ORIGIN_LATITUDE` and
`MAP_DISPLAY_ORIGIN_LONGITUDE`; leaving both blank uses the selected map centre.
The same browser-only transform is applied to the base geometry, mower, routes,
network samples and access points. Stored coordinates are unchanged. Recreate the
container after a change.

## Optional read-only HA cache

The preferred path uses current HA services. To preserve extra raw cache information and inspect cached schedule parameters, set in `.env`:

```dotenv
COMPOSE_FILE=docker-compose.yml:docker-compose.ha-cache.yml
HA_HOST_CONFIG_PATH=/home/pi/.homeassistant
```

Then `docker compose up -d --wait`. The override sets `HA_CONFIG_PATH=/ha-config` and mounts the host directory **read-only**. The parser only reads `.storage/mammotion.*` plus the Mammotion integration manifest. It never opens HA auth/config-entry storage or writes `.storage`.

UID 10001 must be able to traverse the mounted directory and read the Mammotion cache files. Give it narrowly scoped read ACLs if necessary; do not make the entire HA config world-readable. Check permissions again after HA replaces cache files. Alternatively, periodically copy just `mammotion.*` and the integration manifest into a separate read-only export directory with appropriate permissions. Cached data can lag five minutes and is marked as a cache source in each snapshot.

If HA has a dedicated VM filesystem, that directory is not necessarily available on the Docker host. Use the service provider first; a cache mount is optional.

## Health, logs and restart

* `/api/health`: process/database health and HA connection flag. HA downtime does not make the offline archive unhealthy.
* `/api/ready`: console readiness and HA configuration state.
* `docker compose logs --tail=100 console`: server lifecycle/errors. Access logging is disabled so query values never enter the application access log.
* `docker compose restart console`: reload `.env` only after `docker compose up -d` recreates changed configuration; prefer `docker compose up -d --force-recreate` after editing environment values.
* `restart: unless-stopped`: automatic container restart after host reboot or process failure.
* Docker rotates logs at 10 MB, retaining three files. Activity/capture records live in SQLite.

## Backup and restore

`bash scripts/backup.sh` briefly stops console writes, archives SQLite (including its WAL if present) and snapshot files together, then restarts the service even if backup fails. Backups are written to `./backups/` outside `./data`; `.env` is excluded. Copy archives off-host and protect a separate encrypted copy of `.env`.

To restore a trusted backup, stop the service, rename the existing `data` directory to a recovery name, then run:

```sh
bash scripts/restore.sh /absolute/path/mammotion-YYYYMMDDTHHMMSSZ.tar.gz
```

The restore script refuses to overwrite a nonempty data directory. Verify health and inspect several maps after restoring. A single-map ZIP export is useful for transfer, but a full backup is required to preserve every ID, set, version, preset, history item and preference exactly.

## Updates and rollback

Bring the updated source tree onto the VM, then `bash scripts/update.sh`. It backs up data, builds a fresh image and waits for the new container to become healthy. Existing snapshots are never migrated in place. Keep the prior image/tag and backup until acceptance checks pass. For rollback, run the previous image with its matching backup if a future schema migration is not backward compatible.

Schema version 1 is created transactionally on first startup. Future migrations must be additive or explicitly versioned, preceded by backup, and must never rewrite archived snapshot files. The current release refuses unknown future database versions rather than guessing how to use them.

## Real-device acceptance

1. Authenticate to actual HA and select the correct LUBA when multiple mowers are present.
2. Check battery, activity, Wi-Fi, RTK and position against HA.
3. Restore/capture real cloud map A, then B; both must remain visible. Re-capture unchanged A and confirm deduplication. Select A's set for a deliberate changed-version capture.
   When direct-cloud telemetry is stale at the charging station, a complete cached
   capture is allowed only with SDK sync evidence and complete frames. Confirm its
   manifest source is `pymammotion_cloud_cache` for stale reports or
   `pymammotion_cloud_memory` for fresh reports, then verify a second capture is
   unchanged and does not create another snapshot. PyMammotion area hashes are
   numeric dictionary keys; the deployed build must include the numeric-key
   redaction fix so this operation returns 200 rather than the former opaque 500.
   The build must also include the complete raw-frame fallback: valid but partial
   SDK GeoJSON is rebuilt whenever it omits a static ID declared by the manifest.
4. Check all zones and obstacles, dock/RTK, longitude/latitude alignment, and optional display offsets.
   While another cloud map is active, open a historical snapshot and confirm that
   its base remains visible while the mower marker and live route are explicitly hidden.
   The mower-centre button must also be disabled until the selected snapshot matches.
5. Confirm that Online, Suspended, Offline and provider-disconnected remain distinct. Put the mower into low-power suspension and verify that a confirmed Wake request reconnects it without starting a job. Also verify the generic Offline fallback can attempt Wake while pause/resume/dock/stop remain disabled until fresh telemetry returns.
6. Confirm a small safe job, pause/resume/dock, and schedule operations in the real integration. Check weekday interpretation on the mower (schema uses 0–6).
7. Test camera streaming and reconnect on the LAN. Some ICE/codec/network failures can only be diagnosed here.
8. For direct mode, test one low-speed nudge and its stop separately over Wi-Fi/cloud and optional Bluetooth, with the mower in sight. HA movement remains unavailable. Verify release, browser close and disconnect behavior before ordinary use.



## Direct account, camera and Bluetooth

See [DIRECT_ACCOUNT.md](DIRECT_ACCOUNT.md) for the complete configuration and feature matrix. For Bluetooth, add `docker-compose.bluetooth.yml` to `COMPOSE_FILE`, set `BLUETOOTH_ENABLED=true` and `BLUETOOTH_ADDRESS`, and provide host BlueZ/D-Bus access to UID 10001. No Bluetooth host mount is required for cloud/Wi-Fi use.

Check history by launching a small job with explicit speed/height/direction. Confirm
that Jobs shows a growing actual-track point count and opens the saved mowing map.
Close the browser, restart the container, and verify the same map remains available.
During a recharge cycle the history status must remain `recharging`; Resume continues
the current mower task, while Load in planner prepares a separate reviewed repeat.
Then cancel deliberately and reuse its settings for a new job. The full backup includes
the compressed `/data/jobs` geometry together with SQLite and immutable maps. Verify
both direct Agora video and the HA ICE-gathering change on the actual camera; a mocked
signaling test cannot establish real media success.


## Switch provider from the dashboard

Settings > Active connection switches between Home Assistant and Mammotion cloud
without restarting the container. Configure both sets of credentials in server
`.env` first; unavailable choices explain that configuration is missing. Secrets
cannot be entered or changed in the dashboard. The saved provider choice in SQLite
supersedes CONNECTION_MODE, which is now the initial default. Use the selector to
change back. Map archives and job history are retained.

Switching stops outstanding manual nudges, closes camera sessions and disconnects
the old provider before starting the new one. It does not pause/cancel an autonomous
mowing job. The page reload clears old forms and disarms movement. Only one provider
runs at a time. Credential changes still require container recreation.
