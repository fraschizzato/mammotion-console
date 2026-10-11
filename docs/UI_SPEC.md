# UI Specification

## Visual direction
Dense but clean technical dashboard, dark-first, desktop optimized and responsive on tablet/mobile. Avoid generic Bootstrap/admin-template appearance.

## Overview layout
Desktop:
- main map: ~65% width
- status/control rail: ~35%
- lower row: camera + task telemetry + recent events

Map is the visual center of the product.

## Map page
Left/center large interactive map.
Right drawer:
- map set/snapshot selector
- capture current complete map
- sync status
- layer toggles
- zone list with visibility toggles
- metadata/fingerprint/version
- live overlay toggle
- fit selected/all
- export snapshot

Map colors should be visually distinguishable and deterministic per zone. Never rely on color alone; labels and outlines are required.

## Jobs
Two-column composer:
- selected zones/order
- capability-driven job parameters
- estimated summary
- saved presets
- deliberate Start action

## Schedules
Calendar/list hybrid with editable schedule cards. Unsupported fields hidden based on capabilities.

## Drive
Camera, movement pad and a circular GTA-style minimap share one responsive page.
The minimap overlays the video and follows available live position over the archived map.
Choose Wi-Fi/cloud or Bluetooth from connected transports, then arm controls for this session.
Each press sends one short nudge; release, lost capture, blur or page exit requests stop.
Unsupported capabilities show a reason. Changing mower disarms controls and closes video.

Job history lists zones and explicit settings, allows loading them into the composer,
and offers a confirmed resume only for the current paused job. Each observed job can
also retain its planned route, remaining route, actual MQTT/dynamics trace and
estimated mowed area on the matching archived base map. Historical geometry is
server-side and therefore shared across browsers. Loading a historical job restores
its map and settings for review; launching still uses the normal confirmation flow.

## Diagnostics
Tables/cards for transports, versions, entity/service mapping, map acquisition runs, rate-limit signals and sanitized raw capability JSON.


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
