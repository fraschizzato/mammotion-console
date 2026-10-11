# Architecture

## Components

### 1. FastAPI backend
Responsibilities:
- HA REST client
- persistent HA WebSocket client
- capability discovery
- command/service adapter
- map acquisition orchestration
- map normalization + fingerprinting
- SQLite persistence
- map raw/normalized file storage
- camera proxy/bootstrap adapter
- sanitized diagnostics
- SPA static hosting

### 2. React/TypeScript frontend
Responsibilities:
- responsive dark-first dashboard
- live state via backend WebSocket/SSE
- MapLibre/Leaflet renderer
- map library and snapshot selector
- job composer
- schedule manager
- camera UI
- movement pad with safety gating
- diagnostics/settings

### 3. Persistence
Use SQLite for metadata/state and filesystem for larger payloads.

Suggested tables:
- devices
- capabilities
- map_sets
- map_snapshots
- map_zones
- job_presets
- schedule_snapshots
- activity_events
- app_settings (non-secret only)
- sync_runs

Suggested filesystem:
```
/data/
  db/mammotion-console.sqlite3
  maps/<device>/<snapshot_uuid>/raw.json.gz
  maps/<device>/<snapshot_uuid>/normalized.geojson
  maps/<device>/<snapshot_uuid>/manifest.json
  previews/<snapshot_uuid>.webp
  logs/
  backups/
```

## HA adapter
Use server-side:
- REST for bootstrap/states/service calls
- WebSocket for live state_changed/events

Never put HA token in the browser.

Implement `HaClient` plus a `MammotionCapabilityResolver` which maps whatever current entity/service names exist into stable internal capabilities.

## Map acquisition adapters
Interface:
```python
class MapSource(Protocol):
    async def probe(self) -> MapSourceCapabilities: ...
    async def fetch_complete_map(self, device: DeviceRef) -> RawMapPayload: ...
```

Priority:
1. current supported HA Mammotion service response / entity attributes
2. HA-exposed diagnostic or map endpoint if current upstream provides one
3. optional same-host read-only Mammotion state/cache parser
4. optional direct PyMammotion adapter only if necessary and explicitly enabled

Do not couple map persistence to current task progress GeoJSON.

## Normalized map model
Create internal Pydantic model independent of upstream schema:
- zones polygons + source area hash/name
- obstacles/no-go polygons
- channels/lines
- dock/RTK/points
- transforms/coordinate metadata
- source metadata

Persist both raw and normalized forms.

## Fingerprinting
Canonicalize geometry and key metadata then SHA-256 it. Ignore volatile fields (timestamps, current mower pose, progress). The same map fetched twice should fingerprint identically.

## Live overlay
Live state is transient and separate from map snapshot:
- current mower GPS/pose
- current task areas
- path/progress geometry
- status/battery/etc.

Frontend composes selected immutable map snapshot + current overlay.

## Container
Single image, multi-stage:
- Node stage builds SPA
- Python 3.13 stage installs backend and copies built frontend
- app listens on `0.0.0.0:5000`

Volumes:
- `/data`
- optional `/ha-config:ro`

## Security
- token from env/secret file only
- sanitize logs/diagnostics
- CORS off by default/same-origin
- LAN deployment
- no arbitrary HA service caller exposed directly to browser
- explicit backend allowlist for control commands


## Direct account expansion (2026-09-20)

The optional environment-selected Mammotion adapter uses pinned PyMammotion on Python 3.13. HA remains the default. Both providers implement the console contract; account mode does not call HA. See [DIRECT_ACCOUNT.md](DIRECT_ACCOUNT.md) for supported features and limitations.
