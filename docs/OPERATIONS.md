# Everyday operations

## Connect

Configure `HA_BASE_URL`, `HA_TOKEN`, and optionally `MOWER_ENTITY` in `.env`, then recreate the container. Use Settings to select a discovered mower if there is more than one. Diagnostics shows the actual detected service schemas, entity scope and reconnect state. Version information from HA's handshake is always available after connection; Mammotion/PyMammotion versions require the optional integration manifest mount.

## Capture and restore cloud maps

1. Restore the intended property in the Mammotion app.
2. Synchronize the map in Home Assistant. Wait for its map-sync sensor to say `synced`.
3. In Maps, give the capture a name and choose either a new map set or an existing set for a new version.
4. Capture and inspect every zone. Export a ZIP if you want an independent copy.
5. Switch the cloud map and repeat. The archived map selector continues to show all previous captures.

Capturing does not restore the selected map to the mower; cloud restore stays in the official app. Auto-capture checks at `CAPTURE_INTERVAL` (default 300 seconds), does not initiate aggressive map synchronization and never auto-deletes snapshots. Manual acquisition is debounced for 30 seconds to protect the upstream cloud path. Errors and unchanged captures appear in Diagnostics.

Local names and notes can change without modifying immutable geometry. Compare overlays another saved snapshot in an amber dashed outline. Use the layer menu to show/hide individual zones and fixed geometry. Live position/path may belong to a different currently active property and are labeled accordingly. Use fit-to-mower only when the mower is online with valid position data.

## JSON imports

ZIP exports are the normal import path. Developer JSON requires explicit expected zone IDs; do not label active-progress polygons as complete zones. Minimal envelope:

```json
{
  "name": "Front lawn",
  "device": "mower-identity",
  "crs": "EPSG:4326",
  "expected_zone_ids": ["101"],
  "frames_complete": true,
  "geojson": {
    "type": "FeatureCollection",
    "features": [{
      "type": "Feature",
      "properties": {"kind": "zone", "source_id": "101", "name": "Lawn"},
      "geometry": {"type": "Polygon", "coordinates": [[[9,45],[9.001,45],[9.001,45.001],[9,45.001],[9,45]]]}
    }]
  }
}
```

`expected_zone_ids` is an assertion supplied by the exporter/operator, not a guess the console makes. Plain GeoJSON without completeness evidence is classified uncertain and rejected as a complete archive. Inputs are limited to 32 MiB including decompressed content. All coordinates are longitude, latitude in WGS84. Upstream `area`/`hash`/`Name` properties are also understood. Live route/progress is excluded.

## Jobs and schedules

Jobs use currently discovered HA zone switches, not historical IDs from arbitrary archive snapshots. Select zones in desired order, specify supported fields, save a local preset or review and start. To change an in-progress job use `modify` if the integration advertises it. The firmware decides final execution order; the selected list is passed intact.

Schedule operations use upstream task services. A task button often exposes only its name and task ID; full values are shown only if the read-only cache supplies them. Editing sends explicit fields only. Set enabled/disabled deliberately because upstream defaults omitted `enabled` to true. The current schema validates weekday integers 0–6 (UI convention Sunday=0); confirm the real mower's weekday interpretation during deployment acceptance. A successful HA service response means the request was accepted, not that physical work has already started. Refresh and observe state to verify the outcome.

## Troubleshooting

* **HA offline:** verify container-to-HA routing, token, HA certificate and WebSocket access. Reconnect uses capped exponential backoff. Do not paste the token into diagnostics.
* **No mower:** entity/device registry access may require an HA administrator token. Set `MOWER_ENTITY` explicitly if registry access is unavailable, although related entity discovery still needs registry permission.
* **Uncertain/partial map:** update/synchronize Mammotion-HA; inspect missing zones/frame completion and the `map_sync_status` sensor. An older service lacking that sensor cannot establish current-map completeness. Try the read-only cache or an explicit complete-map import.
* **Cache unavailable:** check mount path, directory traversal/read permissions, version and HA's five-minute save delay. The app never modifies HA `.storage`.
* **No satellite tiles:** verify external network access, or choose offline/no tiles; the local map remains available.
* **Camera negotiation fails:** first verify the HA camera itself, then browser WebRTC/codec support and ICE connectivity. The console does not invent RTSP endpoints or bypass Agora negotiation.
* **Movement disabled:** expected for the inspected upstream API. Merely having HA movement buttons or enabling the server flag is insufficient to prove a safe Wi-Fi-only movement/stop method.
* **Permissions on `/data`:** it must be writable by UID 10001. Use the installer on Debian.
* **Damaged database/archive:** stop the service and restore a full backup. Orphan UUID directories after a crash are retained for recovery; preserve them before any manual investigation.


## Direct account expansion (2026-09-20)

For account mode, configure only server `.env` and recreate the container. Check Diagnostics for provider connection and capability reasons. Never paste credentials into the browser. Bluetooth requires host BlueZ/D-Bus permissions; Wi-Fi/cloud requires vendor connectivity. Follow [DIRECT_ACCOUNT.md](DIRECT_ACCOUNT.md) for the feature matrix and physical acceptance procedure.
