# Video controls and network survey — 2026-09-27

## Drive

The HA, cloud-browser and native-relay viewers have Stop streaming, Connect camera /
Reconnect, Take snapshot and Fullscreen controls. Stopping closes the viewer and
its subscription; it does not pause mowing or send a movement command. The native
relay waits up to ten seconds for the preceding worker to release its subscription
when restarted immediately. Leaving Drive and changing video source still clean
up the session.

Snapshot downloads a timestamped JPEG of the currently decoded camera frame to
the browser's device. It excludes the minimap and other UI. The button is disabled
before a decoded frame, after stopping, or when frames are stale. No permanent
server image archive is created. Fullscreen uses the browser API and may be
unavailable in embedded browsers; exit with the browser's fullscreen gesture/Esc.

## Signal indicators

Overview and Drive display the reported connection type plus separate Wi-Fi and
3G/4G RSSI readings, quality bars and freshness. These are radio measurements,
not command round-trip latency, throughput, or a promise of reliable control.
Showing a cellular RSSI does not mean the robot currently uses cellular data.

The adapter uses Mammotion-HA's registry keys `wifi_rssi`, `mnet_rssi`, and
`connect_type`; labels/entity IDs can be localized. Only dBm values are accepted.
Cloud mode listens to the pinned SDK broker's unsolicited `toapp_report_data`
messages and separately timestamps `connect` and `locations` reports. Unrelated
MQTT messages do not refresh signal freshness. The observer sends no commands.
Matched secondary cloud telemetry can supply fresh measurements beside HA.

Zero/default RSSI, positive values such as CSQ, NaN and values outside [-127, 0)
are unavailable, not strong or weak signal. Both readings and positions must be
at most 120 seconds old, with no more than 30 seconds between them, to enter the
survey. HA timestamps come from `last_reported` / `last_updated`. Unchanged HA
values that are not re-reported can therefore leave gaps; we do not invent new
measurements from other entity changes.

Quality bands are indicative UI thresholds, not vendor specifications:

| Radio | Excellent | Good | Fair | Weak |
| --- | --- | --- | --- | --- |
| Wi-Fi RSSI | >= -55 | >= -67 | >= -75 | < -75 dBm |
| Cellular RSSI | >= -75 | >= -85 | >= -95 | < -95 dBm |

## Network page

The Network / Rete page displays measured heat-map cells on Esri imagery or an
archived property map, retaining the zone overlay. Filter by radio, mower and
1/7/30/90 days. Wi-Fi and cellular measurements are never averaged together.
Hover/tap a cell for sample count, average/minimum/maximum dBm and last observation.
The legend states thresholds. Export heat map downloads the displayed GeoJSON
aggregation, including time window, count and truncation metadata.

Cells round WGS84 coordinates to four decimal places, roughly 11 m north–south;
east–west size varies with latitude. Color is the arithmetic mean of reported
dBm in that cell and selected period, not a physical propagation prediction.
There is no interpolation over unsampled ground. Blank areas may mean missing
telemetry, stale coordinates, or that the mower has not visited them; they do not
prove an outage. Access-point identity/BSSID is not exposed by these sources.
The robot's own Wi-Fi MAC must not be presented as its access point.

Collection runs on the server every 15 seconds even without browser viewers,
using already received telemetry. It does not scan networks, create a broker or
drive the mower. History starts with this update; it does not backfill HA recorder.
Samples live in the existing `/data/db/mammotion-console.sqlite3` database, with
device, radio, source, signal/position timestamps, coordinates and dBm. Sampling
is bounded, duplicate observations are ignored, and retention is 90 days.
Existing map snapshots are never modified or deleted by retention. Backups of
the application database include the survey. These coordinates are private LAN
data, subject to the application's existing access policy.

Queries return at most 5000 most recently observed cells. The UI explicitly warns
when truncated and suggests shortening the period. History remains available
when the mower is offline; live indicators mark stale or absent measurements.

## Deployment

Keep the existing `.env` and `/data` volume. Startup adds the survey table/indexes
without rewriting archived maps. Refresh the browser after rebuilding. Check
Drive stop/restart/snapshot, then Network after fresh position and signal reports
arrive. A successful empty history response is not evidence of physical sampling.

Upstream references inspected for this implementation:
- https://github.com/mikey0000/Mammotion-HA/blob/main/custom_components/mammotion/sensor.py
- PyMammotion 0.9.6: `data/model/report_info.py`, `messaging/broker.py`, `device/handle.py`.


## Map/network correction (2026-10-05)
