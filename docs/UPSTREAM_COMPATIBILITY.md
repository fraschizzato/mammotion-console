# Upstream compatibility — inspected 2026-09-19

Source checkouts (research only; not runtime dependencies):

* [Mammotion-HA](https://github.com/mikey0000/Mammotion-HA) `d8928967c89702f22c70d470072be6145c2a4667`, manifest 0.6.5-beta11, pins PyMammotion 0.9.0b9.
* [PyMammotion](https://github.com/mikey0000/PyMammotion) `3dd7a961fa3a3e37b7721b46fc6626add902b9e3`.
* [ha-mammotion-assets](https://github.com/mikey0000/ha-mammotion-assets) `cf9773d10393e3ef5ef2b98de577fbaba253bb26`.

`services.py` exposes response-only `get_geojson`, `get_mow_path_geojson`, and `get_mow_progress_geojson`. Base GeoJSON includes all cached static geometry. Progress is never an archive source. `get_geojson` can start a five-minute report stream, so acquisition is debounced, not polled aggressively.

`get_map_data` returns only `area`, `svg`, and `area_name`; it is NOT a complete raw map export. Cross-check area hashes/frame totals against the base GeoJSON. Archive both responses. A read-only cache provider preserves additional raw geometry and root manifests when available. Without evidence of completeness, report uncertain and refuse automatic archival.

`config.py` stores version 1.2 in `.storage/mammotion.<entry_id>`: `data.devices[device_name]`, with separate `transports`. Saves can lag five minutes. Never write these files. Do not read HA auth or config-entry storage.

PyMammotion `HashList` contains area/obstacle/path and additional static types; frames use `total_frame`, `data`, `current_frame`, `data_couple`. Generated base GeoJSON is WGS84 and properties include `hash`, `type_name`, `type_id`, `Name`. Preserve raw XY and origin metadata; reject unreferenced XY as geographic coordinates.

Schedule services are `create_task`, `edit_task`, `rename_task`, `set_task_enabled`, `delete_task`, `copy_task`, `refresh_tasks`, `start_task`. Existing tasks target button entities with `task_id`; create/refresh target the mower. Button state does not expose full schedule parameters; optionally read `map.plan` from cache, otherwise show fields as unknown. Discover service schemas at runtime; `start_mow.areas` expects switch entity IDs, whereas task `zone_hashs` expects numeric hashes.

Camera uses HA camera WebRTC signaling and upstream Agora bridging. The console relays HA signaling server-side; no invented RTSP URL or persisted Agora credentials.

Movement buttons `emergency_nudge_forward/back/left/right` are ambiguous: upstream defaults to BLE unless integration option `movement_use_wifi` is true. Their availability alone does not prove Wi-Fi. Do not enable these from entity names alone. This HA-only finding is superseded for the explicitly requested direct-account adapter described below.

Additional source-verified details incorporated during tests:

* `switch.py` area switches expose `attributes.hash` (not `area_hash`).
* `device_tracker.py` exposes mower heading as `direction`.
* Static RTK/dock GeoJSON uses `type_name=station` with the station name identifying the subtype.
* `services.py` validates schedule weeks as **0–6**, despite a YAML example that suggests a different range. Runtime schema code takes precedence over the example. The console labels weekdays with Sunday=0; confirm the actual mower calendar in device acceptance.
* `edit_task` defaults omitted `enabled` to true. The console requires an explicit enabled state when that field is advertised.
* `coordinator.py` `map_sync_status` exposes `synced`, `syncing`, `out_of_sync`; `synced` checks the root BOL hash, all required frames and area-name coverage. Capture requires this evidence before/after HA service acquisition.
* PyMammotion `send_movement` sends linear/angular speeds without a duration. HA exposes no equivalent guarded movement-stop service. Therefore upstream button availability cannot authorize even nominal short nudges.
* HA core `camera/webrtc/{get_client_config,offer,candidate}` uses a subscription event for session/answer/candidates, not an immediate SDP-only response. A dedicated upstream socket is kept alive for each browser session.

HA core references: [camera signaling](https://github.com/home-assistant/core/blob/dev/homeassistant/components/camera/webrtc.py), [lawn mower feature flags](https://github.com/home-assistant/core/blob/dev/homeassistant/components/lawn_mower/const.py). The latter confirms start=1, pause=2, dock=4; capability detection intersects flags with installed services.

## LUBA 3 route-setting completion (2026-10-08)

Pinned PyMammotion 0.10.6 was inspected again against the sanitized real LUBA 3
firmware `2.3.30.39`. `OperationSettings` and `NavReqCoverPath` carry
`auto_change_direction` and `ride_boundary_distance`. Upstream documents the
latter as the official app's Edge Coverage switch: `0.0` off and `0.5` on, with no
unit. `DeviceType` gates auto-reverse by model and firmware and Edge Coverage by
model plus nonzero perimeter laps. The console follows those gates and canonicalizes
lower-cased IoT device names before calling the SDK route builder.

Current Mammotion-HA exposes `auto_change_direction` as a device-scoped switch,
while its `start_mow` service does not include either field. HA mode therefore
applies that switch before launch when discovery is unambiguous. Edge Coverage is
offered through direct-account mode only because HA exposes no safe equivalent.
# Direct account expansion (2026-09-20)

The published `pymammotion==0.9.0b9` wheel is pinned and its installed source was inspected. It differs materially from the older checkout above: authentication lives in `client_auth.CloudAuthMixin`; `Priority.USER` bypasses the background queue; camera startup no longer sends an erroneous pre-emptive stop; `OperationSettings` includes model-aware path-order encoding. Contract tests use real wheel models/command serialization, with network calls mocked.

The runtime is now Python 3.13. The user explicitly authorized Bluetooth movement on 2026-09-19, superseding the original prohibition. BLE libraries are installed but no scan occurs unless configured. Manual movement pins the selected `Transport` and uses its user-command path plus zero-speed stop; it does not use `DeviceHandle.send_raw`, which can fall back between radios. The library's share-confirmation side effect is blocked before network submission.

See [DIRECT_ACCOUNT.md](DIRECT_ACCOUNT.md) for configuration, full feature matrix, camera restrictions, history semantics and physical acceptance. Neither real account login, camera media nor actual mower motion has been validated here.


## Runtime import correction (2026-09-20)

The deployed direct-account image failed before login because PyMammotion imports
`packaging.version` without bringing `packaging` into the production environment.
Development test dependencies had masked the missing runtime dependency. The app
now declares it explicitly (locked to 26.3), checks SDK imports during Docker build,
and reports initialization failures without exception contents or credential data.
A configured account displays Connecting while login starts. Rebuild and recreate
the container after transferring Dockerfile, pyproject.toml, requirements.lock and
backend/cloud.py. Existing .env and data remain in place.

Regression validation: 47 backend tests passed; Ruff and mypy passed. Real account
login still requires verification on the destination machine.





## SDK 0.9.6 upgrade (2026-09-26)

PyPI release https://pypi.org/project/pymammotion/0.9.6/ verified current.
Compared installed source with 0.9.4: camera subscription/model unchanged;
route retrieval now uses is_mow_path_current, treats hashes 0/1 as no route,
and repairs partial path/progress caches. Adapter guards follow these semantics.
Universal lock regenerated; all 82 backend tests, Ruff/format and mypy passed.
Real video remains unresolved; this SDK change is not evidence of a video fix.


## Radio measurements (2026-09-27)

Inspected current Mammotion-HA sensor.py: wifi_rssi / mnet_rssi use dBm and
connect_type uses device_connection. PyMammotion 0.9.6 report_info.ConnectData
contains wifi_rssi, mnet_rssi, connect_type and used_net. Observing its public
broker.subscribe_unsolicited API timestamps actual connect/location reports;
general report liveness is insufficient because partial reports preserve old
fields. Device Wi-Fi MAC is not an AP BSSID. No additional commands are sent.

## PyMammotion 0.9.9 audit (2026-09-28)

Verified the PyPI wheel digest and compared source with 0.9.6. Dependency metadata
is unchanged. Stream subscription adds optional `all_cameras=False`, preserving
the current front-camera call. Handle/broker and ConnectData interfaces used by
network sampling remain unchanged. WorkData renames `path` to `plan`; the console
uses route hash/index fields instead. Task cleanup now resets completed/aborted
job settings. SDK logging remains suppressed by the adapter. Both dependency
files and adapter version reporting now pin 0.9.9.

## PyMammotion 0.10.6 upgrade (2026-10-03)

PyPI metadata reports 0.10.6 as latest. Downloaded and verified its wheel SHA256,
then compared SDK source against 0.9.9. Runtime dependency metadata is unchanged,
so transitive pins remain unchanged. Both dependency files and adapter reporting
now pin 0.10.6. Stream-subscription interfaces used by the video relay remain
compatible. The path watcher now retries an unhandled route hash after the
fetch gate reopens, and invalidates stale route caches consistently.

OperationSettings no longer has rain_tactics. The direct-cloud adapter now uses
build_route_information from the SDK for model-specific route encoding and no
longer offers the obsolete per-job rain field. Existing profiles containing that
field fail validation before any command; recreate those profiles without it.
Rain protection is a separate device setting in the SDK and is not silently
changed during a job launch. HA-mode schema remains controlled by HA.

105 backend tests, Ruff/format, mypy and dependency consistency checks passed,
including explicit route parameters and rejection of the old rain field before
dispatch. Frontend is unchanged from its previous browser/build validation.

Real-device check before deploying this update: the mower was mowing, MQTT
reports were fresh, but map_matches and route_current were false and all overlay
collections were empty. The upstream retry change is relevant to this state,
but is not yet proof that completed-area rendering works on the physical mower.


## Map/network correction (2026-10-05)


## RTK units and complete-map projection correction (2026-10-10)

PyMammotion 0.10.6 stores `location.RTK.latitude/longitude` in radians and
stores the dock's unfortunately named `latitude/longitude` fields as local ENU
metres. Treating either pair as WGS84 degrees placed fallback geometry at incorrect geographic locations rather than near the mower.

The adapter retains the SDK-generated WGS84 geometry and supplements only raw
objects omitted by that derived representation. Local fallback conversion changes
radians to degrees, projects map points with RTK yaw, and converts dock ENU offsets
relative to the RTK. A map/device proximity guard rejects implausible results before
archive creation. Existing immutable snapshots are not rewritten.

A synthetic, geographically neutral fixture is used for public validation of complete-map
geometry, dock and RTK placement. No private site bounding boxes or device locations are
included in this repository. Shared display calibration uses
`MAP_DISPLAY_ROTATION_DEGREES`, metric east/north offsets and an optional paired
origin. It is a deployment-specific browser transform, not a replacement for the
RTK conversion fix above. Deployment and a new capture are required to create the corrected immutable
snapshot on the target host.

## Wake and availability audit (2026-10-11)

Current PyMammotion 0.10.8 was inspected without changing the project's tested
0.10.6 pin. Both versions implement `wake_device` as a Mammotion cloud HTTP wake
request and identify sleeping state from work mode/system status 48. Mammotion-HA
does not currently expose a lawn-mower wake feature. The adapter therefore keeps
account connectivity, fresh device telemetry and low-power suspension separate.
An exact sleeping report is labelled `suspended`; missing/stale device transport is
labelled `offline`. Either can expose an explicit confirmed cloud wake attempt when
the account and selected device handle are available, while all motion/job controls
still require fresh state. A 30-second rate limit prevents repeated wake requests.
