# Home Assistant / Mammotion Adapter Specification

## Primary transport
`HA_BASE_URL` + `HA_TOKEN`.

Bootstrap via REST; keep live state with `/api/websocket`.

## Discovery
Do not hard-code a single user's entity ids. Discover Mammotion devices by:
- entity registry/device registry where accessible via HA APIs/websocket commands
- integration/platform identifiers
- domain/entity naming only as fallback

Allow user to pin a discovered mower in settings.

## Capability model
Normalize into internal booleans/details such as:
- `telemetry.live`
- `map.sync`
- `map.complete_fetch`
- `map.progress_geojson`
- `job.start`
- `job.pause`
- `job.resume`
- `job.stop`
- `job.dock`
- `schedule.list`
- `schedule.create/edit/delete/copy/enable/run`
- `camera.live`
- `movement.wifi.nudge_forward/back/left/right`

Capability discovery must inspect current installed services/entities and degrade gracefully.

## Service calls
Backend exposes semantic endpoints, e.g.:
- `POST /api/mower/pause`
- `POST /api/mower/resume`
- `POST /api/mower/dock`
- `POST /api/jobs/start`

Backend translates to current HA service syntax. Never expose generic `/call-any-service` from browser.

## Map complete-fetch strategy
At implementation time inspect current Mammotion-HA source. Existing source evidence shows the integration data model can hold multiple full `map.area` entries at once; do not use only `get_mow_progress_geojson` as the archive source.

If stable HA APIs do not expose the complete structure:
- support `HA_CONFIG_PATH=/ha-config`
- mount HA config read-only
- inspect only Mammotion-specific persisted/cache/state files
- parse using versioned parser modules
- never edit HA files
- copy payload into app storage before parsing when consistency requires it

## Rate limits
Mammotion cloud transport can rate-limit. The app must:
- consume HA live state instead of aggressive polling
- debounce map synchronization
- never continuously trigger map fetch
- make manual sync explicit
- apply backoff after failures
- expose last sync and error

## Camera
Inspect current Mammotion-HA implementation. Prefer existing HA camera entity/WebRTC/Agora mechanisms and server-side token bootstrap. Keep as an adapter because upstream implementation changes.

## Movement
Only expose movement if current installed HA integration reports an API/Wi-Fi capable command path. This project must not initiate or require BLE.
