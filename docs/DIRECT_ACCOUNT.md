# Direct Mammotion account and Drive console

The account adapter is independent of Home Assistant. It uses the published,
pinned `pymammotion==0.10.6` package over Mammotion/Aliyun cloud transports.
Python 3.13 is included in the application container; Debian 12 needs no host
Python upgrade. The 2026-09-19 request enabling Bluetooth supersedes the original
no-Bluetooth design. Bluetooth is optional and disabled by default.

## Configuration

Edit `.env` on the server (no browser login form):

```dotenv
CONNECTION_MODE=mammotion
MAMMOTION_USERNAME=your-account-email
MAMMOTION_PASSWORD='your-account-password'
MAMMOTION_DEVICE=Luba-your-device-name
ALLOW_MANUAL_MOVEMENT=false
BLUETOOTH_ENABLED=false
BLUETOOTH_ADDRESS=
```

Leave `MAMMOTION_DEVICE` empty to automatically select exactly one discovered
mower. With multiple mowers, set its exact discovered name in `.env` or select one
in Settings. An explicit environment selection takes precedence. Never paste
account credentials into Diagnostics, job fields or map imports. Passwords with
special characters should be quoted using dotenv syntax. Recreate the container
after edits: `docker compose up -d --build --force-recreate --wait`.

To use HA again, set `CONNECTION_MODE=home_assistant`, `HA_BASE_URL` and `HA_TOKEN`.
Archived maps and job history remain in `/data` across connection-mode changes.
There is no silent fallback to HA when account login fails. Missing credentials
leave the archive available and the provider disconnected.

Refresh tokens remain in process memory only. Restarting performs a new login.
The library handles MQTT reconnect and token renewal; terminal authentication
failures require checking the account and restarting. Simultaneous sessions with
the vendor app/HA may conflict on older Aliyun devices. Pending device-share
invitations must be handled in the vendor app: the console blocks the library's
automatic acceptance instead of modifying account sharing.

## Feature matrix

| Feature | Home Assistant mode | Direct account mode |
|---|---|---|
| Live state, battery, position | Discovered HA entities | Device reports over cloud/MQTT; stale reports disable control |
| Complete map capture | HA services, optional read-only cache | Map synchronization plus BOL/frame completeness checks |
| Permanent map library, versions, import/export | Available offline | Available offline; same archive |
| Pause / resume | Discovered HA services | Current-state-checked commands; resume only for paused work |
| Dock / cancel job | Discovered HA services | Idle/paused docking; pause first before cancellation |
| Availability / wake | HA live state, enriched by a matching configured account | Online, suspended, offline and account-disconnected are distinct; suspended or offline mowers can receive an explicit confirmed cloud wake attempt |
| Job composer and presets | Fields exposed by installed HA service | Zones, speed, height, lane spacing, angle, angle mode, route mode, perimeter/obstacle laps |
| Job history / reuse | Console-launched jobs; explicit submitted settings | Console-launched jobs plus current tasks reported while connected |
| Schedules: list/create/edit/rename/copy/enable/delete/run/refresh | When exposed by HA | Not exposed by this adapter; use the Mammotion app or HA mode |
| Camera | HA WebRTC/Agora bridge | Direct Agora browser SDK with temporary media token, model/subscription dependent |
| Manual Wi-Fi/cloud movement | Disabled: HA buttons lack a scoped stop contract | Opt-in short nudges and explicit zero-speed stop |
| Manual Bluetooth movement | Not routed through HA | Opt-in, selected server-side Bluetooth adapter/device |

Other direct-mode limitations:

* `connected` describes the provider account/session, while `online` requires fresh
  mower telemetry. The dashboard labels a reported low-power state as **Suspended**
  and an unresponsive device as **Offline**. Both may offer **Wake** when a matching
  Mammotion account and device handle are available; wake is rate-limited, requires
  confirmation and never starts mowing. Other controls remain disabled without
  fresh telemetry.

* The overview exposes cloud battery, charge state, speed, cutting height,
  progress, total/elapsed/remaining mowing minutes, Wi-Fi/mobile RSSI, RTK
  fix/localization and satellite counts. Next-task metadata remains unavailable
  when the provider does not report it.
* Advanced job fields such as grass collection/dumping, partial
  start progress and modifying an active route are not exposed. They are rejected,
  not silently sent with guessed meanings. Model-specific speed/height/spacing
  ranges come from the installed library's device model.
* Camera has one direct viewer at a time. First-generation LUBA has no supported
  camera. Encrypted streams use AES-256-GCM2 when the subscription supplies valid key/salt parameters. Missing encryption parameters are reported explicitly. No RTSP endpoint
  is invented. Audio/talkback and multiple camera selection are not implemented.
* The console does not restore an aborted job's completed path from history.
  It does not upload an archived map back to the mower or reconstruct firmware
  progress. A fresh job starts on the currently active mower map.
* Wi-Fi/cloud means the vendor's Internet API/MQTT transport, not an offline
  local-IP mower API. Browser Bluetooth is not used; the server must be in range.

## History and reusable settings

Jobs stores each launch in SQLite before sending it, including mower identity,
ordered zone hashes/names, explicitly submitted settings per zone, request time,
delivery status and observed activity transitions. Uncertain delivery is recorded
as such; a network timeout never proves that mowing did not start. A transition to
idle/docked without a completion result is `ended_unknown`, not claimed completed.

**Reuse settings** loads the job into the composer. Current zone hashes and field
capabilities are revalidated; missing zones or incompatible settings are refused.
Adjust zones/parameters, save a named preset, or use **Review & start** for an
on-demand job. Modify flags and partial-progress markers are removed for a fresh
launch. Explicit settings are preserved; omitted HA defaults cannot be recovered
retroactively. Direct device-reported task settings are preferred when available.
All selected zones in a normal multi-zone launch share one settings object because
that is the firmware command model. For different settings, the confirmed local
per-zone sequence persists the selected order and submits each saved zone profile
as a separate one-zone job only after the prior item was observed active and ended.

**Resume current paused job** resumes the task held by the mower. It is deliberately
separate from history reuse, so clicking an old history item cannot silently resume
a different mower task. Jobs performed while the console was offline are not a
complete retrospective history; direct mode records tasks it actually observes.

## Drive page and movement

Drive combines video, directional controls and a circular, north-up minimap with
the live mower marker and heading. The map can use an archived base from another
cloud map, visibly labelled; it is not an obstacle-avoidance aid.

Enable `ALLOW_MANUAL_MOVEMENT=true` only when ready to test. Choose **Wi-Fi/cloud**
or **Bluetooth** and enable nudges for the current browser session. Each press
sends one low-speed command and then zero speed after 250 ms, or on release.
Holding never floods repeated commands. The transport and mower are pinned for
the nudge and stop; no fallback to a different transport occurs. Browser blur,
release and visibility changes request stop. Server-side stop survives a cancelled
HTTP request. A failed stop disables further nudges until the mower is checked and
the console restarted. This is not a hardware emergency stop: network delays,
radio loss or server failure can delay/prevent delivery. Keep the mower in sight.

For Bluetooth on Debian, pass a USB adapter through to the VM and run BlueZ on the
host. Set `BLUETOOTH_ENABLED=true` and `BLUETOOTH_ADDRESS` to the mower's address.
Use the optional `docker-compose.bluetooth.yml` override to mount the system D-Bus
socket; it does not grant privileged mode or mount all devices. Host BlueZ/D-Bus
policy must allow the container UID (10001) to use the adapter. Missing permissions,
adapter or range are surfaced in Drive; do not broadly disable host security to
hide those errors. Start with a single visible short nudge in a clear area.

## Camera troubleshooting

The HA viewer now gathers ICE candidates into its initial SDP before submitting
the offer and prefers H.264 when the browser exposes it. It also shows HA's actual
structured error message. This removes a negotiation race with the Mammotion
bridge; it does not prove the reported physical-device failure is fixed.

Direct mode bypasses that HA bridge using the Agora SDK. It still needs a valid
Mammotion camera entitlement, working Internet access and compatible firmware.
Try the stream in the vendor app, close competing viewers, check the Drive error,
and reconnect. Use HTTPS at a reverse proxy if the browser restricts WebRTC on a
LAN HTTP origin. Only temporary media credentials are sent to the viewer; account
passwords, HA tokens and MQTT credentials remain server-side. Camera tokens are
never saved in SQLite, maps or logs. No actual camera or physical movement has
been validated in this development environment; use the deployment checklist.


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
