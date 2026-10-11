# Mammotion Console

**Unofficial, self-hosted dashboard for Mammotion robotic mowers.**

Mammotion Console is an independent web application built with **React, TypeScript, Leaflet and FastAPI**. It brings together mower status and controls, map archives and immutable snapshots, zone labels, jobs and job history, network/Wi-Fi telemetry, camera viewing and optional Telegram notifications.

It supports two connection paths:

- **Home Assistant (recommended):** connects to a working Home Assistant installation with a compatible Mammotion integration.
- **Mammotion direct (experimental):** uses your Mammotion account via the unofficial PyMammotion library. Some features depend on the mower model, firmware and network environment.

> [!WARNING]
> **Independent, unofficial project.** Mammotion and LUBA are trademarks of their respective owners. This project is not affiliated with, endorsed by, or supported by Mammotion. Direct-account access uses unofficial interfaces and may conflict with the manufacturer's terms of service. Use at your own risk.
>
> **LAN-only security model:** the application has **no built-in user authentication**. Do not expose its port to the public internet. Use a trusted network, or an authenticated reverse proxy/VPN with appropriate access controls.

## Requirements

- Docker Engine with the Docker Compose plugin (or Docker Desktop with Compose on Windows).
- A compatible Mammotion mower.
- **Either** a Home Assistant installation with the Mammotion integration **or** Mammotion account credentials for experimental direct mode.
- Network access from the **container** to the selected provider. A reachable web browser alone is not enough.

Python and Node.js are **not required on the host** when using Docker: both are included in the build.

## Quick start â€” five steps

### 1. Get the sources

```bash
git clone https://github.com/fraschizzato/mammotion-console.git
cd mammotion-console
```



### 2. Create your private configuration

On Linux/macOS:

```bash
cp .env.example .env
chmod 600 .env
```

On Windows PowerShell:

```powershell
Copy-Item .env.example .env
notepad .env
```

`.env` contains passwords/tokens and **must never be committed**, shared in issues or pasted into logs. `.gitignore` already excludes it.

### 3. Choose your provider

**Option A â€” Home Assistant (recommended):** set at least:

```dotenv
CONNECTION_MODE=home_assistant
HA_BASE_URL=http://YOUR_HA_HOST:8123
HA_TOKEN=YOUR_LONG_LIVED_ACCESS_TOKEN
MOWER_ENTITY=
MQTT_TELEMETRY_ENABLED=false
ALLOW_MANUAL_MOVEMENT=false
```

Create a *Long-Lived Access Token* in your Home Assistant user profile. Use an address reachable **from Docker**; `localhost` refers to the container itself, not a separate Home Assistant installation. `MOWER_ENTITY` can usually remain empty if there is just one discoverable mower. MQTT direct-account telemetry is optional and requires Mammotion credentials; the example disables it for a HA-only installation.

**Option B â€” Mammotion account (experimental):** set at least:

```dotenv
CONNECTION_MODE=mammotion
MAMMOTION_USERNAME=YOUR_ACCOUNT_EMAIL_OR_PHONE
MAMMOTION_PASSWORD=YOUR_ACCOUNT_PASSWORD
MAMMOTION_DEVICE=
BLUETOOTH_ENABLED=false
ALLOW_MANUAL_MOVEMENT=false
AUTO_CAPTURE=false
```

Leave `MAMMOTION_DEVICE` empty if your account has a single mower. For multiple devices, specify the exact device name recognized by the provider. Start with Bluetooth and manual movement disabled; they need additional setup and supervised hardware testing. Direct mode depends on the unofficial Mammotion interfaces and may not work with every account/model/firmware.

> **Switching providers later:** you can configure credentials for both modes in `.env` and select the active provider in the dashboard. `CONNECTION_MODE` specifies the **initial default**; a provider choice saved in the application's database may take precedence on subsequent starts. Changing credentials in `.env` requires container recreation.

### 4. Start the application

On Linux, the provided installer creates the data directories and assigns ownership for the container's unprivileged user (UID 10001):

```bash
bash scripts/install.sh
```

Alternatively, once `./data` has suitable write permissions for UID 10001:

```bash
docker compose up -d --build --wait
docker compose ps
```

On Docker Desktop/Windows (PowerShell):

```powershell
docker compose up -d --build --wait
docker compose ps
```

Docker Desktop file-sharing/volume permissions differ from native Linux. If the container cannot write to `./data`, see [Deployment](docs/DEPLOYMENT.md); do not solve this by running the application as root without understanding the implications.

### 5. Open the dashboard

With the supplied `.env.example` defaults, open **`http://YOUR_DOCKER_HOST:5000`** from your trusted LAN, or **`http://localhost:5000`** on the Docker host.

For local-only access, set `BIND_ADDRESS=127.0.0.1`. To change the **host** port, change `PORT` (for example, `PORT=5010`). The container continues listening on port 5000.

Verify health and logs:

```bash
docker compose ps
docker compose logs --tail=100 console
```

Health endpoints: `/api/health` and `/api/ready`. Readiness may reflect missing provider configuration even when the offline archive is healthy.

## Visual map alignment

Map alignment offsets are configured **server-side**, not in the dashboard preferences. Add to `.env`:

```dotenv
MAP_OFFSET_LAT=0
MAP_OFFSET_LON=0
```

Values are signed **metres**: positive latitude offset moves the displayed map north; positive longitude offset moves it east. Negative values move it south/west. They adjust only the **display of archived map geometry** relative to the base map; they do **not** rewrite historical snapshots, RTK coordinates or mower telemetry. Keep both at `0` unless the displayed geometry needs manual alignment. Restart the service with `docker compose up -d` after editing `.env`. These offsets do not correct SDK conversion errors or actual GPS measurements.

## `.env` reference

All settings below are defined in the supplied `.env.example`. Empty values typically disable an optional feature or allow provider discovery. Boolean values are `true`/`false`.

### Provider and credentials

| Variable | Default | What it does |
|---|---|---|
| `CONNECTION_MODE` | `home_assistant` | Initial provider: `home_assistant` or `mammotion`. A saved dashboard selection can override it later. |
| `HA_BASE_URL` | `http://homeassistant.local:8123` | Home Assistant base URL, accessible from the container. Used in HA mode. |
| `HA_TOKEN` | empty | Home Assistant long-lived access token. **Secret.** |
| `MOWER_ENTITY` | empty | Explicit Home Assistant mower entity ID; leave blank when automatic single-mower selection works. |
| `MAMMOTION_USERNAME` | empty | Account email/phone for direct mode (also used when opting into direct MQTT telemetry). **Sensitive.** |
| `MAMMOTION_PASSWORD` | empty | Mammotion account password. **Secret.** |
| `MAMMOTION_DEVICE` | empty | Explicit device identifier/name, needed when the account has multiple mowers. |
| `MQTT_TELEMETRY_ENABLED` | `true` | Enables the optional Mammotion account MQTT state stream alongside HA; requires Mammotion credentials. Set `false` for a simple HA-only setup. |

### Operation, access and data

| Variable | Default | What it does |
|---|---|---|
| `ALLOW_MANUAL_MOVEMENT` | `false` | Gate for manual movement commands. Keep disabled until the safety behavior is tested with the mower in sight. |
| `BLUETOOTH_ENABLED` | `false` | Enables optional direct Bluetooth functions. Requires host adapter and additional container/BlueZ configuration. |
| `BLUETOOTH_ADDRESS` | empty | Optional Bluetooth device address for direct communication. |
| `AUTO_CAPTURE` | `true` | Enables periodic map/snapshot capture when supported. Set `false` for the first isolated test or when another instance uses the same mower. |
| `CAPTURE_INTERVAL` | `300` | Interval in seconds between automatic capture attempts. |
| `PORT` | `5000` | TCP port exposed **on the host** by Docker Compose. |
| `BIND_ADDRESS` | `0.0.0.0` | Host interface on which Docker publishes the port; `127.0.0.1` restricts access to the host. |
| `APP_ORIGIN` | empty | Explicit browser origin used with a reverse proxy when request `Origin` differs from backend host. Use the exact expected origin, not a wildcard. |
| `DATA_DIR` | `data` | Native-development storage location. Docker Compose overrides it internally to `/data`, bound to host `./data`. |

### Notifications, camera and optional HA cache

| Variable | Default | What it does |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | empty | Telegram bot credential. **Secret.** Configure recipients and enable notifications in dashboard Settings. |
| `CAMERA_SNAPSHOT_URL` | empty | Optional trusted HTTP(S) JPEG/PNG snapshot endpoint for unattended notifications. May contain embedded credentials: treat as **secret**. |
| `TELEGRAM_CAMERA_CAPTURE` | `true` | Allow on-demand server-side camera capture for Telegram when no recent browser frame exists. |
| `HA_HOST_CONFIG_PATH` | `/home/pi/.homeassistant` | Host path to optional read-only HA Mammotion cache; ignored unless the HA-cache Compose override is enabled. |
| `COMPOSE_FILE` | not set | Optional Docker Compose override list, e.g. `docker-compose.yml:docker-compose.ha-cache.yml` on Linux. This is a Compose setting, not a backend setting. |

The optional Home Assistant cache override mounts files **read-only** and is not needed for an ordinary installation. For Bluetooth use the separate `docker-compose.bluetooth.yml` override; see [Deployment](docs/DEPLOYMENT.md) and [Direct account](docs/DIRECT_ACCOUNT.md). Host-specific volume access and Linux D-Bus permissions may require additional configuration.

**Security note:** although secrets are kept server-side and redacted from application output where supported, host administrators with Docker access can inspect container environment variables. Protect the Docker host, `.env`, backups and any diagnostics.

## Everyday operations

```bash
# Check status and recent logs
docker compose ps
docker compose logs --tail=100 console

# Recreate container after changing .env
docker compose up -d --force-recreate --wait

# Rebuild after updating source files
docker compose up -d --build --wait

# Stop (preserves the data directory)
docker compose down
```

Do not run two writable instances against the same `./data` directory. Your history, configuration, maps and snapshots are stored under `./data`; **back this up** before upgrades. See [Deployment](docs/DEPLOYMENT.md) for backup, restore and upgrade scripts.

## Troubleshooting

| Symptom | First check |
|---|---|
| Dashboard not reachable | `docker compose ps`; confirm `BIND_ADDRESS` and `PORT` and host firewall. |
| HA not connecting | Verify `HA_BASE_URL` from the container's network and the long-lived token. Do not use the browser's `localhost` URL for remote HA. |
| Direct login fails | Verify account credentials, model and upstream compatibility; read server logs **without posting credentials**. |
| No mower found | Configure `MOWER_ENTITY` or `MAMMOTION_DEVICE` when automatic discovery is ambiguous. |
| Database/data permission errors | Check write permission for UID 10001 on host `./data`. |
| Settings changed but not applied | Run `docker compose up -d --force-recreate --wait`; a saved provider selection may override the initial mode. |
| Camera/Bluetooth unavailable | These depend on hardware, firmware, OS and networking; use the specific guides before enabling them. |

## Features and architecture

- **Frontend:** React, TypeScript, Vite, Leaflet.
- **Backend:** FastAPI, Python 3.13, SQLite.
- **Integrations:** Home Assistant; experimental Mammotion cloud/MQTT and optional Bluetooth via PyMammotion.
- **Maps:** historical archives, immutable snapshot versions, zones and overlays.
- **Operations:** mower state, compatible commands, job history, network telemetry, optional camera and notifications.
- **Deployment:** Docker Compose with a non-root, read-only container and persistent `./data` storage.

For technical details: [Architecture](docs/ARCHITECTURE.md) Â· [Deployment](docs/DEPLOYMENT.md) Â· [Testing](docs/TESTING.md) Â· [Direct account](docs/DIRECT_ACCOUNT.md) Â· [Security](SECURITY.md).

## Development and testing

Native development requires Python 3.13 and a supported Node.js version (Docker frontend build uses Node 22):

```bash
python -m pip install -r requirements.lock
python -m pip install pytest pytest-asyncio
python -m pytest tests -q
cd frontend
npm ci
npm run lint
npm test
npm run build
```

The project was tested with a Python test suite and a frontend build; **automated tests do not establish full hardware compatibility**. Complete a supervised acceptance test with your own mower before enabling movements or scheduling. Some camera, Bluetooth and direct-cloud features cannot be completely exercised without the corresponding hardware.

## Privacy and security

- Never commit `.env`, `data/`, databases, logs, map exports, camera captures or property screenshots.
- Scrub device IDs, account identifiers, IP addresses, SSIDs, GPS/RTK coordinates and map geometry before sharing bug reports.
- The initial zone mapping is empty by design; configure labels for your own installation in the dashboard.
- Do not forward port 5000 on your router. Use a VPN or authenticated reverse proxy for remote access.
- Do not enable manual movement unless the mower is within sight and safe to operate.

Read [Security](SECURITY.md) before deploying outside a private LAN.

## Contributing

Reproducible issues, fixes and pull requests are welcome. Include relevant software versions and **synthetic, sanitized** logs or fixtures; never upload real tokens, device/account identifiers, precise locations or private map contents.

## License and trademarks

GPL-3.0-only â€” see [LICENSE](LICENSE). Third-party dependencies retain their respective licenses. No official Mammotion imagery is redistributed. Mammotion and related product names are trademarks of their respective owners; this software is independently developed.

