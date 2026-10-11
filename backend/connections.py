"""Independent video source and read-only MQTT telemetry beside the control provider."""

import asyncio
import time
from contextlib import suppress

from backend.camera_relay import available as native_video_available
from backend.cloud import CloudClient
from backend.ha import HaClient, HAError


def provider_mode(provider):
    return "mammotion" if isinstance(provider, CloudClient) else "home_assistant"


def physical_name(provider, mower):
    if not mower:
        return ""
    if not isinstance(provider, CloudClient):
        for entity, row in provider.registry_index().items():
            if entity.startswith("camera.") and row.get("device_id") == mower.get("device_id"):
                name = provider.states.get(entity, {}).get("attributes", {}).get("model_name")
                if name:
                    return str(name).casefold()
    return str(mower.get("device_name", "")).casefold()


class Connections:
    def __init__(self, settings, store, primary):
        self.settings, self.store, self.primary = settings, store, primary
        self.secondary = None
        self.secondary_task = None
        self.paused = False
        self.last_report_stream = 0.0
        self.monitor_running = False
        self.monitor_tick_at = 0.0
        self.monitor_state = "not_started"
        self.report_outcome = "not_requested"
        self.report_attempts = 0
        self.monitor_errors = 0
        self.monitor_last_event = None

    def configured(self, mode):
        s = self.settings
        return (
            bool(
                s.mammotion_username.get_secret_value() and s.mammotion_password.get_secret_value()
            )
            if mode == "mammotion"
            else bool(s.ha_base_url and s.ha_token.get_secret_value())
        )

    def mode(self):
        saved = self.store.documents("camera_connection")
        return saved[0]["mode"] if saved else provider_mode(self.primary())

    def ensure(self, mode):
        primary = self.primary()
        if mode == provider_mode(primary):
            return primary
        if not self.configured(mode):
            raise HAError("Configure this camera provider in the server .env first")
        if self.secondary is None:
            # Secondary adapters never enable Bluetooth or manual movement.
            settings = self.settings.model_copy(
                update={"bluetooth_enabled": False, "allow_manual_movement": False}
            )
            self.secondary = (
                CloudClient(settings, self.store)
                if mode == "mammotion"
                else HaClient(settings, self.store)
            )
            self.secondary_task = asyncio.create_task(self.secondary.run())
        return self.secondary

    def match(self, provider):
        primary = self.primary()
        if provider is primary:
            return provider.selected()
        expected = physical_name(primary, primary.selected())
        if not expected:
            expected = self.settings.mammotion_device.casefold()
        matches = [
            m for m in provider.mowers() if physical_name(provider, m) == expected and expected
        ]
        if len(matches) != 1:
            return None
        provider.selection_override = matches[0]["entity_id"]
        return matches[0]

    async def video_provider(self):
        provider = self.ensure(self.mode())
        for _ in range(60):
            if provider.connected:
                if self.match(provider):
                    return provider
                raise HAError("The video provider has no unique mower matching the selected robot")
            if self.secondary_task and self.secondary_task.done() and provider is self.secondary:
                break
            await asyncio.sleep(0.5)
        raise HAError("Camera provider is not connected yet; inspect its status and retry")

    def status(self):
        mode = self.mode()
        provider = self.primary() if mode == provider_mode(self.primary()) else self.secondary
        matched = self.match(provider) if provider is not None and provider.connected else None
        return {
            "mode": mode,
            "relay_supported": native_video_available(),
            "configured": {name: self.configured(name) for name in ("home_assistant", "mammotion")},
            "connected": bool(provider and provider.connected and matched),
            "error": provider.clean(provider.error) if provider else "",
            "device": physical_name(provider, matched)
            if provider is not None and matched
            else None,
            "capability": provider.capabilities().get("camera")
            if provider is not None and matched
            else None,
        }

    def enrich(self, live):
        # Keep control state/capabilities on the explicitly selected primary source.
        if live.get("type"):
            return live
        provider = self.primary() if isinstance(self.primary(), CloudClient) else self.secondary
        result = dict(live)
        result["camera_connection"] = self.status()
        result["position_source"] = provider_mode(self.primary())
        result["position_online"] = live.get("position_online", live.get("online", False))
        if (
            not isinstance(provider, CloudClient)
            or not provider.connected
            or not self.match(provider)
        ):
            result["mqtt"] = {"connected": False}
            return result
        cloud = provider.telemetry()
        if cloud.get("state") == "hibernating" and live.get("state") in {
            "unavailable",
            "unknown",
            "idle",
            "docked",
            "sleeping",
            "hibernating",
        }:
            result["state"] = "hibernating"
            result["online"] = False
            result["capabilities"] = {k: dict(v) for k, v in live.get("capabilities", {}).items()}
            for action in ("pause", "resume", "dock", "stop"):
                if action in result["capabilities"]:
                    result["capabilities"][action]["supported"] = False
            result["capabilities"]["wake"] = cloud["capabilities"]["wake"]
        result["mqtt"] = cloud.get("mqtt", {"connected": False})
        if (
            cloud.get("online")
            and result["mqtt"].get("connected")
            and self.settings.mqtt_telemetry_enabled
        ):
            if cloud.get("position") and cloud.get("position_online"):
                result["position"] = cloud["position"]
                result["position_observed_at"] = cloud.get("position_observed_at")
                result["position_online"] = True
                result["position_source"] = "mammotion_mqtt"
            cloud_network = cloud.get("network", {})
            if any(s.get("fresh") for s in cloud_network.get("signals", {}).values()):
                result["network"] = cloud_network
            by_key = {m["key"]: m for m in live.get("metrics", [])}
            by_key.update({m["key"]: m for m in cloud.get("metrics", [])})
            result["metrics"] = list(by_key.values())
            for key in ("area_m2", "area_m2_raw", "zone_name"):
                if cloud.get(key) is not None:
                    result[key] = cloud[key]
            if cloud.get("zone_hash"):
                result["zone_hash"] = cloud["zone_hash"]
                result["zone_name"] = cloud.get("zone_name")
                result["zone_name_source"] = cloud.get("zone_name_source")
        return result

    def monitor_diagnostics(self):
        stamp = time.monotonic()
        return {
            "running": self.monitor_running,
            "state": self.monitor_state,
            "enabled": self.settings.mqtt_telemetry_enabled,
            "paused_for_provider_switch": self.paused,
            "viewers": len(self.primary().listeners),
            "tick_age_seconds": round(stamp - self.monitor_tick_at, 1)
            if self.monitor_tick_at
            else None,
            "attempt_age_seconds": round(stamp - self.last_report_stream, 1)
            if self.last_report_stream
            else None,
            "attempts": self.report_attempts,
            "errors": self.monitor_errors,
            "last_result": self.report_outcome,
        }

    async def monitor_tick(self):
        self.monitor_tick_at = time.monotonic()
        if self.paused:
            self.monitor_state = "provider_switch"
            return
        if not self.settings.mqtt_telemetry_enabled:
            self.monitor_state = "disabled"
            return
        if not self.configured("mammotion"):
            self.monitor_state = "not_configured"
            return
        provider = self.ensure("mammotion")
        if not self.match(provider):
            self.monitor_state = "no_matching_mower"
            return
        if provider is not self.primary() and provider.last_event != self.monitor_last_event:
            self.monitor_last_event = provider.last_event
            self.primary().publish()
        handle = provider.handle()
        if not self.primary().listeners:
            self.monitor_state = "no_viewers"
            return
        if not handle or not hasattr(handle, "start_report_stream"):
            self.monitor_state = "stream_unavailable"
            return
        if getattr(getattr(handle, "queue", None), "is_saga_active", False):
            self.monitor_state = "waiting_for_saga"
            return
        if time.monotonic() - self.last_report_stream <= 45:
            self.monitor_state = "cooldown"
            return
        self.last_report_stream = time.monotonic()
        self.report_attempts += 1
        self.monitor_state = "requesting_reports"
        before = handle.last_report_data_at
        try:
            # SDK verification waits 5s twice, with sync/send overhead.
            async with asyncio.timeout(25):
                await handle.start_report_stream(duration_ms=60000)
                deadline = time.monotonic() + 12
                while handle.last_report_data_at <= before and time.monotonic() < deadline:
                    await asyncio.sleep(0.25)
            self.report_outcome = (
                "report_received"
                if handle.last_report_data_at > before
                else "sdk_returned_without_new_report"
            )
        except TimeoutError:
            self.report_outcome = "timed_out"
            self.monitor_errors += 1
        except Exception:
            self.report_outcome = "request_failed"
            self.monitor_errors += 1
        self.monitor_state = "cooldown"

    async def monitor(self):
        self.monitor_running = True
        try:
            while True:
                try:
                    await self.monitor_tick()
                except Exception:
                    # Selection/reconnect failures must not permanently kill
                    # telemetry. Never expose upstream exception strings.
                    self.monitor_errors += 1
                    self.monitor_state = "monitor_error"
                await asyncio.sleep(1)
        finally:
            self.monitor_running = False
            self.monitor_state = "stopped"

    async def close(self):
        if self.secondary_task:
            self.secondary_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.secondary_task
        if self.secondary:
            await self.secondary.close()
        self.secondary = self.secondary_task = None
