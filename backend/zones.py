"""One server-side area/name mapping, shared by telemetry and notifications."""

import json
import math
from typing import Any

DEFAULT_ZONES: list[dict[str, Any]] = []  # Configure your own zone_mapping in preferences.


def resolve_zone_name(area_m2, mapping=None):
    if isinstance(area_m2, bool):
        return None
    try:
        area = float(area_m2)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(area):
        return None
    matches = [
        z for z in (DEFAULT_ZONES if mapping is None else mapping) if abs(area - z["area_m2"]) <= 2
    ]
    # Ambiguous overlaps must not produce an invented zone.
    return matches[0]["name"] if len(matches) == 1 else None


def zone_info(area_m2, store):
    try:
        numeric = float(area_m2) if not isinstance(area_m2, bool) else None
        if numeric is not None and not math.isfinite(numeric):
            numeric = None
    except (TypeError, ValueError):
        numeric = None
    return {
        "area_m2": numeric,
        "area_m2_raw": area_m2,
        "zone_name": resolve_zone_name(
            area_m2, store.preferences().get("zone_mapping", DEFAULT_ZONES)
        ),
    }


def resolve_zone_hash(zone_hash, device, map_areas, store):
    """Resolve a device zone hash without mistaking total job area for zone area."""
    try:
        value = int(zone_hash)
    except (TypeError, ValueError, OverflowError):
        return None, None
    if value <= 0:
        return None, None
    for area in map_areas:
        if int(getattr(area, "hash", 0)) == value:
            name = str(getattr(area, "name", "")).strip()
            if name and not (name.lower().startswith("area ") and name[5:].isdigit()):
                return name, "device_hash"
    with store.connect() as db:
        rows = db.execute(
            "SELECT manifest FROM snapshots WHERE device=? ORDER BY created DESC", (device,)
        ).fetchall()
    for row in rows:
        for zone in json.loads(row["manifest"]).get("zones", []):
            if str(zone.get("source_id")) == str(value) and zone.get("name"):
                return str(zone["name"]), "archive_hash"
    return None, None
