# Persistent Multi-Map Archive Specification

## Problem being solved
Mammotion can have different cloud maps restored at different times. Home Assistant's map view may follow current mow-progress/current map state and can visually narrow to the active mowing zone. The application must preserve complete map datasets locally and render them independently from the live task.

## Core invariant
**Once a complete map snapshot is captured, changing/restoring/syncing the Mammotion cloud map must never make that local snapshot disappear or mutate.**

## Snapshot capture transaction
1. Discover active device and current map source.
2. Request/fetch complete map data, not only current mow-progress data.
3. Verify payload contains enough geometry to qualify as a complete map.
4. Normalize all areas/zones, obstacles, channels and fixed points available.
5. Compute canonical fingerprint.
6. If fingerprint already exists for device, record a sync hit but do not duplicate unless user explicitly requests a forced version.
7. Otherwise create immutable snapshot directory and DB transaction.
8. Generate preview and bounds.
9. Emit `map_snapshot_created` event to UI.

## Completeness checks
A payload should not be considered complete merely because it contains one polygon. Where source metadata tells us expected area hashes/counts, validate all are present. Store a completeness status:
- complete
- partial
- uncertain

Never replace a complete snapshot with a partial capture.

## Map-set workflow for cloud restores
Typical use:
1. User restores Cloud Map A in Mammotion app.
2. User syncs Mammotion-HA.
3. Mammotion Console detects map fingerprint change and captures A.
4. User names set `Estate - Mappa principale`.
5. User restores Cloud Map B.
6. App captures B as another map set/version.
7. Both A and B remain selectable forever.

## Zone identity
Upstream area hashes may change across recreated/restored maps. Store:
- source zone hash/id
- zone name
- geometry fingerprint
- local zone UUID
- optional user-defined alias/group across snapshots

Do not assume area hash alone is globally permanent.

## Coordinate handling
Raw Mammotion map geometry may use local XY coordinates while HA live position uses WGS84 GPS. Preserve all transform/origin data from source. Build an explicit transform layer. Never silently mix coordinate systems.

If current HA/PyMammotion code already provides WGS84 GeoJSON, store that normalized representation but retain raw map coordinates too.

## Live progress
Current mowing path/progress is a separate, expiring overlay. It can be saved as an activity record, but it must not define the base map.

## Export/import
Support:
- export a snapshot/set as ZIP with manifest + raw + normalized GeoJSON
- import exported snapshots
- raw JSON/GeoJSON developer import for diagnostics/fixtures

## Deletion
Manual explicit action only. No automatic retention deletion for map snapshots by default.
