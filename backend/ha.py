import asyncio
import json
import random
import time
from contextlib import asynccontextmanager

import httpx
from websockets.asyncio.client import connect

from backend.config import Settings, sanitize
from backend.network import epoch, ha_network
from backend.providers import CacheSource, map_evidence
from backend.storage import Store, now
from backend.zones import zone_info


class HAError(Exception):
    pass


class HaClient:
    def __init__(self, settings: Settings, store: Store):
        self.settings, self.store = settings, store
        self.http = httpx.AsyncClient(
            base_url=settings.ha_base_url or "http://unconfigured.invalid",
            headers={"Authorization": f"Bearer {settings.ha_token.get_secret_value()}"},
            timeout=20,
            follow_redirects=False,
        )
        self.states: dict = {}
        self.services: dict = {}
        self.registry: list = []
        self._registry_signature: tuple[int, int] | None = None
        self._registry_index: dict = {}
        self._device_entities: dict = {}
        self.devices: list = []
        self.connected = False
        self.selection_override: str | None = None
        self.version = "unknown"
        self.last_event: str | None = None
        self.error = "Home Assistant is not configured"
        self.backoff = 0.0
        self.listeners: set[asyncio.Queue] = set()
        self.cache = CacheSource(settings.ha_config_path)
        self.last_command = 0.0
        self.rate_limited_until = 0.0
        self.last_http_status: int | str | None = None
        self.control_lock = asyncio.Lock()
        self._publish_handle: asyncio.TimerHandle | None = None

    def clean(self, value):
        return sanitize(value, self.settings.redaction_secrets)

    async def close(self):
        if self._publish_handle:
            self._publish_handle.cancel()
        await self.http.aclose()

    def current_job(self):
        return None

    async def request(self, method: str, path: str, body=None):
        if time.monotonic() < self.rate_limited_until:
            raise HAError("Home Assistant rate limit is active; wait before retrying")
        try:
            response = await self.http.request(method, path, json=body)
            self.last_http_status = response.status_code
            if response.status_code == 429:
                try:
                    delay = min(300, max(5, float(response.headers.get("Retry-After", "60"))))
                except ValueError:
                    delay = 60
                self.rate_limited_until = time.monotonic() + delay
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            code = (
                exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else "connection"
            )
            raise HAError(
                f"Home Assistant request failed ({code}); inspect HA connectivity and permissions"
            ) from None

    @asynccontextmanager
    async def socket(self):
        url = (
            self.settings.ha_base_url.replace("https://", "wss://").replace("http://", "ws://")
            + "/api/websocket"
        )
        async with connect(url, max_size=MAX_WS_BYTES, open_timeout=15, ping_interval=20) as ws:
            async with asyncio.timeout(15):
                hello = json.loads(await ws.recv())
                if hello.get("type") != "auth_required":
                    raise HAError("Unexpected HA authentication protocol")
                await ws.send(
                    json.dumps(
                        {"type": "auth", "access_token": self.settings.ha_token.get_secret_value()}
                    )
                )
                auth = json.loads(await ws.recv())
                if auth.get("type") != "auth_ok":
                    raise HAError("Home Assistant rejected authentication")
                self.version = auth.get("ha_version", "unknown")
            yield ws

    async def bootstrap(self):
        states, services = await asyncio.gather(
            self.request("GET", "/api/states"), self.request("GET", "/api/services")
        )
        self.states = {s["entity_id"]: s for s in states}
        self.services = {s["domain"]: s["services"] for s in services}

    async def run(self):
        if not self.settings.ha_base_url or not self.settings.ha_token.get_secret_value():
            return
        failures = 0
        while True:
            try:
                async with self.socket() as ws:
                    # Subscribe before REST bootstrap; events queue while the snapshot loads.
                    for key, kind in [
                        (1, "subscribe_events"),
                        (2, "config/entity_registry/list"),
                        (3, "config/device_registry/list"),
                    ]:
                        command = {"id": key, "type": kind}
                        if key == 1:
                            command["event_type"] = "state_changed"
                        await ws.send(json.dumps(command))
                    await self.bootstrap()
                    pending = {2: "entities", 3: "devices"}
                    next_id = 8
                    for key, event in (
                        (4, "entity_registry_updated"),
                        (5, "device_registry_updated"),
                        (6, "service_registered"),
                        (7, "service_removed"),
                    ):
                        await ws.send(
                            json.dumps({"id": key, "type": "subscribe_events", "event_type": event})
                        )
                    subscribed = False
                    async for raw in ws:
                        message = json.loads(raw)
                        if message.get("type") == "result":
                            if message["id"] == 1:
                                if not message.get("success"):
                                    raise HAError("HA state subscription denied")
                                subscribed = True
                                self.connected, self.error, self.backoff = True, "", 0
                                failures = 0
                            if message.get("success") and message["id"] in pending:
                                if pending.pop(message["id"]) == "entities":
                                    self.registry = message["result"]
                                else:
                                    self.devices = message["result"]
                        elif message.get("type") == "event":
                            event = message.get("event", {})
                            event_type = event.get("event_type", "state_changed")
                            if event_type in {"entity_registry_updated", "device_registry_updated"}:
                                kind = (
                                    "entities"
                                    if event_type == "entity_registry_updated"
                                    else "devices"
                                )
                                registry_command = (
                                    "config/entity_registry/list"
                                    if kind == "entities"
                                    else "config/device_registry/list"
                                )
                                pending[next_id] = kind
                                await ws.send(json.dumps({"id": next_id, "type": registry_command}))
                                next_id += 1
                                continue
                            if event_type in {"service_registered", "service_removed"}:
                                services = await self.request("GET", "/api/services")
                                self.services = {s["domain"]: s["services"] for s in services}
                                self.publish()
                                continue
                            data = event.get("data", {})
                            state = data.get("new_state")
                            entity_id = data.get("entity_id")
                            if state:
                                previous = self.states.get(entity_id, {})
                                if state.get("last_updated", "") >= previous.get(
                                    "last_updated", ""
                                ):
                                    self.states[entity_id] = state
                            elif entity_id:
                                self.states.pop(entity_id, None)
                            self.last_event = now()
                            if entity_id not in {e["entity_id"] for e in self.entities()}:
                                continue
                        if subscribed:
                            self.publish()
                    raise HAError("Home Assistant WebSocket disconnected")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                self.error = (
                    f"Home Assistant disconnected ({type(exc).__name__}); reconnecting with backoff"
                )
                failures += 1
                self.backoff = min(60, 2 ** min(failures, 6)) + random.random()
                self.store.event("connection", self.error, {"retry_seconds": round(self.backoff)})
                self.publish()
                await asyncio.sleep(self.backoff)

    def registry_index(self):
        signature = (id(self.registry), len(self.registry))
        if signature != self._registry_signature:
            self._registry_signature = signature
            self._registry_index = {r["entity_id"]: r for r in self.registry}
            self._device_entities = {}
            for row in self.registry:
                if row.get("platform") == "mammotion":
                    self._device_entities.setdefault(row.get("device_id"), set()).add(
                        row["entity_id"]
                    )
        return self._registry_index

    def mowers(self):
        registered = {
            key: r
            for key, r in self.registry_index().items()
            if key.startswith("lawn_mower.") and r.get("platform") == "mammotion"
        }
        result = []
        candidates = set(registered) | (
            {self.settings.mower_entity} if self.settings.mower_entity else set()
        )
        for key in sorted(candidates):
            state = self.states.get(key)
            if not state or not key.startswith("lawn_mower."):
                continue
            if key not in registered and key != self.settings.mower_entity:
                continue
            reg = registered.get(key, {})
            device: dict = next((d for d in self.devices if d["id"] == reg.get("device_id")), {})
            result.append(
                {
                    "entity_id": key,
                    "device_id": reg.get("device_id"),
                    "name": state.get("attributes", {}).get("friendly_name", key),
                    "device_name": device.get("name", ""),
                    "model": device.get("model"),
                    "firmware": device.get("sw_version"),
                    "entry_id": reg.get("config_entry_id"),
                }
            )
        return result

    def selected(self):
        mowers = self.mowers()
        if self.selection_override:
            return next((m for m in mowers if m["entity_id"] == self.selection_override), None)
        preferred = self.store.preferences().get("mower_entity") or self.settings.mower_entity
        if preferred:
            return next((m for m in mowers if m["entity_id"] == preferred), None)
        return mowers[0] if len(mowers) == 1 else None

    def entities(self):
        selected = self.selected()
        if not selected:
            return []
        self.registry_index()
        ids = self._device_entities.get(selected["device_id"], set()) | {selected["entity_id"]}
        return [self.states[key] for key in sorted(ids) if key in self.states]

    def entity_key(self, entity_id: str):
        reg: dict = self.registry_index().get(entity_id, {})
        return reg.get("translation_key") or reg.get("unique_id") or entity_id

    def find(self, suffix: str):
        return next(
            (s for s in self.entities() if self.entity_key(s["entity_id"]).endswith(suffix)), None
        )

    def job_selects(self):
        """Resolve device-scoped selects by registry key, with localized-ID fallback."""
        result: dict = {}
        if "select_option" not in self.services.get("select", {}):
            return result
        for key, suffix, localized, label in (
            ("ha_turning_mode", "turning_mode", "modalita_di_svolta", "Modalità di svolta"),
            ("ha_bypass_mode", "bypass_mode", "rilevamento_ostacoli", "Rilevamento ostacoli"),
        ):
            matches = [
                state
                for state in self.entities()
                if state["entity_id"].startswith("select.")
                and (
                    self.entity_key(state["entity_id"]).endswith(suffix)
                    or state["entity_id"].endswith("_" + localized)
                )
                and state.get("state") not in {"unavailable", "unknown"}
                and state.get("attributes", {}).get("options")
            ]
            if len(matches) == 1:
                result[key] = {"state": matches[0], "name": label}
        return result

    def job_switches(self):
        """Resolve device-scoped persistent job switches exposed by Mammotion-HA."""
        services = self.services.get("switch", {})
        if not {"turn_on", "turn_off"} <= services.keys():
            return {}
        matches = [
            state
            for state in self.entities()
            if state["entity_id"].startswith("switch.")
            and (
                self.entity_key(state["entity_id"]).endswith("auto_change_direction")
                or state["entity_id"].endswith("_inversione_automatica_della_direzione_di_taglio")
            )
            and state.get("state") in {"on", "off"}
        ]
        return (
            {
                "ha_auto_change_direction": {
                    "state": matches[0],
                    "name": "Auto-reverse mowing direction",
                }
            }
            if len(matches) == 1
            else {}
        )

    def capabilities(self):
        mammotion = self.services.get("mammotion", {})
        lawn = self.services.get("lawn_mower", {})
        available = bool(self.connected and self.selected())
        selected = self.selected()
        features = (
            self.states.get(selected["entity_id"], {})
            .get("attributes", {})
            .get("supported_features", 0)
            if selected
            else 0
        )
        caps: dict = {}
        caps["wake"] = {
            "supported": False,
            "reason": "Configure a matching Mammotion account to wake the mower",
        }
        for action, service in {"pause": "pause", "resume": "start_mowing", "dock": "dock"}.items():
            caps[action] = {
                "supported": available
                and service in lawn
                and bool(features & {"pause": 2, "resume": 1, "dock": 4}[action]),
                "reason": "HA lawn_mower service"
                if service in lawn
                else "Service not exposed by HA",
            }
        caps["stop"] = {
            "supported": available and "cancel_job" in mammotion,
            "reason": "Cancel mowing job; this is not a hardware emergency stop",
        }
        for action, service in {
            "map": "get_geojson",
            "job": "start_mow",
            "schedule_create": "create_task",
            "schedule_edit": "edit_task",
            "schedule_rename": "rename_task",
            "schedule_enable": "set_task_enabled",
            "schedule_delete": "delete_task",
            "schedule_copy": "copy_task",
            "schedule_refresh": "refresh_tasks",
            "schedule_run": "start_task",
        }.items():
            caps[action] = {
                "supported": available and service in mammotion,
                "reason": f"mammotion.{service}"
                if service in mammotion
                else "Not exposed by installed integration",
                "fields": self.clean(mammotion.get(service, {}).get("fields", {})),
            }
        for key, select in self.job_selects().items():
            caps["job"]["fields"][key] = {
                "name": select["name"],
                "selector": {"select": {"options": select["state"]["attributes"]["options"]}},
            }
        for key, switch in self.job_switches().items():
            caps["job"]["fields"][key] = {
                "name": switch["name"],
                "description": "Applied through the selected mower's Home Assistant switch before launch.",
                "selector": {"boolean": {}},
            }
        camera = next((s for s in self.entities() if s["entity_id"].startswith("camera.")), None)
        caps["camera"] = {
            "supported": bool(camera),
            "entity_id": camera["entity_id"] if camera else None,
            "reason": "HA WebRTC / Agora signaling" if camera else "No camera entity exposed",
            "protocol": "agora"
            if {"refresh_stream", "get_tokens", "stop_video"} <= mammotion.keys()
            else "webrtc",
        }
        caps["movement"] = {
            "supported": False,
            "reason": "HA nudge buttons do not attest Wi-Fi-only transport or a bounded stop duration. Movement remains disabled until a verified safe API is exposed.",
            "configured": self.settings.allow_manual_movement,
        }
        current = self.states.get(selected["entity_id"], {}).get("state") if selected else None
        for action, allowed in {
            "pause": {"mowing", "returning"},
            "resume": {"paused"},
            "dock": {"idle", "paused", "mowing"},
            "stop": {"mowing", "paused", "returning"},
        }.items():
            if current not in allowed:
                caps[action]["supported"] = False
                caps[action]["reason"] = "Unavailable in current mower state"
        caps["camera"]["supported"] = bool(camera and self.connected)
        if camera and not self.connected:
            caps["camera"]["reason"] = (
                "Home Assistant connection is offline; camera entity is cached"
            )
        return caps

    def telemetry(self):
        selected = self.selected()
        entities = self.entities()
        state = self.states.get(selected["entity_id"], {}) if selected else {}
        mower_state = state.get("state", "unavailable")
        availability = (
            "disconnected"
            if not self.connected
            else "suspended"
            if mower_state in {"hibernating", "sleeping", "suspended"}
            else "offline"
            if mower_state in {"unavailable", "unknown"}
            else "online"
        )
        metrics = []
        for item in entities:
            if item["entity_id"].startswith(("sensor.", "binary_sensor.", "number.")):
                attr = item.get("attributes", {})
                metrics.append(
                    {
                        "id": item["entity_id"],
                        "key": self.entity_key(item["entity_id"]),
                        "name": attr.get("friendly_name", item["entity_id"]),
                        "value": item["state"],
                        "unit": attr.get("unit_of_measurement", ""),
                        "observed_at": item.get("last_reported") or item.get("last_updated"),
                    }
                )
        tracker = next(
            (
                s
                for s in entities
                if s["entity_id"].startswith("device_tracker.")
                and "latitude" in s.get("attributes", {})
            ),
            None,
        )
        area_metric = next((m for m in metrics if m["key"] == "area"), None)
        if area_metric is None:
            area_metric = next(
                (
                    m
                    for m in metrics
                    if m["id"].endswith("_zona")
                    or m["id"].endswith("_area")
                    and not m["id"].endswith("_work_area")
                ),
                None,
            )
        area = area_metric["value"] if area_metric else None
        position_observed = (
            epoch(tracker.get("last_reported") or tracker.get("last_updated")) if tracker else None
        )
        position_online = bool(
            self.connected
            and position_observed is not None
            and 0 <= time.time() - position_observed <= 120
        )
        return self.clean(
            {
                **zone_info(area, self.store),
                "provider": "home_assistant",
                "provider_label": "Home Assistant",
                "connected": self.connected,
                "error": self.error,
                "device": selected,
                "state": mower_state,
                "online": availability == "online",
                "availability": availability,
                "network": ha_network(
                    metrics,
                    availability == "online",
                ),
                "position_observed_at": position_observed,
                "position_online": position_online,
                "metrics": metrics,
                "position": tracker.get("attributes") if tracker else None,
                "last_event": self.last_event,
                "capabilities": self.capabilities(),
            }
        )

    def publish(self):
        # Coalesce sensor bursts; never serialize the entire mower on every HA event.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.publish_message(self.telemetry())
            return
        if self._publish_handle is None:

            def send():
                self._publish_handle = None
                self.publish_message(self.telemetry())

            self._publish_handle = loop.call_later(0.1, send)

    def publish_message(self, data: dict):
        for queue in self.listeners:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(data)

    async def call(self, domain: str, service: str, data: dict, response: bool = False):
        if not self.connected:
            raise HAError("HA is offline; cached controls cannot be executed")
        if service not in self.services.get(domain, {}):
            raise HAError("Service is not supported by the installed integration")
        return await self.request(
            "POST",
            f"/api/services/{domain}/{service}" + ("?return_response" if response else ""),
            data,
        )

    async def control(self, action: str):
        mapping = {
            "pause": ("lawn_mower", "pause"),
            "resume": ("lawn_mower", "start_mowing"),
            "dock": ("lawn_mower", "dock"),
            "stop": ("mammotion", "cancel_job"),
        }
        selected = self.selected()
        if action not in mapping or not selected:
            raise ValueError("Select a discovered mower and supported action")
        async with self.control_lock:
            if time.monotonic() - self.last_command < 1:
                raise HAError("Please wait before sending another command")
            current = await self.request("GET", f"/api/states/{selected['entity_id']}")
            if current.get("state") in {"unavailable", "unknown"}:
                raise HAError("Mower is unavailable")
            allowed = {
                "pause": {"mowing", "returning"},
                "resume": {"paused"},
                "dock": {"idle", "paused", "mowing"},
                "stop": {"mowing", "paused", "returning"},
            }
            if current.get("state") not in allowed[action]:
                raise HAError("Action is incompatible with current mower state")
            result = await self.call(*mapping[action], {"entity_id": selected["entity_id"]})
            self.last_command = time.monotonic()
            self.store.event("control", f"{action.title()} requested")
            return {"accepted": True, "action": action, "result": self.clean(result)}

    async def service_response(self, service: str):
        selected = self.selected()
        if not selected:
            raise ValueError("Select a discovered mower first")
        response = await self.call("mammotion", service, {"entity_id": selected["entity_id"]}, True)
        return response.get("service_response", response)

    async def complete_map(self):
        selected = self.selected()
        if not selected:
            raise ValueError("Select a discovered mower first")
        try:
            sync = self.find("map_sync_status")
            if sync and sync["state"] != "synced":
                raise ValueError(
                    "HA reports an incomplete or syncing map. Synchronize in HA before capturing"
                )
            geo = await self.service_response("get_geojson")
            raw = await self.service_response("get_map_data")
            payload = map_evidence(raw, geo, "ha-services")
            if sync:
                current = await self.request("GET", f"/api/states/{sync['entity_id']}")
                payload["sync_verified"] = current.get("state") == "synced"
            else:
                payload["sync_verified"] = False
            return payload
        except HAError:
            return self.cache.fetch(selected["device_name"])


MAX_WS_BYTES = 32 * 1024 * 1024
