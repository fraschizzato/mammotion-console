"""Pinned PyMammotion boundary; no Home Assistant requests or credential cache."""

import asyncio
import base64
import copy
import importlib
import logging
import math
import threading
import time
from contextlib import suppress
from typing import Any

import aiohttp
from pydantic import SecretStr

from backend.config import Settings
from backend.ha import HaClient, HAError
from backend.maps import geometry_area_m2, geometry_fingerprint, normalize
from backend.movement import Movement
from backend.network import signals
from backend.providers import local_map_geojson, map_evidence, merge_missing_map_features
from backend.storage import Store, now
from backend.zones import resolve_zone_hash, zone_info

STATES = {
    10: "idle",
    11: "idle",
    13: "mowing",
    14: "returning",
    15: "docked",
    17: "locked",
    19: "paused",
    48: "hibernating",
}

DYNAMIC_SESSION_STATES = {13, 14, 19}
# A docked mower can be charging in the middle of a task. Keep the accumulated
# path until idle/hibernate or a different job ID proves a new session.
DYNAMIC_SESSION_END_STATES = {10, 11, 48}
TELEMETRY_FRESH_SECONDS = 120


def preload_sdk():
    """Resolve SDK imports before API workers and provider tasks can race them."""
    for module in (
        "pymammotion.client",
        "pymammotion.transport.base",
        "pymammotion.utility.device_type",
        "pymammotion.messaging.command_queue",
        "pymammotion.data.model.device_config",
        "pymammotion.data.model.coordinates",
        "pymammotion.data.model.generate_geojson",
    ):
        importlib.import_module(module)


class CloudClient(HaClient):
    """Expose the console adapter contract through cloud device handles.

    Synthetic entity identifiers are internal console IDs, never HA resources.
    The service-shaped field schema keeps existing job validation/UI reusable.
    """

    def __init__(self, settings: Settings, store: Store):
        super().__init__(
            settings.model_copy(
                update={"ha_base_url": "", "ha_token": SecretStr(""), "ha_config_path": None}
            ),
            store,
        )
        self.settings = settings
        self.client: Any = None
        self.session: aiohttp.ClientSession | None = None
        self.error = "Mammotion account is not configured"
        self.version = "PyMammotion 0.10.6"
        self.subscriptions: list[Any] = []
        self.ble_error = "Bluetooth is disabled in .env"
        self.auth_failed = asyncio.Event()
        self.movement = Movement()
        self.movement_epoch = 0
        self.camera_lock = asyncio.Lock()
        self.coverage_key: tuple | None = None
        self.coverage_segments: dict = {}
        self.coverage_lock = threading.Lock()
        self.coverage_width_cache: dict[str, float] = {}
        self.overlay_task: asyncio.Task | None = None
        self.overlay_attempt = 0.0
        self.overlay_fetch_status = "not_requested"
        self.route_layers: dict = {}
        self.route_frame: tuple | None = None
        self.route_progress: tuple | None = None
        self.dynamics_task: asyncio.Task | None = None
        self.dynamics_attempt = 0.0
        self.dynamics_status = "not_requested"
        self.dynamics_key: tuple | None = None
        self.dynamics_layer: dict = {}
        self.dynamics_empty_responses = 0
        self.dynamics_epochs: dict[str, int] = {}
        self.dynamics_active: set[str] = set()
        self.dynamics_job_ids: dict[str, int] = {}
        self.position_trace_key: tuple | None = None
        self.position_trace_segments: list[list[list[float]]] = []
        self.position_trace_last_at = 0.0
        self.position_trace_breaks = 0
        self.last_wake = 0.0
        self.radio_reports: dict[str, dict] = {}

    def handles(self):
        return (
            [
                h
                for h in self.client.device_registry.all_devices
                if hasattr(h.snapshot.raw, "map") and hasattr(h.snapshot.raw, "work")
            ]
            if self.client
            else []
        )

    def mowers(self):
        return [
            {
                "entity_id": f"mammotion.{h.device_name}",
                "device_id": h.device_name,
                "device_name": h.device_name,
                "name": h.device_name,
                "model": type(h.snapshot.raw).__name__,
            }
            for h in self.handles()
        ]

    def selected(self):
        mowers = self.mowers()
        if self.selection_override:
            return next((m for m in mowers if m["entity_id"] == self.selection_override), None)
        configured = self.settings.mammotion_device
        if configured:
            return next((m for m in mowers if m["device_name"] == configured), None)
        preferred = self.store.preferences().get("mower_entity")
        return next(
            (m for m in mowers if m["entity_id"] == preferred),
            mowers[0] if len(mowers) == 1 else None,
        )

    def handle(self):
        selected = self.selected()
        return self.client.mower(selected["device_name"]) if selected else None

    async def _network_report(self, device, message):
        system = getattr(message, "sys", None)
        report = getattr(system, "toapp_report_data", None)
        if report is None:
            return
        saved = self.radio_reports.setdefault(device, {})
        timestamp = time.time()
        connect = report.connect
        if connect is not None:
            used = str(connect.used_net).upper()
            connection = (
                "WIFI"
                if used.endswith("WIFI")
                else "3G/4G"
                if used.endswith("MNET")
                else {1: "3G/4G", 2: "WIFI"}.get(connect.connect_type, "unknown")
            )
            saved.update(
                wifi=connect.wifi_rssi,
                cellular=connect.mnet_rssi,
                connection=connection,
                observed=timestamp,
            )
        if report.locations:
            saved["position_time"] = timestamp

    async def _changed(self, snapshot):
        for handle in self.handles():
            raw = handle.snapshot.raw
            self._track_dynamics_session(handle)
            self._record_position_trace(handle)
            if (
                self.supports_dynamics_line(handle)
                and raw.report_data.dev.sys_status in DYNAMIC_SESSION_STATES
                and hasattr(handle, "set_mow_path_fetch_enabled")
            ):
                # Registered before the SDK route watcher: once firmware data
                # proves live-progress support, prevent a large unstable route
                # download on the same state update.
                handle.set_mow_path_fetch_enabled(value=False)
        self.last_event = now()
        self.publish()

    async def _auth_error(self, *args):
        self.connected = False
        self.error = (
            "Mammotion session expired or was rejected; check account and restart the console"
        )
        self.auth_failed.set()
        self.publish()

    async def run(self):
        try:
            await self._run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.connected = False
            # Exception text and traceback may contain account/session material.
            self.error = (
                f"Mammotion initialization failed ({type(exc).__name__}); "
                "rebuild the application image and check runtime dependencies"
            )
            self.publish()
            await self._disconnect()

    async def _run(self):
        if (
            not self.settings.mammotion_username.get_secret_value()
            or not self.settings.mammotion_password.get_secret_value()
        ):
            return
        self.error = "Connecting to Mammotion account"
        self.publish()
        # The upstream library logs raw account/session structures in some paths.
        # Keep its logging sink private; surface only our controlled diagnostics.
        for name in ("pymammotion", "aliyunsdkcore", "alibabacloud_tea"):
            logger = logging.getLogger(name)
            logger.handlers = [logging.NullHandler()]
            logger.propagate = False

        from pymammotion.client import MammotionClient
        from pymammotion.transport.base import AuthError

        class ConsoleClient(MammotionClient):
            @staticmethod
            async def _connect_iot(cloud_client):
                async def refuse_share(*args, **kwargs):
                    raise HAError(
                        "Handle pending device shares in the Mammotion app before connecting"
                    )

                cloud_client.confirm_share = refuse_share
                await MammotionClient._connect_iot(cloud_client)

        async def check_request(session, trace_config_ctx, params):
            if params.url.path.endswith("/share/device/confirm"):
                raise HAError("Handle pending device shares in the Mammotion app before connecting")

        attempts = 0
        while True:
            self.client = ConsoleClient()
            self.client.on_unrecoverable_auth_error = self._auth_error
            trace = aiohttp.TraceConfig()
            trace.on_request_start.append(check_request)
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30), trace_configs=[trace]
            )
            try:
                async with asyncio.timeout(120):
                    await self.client.login_and_initiate_cloud(
                        self.settings.mammotion_username.get_secret_value(),
                        self.settings.mammotion_password.get_secret_value(),
                        session=self.session,
                    )
                self.connected, self.error, self.backoff = True, "", 0
                for handle in self.handles():
                    if self.supports_dynamics_line(handle):
                        # LUBA 3 changes route/map hashes while mowing. Avoid the
                        # very large automatic cover-path saga; the console uses
                        # the firmware's dedicated live cut-path endpoint instead.
                        handle.set_mow_path_fetch_enabled(value=False)
                    self.subscriptions.append(handle.subscribe_state_changed(self._changed))

                    async def network_message(message, device=handle.device_name):
                        await self._network_report(device, message)

                    self.subscriptions.append(handle.broker.subscribe_unsolicited(network_message))
                self.client.setup_all_mower_watchers()
                self.publish()
                await self.connect_bluetooth()
                self.publish()
                # Library handles MQTT reconnection and refresh-token rotation.
                # Do not loop password logins after an authentication rejection.
                await self.auth_failed.wait()
                return
            except AuthError:
                await self._auth_error()
                return
            except HAError as exc:
                self.connected, self.error = False, str(exc)
                self.publish()
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                self.connected = False
                self.error = "Mammotion connection failed; check credentials, account session and Internet connectivity"
                attempts += 1
                self.backoff = min(300, 30 * 2 ** min(attempts - 1, 4))
                self.publish()
            finally:
                await self._disconnect()
            await asyncio.sleep(self.backoff)

    async def connect_bluetooth(self):
        if not self.settings.bluetooth_enabled:
            return
        handle = self.handle()
        if not self.settings.bluetooth_address or not handle:
            self.ble_error = "Set BLUETOOTH_ADDRESS and select one mower in .env"
            return
        try:
            from bleak import BleakScanner

            device = await BleakScanner.find_device_by_address(
                self.settings.bluetooth_address, timeout=10
            )
            if device is None:
                raise ValueError("Device not found")
            await self.client.add_ble_to_device(handle.device_name, device)
            await self.client.connect_ble(handle.device_name)
            self.ble_error = ""
        except Exception:
            self.ble_error = (
                "Bluetooth unavailable: check adapter, address, range and host BlueZ permissions"
            )

    async def _disconnect(self):
        self.connected = False
        if self.overlay_task:
            self.overlay_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.overlay_task
            self.overlay_task = None
        self.subscriptions.clear()
        if self.client:
            with suppress(Exception):
                await self.client.stop()
        if self.session:
            await self.session.close()

    async def close(self):
        with suppress(Exception):
            await self.movement.stop()
        await self._disconnect()
        await super().close()

    def entities(self):
        handle = self.handle()
        if not handle:
            return []
        names = {str(a.hash): a.name for a in handle.snapshot.raw.map.area_name}
        return [
            {
                "entity_id": f"switch.zone_{key}",
                "state": "on",
                "attributes": {
                    "hash": int(key),
                    "friendly_name": names.get(str(key), f"Zone {key}"),
                },
            }
            for key in handle.snapshot.raw.map.area
        ]

    def zone_geometry_details(self):
        """Associate volatile mower hashes with stable geometry diagnostics.

        Polygon area is derived from the map coordinates. It is not the native
        area value reported by the mower and must not drive the user's +/-2 m2
        zone-name mapping.
        """
        handle = self.handle()
        if not handle:
            return {}
        raw = handle.snapshot.raw
        try:
            geojson = local_map_geojson(raw.map.to_dict(), raw.location.to_dict())
        except (AttributeError, TypeError, ValueError):
            geojson = getattr(raw.map, "generated_geojson", {})
        result = {}
        for feature in geojson.get("features", []) if isinstance(geojson, dict) else []:
            properties = feature.get("properties") or {}
            kind = str(properties.get("kind", properties.get("type_name", ""))).casefold()
            if kind not in {"zone", "area"}:
                continue
            source_id = properties.get("source_id", properties.get("hash", feature.get("id")))
            if source_id is None:
                continue
            try:
                result[str(source_id)] = {
                    "geometry_fingerprint": geometry_fingerprint(feature["geometry"]),
                    "geometry_area_m2": round(geometry_area_m2(feature["geometry"]), 2),
                }
            except (KeyError, TypeError, ValueError):
                continue
        return result

    def zone_geometry_fingerprints(self):
        return {
            zone_hash: details["geometry_fingerprint"]
            for zone_hash, details in self.zone_geometry_details().items()
        }

    def current_job(self):
        handle = self.handle()
        if not handle or not self.telemetry()["online"]:
            return None
        task = handle.snapshot.raw.work
        if not task.job_id or not task.zone_hashs:
            return None
        fields = {
            "areas": [f"switch.zone_{h}" for h in task.zone_hashs],
            "speed": task.speed,
            "blade_height": task.knife_height,
            "toward": task.toward,
            "toward_mode": task.toward_mode,
            "channel_mode": task.channel_mode,
            "channel_width": task.channel_width,
            "toward_included_angle": task.toward_included_angle,
            "mowing_laps": task.edge_mode,
        }
        if task.auto_change_direction is not None:
            fields["auto_change_direction"] = bool(task.auto_change_direction)
        if self._supports_ride_boundary_distance(handle):
            fields["ride_boundary_distance"] = bool(task.ride_boundary_distance)
        return {"job_id": str(task.job_id), "fields": fields}

    def job_schema(self):
        handle = self.handle()
        if not handle:
            return {}
        limits = handle.snapshot.raw.device_limits
        schema: dict[str, Any] = {
            "areas": {"required": True, "selector": {"entity": {"multiple": True}}}
        }
        for key, limit, unit in (
            ("speed", limits.working_speed, "m/s"),
            ("blade_height", limits.blade_height, "mm"),
            ("channel_width", limits.path_spacing, "cm"),
        ):
            if limit.max > 0 and limit.max >= limit.min:
                schema[key] = {
                    "required": True,
                    "name": f"{key.replace('_', ' ').title()} ({unit})",
                    "selector": {
                        "number": {
                            "min": limit.min,
                            "max": limit.max,
                            "step": 0.1 if key == "speed" else 1,
                        }
                    },
                }
        for key, minimum, maximum in (
            ("toward", -180, 180),
            ("toward_included_angle", 0, 180),
            ("channel_mode", 0, 3),
            ("toward_mode", 0, 2),
            ("mowing_laps", 0, 4),
            ("obstacle_laps", 0, 4),
        ):
            schema[key] = {"selector": {"number": {"min": minimum, "max": maximum, "step": 1}}}
        for key, choices in {
            "ultra_wave": [0, 1, 2, 10, 11],
            "border_mode": [0, 1],
        }.items():
            schema[key] = {"selector": {"select": {"options": [str(n) for n in choices]}}}
        from pymammotion.utility.device_type import DeviceType

        device_name = self._sdk_device_name(handle)
        firmware = self._firmware_version(handle)
        if DeviceType.supports_auto_change_direction(device_name, firmware):
            schema["auto_change_direction"] = {
                "name": "Auto-reverse mowing direction",
                "description": "Alternate the mowing direction between jobs to reduce matting.",
                "selector": {"boolean": {}},
            }
        if self._supports_ride_boundary_distance(handle):
            schema["ride_boundary_distance"] = {
                "name": "Edge coverage",
                "description": "Ride the boundary while perimeter laps are enabled.",
                "selector": {"boolean": {}},
            }
        return schema

    def capabilities(self):
        handle = self.handle()
        available = bool(self.connected and handle and handle.has_usable_transport)
        schema = self.job_schema()
        self.services = {
            "mammotion": {
                "start_mow": {"fields": schema},
                "get_mow_path_geojson": {},
                "get_mow_progress_geojson": {},
            }
        }
        reason = "Mammotion cloud / device API" if available else "Select an online Mammotion mower"
        caps = {
            key: {"supported": available, "reason": reason}
            for key in ("pause", "resume", "dock", "stop", "map")
        }
        state = (
            STATES.get(handle.snapshot.raw.report_data.dev.sys_status, "unknown")
            if handle
            else "unavailable"
        )
        caps["wake"] = {
            "supported": bool(
                self.connected
                and handle
                and state == "hibernating"
                and callable(getattr(self.client, "wake_device", None))
            ),
            "reason": "Wake through Mammotion cloud; does not start mowing",
            "provider": "mammotion",
        }
        for action, allowed in {
            "pause": {"mowing", "returning"},
            "resume": {"paused"},
            "dock": {"idle", "paused"},
            "stop": {"paused"},
        }.items():
            caps[action]["supported"] = available and state in allowed
            if state not in allowed:
                caps[action]["reason"] = (
                    "Unavailable in current state; pause an active job before docking or cancelling"
                )
        caps["job"] = {
            "supported": available and bool(schema.get("speed")),
            "reason": reason,
            "fields": schema,
        }
        for action in ("create", "edit", "rename", "enable", "delete", "copy", "refresh", "run"):
            caps[f"schedule_{action}"] = {
                "supported": False,
                "reason": "Schedule management is not exposed by this direct-account adapter; use the Mammotion app or HA mode",
                "fields": {},
            }
        has_camera = False
        if handle:
            from pymammotion.utility.device_type import DeviceType

            has_camera = not DeviceType.is_luba1(handle.device_name)

        caps["camera"] = {
            "supported": bool(self.connected and has_camera),
            "reason": "Direct Mammotion / Agora camera; cloud token and model support required",
            "provider": "mammotion",
        }
        caps["movement"] = {
            "supported": available
            and self.settings.allow_manual_movement
            and not self.movement.fault
            and state in {"idle", "paused"},
            "reason": "Short nudges with automatic zero-speed stop; choose Wi-Fi/cloud or Bluetooth. Enable ALLOW_MANUAL_MOVEMENT in .env.",
            "transports": self.movement_transports(),
            "bluetooth_error": self.ble_error,
            "fault": self.movement.fault,
        }
        return caps

    async def move(self, direction: str, transport_name: str):
        if direction == "stop":
            self.movement_epoch += 1
            return await self.movement.stop()
        epoch = self.movement_epoch
        if not self.settings.allow_manual_movement:
            raise HAError("Manual movement is disabled in .env")
        if transport_name not in {"wifi", "bluetooth"}:
            raise ValueError("Choose Wi-Fi or Bluetooth")
        if self.control_lock.locked():
            raise HAError("Another mower operation is in progress")
        async with self.control_lock:
            handle = await self.fresh()
            if epoch != self.movement_epoch:
                return {"accepted": False, "stopped": True}
            if self.telemetry()["state"] not in {"idle", "paused"}:
                raise ValueError("Manual movement requires an idle or paused mower")
            from pymammotion.transport.base import TransportType

            kinds = (
                (TransportType.BLE,)
                if transport_name == "bluetooth"
                else (TransportType.CLOUD_MAMMOTION, TransportType.CLOUD_ALIYUN)
            )
            transport = next(
                (t for kind in kinds if (t := handle.get_transport(kind)) and t.is_connected), None
            )
            if not transport:
                raise HAError("Selected transport is not connected; no fallback was attempted")
            return await self.movement.nudge(handle, transport, direction)

    async def camera_session(self, ws):
        if self.camera_lock.locked():
            await ws.send_json(
                {"type": "error", "message": "A direct camera viewer is already connected"}
            )
            await ws.close()
            return
        async with self.camera_lock:
            handle = self.handle()
            if not self.connected or not handle:
                raise HAError("Connect the Mammotion account and select a camera-capable mower")
            try:

                async def token():
                    async with asyncio.timeout(30):
                        result = await self.client.get_stream_subscription(
                            handle.device_name, handle.iot_id
                        )
                    data = result.data if result else None
                    if not data or not data.token:
                        raise HAError(
                            "Mammotion returned no camera subscription; check camera support and quota in the app"
                        )
                    encryption = {}
                    if data.openEncrypt:
                        key, salt = getattr(data, "key", None), getattr(data, "salt", None)
                        try:
                            valid = (
                                isinstance(key, str)
                                and bool(key)
                                and isinstance(salt, str)
                                and len(base64.b64decode(salt, validate=True)) == 32
                            )
                        except (TypeError, ValueError):
                            valid = False
                        if not valid:
                            raise HAError(
                                "Camera encryption parameters are missing; retry the subscription or check firmware"
                            )
                        encryption = {
                            "encryption": {"mode": "aes-256-gcm2", "key": key, "salt": salt}
                        }
                    # A scoped, short-lived media token is necessary in the viewer.
                    # Account/password/MQTT credentials never cross this boundary.
                    await ws.send_json(
                        {
                            **encryption,
                            "type": "agora",
                            "appid": data.appid,
                            "channel": data.channelName,
                            "token": data.token,
                            "uid": data.uid,
                            "available_seconds": data.availableTime,
                        }
                    )

                await token()
                while True:
                    message = await asyncio.wait_for(ws.receive_json(), 45)
                    if message.get("type") == "renew":
                        await token()
                    elif message.get("type") == "heartbeat":
                        if handle.snapshot.raw.report_data.connect.used_net == "MNET":
                            await self.command(handle, "refresh_fpv")
                    else:
                        raise ValueError("Unsupported camera message")
            finally:
                with suppress(Exception):
                    async with asyncio.timeout(10):
                        await self.client.stop_stream(handle.device_name)

    def movement_transports(self):
        from pymammotion.transport.base import TransportType

        handle = self.handle()
        if not handle:
            return []
        found = []
        for name, kinds in (
            ("wifi", (TransportType.CLOUD_MAMMOTION, TransportType.CLOUD_ALIYUN)),
            ("bluetooth", (TransportType.BLE,)),
        ):
            if any((t := handle.get_transport(kind)) and t.is_connected for kind in kinds):
                found.append(name)
        return found

    def telemetry(self):
        handle = self.handle()
        metrics, position, state, online = [], None, "unavailable", False
        radio = self.radio_reports.get(handle.device_name, {}) if handle else {}
        if handle:
            raw = handle.snapshot.raw
            online = (
                self.connected
                and handle.has_usable_transport
                and handle.last_report_data_at > 0
                and time.monotonic() - handle.last_report_data_at < TELEMETRY_FRESH_SECONDS
            )
            state = (
                STATES.get(raw.report_data.dev.sys_status, "unknown") if online else "unavailable"
            )
            if raw.report_data.dev.sys_status == 48:
                state = "hibernating"
            if online:
                for key, name, value, unit in (
                    ("battery_percent", "Battery", raw.report_data.dev.battery_val, "%"),
                    ("progress", "Job progress", raw.report_data.work.mow_percent, "%"),
                    ("blade_height", "Cutting height", raw.report_data.work.knife_height, "mm"),
                    ("mowing_speed", "Mowing speed", raw.work.speed, "m/s"),
                    (
                        "total_time",
                        "Total mowing time",
                        raw.report_data.work.progress & 0xFFFF,
                        "min",
                    ),
                    (
                        "elapsed_time",
                        "Elapsed mowing time",
                        max(
                            0,
                            (raw.report_data.work.progress & 0xFFFF)
                            - (raw.report_data.work.progress >> 16),
                        ),
                        "min",
                    ),
                    (
                        "left_time",
                        "Time remaining",
                        raw.report_data.work.progress >> 16,
                        "min",
                    ),
                    (
                        "charging_state",
                        "Charging",
                        "charging" if raw.report_data.dev.charge_state else "not charging",
                        "",
                    ),
                    ("wifi_rssi", "Wi-Fi signal", raw.report_data.connect.wifi_rssi, "dBm"),
                    ("mnet_rssi", "Mobile signal", raw.report_data.connect.mnet_rssi, "dBm"),
                ):
                    metrics.append(
                        {"id": key, "key": key, "name": name, "value": str(value), "unit": unit}
                    )
                rapid = raw.mowing_state
                metrics.extend(
                    [
                        {
                            "id": "rtk_status",
                            "key": "rtk_status",
                            "name": "RTK fix",
                            "value": rapid.rtk_status.name.lower(),
                            "unit": "",
                        },
                        {
                            "id": "satellites_total",
                            "key": "satellites_total",
                            "name": "GNSS satellites",
                            "value": str(rapid.satellites_total),
                            "unit": "",
                        },
                        {
                            "id": "satellites_l2",
                            "key": "satellites_l2",
                            "name": "L2 satellites",
                            "value": str(rapid.satellites_l2),
                            "unit": "",
                        },
                        {
                            "id": "localization_status",
                            "key": "localization_status",
                            "name": "Localization",
                            "value": raw.report_data.dev.fuse_localization_status.name.lower(),
                            "unit": "",
                        },
                    ]
                )
                codes = raw.errors.active_codes
                if codes:
                    code = abs(codes[0])
                    metrics.append(
                        {
                            "id": "error_code",
                            "key": "error_code",
                            "name": "Latest reported error",
                            "value": str(code),
                            "unit": "",
                        }
                    )
                    metrics.append(
                        {
                            "id": "last_error",
                            "key": "last_error",
                            "name": "Error detail",
                            "value": raw.errors.describe(code),
                            "unit": "",
                        }
                    )
                p = raw.location.device
                if raw.report_data.locations and raw.location.RTK.latitude:
                    # Re-evaluate a received local fix if RTK yaw arrived later.
                    # Match the SDK's update_report_data conversion exactly.
                    from pymammotion.data.model.coordinates import CoordinateConverter

                    loc, rtk = raw.report_data.locations[0], raw.location.RTK
                    x, y = loc.real_pos_x / 10000, loc.real_pos_y / 10000
                    east = math.cos(rtk.yaw) * x - math.sin(rtk.yaw) * y
                    north = math.sin(rtk.yaw) * x + math.cos(rtk.yaw) * y
                    p = CoordinateConverter(rtk.latitude, rtk.longitude).enu_to_lla(north, east)
                if p.latitude or p.longitude:
                    position = {
                        "latitude": p.latitude,
                        "longitude": p.longitude,
                        "direction": raw.location.orientation,
                    }
        current_zone = zone_info(
            handle.snapshot.raw.report_data.work.area & 65535 if handle else None,
            self.store,
        )
        if handle:
            zone_hash = handle.snapshot.raw.location.work_zone
            zone_name, source = resolve_zone_hash(
                zone_hash,
                handle.device_name,
                handle.snapshot.raw.map.computed_areas,
                self.store,
            )
            current_zone |= {"zone_hash": str(zone_hash) if zone_hash else None}
            if zone_hash:
                current_zone |= {"zone_name": zone_name, "zone_name_source": source}
        return self.clean(
            {
                **current_zone,
                "mqtt": {
                    "connected": bool(handle and "wifi" in self.movement_transports()),
                    "report_age_seconds": round(time.monotonic() - handle.last_report_data_at, 1)
                    if handle and handle.last_report_data_at
                    else None,
                    "source": "pymammotion_state_subscription",
                },
                "provider": "mammotion",
                "provider_label": "Mammotion account",
                "connected": self.connected,
                "online": online,
                "error": self.error,
                "device": self.selected(),
                "state": state,
                "network": signals(
                    radio.get("wifi"),
                    radio.get("cellular"),
                    radio.get("connection"),
                    "mammotion",
                    {"wifi": radio.get("observed"), "cellular": radio.get("observed")},
                    online,
                )
                | {
                    "ssid": (getattr(handle.snapshot.raw.mower_state, "wifi_ssid", "") or None)
                    if handle
                    else None,
                    "bssid": None,
                },
                "position_observed_at": radio.get("position_time"),
                "position_online": bool(
                    online
                    and radio.get("position_time")
                    and 0 <= time.time() - radio["position_time"] <= 120
                ),
                "metrics": metrics,
                "position": position,
                "last_event": self.last_event,
                "capabilities": self.capabilities(),
            }
        )

    async def fresh(self):
        handle = self.handle()
        if not self.connected or not handle or not handle.has_usable_transport:
            raise HAError("Mammotion mower is offline")
        try:
            async with asyncio.timeout(20):
                await self.client.ensure_fresh_state(handle.device_name, max_age_s=10)
            if time.monotonic() - handle.last_report_data_at > 30:
                raise ValueError("Stale telemetry")
            if self.handle() is not handle:
                raise ValueError("Selected mower changed")
        except Exception:
            raise HAError("Fresh mower telemetry is unavailable; command was not sent") from None
        return handle

    async def request(self, method: str, path: str, body=None):
        selected = self.selected()
        if method != "GET" or not selected or path != f"/api/states/{selected['entity_id']}":
            raise ValueError("Unsupported direct-account operation")
        await self.fresh()
        return {"state": self.telemetry()["state"]}

    async def command(self, handle, key: str, **kwargs):
        try:
            async with asyncio.timeout(25):
                from pymammotion.messaging.command_queue import Priority

                await self.client.send_command_with_args(
                    handle.device_name, key, prefer_ble=False, priority=Priority.USER, **kwargs
                )
        except Exception:
            raise HAError(
                "Mammotion command could not be confirmed; check mower state before retrying"
            ) from None

    async def control(self, action: str):
        async with self.control_lock:
            if action == "wake":
                handle = self.handle()
                if not handle or not self.capabilities()["wake"]["supported"]:
                    raise HAError("Wake is unavailable for the selected mower")
                if time.monotonic() - self.last_wake < 30:
                    raise HAError("Wake already requested; wait for the mower to reconnect")
                self.last_wake = time.monotonic()
                try:
                    async with asyncio.timeout(30):
                        accepted = await self.client.wake_device(handle.device_name)
                except Exception:
                    raise HAError("Cloud wake request failed; check account connectivity") from None
                if accepted is not True:
                    raise HAError("Mammotion did not accept the wake request")
                self.store.event("control", "Wake requested through Mammotion cloud")
                return {"accepted": True, "action": "wake", "pending": True}
            handle = await self.fresh()
            state = self.telemetry()["state"]
            if action == "resume" and state == "paused":
                command = "resume_execute_task"
            elif action == "pause" and state in {"mowing", "returning"}:
                command = "pause_execute_task" if state == "mowing" else "cancel_return_to_dock"
            elif action == "dock" and state in {"idle", "paused"}:
                command = "return_to_dock"
            elif action == "stop" and state == "paused":
                command = "cancel_job"
            else:
                raise ValueError(
                    "Command is unavailable in the current state; pause active mowing before docking or cancelling"
                )
            await self.command(handle, command)
            self.store.event("control", f"{action.title()} requested through Mammotion")
            return {"accepted": True, "action": action}

    async def call(self, domain: str, service: str, data: dict, response: bool = False):
        if domain != "mammotion" or service != "start_mow":
            raise HAError("This operation is not implemented by the direct-account adapter")
        async with self.control_lock:
            handle = await self.fresh()
            if data.get("entity_id") != f"mammotion.{handle.device_name}":
                raise ValueError("Selected mower changed; review the job again")
            if self.telemetry()["state"] not in {"idle", "docked"}:
                raise ValueError("Finish or cancel the current task before launching a new job")
            from pymammotion.data.model.device_config import (
                OperationSettings,
                build_route_information,
            )

            fields = {k: v for k, v in data.items() if k not in {"areas", "entity_id"}}
            for key in ("ultra_wave", "border_mode"):
                if key in fields:
                    fields[key] = int(fields[key])
            if "auto_change_direction" in fields:
                fields["auto_change_direction"] = int(fields["auto_change_direction"])
            if "ride_boundary_distance" in fields:
                fields["ride_boundary_distance"] = 0.5 if fields["ride_boundary_distance"] else 0.0
            options = OperationSettings(
                **fields,
                areas=[int(x.removeprefix("switch.zone_")) for x in data["areas"]],
                is_dump=False,
            )
            route = build_route_information(
                self._sdk_device_name(handle), handle.snapshot.raw, options
            )
            try:
                async with asyncio.timeout(60):
                    await self.client.send_command_and_wait(
                        handle.device_name,
                        "generate_route_information",
                        "bidire_reqconver_path",
                        prefer_ble=False,
                        generate_route_information=route,
                    )
                    await self.client.send_command_and_wait(
                        handle.device_name, "start_job", "zone_start_precent_t", prefer_ble=False
                    )
            except Exception:
                raise HAError(
                    "Job planning/start was not confirmed; check mower state before retrying"
                ) from None
            return {"accepted": True}

    def _complete_map_payload(self, handle, source: str, cache_warning: str | None = None):
        raw = handle.snapshot.raw
        self.client.regenerate_stale_geojson(handle.device_name)
        raw_map = raw.map.to_dict()
        location = raw.location.to_dict()
        result = map_evidence(raw_map, copy.deepcopy(raw.map.generated_geojson), source)
        geometry_source = "pymammotion_generated_geojson"
        try:
            normalized = normalize(result)
        except (KeyError, IndexError, TypeError, ValueError):
            # The SDK can retain a malformed derived feature even when every
            # authoritative raw frame is complete. Rebuild from those frames
            # before refusing the immutable snapshot.
            result = map_evidence(raw_map, local_map_geojson(raw_map, location), source)
            if normalize(result)["completeness"] != "complete":
                raise ValueError("Raw-frame geometry is incomplete") from None
            geometry_source = "local_raw_frames"
        else:
            if normalized["completeness"] != "complete":
                rebuilt = local_map_geojson(raw_map, location)
                completed = merge_missing_map_features(
                    copy.deepcopy(raw.map.generated_geojson), rebuilt
                )
                result = map_evidence(raw_map, completed, source)
                if normalize(result)["completeness"] != "complete":
                    raise ValueError("Raw-frame geometry is incomplete")
                geometry_source = "pymammotion_generated_plus_raw_frames"
        self._assert_map_near_device(result, location)
        sync = self._map_sync_evidence(raw, result)
        if not sync["sdk_map_synced"] and not sync["root_manifest_synced"]:
            raise ValueError("Map is not fully synchronized")
        if not result["frames_complete"]:
            raise ValueError("Incomplete map frames")
        result["source_metadata"]["location"] = location
        result["source_metadata"]["geometry_source"] = geometry_source
        result["source_metadata"]["sync_evidence"] = (
            "pymammotion_sdk" if sync["sdk_map_synced"] else "root_manifest"
        )
        if sync["orphan_area_name_count"]:
            result["source_metadata"]["ignored_orphan_area_names"] = sync["orphan_area_name_count"]
        if cache_warning:
            result["source_metadata"]["cache_warning"] = cache_warning
        return result

    @staticmethod
    def _assert_map_near_device(result: dict, location: dict) -> None:
        """Reject a projected map that is implausibly far from the live mower."""
        device = location.get("device", {})
        try:
            device_lat = float(device["latitude"])
            device_lon = float(device["longitude"])
        except (KeyError, TypeError, ValueError):
            return
        if (device_lat == 0 and device_lon == 0) or not (
            -85 < device_lat < 85 and -180 <= device_lon <= 180
        ):
            return
        west, south, east, north = normalize(result)["bounds"]
        nearest_lon = min(max(device_lon, west), east)
        nearest_lat = min(max(device_lat, south), north)
        distance = math.hypot(
            (nearest_lon - device_lon) * 111320 * math.cos(math.radians(device_lat)),
            (nearest_lat - device_lat) * 111320,
        )
        span = math.hypot(
            (east - west) * 111320 * math.cos(math.radians(device_lat)),
            (north - south) * 111320,
        )
        if distance > 5000 or span > 10000:
            raise ValueError("Map coordinates are inconsistent with the mower position")

    @staticmethod
    def _map_sync_evidence(raw, evidence: dict):
        locations = raw.report_data.locations
        bol_hash = locations[0].bol_hash if locations else 0
        map_data = raw.map
        root_manifest_available = bool(getattr(map_data, "root_hash_lists", None))
        computed_bol_hash = getattr(map_data, "computed_bol_hash", 0)
        reported_bol_available = bool(bol_hash)
        computed_bol_available = bool(computed_bol_hash)
        bol_hash_matches = (
            computed_bol_hash == bol_hash
            if reported_bol_available and computed_bol_available
            else None
        )
        try:
            incomplete_hash_count = len(map_data.find_incomplete_hashes(0))
        except (AttributeError, TypeError, ValueError):
            incomplete_hash_count = None
        try:
            sdk_synced = bool(map_data.is_map_synced(bol_hash))
        except (AttributeError, TypeError, ValueError):
            sdk_synced = False
        root_manifest_synced = bool(
            reported_bol_available
            and root_manifest_available
            and bol_hash_matches
            and incomplete_hash_count == 0
            and evidence["frames_complete"]
        )
        area_hashes = {str(key) for key in getattr(map_data, "area", {})}
        name_hashes = {
            str(getattr(item, "hash", "")) for item in getattr(map_data, "area_name", [])
        }
        return {
            "sdk_map_synced": sdk_synced,
            "root_manifest_synced": root_manifest_synced,
            "reported_bol_available": reported_bol_available,
            "computed_bol_available": computed_bol_available,
            "root_manifest_available": root_manifest_available,
            "bol_hash_matches": bol_hash_matches,
            "incomplete_hash_count": incomplete_hash_count,
            "orphan_area_name_count": len(name_hashes - area_hashes),
        }

    async def complete_map(self):
        if self.control_lock.locked():
            raise HAError("Wait for the current mower operation before synchronizing maps")
        handle = self.handle()
        try:
            report_age = (
                time.monotonic() - handle.last_report_data_at
                if handle and handle.last_report_data_at > 0
                else float("inf")
            )
            if handle:
                try:
                    return self._complete_map_payload(
                        handle,
                        (
                            "pymammotion_cloud_cache"
                            if report_age >= TELEMETRY_FRESH_SECONDS
                            else "pymammotion_cloud_memory"
                        ),
                        (
                            "Snapshot captured from the last internally synchronized cloud map "
                            "while live telemetry was stale"
                            if report_age >= TELEMETRY_FRESH_SECONDS
                            else None
                        ),
                    )
                except (AttributeError, KeyError, TypeError, ValueError):
                    if report_age >= TELEMETRY_FRESH_SECONDS:
                        raise
            handle = await self.fresh()
            async with asyncio.timeout(120):
                await self.client.start_map_sync(handle.device_name)
            return self._complete_map_payload(handle, "pymammotion_cloud")
        except Exception:
            raise HAError(
                "Complete map synchronization is not yet available; archive is unchanged"
            ) from None

    async def service_response(self, service: str):
        handle = self.handle()
        if not handle or not self.connected:
            return {}
        fields = {
            "get_mow_path_geojson": "generated_mow_path_geojson",
            "get_mow_progress_geojson": "generated_mow_progress_geojson",
            "get_dynamics_line_geojson": "generated_dynamics_line_geojson",
        }
        if service not in fields:
            raise ValueError("Unsupported overlay")
        self.request_overlay(handle)
        if service == "get_dynamics_line_geojson":
            return self._traveled_layer(handle)
        self.regenerate_route(handle)
        return copy.deepcopy(
            self.route_layers.get(service, {"type": "FeatureCollection", "features": []})
        )

    def regenerate_route(self, handle):
        # SDK regenerate_stale_geojson rebuilds the base only. Reproject BOTH
        # route layers from the same current RTK frame, never mixed cached yaw.
        from pymammotion.data.model.generate_geojson import (
            apply_mow_progress_geojson,
            apply_mowing_geojson,
        )

        raw = handle.snapshot.raw
        rtk, work = raw.location.RTK, raw.report_data.work
        frame = (
            handle.device_name,
            raw.work.job_id,
            work.path_hash,
            rtk.latitude,
            rtk.longitude,
            rtk.yaw,
        )
        if frame != self.route_frame:
            self.route_frame, self.route_layers = frame, {}
            self.route_progress = None
        if not rtk.latitude or not raw.map.is_mow_path_current(work.path_hash):
            return
        progress = (work.now_index, work.ub_path_hash, work.path_pos_x, work.path_pos_y)
        if self.route_progress == progress:
            return
        apply_mowing_geojson(raw.map, rtk)
        apply_mow_progress_geojson(
            raw.map, rtk, work.now_index, work.ub_path_hash, work.path_pos_x, work.path_pos_y
        )
        self.route_layers = {
            "get_mow_path_geojson": copy.deepcopy(raw.map.generated_mow_path_geojson),
            "get_mow_progress_geojson": copy.deepcopy(raw.map.generated_mow_progress_geojson),
        }
        self.route_progress = progress

    def overlay_diagnostics(self):
        """Read-only route evidence, without account data or geographic coordinates."""
        handle = self.handle()
        result = {"sdk": self.version, "fetch_status": self.overlay_fetch_status}
        if not handle:
            return {**result, "device_available": False}
        raw = handle.snapshot.raw
        coverage_width_cm, coverage_width_source = self._coverage_width_cm(handle)
        locations = raw.report_data.locations
        reported_map = locations[0].bol_hash if locations else 0
        return {
            **result,
            "device_available": True,
            "report_age_seconds": round(time.monotonic() - handle.last_report_data_at, 1)
            if handle.last_report_data_at
            else None,
            "state": STATES.get(raw.report_data.dev.sys_status, "unknown"),
            "route_hash": str(raw.report_data.work.path_hash),
            "active_segment_hash": str(raw.report_data.work.ub_path_hash),
            "segment_index": raw.report_data.work.now_index,
            "map_matches": bool(reported_map and reported_map == raw.map.computed_bol_hash),
            "route_current": raw.map.is_mow_path_current(raw.report_data.work.path_hash),
            "route_segments": len(raw.map.current_mow_path),
            "rtk_origin_available": bool(raw.location.RTK.latitude),
            "saga_active": bool(getattr(getattr(handle, "queue", None), "is_saga_active", False)),
            "fetch_running": bool(self.overlay_task and not self.overlay_task.done()),
            "dynamics_supported": self.supports_dynamics_line(handle),
            "dynamics_device_type": self._device_type(handle).name,
            "dynamics_firmware": self._firmware_version(handle),
            "dynamics_capability_source": self._dynamics_capability(handle)[1],
            "dynamics_status": self.dynamics_status,
            "dynamics_points": len(raw.map.dynamics_line),
            "dynamics_fetch_running": bool(self.dynamics_task and not self.dynamics_task.done()),
            "dynamics_empty_responses": self.dynamics_empty_responses,
            "position_trace_points": sum(len(segment) for segment in self.position_trace_segments),
            "position_trace_segments": len(
                [segment for segment in self.position_trace_segments if len(segment) >= 2]
            ),
            "position_trace_breaks": self.position_trace_breaks,
            "live_progress_source": self._live_progress_source(handle),
            "coverage_width_cm": coverage_width_cm,
            "coverage_width_source": coverage_width_source,
        }

    def map_capture_diagnostics(self):
        """Explain whether the in-memory base can be archived without mower traffic."""
        handle = self.handle()
        result = {
            "stale_complete_cache_supported": True,
            "telemetry_fresh_seconds": TELEMETRY_FRESH_SECONDS,
            "device_available": bool(handle),
        }
        if not handle:
            return result
        report_age = (
            time.monotonic() - handle.last_report_data_at
            if handle.last_report_data_at > 0
            else None
        )
        raw = handle.snapshot.raw
        try:
            evidence = map_evidence(
                raw.map.to_dict(), copy.deepcopy(raw.map.generated_geojson), "diagnostic"
            )
            frames_complete = bool(evidence["frames_complete"])
            zone_count = len(evidence["expected_zone_ids"])
            sync = self._map_sync_evidence(raw, evidence)
        except (AttributeError, TypeError, ValueError):
            frames_complete, zone_count = False, 0
            sync = {
                "sdk_map_synced": False,
                "root_manifest_synced": False,
                "reported_bol_available": False,
                "computed_bol_available": False,
                "root_manifest_available": False,
                "bol_hash_matches": None,
                "incomplete_hash_count": None,
                "orphan_area_name_count": 0,
            }
        return {
            **result,
            "report_age_seconds": round(report_age, 1) if report_age is not None else None,
            "report_stale": report_age is None or report_age >= TELEMETRY_FRESH_SECONDS,
            **sync,
            "frames_complete": frames_complete,
            "zone_count": zone_count,
            "cached_capture_ready": bool(
                frames_complete and (sync["sdk_map_synced"] or sync["root_manifest_synced"])
            ),
        }

    @staticmethod
    def _valid_coverage_width(value):
        try:
            width = float(value)
        except (TypeError, ValueError):
            return None
        return width if math.isfinite(width) and 5 <= width <= 100 else None

    def _remember_coverage_width(self, handle, width: float, source: str):
        """Persist a real setting once so a process restart cannot blank live coverage."""
        name = handle.device_name
        if self.coverage_width_cache.get(name) == width:
            return
        self.coverage_width_cache[name] = width
        self.store.put(
            "coverage_width",
            {"device": name, "width_cm": width, "source": source},
            name,
        )

    def _coverage_width_cm(self, handle, override_cm=None):
        override = self._valid_coverage_width(override_cm)
        if override is not None:
            self._remember_coverage_width(handle, override, "home_assistant")
            return override, "home_assistant"

        reported = self._valid_coverage_width(handle.snapshot.raw.work.channel_width)
        if reported is not None:
            self._remember_coverage_width(handle, reported, "mammotion_report")
            return reported, "mammotion_report"

        cached = self.coverage_width_cache.get(handle.device_name)
        if cached is None:
            saved = next(
                (
                    item
                    for item in self.store.documents("coverage_width")
                    if item.get("device") == handle.device_name
                ),
                None,
            )
            cached = self._valid_coverage_width(saved.get("width_cm")) if saved else None
            if cached is not None:
                self.coverage_width_cache[handle.device_name] = cached
        if cached is not None:
            return cached, "last_known"

        # LUBA 3 can omit the live work settings after a cloud reconnect. The
        # dashboard's configured/tested default keeps the swept-area estimate
        # visible until either HA or the mower publishes the actual setting.
        if self._device_type(handle).name == "LUBA_VA":
            return 27.0, "luba_va_default"
        return None, "unavailable"

    def _device_type(self, handle):
        from pymammotion.utility.device_type import DeviceType

        product_key = getattr(handle, "resolved_product_key", "") or getattr(
            handle, "product_key", ""
        )
        if product_key:
            resolved = DeviceType.value_of_str("", product_key)
            if resolved is not DeviceType.UNKNOWN:
                return resolved

        # Mammotion IoT may canonicalize the registry name to lower case. The
        # upstream name resolver is case-sensitive, so recover the most specific
        # known prefix rather than accidentally classifying Luba-VA as Luba 1.
        name = str(handle.device_name).casefold()
        matches = [
            (len(prefix), candidate)
            for candidate in DeviceType
            for prefix in candidate.get_name().split(",")
            if name.startswith(prefix.casefold())
        ]
        if matches:
            return max(matches, key=lambda item: item[0])[1]
        return DeviceType.value_of_str(handle.device_name)

    def _sdk_device_name(self, handle):
        """Return a canonical model prefix for case-sensitive SDK capability gates."""
        from pymammotion.utility.device_type import DeviceType

        device_type = self._device_type(handle)
        if device_type is DeviceType.UNKNOWN:
            return handle.device_name
        return device_type.get_name().split(",", 1)[0]

    def _supports_ride_boundary_distance(self, handle):
        from pymammotion.utility.device_type import DeviceType

        return DeviceType.supports_ride_boundary_distance(self._sdk_device_name(handle))

    def supports_dynamics_line(self, handle):
        return self._dynamics_capability(handle)[0]

    def _dynamics_capability(self, handle):
        from pymammotion.utility.device_type import DeviceType

        device_type = self._device_type(handle)
        firmware = self._firmware_version(handle)
        if device_type is DeviceType.LUBA_VA and not firmware:
            # The Mammotion-IoT snapshot can omit every firmware representation
            # even though the product key identifies a LUBA 3. Type 18 is a
            # read-only query: probing it is safe, and unsupported firmware will
            # simply return no frames or time out under our bounded retry.
            return True, "luba_va_readonly_probe"
        return device_type.is_support_dynamics_line(firmware or None), "sdk_model_firmware"

    @staticmethod
    def _firmware_version(handle):
        """Resolve the whole-device version from all SDK representations."""
        raw = handle.snapshot.raw
        candidates = (
            getattr(getattr(raw, "device_firmwares", None), "device_version", ""),
            getattr(raw, "main_firmware_version", ""),
            getattr(handle, "firmware_version", ""),
            getattr(getattr(raw, "mower_state", None), "swversion", ""),
            getattr(getattr(raw, "update_check", None), "current_version", ""),
            getattr(raw, "device_version", ""),
        )
        return next(
            (
                str(version).strip()
                for version in candidates
                if version and str(version).strip() not in {"0", "1.0.0.0"}
            ),
            "",
        )

    def _dynamics_key(self, handle):
        return (handle.device_name, self.dynamics_epochs.get(handle.device_name, 0))

    def _track_dynamics_session(self, handle):
        """Keep one stable key while firmware replaces path/map hashes mid-job."""
        raw = handle.snapshot.raw
        name = handle.device_name
        state = raw.report_data.dev.sys_status
        job_id = raw.work.job_id
        previous_job_id = self.dynamics_job_ids.get(name, 0)
        new_session = state in DYNAMIC_SESSION_STATES and name not in self.dynamics_active
        changed_task_job = bool(job_id and previous_job_id and job_id != previous_job_id)
        if new_session or changed_task_job:
            self.dynamics_active.add(name)
            self.dynamics_epochs[name] = self.dynamics_epochs.get(name, 0) + 1
            self.dynamics_status = "awaiting_live_progress"
            self.dynamics_attempt = 0.0
            self.dynamics_empty_responses = 0
            if self.dynamics_key and self.dynamics_key[0] == name:
                self.dynamics_key, self.dynamics_layer = None, {}
            self._clear_position_trace(name)
        if job_id:
            self.dynamics_job_ids[name] = job_id
        if state in DYNAMIC_SESSION_END_STATES:
            self.dynamics_active.discard(name)
            self.dynamics_job_ids.pop(name, None)
            self.dynamics_status = "session_inactive"
            self.dynamics_attempt = 0.0
            self.dynamics_empty_responses = 0
            if self.dynamics_key and self.dynamics_key[0] == name:
                self.dynamics_key, self.dynamics_layer = None, {}
            self._clear_position_trace(name)

    def _clear_position_trace(self, device_name):
        if self.position_trace_key and self.position_trace_key[0] != device_name:
            return
        with self.coverage_lock:
            self.position_trace_key = None
            self.position_trace_segments = []
            self.position_trace_last_at = 0.0
            self.position_trace_breaks = 0

    @staticmethod
    def _position_coordinates(handle):
        raw = handle.snapshot.raw
        if not raw.report_data.locations or not raw.location.RTK.latitude:
            return None
        from pymammotion.data.model.coordinates import CoordinateConverter

        loc, rtk = raw.report_data.locations[0], raw.location.RTK
        x, y = loc.real_pos_x / 10000, loc.real_pos_y / 10000
        east = math.cos(rtk.yaw) * x - math.sin(rtk.yaw) * y
        north = math.sin(rtk.yaw) * x + math.cos(rtk.yaw) * y
        point = CoordinateConverter(rtk.latitude, rtk.longitude).enu_to_lla(north, east)
        if not (-90 <= point.latitude <= 90 and -180 <= point.longitude <= 180):
            return None
        return [point.longitude, point.latitude]

    def _record_position_trace(self, handle):
        raw = handle.snapshot.raw
        if raw.report_data.dev.sys_status not in DYNAMIC_SESSION_STATES:
            return
        observed = handle.last_report_data_at
        if not observed or time.monotonic() - observed > 120:
            return
        point = self._position_coordinates(handle)
        if point is None:
            return
        key = self._dynamics_key(handle)
        with self.coverage_lock:
            if key != self.position_trace_key:
                self.position_trace_key = key
                self.position_trace_segments = [[]]
                self.position_trace_last_at = 0.0
                self.position_trace_breaks = 0
            if observed <= self.position_trace_last_at:
                return
            segment = self.position_trace_segments[-1]
            if segment:
                previous = segment[-1]
                mean_latitude = math.radians((previous[1] + point[1]) / 2)
                dx = (point[0] - previous[0]) * 111320 * math.cos(mean_latitude)
                dy = (point[1] - previous[1]) * 111320
                distance = math.hypot(dx, dy)
                elapsed = observed - self.position_trace_last_at
                if elapsed > 30 or distance > max(8, elapsed * 2.5):
                    self.position_trace_segments.append([])
                    self.position_trace_breaks += 1
                    segment = self.position_trace_segments[-1]
                elif distance < 0.1:
                    self.position_trace_last_at = observed
                    return
            segment.append(point)
            self.position_trace_last_at = observed

    def _position_trace_layer(self, handle):
        if self.position_trace_key != self._dynamics_key(handle):
            return {"type": "FeatureCollection", "features": []}
        features = [
            {
                "type": "Feature",
                "properties": {
                    "kind": "completed_path",
                    "source": "mammotion_mqtt_position_trace",
                    "point_count": len(segment),
                },
                "geometry": {"type": "LineString", "coordinates": copy.deepcopy(segment)},
            }
            for segment in self.position_trace_segments
            if len(segment) >= 2
        ]
        return {"type": "FeatureCollection", "features": features}

    def _traveled_layer(self, handle):
        dynamic = (
            copy.deepcopy(self.dynamics_layer)
            if self.dynamics_key == self._dynamics_key(handle)
            else {"type": "FeatureCollection", "features": []}
        )
        dynamic_features: list[dict[str, Any]] = [
            feature for feature in dynamic.get("features", []) if isinstance(feature, dict)
        ]
        for feature in dynamic_features:
            properties = feature.get("properties")
            if not isinstance(properties, dict):
                properties = {}
                feature["properties"] = properties
            properties.setdefault("source", "mammotion_type18")
        traced = self._position_trace_layer(handle)
        return {
            "type": "FeatureCollection",
            "features": dynamic_features + traced.get("features", []),
        }

    @staticmethod
    def _layer_source(layer):
        sources = {
            feature.get("properties", {}).get("source")
            for feature in layer.get("features", [])
            if feature.get("properties", {}).get("source")
        }
        if {"mammotion_type18", "mammotion_mqtt_position_trace"} <= sources:
            return "mammotion_hybrid_live_progress"
        return next(iter(sources), None)

    def _live_progress_source(self, handle):
        return self._layer_source(self._traveled_layer(handle))

    def request_overlay(self, handle):
        """Bootstrap a current route without planning or starting a mower job."""
        raw = handle.snapshot.raw
        self._track_dynamics_session(handle)
        self._record_position_trace(handle)
        if (
            self.supports_dynamics_line(handle)
            and raw.report_data.dev.sys_status in DYNAMIC_SESSION_STATES
        ):
            if hasattr(handle, "set_mow_path_fetch_enabled"):
                handle.set_mow_path_fetch_enabled(value=False)
            self.request_dynamics_line(handle)
            return
        if hasattr(handle, "set_mow_path_fetch_enabled"):
            handle.set_mow_path_fetch_enabled(value=True)
        if (
            not self.telemetry()["online"]
            or raw.report_data.work.path_hash <= 1
            or raw.map.is_mow_path_current(raw.report_data.work.path_hash)
            or self.control_lock.locked()
            or getattr(getattr(handle, "queue", None), "is_saga_active", False)
            or (self.overlay_task and not self.overlay_task.done())
            or time.monotonic() - self.overlay_attempt < 120
        ):
            return
        self.overlay_attempt = time.monotonic()
        self.overlay_task = asyncio.create_task(self.fetch_overlay(handle))

    def request_dynamics_line(self, handle):
        key = self._dynamics_key(handle)
        if key != self.dynamics_key:
            self.dynamics_key, self.dynamics_layer = key, {}
        if (
            self.control_lock.locked()
            or getattr(getattr(handle, "queue", None), "is_saga_active", False)
            or (self.dynamics_task and not self.dynamics_task.done())
            or self.dynamics_empty_responses >= 2
            or time.monotonic() - self.dynamics_attempt < 10
        ):
            return
        self.dynamics_attempt = time.monotonic()
        self.dynamics_task = asyncio.create_task(self.fetch_dynamics_line(handle, key))

    async def fetch_dynamics_line(self, handle, key):
        try:
            raw = handle.snapshot.raw
            before = tuple((point.x, point.y) for point in raw.map.dynamics_line)
            self.dynamics_status = "requesting_live_progress"
            await self.client.get_dynamics_line(handle.device_name)
            changed = await self._wait_for_overlay(
                handle,
                lambda: tuple((point.x, point.y) for point in raw.map.dynamics_line) != before,
                timeout=35,
            )
            from pymammotion.data.model.generate_geojson import apply_dynamics_line_geojson

            apply_dynamics_line_geojson(raw.map, raw.location.RTK)
            if key == self._dynamics_key(handle):
                layer = copy.deepcopy(raw.map.generated_dynamics_line_geojson)
                if layer.get("features"):
                    self.dynamics_key, self.dynamics_layer = key, layer
                    self.dynamics_empty_responses = 0
            if not raw.map.dynamics_line:
                self.dynamics_empty_responses += 1
            self.dynamics_status = (
                "live_progress_empty_using_mqtt"
                if self.dynamics_empty_responses >= 2
                else "live_progress_ready"
                if changed
                else "live_progress_unchanged"
            )
            self.publish()
        except TimeoutError:
            self.dynamics_status = "live_progress_timed_out"
        except Exception:
            self.dynamics_status = "live_progress_failed"
            logging.getLogger(__name__).info("Live progress fetch incomplete; retry deferred")

    async def fetch_overlay(self, handle):
        try:
            self.overlay_fetch_status = "checking_map"
            async with asyncio.timeout(240):
                raw = handle.snapshot.raw
                locations = raw.report_data.locations
                if not locations or not locations[0].bol_hash:
                    self.overlay_fetch_status = "missing_map_hash"
                    return
                if raw.map.computed_bol_hash != locations[0].bol_hash:
                    # SDK auto-watchers skip this initial fetch on active LiDAR
                    # devices. Names are unnecessary for downloading the route.
                    self.overlay_fetch_status = "syncing_map"
                    await self.client.start_map_sync(handle.device_name, skip_area_names=True)
                    if not await self._wait_for_overlay(
                        handle,
                        lambda: bool(
                            raw.report_data.locations
                            and raw.report_data.locations[0].bol_hash == raw.map.computed_bol_hash
                        ),
                    ):
                        self.overlay_fetch_status = "map_sync_incomplete"
                        return
                self.overlay_fetch_status = "requesting_route"
                fetched = await self.client.check_and_get_mow_path(handle.device_name)
                if fetched:
                    self.overlay_fetch_status = "downloading_route"
                    if not await self._wait_for_overlay(
                        handle,
                        lambda: raw.map.is_mow_path_current(raw.report_data.work.path_hash),
                    ):
                        self.overlay_fetch_status = "route_download_incomplete"
                        return
                self.overlay_fetch_status = (
                    "route_ready"
                    if raw.map.is_mow_path_current(raw.report_data.work.path_hash)
                    else "route_not_requested"
                )
                self.client.regenerate_stale_geojson(handle.device_name)
                self.regenerate_route(handle)
                self.publish()
        except TimeoutError:
            self.overlay_fetch_status = "timed_out"
        except Exception:
            self.overlay_fetch_status = "failed"
            # A bounded retry is triggered by a later viewer request. Retain the
            # existing map and never persist incomplete frames as an archive.
            logging.getLogger(__name__).info("Live route fetch incomplete; retry deferred")

    async def _wait_for_overlay(self, handle, ready, timeout=115):
        """Wait for an enqueued read-only saga and its state reducer callback."""
        deadline = time.monotonic() + timeout
        observed_busy = False
        while time.monotonic() < deadline:
            if ready():
                return True
            busy = bool(handle.has_queued_commands())
            observed_busy = observed_busy or busy
            if observed_busy and not busy:
                # Give state subscribers/on_complete one event-loop turn.
                await asyncio.sleep(0)
                return bool(ready())
            await asyncio.sleep(0.1)
        return bool(ready())

    def completed_coverage(self, width_cm=None):
        with self.coverage_lock:
            return self._completed_coverage(width_cm)

    def _completed_coverage(self, width_cm=None):
        from pymammotion.data.model.coordinates import CoordinateConverter
        from pymammotion.data.model.generate_geojson import GeojsonGenerator
        from shapely.geometry import Point

        from backend.coverage import completed_segments, swept_area

        empty = {"type": "FeatureCollection", "features": []}
        handle = self.handle()
        if not handle or not self.telemetry()["online"]:
            return empty
        raw = handle.snapshot.raw
        resolved_width_cm, width_source = self._coverage_width_cm(handle, width_cm)
        if resolved_width_cm is None:
            return empty
        width = resolved_width_cm / 100
        rtk = raw.location.RTK
        if not rtk.latitude:
            return empty
        traveled = self._traveled_layer(handle)
        if traveled.get("features"):
            dynamic_coverage = swept_area(traveled, width)
            if dynamic_coverage.get("features"):
                properties = dynamic_coverage["features"][0]["properties"]
                properties["source"] = self._layer_source(traveled) or "mammotion_live_cut_path"
                properties["width_source"] = width_source
                return dynamic_coverage
        if not raw.map.current_mow_path:
            return empty
        for frames in raw.map.current_mow_path.values():
            if not frames or len(frames) != next(iter(frames.values())).total_frame:
                return empty
        work = raw.report_data.work
        job_id = raw.work.job_id
        key = (handle.device_name, job_id, work.path_hash, rtk.latitude, rtk.longitude, rtk.yaw)
        if key != self.coverage_key or not job_id:
            self.coverage_key, self.coverage_segments = key, {}
        converter = CoordinateConverter(rtk.latitude, rtk.longitude)
        origin = converter.enu_to_lla(0, 0)
        full = GeojsonGenerator.generate_mow_progress_geojson(
            raw.map, 0, Point(origin.latitude, origin.longitude), yaw=rtk.yaw
        )
        ordered = [
            h
            for root in raw.map.root_hash_lists
            if root.sub_cmd == 3
            for entry in root.data
            for h in entry.data_couple
        ]
        completed = completed_segments(
            full, raw.map.generated_mow_progress_geojson, work.ub_path_hash, work.now_index, ordered
        )
        for feature in completed["features"]:
            self.coverage_segments[feature["properties"]["path_hash"]] = feature
        if len(self.coverage_segments) > 1000:
            self.coverage_segments = {}
        result = swept_area(
            {"type": "FeatureCollection", "features": list(self.coverage_segments.values())}, width
        )
        if result.get("features"):
            result["features"][0]["properties"]["width_source"] = width_source
        return result
