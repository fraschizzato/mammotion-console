import asyncio
import math
import re
from datetime import UTC, datetime

from backend.ha import HaClient, HAError
from backend.job_routes import JobRoutes
from backend.storage import now

DETECTION_VALUES = {
    "direct_touch": 0,
    "slow_touch": 1,
    "less_touch": 2,
    "no_touch": 10,
    "sensitive": 11,
}

TASK_SERVICES = {
    "create": "create_task",
    "edit": "edit_task",
    "rename": "rename_task",
    "enable": "set_task_enabled",
    "delete": "delete_task",
    "copy": "copy_task",
    "refresh": "refresh_tasks",
    "run": "start_task",
}
JOB_FIELDS = {
    "ha_turning_mode",
    "ha_bypass_mode",
    "ha_auto_change_direction",
    "modify",
    "is_mow",
    "is_dump",
    "is_edge",
    "collect_grass_frequency",
    "border_mode",
    "speed",
    "ultra_wave",
    "channel_mode",
    "channel_width",
    "rain_tactics",
    "blade_height",
    "toward",
    "toward_included_angle",
    "toward_mode",
    "mowing_laps",
    "obstacle_laps",
    "auto_change_direction",
    "ride_boundary_distance",
    "start_progress",
    "areas",
    "plan_only",
}
TASK_FIELDS = {
    "name",
    "enabled",
    "weeks",
    "start_time",
    "end_time",
    "start_date",
    "end_date",
    "trigger_type",
    "day",
    "knife_height",
    "speed",
    "edge_mode",
    "route_angle",
    "route_spacing",
    "zone_hashs",
    "work_mode",
    "sub_mode",
    "operating_power",
}


def validate_fields(fields: dict, schema: dict, allowed: set[str]) -> dict:
    if set(fields) - (set(schema) & allowed):
        raise ValueError("One or more fields are not supported by the installed integration")
    for key, definition in schema.items():
        if key in allowed and definition.get("required") and key not in fields:
            raise ValueError(f"{key} is required")
    for key, value in fields.items():
        selector = schema[key].get("selector", {})
        if "number" in selector:
            limits = selector["number"] or {}
            if (
                isinstance(value, bool)
                or not isinstance(value, (float, int))
                or not math.isfinite(value)
                or value < limits.get("min", -1e12)
                or value > limits.get("max", 1e12)
            ):
                raise ValueError(f"{key} is outside the supported range")
            if limits.get("step") == 1 and value != int(value):
                raise ValueError(f"{key} must be a whole number")
        elif "boolean" in selector and not isinstance(value, bool):
            raise ValueError(f"{key} must be a boolean")
        elif "select" in selector:
            options = [
                (o.get("value") if isinstance(o, dict) else o)
                for o in selector["select"].get("options", [])
            ]
            if value not in options:
                raise ValueError(f"{key} must be a supported option")
        elif "text" in selector and (not isinstance(value, str) or len(value) > 200):
            raise ValueError(f"{key} must be short text")
        if key in {"start_time", "end_time"} and (
            not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value)
        ):
            raise ValueError("Times must use HH:MM")
        if key in {"start_date", "end_date"}:
            from datetime import date

            date.fromisoformat(value)
        if key in {"weeks", "zone_hashs", "areas"} and (
            not isinstance(value, list) or len(value) > 100
        ):
            raise ValueError(f"{key} must be a bounded list")
        if key == "weeks" and any(type(x) is not int or x not in range(7) for x in value):
            raise ValueError("Weekdays must be integers from 0 to 6 (Sunday is 0)")
        if key == "zone_hashs":
            converted = []
            for item in value:
                if isinstance(item, str) and re.fullmatch(r"[0-9]{1,20}", item):
                    item = int(item)
                if type(item) is not int or not 0 <= item < 2**64:
                    raise ValueError("Zone hashes must be unsigned 64-bit integer IDs")
                converted.append(item)
            fields[key] = converted
    if fields.get("ride_boundary_distance") and fields.get("mowing_laps") == 0:
        raise ValueError("Edge coverage requires at least one perimeter lap")
    return fields


def provider_zones(ha: HaClient, archive=None) -> list[dict]:
    """Return current provider zones with stable geometry/name aliases when available."""
    details = ha.zone_geometry_details() if hasattr(ha, "zone_geometry_details") else {}
    archived_names = archive.zone_names_by_geometry() if archive else {}
    result = []
    for state in ha.entities():
        attributes = state.get("attributes", {})
        if (
            not state["entity_id"].startswith("switch.")
            or state.get("state") == "unavailable"
            or "hash" not in attributes
        ):
            continue
        zone_hash = str(attributes["hash"])
        geometry = details.get(zone_hash, {})
        reported = attributes.get("friendly_name", state["entity_id"])
        generic = str(reported).casefold().startswith(("zone ", "area "))
        name = reported
        if generic:
            name = (
                geometry.get("mapped_name")
                or archived_names.get(geometry.get("geometry_fingerprint"))
                or reported
            )
        zone = {
            "entity_id": state["entity_id"],
            "name": name,
            # Area hashes are uint64; JSON numbers lose precision in JavaScript.
            "hash": zone_hash,
            "geometry_fingerprint": geometry.get("geometry_fingerprint"),
            # Adapters may expose a native mower value. Legacy/custom adapters
            # used area_m2 directly, so retain that boundary contract.
            "area_m2": geometry.get("native_area_m2", geometry.get("area_m2")),
        }
        if geometry.get("geometry_area_m2") is not None:
            zone["geometry_area_m2"] = geometry["geometry_area_m2"]
        result.append(zone)
    return result


def zones_match_snapshot(current: list[dict], archived: list[dict]) -> bool | None:
    """Fail closed unless every current zone maps one-to-one onto the chosen snapshot."""
    if not current or not archived:
        return None
    if len(current) != len(archived):
        return False
    used: set[int] = set()
    for zone in current:
        source_hash = str(zone.get("hash") or "")
        exact = [
            index
            for index, saved in enumerate(archived)
            if index not in used
            and source_hash
            and source_hash == str(saved.get("source_id") or "")
        ]
        fingerprint = str(zone.get("geometry_fingerprint") or "")
        geometry = [
            index
            for index, saved in enumerate(archived)
            if index not in used
            and fingerprint
            and fingerprint == str(saved.get("geometry_fingerprint") or "")
        ]
        name = str(zone.get("name") or "").strip()
        generic = name.casefold().startswith(("zone ", "area "))
        names = [
            index
            for index, saved in enumerate(archived)
            if name
            and not generic
            and name.casefold() == str(saved.get("name") or "").strip().casefold()
        ]
        matches = (
            exact
            if len(exact) == 1
            else geometry
            if len(geometry) == 1
            else names
            if len(names) == 1 and names[0] not in used
            else []
        )
        if len(matches) != 1:
            return False
        used.add(matches[0])
    return len(used) == len(archived)


class Jobs:
    def __init__(self, ha: HaClient, archive=None):
        self.ha = ha
        self.archive = archive
        self.lock = asyncio.Lock()
        self.sequence_lock = asyncio.Lock()
        self.active_history: str | None = None
        self.routes = JobRoutes(self.ha.store.root)
        # A process can stop after persisting "starting" but before learning
        # whether the provider accepted the command. Never retry that item.
        for sequence in self.ha.store.documents("job_sequence"):
            if sequence.get("status") == "starting":
                transitions = list(sequence.get("transitions", []))
                transitions.append(
                    {
                        "state": "needs_review",
                        "reason": "restart_during_delivery",
                        "at": now(),
                        "index": sequence.get("current_index", 0),
                    }
                )
                self.ha.store.put(
                    "job_sequence",
                    sequence
                    | {
                        "status": "needs_review",
                        "finished": now(),
                        "transitions": transitions[-200:],
                    },
                    sequence["id"],
                )

    def sequences(self):
        selected = self.ha.selected()
        if not selected:
            return []
        identity = selected.get("device_name") or selected["entity_id"]
        return [
            item
            for item in self.ha.store.documents("job_sequence")
            if item.get("device") == identity
        ][:100]

    def active_sequence(self):
        return next(
            (item for item in self.sequences() if item.get("status") in {"running", "starting"}),
            None,
        )

    def history(self):
        selected = self.ha.selected()
        if not selected:
            return []
        identity = selected.get("device_name") or selected["entity_id"]
        return [
            item
            for item in self.ha.store.documents("job_history")
            if item.get("device") == identity
        ][:200]

    @staticmethod
    def _active_status(status: str | None) -> bool:
        return status in {"requested", "mowing", "paused", "returning", "recharging"}

    def _reported_fields(self, source: HaClient, reported: dict | None):
        """Translate a cloud job's synthetic zone entities onto current HA entities."""
        if not reported:
            return None
        fields = dict(reported.get("fields") or {})
        areas = fields.get("areas")
        if not isinstance(areas, list):
            return None
        current = self.zones()
        current_entities = {zone["entity_id"]: zone for zone in current}
        source_entities = {zone["entity_id"]: zone for zone in provider_zones(source, self.archive)}
        translated = []
        for entity in areas:
            if entity in current_entities:
                translated.append(entity)
                continue
            saved = source_entities.get(entity)
            if not saved:
                return None
            matches = [
                zone
                for zone in current
                if str(zone.get("hash")) == str(saved.get("hash"))
                or saved.get("geometry_fingerprint")
                and saved.get("geometry_fingerprint") == zone.get("geometry_fingerprint")
            ]
            if len(matches) != 1:
                return None
            translated.append(matches[0]["entity_id"])
        return fields | {"areas": translated}

    def _matching_snapshot(self, source: HaClient) -> str | None:
        if not self.archive:
            return None
        current = provider_zones(source, self.archive) or self.zones()
        selected = self.ha.selected()
        identity = selected.get("device_name") or selected["entity_id"] if selected else None
        for snapshot in self.archive.list():
            if identity and str(snapshot.get("device", "")).casefold() != str(identity).casefold():
                continue
            if zones_match_snapshot(current, snapshot["manifest"].get("zones", [])) is True:
                return snapshot["id"]
        return None

    @staticmethod
    def _recent(record: dict, hours: int = 72) -> bool:
        try:
            updated = datetime.fromisoformat(record["updated"])
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=UTC)
            return 0 <= (datetime.now(UTC) - updated).total_seconds() <= hours * 3600
        except (KeyError, TypeError, ValueError):
            return False

    def observe(self, source: HaClient | None = None):
        """Record transitions and retain a recharge stop as part of the same job."""
        source = source or self.ha
        live = source.telemetry()
        if not live.get("online"):
            return
        reported = source.current_job()
        reported_fields = self._reported_fields(source, reported)
        state = live.get("state", "unknown")
        session_id = str(reported.get("job_id")) if reported else None
        if not session_id and hasattr(source, "overlay_diagnostics"):
            route_hash = source.overlay_diagnostics().get("route_hash")
            if route_hash and route_hash not in {"0", "1"}:
                session_id = f"route:{route_hash}"

        if self.active_history:
            active = next((r for r in self.history() if r["id"] == self.active_history), None)
            if active and session_id and active.get("device_job_id") not in {None, session_id}:
                active["status"] = "superseded"
                active["finished"] = now()
                self.ha.store.put("job_history", active, active["id"])
                self.active_history = None
        previous = next(
            (
                record
                for record in self.history()
                if self._active_status(record.get("status"))
                and (not session_id or record.get("device_job_id") in {None, session_id})
            ),
            None,
        )
        continue_recharge = bool(
            state == "docked"
            and previous
            and previous.get("status") == "recharging"
            and self._recent(previous)
        )
        if not self.active_history and (
            state in {"mowing", "paused", "returning"}
            or state == "docked"
            and (reported or continue_recharge)
        ):
            selected = self.ha.selected()
            if not selected:
                return
            if previous:
                self.active_history = previous["id"]
            else:
                zones = {z["entity_id"]: z for z in self.zones()}
                fields = reported_fields or {"areas": []}
                record = self.ha.store.put(
                    "job_history",
                    {
                        "device": selected.get("device_name") or selected["entity_id"],
                        "device_job_id": session_id,
                        "started": now(),
                        "status": "recharging" if state == "docked" else state,
                        "fields": fields,
                        "zones": [
                            zones[key]
                            | {"settings": {k: v for k, v in fields.items() if k != "areas"}}
                            for key in fields["areas"]
                            if key in zones
                        ],
                        "settings_source": "device_report"
                        if reported_fields
                        else "route_observation",
                        "settings_note": (
                            "Settings reported by the mower. Timestamp is first observation, not necessarily job start."
                            if reported_fields
                            else "The live route was observed, but this provider did not expose reusable job settings."
                        ),
                        "transitions": [],
                    },
                )
                self.active_history = record["id"]
        if not self.active_history:
            return
        record = next((r for r in self.history() if r["id"] == self.active_history), None)
        if not record:
            self.active_history = None
            return
        if reported_fields:
            record["device_job_id"] = str(reported["job_id"])
            record["reported_fields"] = reported_fields
            if not record.get("zones"):
                zones = {z["entity_id"]: z for z in self.zones()}
                record["fields"] = reported_fields
                record["zones"] = [
                    zones[key]
                    | {"settings": {k: v for k, v in reported_fields.items() if k != "areas"}}
                    for key in reported_fields["areas"]
                    if key in zones
                ]
        if record.get("last_state") == state and not reported:
            return
        transitions = record.get("transitions", [])
        transitions.append({"state": state, "at": now()})
        record |= {"last_state": state, "transitions": transitions[-200:]}
        if state in {"mowing", "paused", "returning"}:
            record["status"] = state
            record["observed_active"] = True
        elif state == "docked" and record.get("observed_active"):
            # Docking can be a recharge cycle inside a larger mower task. The
            # next active report or a new job ID disambiguates it safely.
            record["status"] = "recharging"
        elif record.get("observed_active"):
            record["status"] = "ended_unknown"
            record["finished"] = now()
            self.active_history = None
        self.ha.store.put("job_history", record, record["id"])

    def capture_overlay(self, overlay: dict, source: HaClient | None = None):
        if not self.active_history:
            return None
        source = source or self.ha
        record = next((r for r in self.history() if r["id"] == self.active_history), None)
        if not record:
            self.active_history = None
            return None
        live = source.telemetry()
        snapshot_id = record.get("route", {}).get("snapshot_id") or self._matching_snapshot(source)
        summary = self.routes.save(
            record["id"],
            overlay,
            device=record["device"],
            state=record.get("status", live.get("state", "unknown")),
            snapshot_id=snapshot_id,
            position=live.get("position"),
        )
        if record.get("route") != summary:
            record["route"] = summary
            self.ha.store.put("job_history", record, record["id"])
        return summary

    def route(self, key: str):
        if not any(record["id"] == key for record in self.history()):
            raise KeyError(key)
        return self.routes.response(key)

    def merge_active_overlay(self, overlay: dict):
        if not self.active_history:
            return overlay
        try:
            saved = self.routes.response(self.active_history)
        except KeyError:
            return overlay
        result = dict(overlay)
        for layer in ("path", "progress", "traveled", "mowed"):
            live = result.get(layer) or {}
            if layer in {"traveled", "mowed"} or not live.get("features"):
                result[layer] = saved[layer]
        status = dict(result.get("status") or {})
        status.update(
            {
                "persisted": True,
                "history_id": self.active_history,
                "history_updated": saved["status"].get("updated"),
            }
        )
        result["status"] = status
        return result

    def reusable_fields(self, key: str):
        record = next((r for r in self.history() if r["id"] == key), None)
        if not record:
            raise KeyError(key)
        if not record.get("zones"):
            raise ValueError(
                "This route has no reusable zone settings; choose zones in the planner"
            )
        current = self.zones()

        def current_zone_for(saved: dict):
            exact = [zone for zone in current if str(zone.get("hash")) == str(saved.get("hash"))]
            if len(exact) == 1:
                return exact[0]
            fingerprint = str(saved.get("geometry_fingerprint") or "")
            geometry = [
                zone
                for zone in current
                if fingerprint and fingerprint == str(zone.get("geometry_fingerprint") or "")
            ]
            if len(geometry) == 1:
                return geometry[0]
            name = str(saved.get("name") or "").strip()
            generic = name.casefold().startswith(("zone ", "area "))
            names = [
                zone
                for zone in current
                if name
                and not generic
                and name.casefold() == str(zone.get("name") or "").strip().casefold()
            ]
            return names[0] if len(names) == 1 else None

        areas = []
        for zone in record["zones"]:
            match = current_zone_for(zone)
            if not match:
                raise ValueError("A recorded zone is not on the current map; select zones again")
            areas.append(match["entity_id"])
        fields = record["fields"] | (record.get("reported_fields") or {}) | {"areas": areas}
        if "ha_bypass_mode" in fields:
            fields.pop("ultra_wave", None)
        # Rerunning an aborted job creates a new job. Never silently inherit a
        # partial-progress marker or modify the currently running job.
        fields.pop("start_progress", None)
        fields.pop("modify", None)
        self.validate_job(fields)
        return fields

    def record_control(self, action: str):
        record = next((r for r in self.history() if r["id"] == self.active_history), None)
        if record:
            actions = record.get("requested_actions", [])
            actions.append({"action": action, "at": now()})
            record["requested_actions"] = actions[-100:]
            self.ha.store.put("job_history", record, record["id"])

    def zones(self):
        return provider_zones(self.ha, self.archive)

    def validate_job(self, fields: dict):
        schema = self.ha.capabilities()["job"]["fields"]
        validate_fields(fields, schema, JOB_FIELDS)
        if "ha_bypass_mode" in fields and "ultra_wave" in fields:
            raise ValueError(
                "Choose obstacle detection through the HA selector or ultra_wave, not both"
            )
        if "ha_bypass_mode" in fields and fields["ha_bypass_mode"] not in DETECTION_VALUES:
            raise ValueError("Unknown HA obstacle option; update the adapter before launching")
        zones = {z["entity_id"] for z in self.zones()}
        if not fields.get("areas") or any(x not in zones for x in fields["areas"]):
            raise ValueError("Explicitly choose zones belonging to the selected mower")
        if len(fields["areas"]) != len(set(fields["areas"])):
            raise ValueError("Zone order cannot contain duplicates")
        return fields

    async def start(self, fields: dict, sequence_id: str | None = None):
        active = self.active_sequence()
        if active and active["id"] != sequence_id:
            raise ValueError("A per-zone sequence is already active; cancel it before another job")
        async with self.lock:
            return await self._start(fields)

    async def start_sequence(self, map_id: str, zone_hashes: list[str], profiles: list[dict]):
        async with self.sequence_lock:
            if self.active_sequence():
                raise ValueError("A per-zone sequence is already active")
            selected = self.ha.selected()
            if not selected:
                raise ValueError("Select a mower")
            if not zone_hashes or len(zone_hashes) != len(set(zone_hashes)):
                raise ValueError("Choose a nonempty zone order without duplicates")
            current = {str(zone["hash"]): zone for zone in self.zones()}
            scoped_profiles = [profile for profile in profiles if profile.get("map_id") == map_id]

            def unique_index(key: str):
                candidates: dict[str, list[dict]] = {}
                for profile in scoped_profiles:
                    value = str(profile.get(key) or "")
                    if value:
                        candidates.setdefault(value.casefold(), []).append(profile)
                return {
                    value: matches[0] for value, matches in candidates.items() if len(matches) == 1
                }

            saved_by_hash = unique_index("zone_hash")
            saved_by_geometry = unique_index("geometry_fingerprint")
            saved_by_name = unique_index("zone_name")
            items = []
            for zone_hash in zone_hashes:
                zone = current.get(zone_hash)
                if not zone:
                    raise ValueError("Every sequence zone must belong to the current mower map")
                profile = (
                    saved_by_hash.get(zone_hash.casefold())
                    or saved_by_geometry.get(str(zone.get("geometry_fingerprint") or "").casefold())
                    or saved_by_name.get(str(zone.get("name") or "").casefold())
                )
                if not profile:
                    raise ValueError("Save a profile for every selected zone before sequencing")
                fields = dict(profile.get("fields", {})) | {"areas": [zone["entity_id"]]}
                self.validate_job(fields)
                items.append(
                    {
                        "zone_hash": zone_hash,
                        "zone_name": zone["name"],
                        "entity_id": zone["entity_id"],
                        "profile_id": profile["id"],
                        "fields": self.ha.clean(fields),
                    }
                )
            record = self.ha.store.put(
                "job_sequence",
                {
                    "device": selected.get("device_name") or selected["entity_id"],
                    "map_id": map_id,
                    "created": now(),
                    "status": "starting",
                    "current_index": 0,
                    "observed_active": False,
                    "items": items,
                    "transitions": [{"state": "starting", "at": now(), "index": 0}],
                },
            )
            try:
                launched = await self.start(items[0]["fields"], record["id"])
            except Exception:
                self.ha.store.put(
                    "job_sequence",
                    record
                    | {
                        "status": "delivery_unknown",
                        "finished": now(),
                        "transitions": record["transitions"]
                        + [{"state": "delivery_unknown", "at": now(), "index": 0}],
                    },
                    record["id"],
                )
                raise
            record |= {
                "status": "running",
                "current_history_id": launched["history_id"],
                "transitions": record["transitions"]
                + [{"state": "requested", "at": now(), "index": 0}],
            }
            self.ha.store.put("job_sequence", record, record["id"])
            self.ha.store.event(
                "job", "Per-zone sequence requested", {"id": record["id"], "zones": zone_hashes}
            )
            return {"accepted": True, "sequence_id": record["id"], "zone_count": len(items)}

    async def advance_sequence(self):
        """Advance only after the current item was observed active and later ended."""
        async with self.sequence_lock:
            record = self.active_sequence()
            if not record or record.get("status") != "running":
                return
            live = self.ha.telemetry()
            state = live.get("state")
            index = int(record.get("current_index", 0))
            transitions = list(record.get("transitions", []))
            active_job = next(
                (item for item in self.history() if item["id"] == self.active_history), None
            )
            if state == "docked" and active_job and active_job.get("status") == "recharging":
                return
            if state in {"mowing", "paused", "returning"}:
                if not record.get("observed_active"):
                    transitions.append({"state": "active", "at": now(), "index": index})
                    self.ha.store.put(
                        "job_sequence",
                        record | {"observed_active": True, "transitions": transitions[-200:]},
                        record["id"],
                    )
                return
            if not record.get("observed_active") or state not in {"idle", "docked"}:
                return
            next_index = index + 1
            if next_index >= len(record["items"]):
                transitions.append({"state": "completed", "at": now(), "index": index})
                self.ha.store.put(
                    "job_sequence",
                    record
                    | {
                        "status": "completed",
                        "finished": now(),
                        "transitions": transitions[-200:],
                    },
                    record["id"],
                )
                return
            transitions.append({"state": "starting", "at": now(), "index": next_index})
            record |= {
                "status": "starting",
                "current_index": next_index,
                "observed_active": False,
                "transitions": transitions[-200:],
            }
            self.ha.store.put("job_sequence", record, record["id"])
            try:
                launched = await self.start(record["items"][next_index]["fields"], record["id"])
            except Exception:
                transitions.append({"state": "delivery_unknown", "at": now(), "index": next_index})
                self.ha.store.put(
                    "job_sequence",
                    record
                    | {
                        "status": "delivery_unknown",
                        "finished": now(),
                        "transitions": transitions[-200:],
                    },
                    record["id"],
                )
                return
            transitions.append({"state": "requested", "at": now(), "index": next_index})
            self.ha.store.put(
                "job_sequence",
                record
                | {
                    "status": "running",
                    "current_history_id": launched["history_id"],
                    "transitions": transitions[-200:],
                },
                record["id"],
            )

    def cancel_sequence(self, reason: str = "user_cancelled"):
        record = self.active_sequence()
        if not record:
            return False
        transitions = list(record.get("transitions", []))
        transitions.append(
            {
                "state": "cancelled",
                "reason": reason,
                "at": now(),
                "index": record.get("current_index", 0),
            }
        )
        self.ha.store.put(
            "job_sequence",
            record
            | {
                "status": "cancelled",
                "finished": now(),
                "transitions": transitions[-200:],
            },
            record["id"],
        )
        self.ha.store.event("job", "Per-zone sequence cancelled", {"reason": reason})
        return True

    async def _start(self, fields: dict):
        selected = self.ha.selected()
        if not selected:
            raise ValueError("Select a mower")
        self.validate_job(fields)
        state = await self.ha.request("GET", f"/api/states/{selected['entity_id']}")
        if self.ha.selected() != selected:
            raise ValueError("Selected mower changed; review the job again")
        if state.get("state") in {"unavailable", "unknown"}:
            raise HAError("Mower is unavailable")
        zones = {z["entity_id"]: z for z in self.zones()}
        record = self.ha.store.put(
            "job_history",
            {
                "device": selected.get("device_name") or selected["entity_id"],
                "started": now(),
                "status": "sending",
                "fields": self.ha.clean(fields),
                "zones": [
                    zones[key] | {"settings": {k: v for k, v in fields.items() if k != "areas"}}
                    for key in fields["areas"]
                ],
                "settings_source": "console_request",
                "settings_note": "Explicit submitted values; omitted fields use mower/integration defaults.",
                "transitions": [],
            },
        )
        try:
            service_fields = dict(fields)
            # These are persistent mower settings, not start_mow service fields.
            # Re-read options before dispatch and never start after a failed select.
            selected_options = (
                self.ha.job_selects() if any(k.startswith("ha_") for k in fields) else {}
            )
            selected_switches = (
                self.ha.job_switches() if "ha_auto_change_direction" in fields else {}
            )
            for key in ("ha_turning_mode", "ha_bypass_mode"):
                if key not in service_fields:
                    continue
                option = service_fields.pop(key)
                control = selected_options.get(key)
                if not control:
                    raise HAError("Mower select became unavailable; job was not started")
                entity = control["state"]["entity_id"]
                current = await self.ha.request("GET", f"/api/states/{entity}")
                if current.get("state") in {"unknown", "unavailable"} or option not in current.get(
                    "attributes", {}
                ).get("options", []):
                    raise HAError("Mower select became unavailable; job was not started")
                if key == "ha_bypass_mode":
                    # HA start_mow injects a default of 2; explicitly preserve the selected mode.
                    service_fields["ultra_wave"] = DETECTION_VALUES[option]
                await self.ha.call(
                    "select", "select_option", {"entity_id": entity, "option": option}
                )
            if "ha_auto_change_direction" in service_fields:
                enabled = service_fields.pop("ha_auto_change_direction")
                control = selected_switches.get("ha_auto_change_direction")
                if not control:
                    raise HAError(
                        "Mower auto-direction switch became unavailable; job was not started"
                    )
                entity = control["state"]["entity_id"]
                current = await self.ha.request("GET", f"/api/states/{entity}")
                if current.get("state") not in {"on", "off"}:
                    raise HAError(
                        "Mower auto-direction switch became unavailable; job was not started"
                    )
                await self.ha.call(
                    "switch",
                    "turn_on" if enabled else "turn_off",
                    {"entity_id": entity},
                )
            await self.ha.call(
                "mammotion", "start_mow", service_fields | {"entity_id": selected["entity_id"]}
            )
        except Exception:
            self.ha.store.put("job_history", record | {"status": "delivery_unknown"}, record["id"])
            raise
        self.ha.store.put("job_history", record | {"status": "requested"}, record["id"])
        self.active_history = record["id"]
        self.ha.store.event("job", "Mowing job requested", fields)
        return {"accepted": True, "history_id": record["id"]}

    def schedules(self):
        selected = self.ha.selected()
        cached = self.ha.cache.devices().get(selected["device_name"], {}) if selected else {}
        plans = cached.get("map", {}).get("plan", {})
        result = []
        for s in self.ha.entities():
            attr = s.get("attributes", {})
            if s["entity_id"].startswith("button.") and "task_id" in attr:
                result.append(
                    {
                        "entity_id": s["entity_id"],
                        "name": attr.get("friendly_name", s["entity_id"]),
                        "task_id": str(attr["task_id"]),
                        "details": self.ha.clean(plans.get(str(attr["task_id"]))),
                        "available": s["state"] != "unavailable",
                    }
                )
        return result

    def schedule_history(self):
        selected = self.ha.selected()
        if not selected:
            return []
        rows = [
            row
            for row in self.ha.store.documents("schedule_history")
            if row.get("device") == selected["entity_id"]
        ]
        # Old startup observations of [] do not prove the mower has no plans.
        return [r for r in rows if r.get("action") != "observed" or r.get("schedules")]

    def observe_schedules(self):
        selected = self.ha.selected()
        schedules = self.schedules()
        if not selected or not self.ha.connected or not schedules:
            return
        latest = next((r for r in self.schedule_history() if r.get("action") == "observed"), None)
        if not latest or latest["schedules"] != schedules:
            self.ha.store.put(
                "schedule_history",
                {"device": selected["entity_id"], "schedules": schedules, "action": "observed"},
            )

    async def schedule_action(self, action: str, target: str | None, fields: dict):
        if action not in TASK_SERVICES:
            raise ValueError("Unsupported schedule operation")
        selected = self.ha.selected()
        if not selected:
            raise ValueError("Select a mower")
        service = TASK_SERVICES[action]
        schema = self.ha.services.get("mammotion", {}).get(service, {}).get("fields", {})
        validate_fields(fields, schema, TASK_FIELDS)
        if action in {"create", "edit"} and "enabled" in schema and "enabled" not in fields:
            raise ValueError(
                "Explicitly choose enabled or disabled; upstream otherwise defaults to enabled"
            )
        if "zone_hashs" in fields:
            available_hashes = {int(z["hash"]) for z in self.zones() if z["hash"] is not None}
            if not fields["zone_hashs"] or not set(fields["zone_hashs"]).issubset(available_hashes):
                raise ValueError("Choose current zones for this mower")
        if action in {"create", "refresh"}:
            target = selected["entity_id"]
        elif target not in {s["entity_id"] for s in self.schedules() if s["available"]}:
            raise ValueError("Select an available schedule for this mower")
        before = self.schedules()
        await self.ha.call("mammotion", service, fields | {"entity_id": target})
        self.ha.store.put(
            "schedule_history",
            {"device": selected["entity_id"], "schedules": before, "action": action},
        )
        self.ha.store.event("schedule", f"Schedule {action} requested")
        return {"accepted": True, "message": "Requested; wait for HA state synchronization"}
