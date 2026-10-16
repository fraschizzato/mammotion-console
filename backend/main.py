import asyncio
import json
import logging
import time
import zipfile
from contextlib import asynccontextmanager, suppress
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import (
    FastAPI,
    File,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.access_points import access_point_routes
from backend.cloud import CloudClient, preload_sdk
from backend.config import Settings
from backend.connections import Connections, physical_name
from backend.ha import HaClient, HAError
from backend.jobs import (
    JOB_FIELDS,
    TASK_FIELDS,
    Jobs,
    provider_zones,
    validate_fields,
    zones_match_snapshot,
)
from backend.logging_config import configure_logging
from backend.maps import MAX_BYTES, MapArchive
from backend.middleware import RequestSizeLimit
from backend.network import NetworkHistory
from backend.notifications import Telegram
from backend.storage import Store
from backend.zones import DEFAULT_ZONES


class Capture(BaseModel):
    name: str = Field(default="Captured map", min_length=1, max_length=200)
    set_id: str | None = None
    force: bool = False


class Metadata(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    notes: str = Field(default="", max_length=4000)


class Preset(BaseModel):
    map_id: str = ""
    name: str = Field(min_length=1, max_length=200)
    fields: dict = Field(default_factory=dict)


class ZoneProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    map_id: str = Field(min_length=1, max_length=80)
    zone_hash: str = Field(min_length=1, max_length=80)
    zone_fingerprint: str = Field(default="", pattern=r"^(?:[0-9a-f]{64})?$")
    fields: dict = Field(default_factory=dict)


class ZoneSequence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    map_id: str = Field(min_length=1, max_length=80)
    zone_hashes: list[str] = Field(min_length=1, max_length=100)
    confirmed: bool = False


class Action(BaseModel):
    map_id: str = ""
    confirmed: bool = False
    fields: dict = Field(default_factory=dict)
    target: str | None = None


class ProviderRequest(BaseModel):
    mode: Literal["home_assistant", "mammotion"]


class MovementRequest(BaseModel):
    transport: str = "wifi"


class ZoneMapping(BaseModel):
    area_m2: float = Field(gt=0, allow_inf_nan=False)
    name: str = Field(min_length=1, max_length=120)


class Preferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mower_entity: str = ""
    default_snapshot: str = ""
    auto_capture: bool = True
    tiles: str = Field(default="satellite", pattern="^(satellite|none)$")
    telegram_enabled: bool = False
    telegram_chat_id: str = Field(default="", pattern=r"^(?:-?[0-9]+)?$")
    telegram_user_ids: list[int] = Field(default_factory=list, max_length=30)
    telegram_pauses_only: bool = True
    zone_mapping: list[ZoneMapping] = Field(
        default_factory=lambda: [ZoneMapping(**z) for z in DEFAULT_ZONES], max_length=200
    )

    @field_validator("tiles", mode="before")
    @classmethod
    def migrate_street(cls, value):
        return "satellite" if value == "street" else value

    @field_validator("zone_mapping")
    @classmethod
    def unique_areas(cls, value):
        areas = sorted(z.area_m2 for z in value)
        if any(b - a <= 4 for a, b in zip(areas, areas[1:], strict=False)):
            raise ValueError("Zone areas must be more than 4 m2 apart to avoid ambiguous matches")
        return value


def create_app(settings: Settings | None = None):
    settings = settings or Settings()
    store = Store(settings.data_dir)
    saved = store.documents("connection")
    if saved:
        settings = settings.model_copy(update={"connection_mode": saved[0]["mode"]})
    ha = (
        CloudClient(settings, store)
        if settings.connection_mode == "mammotion"
        else HaClient(settings, store)
    )
    archive = MapArchive(store)
    jobs = Jobs(ha, archive)
    connections = Connections(settings, store, lambda: ha)
    network_history = NetworkHistory(store)
    capture_lock = asyncio.Lock()
    last_capture = 0.0
    mutation_lock = asyncio.Lock()
    camera_tasks: set[asyncio.Task] = set()
    relay_lock = asyncio.Lock()
    ha_camera_lock = asyncio.Lock()
    tasks: list[asyncio.Task] = []
    telegram = Telegram(settings, store, lambda: ha, mutation_lock)

    def route_source():
        secondary = connections.secondary
        if (
            isinstance(secondary, CloudClient)
            and secondary.connected
            and connections.match(secondary)
        ):
            return secondary
        return ha

    def ha_coverage_width():
        for entity in ha.entities():
            entity_id = str(entity.get("entity_id", ""))
            if not entity_id.startswith("number."):
                continue
            key = str(ha.entity_key(entity_id)).casefold()
            if not any(
                token in key for token in ("path_spacing", "channel_width", "spaziatura_percorso")
            ):
                continue
            unit = str(entity.get("attributes", {}).get("unit_of_measurement", ""))
            if unit.casefold() not in {"cm", "centimeter", "centimeters"}:
                continue
            try:
                candidate = float(entity.get("state"))
            except (TypeError, ValueError):
                continue
            if 5 <= candidate <= 100:
                return candidate
        return None

    async def current_route(source):
        result = {}
        for key, service in (
            ("path", "get_mow_path_geojson"),
            ("progress", "get_mow_progress_geojson"),
            ("traveled", "get_dynamics_line_geojson"),
        ):
            if service == "get_dynamics_line_geojson" and isinstance(source, CloudClient):
                result[key] = await source.service_response(service)
            elif service in source.services.get("mammotion", {}):
                result[key] = await source.service_response(service)
        if isinstance(source, CloudClient):
            width = ha_coverage_width() if source is connections.secondary else None
            result["mowed"] = await asyncio.to_thread(source.completed_coverage, width)
        return result

    async def archive_active_job_route():
        """Persist an active route from the server loop, without a browser request."""
        source = route_source()
        jobs.observe(source)
        if not jobs.active_history:
            return None
        route = await current_route(source)
        return await asyncio.to_thread(jobs.capture_overlay, route, source)

    def capture_event(message: str, details: dict | None = None, automatic: bool = False):
        if automatic:
            store.event_coalesced("map_sync", message, details)
        else:
            store.event("map_sync", message, details)

    async def capture(body: Capture, automatic: bool = False):
        nonlocal last_capture
        async with capture_lock:
            if time.monotonic() - last_capture < 30:
                raise HAError("Map acquisition is debounced for 30 seconds")
            last_capture = time.monotonic()
            stage = "selection"
            try:
                selected = ha.selected()
                if not selected:
                    raise HAError("Select one mower before capturing its map")
                device = selected.get("device_name") or selected.get("entity_id")
                if not device:
                    raise HAError("The selected mower has no stable identity")
                stage = "provider"
                payload = ha.clean(await ha.complete_map())
                stage = "archive"
                result = await asyncio.to_thread(
                    archive.capture,
                    payload,
                    device,
                    body.name,
                    body.set_id,
                    body.force,
                    payload["source_metadata"]["provider"],
                )
            except (ValueError, HAError) as exc:
                capture_event(str(exc), automatic=automatic)
                raise
            except Exception as exc:
                error_type = type(exc).__name__
                logging.getLogger(__name__).error(
                    "Map capture failed at %s (%s); archive state preserved",
                    stage,
                    error_type,
                )
                message = "Map capture failed; archive state was preserved"
                try:
                    capture_event(
                        message,
                        {"stage": stage, "error_type": error_type},
                        automatic,
                    )
                except Exception:
                    logging.getLogger(__name__).error(
                        "Could not persist the map capture failure event"
                    )
                raise HAError(message) from None
            try:
                capture_event(
                    "Map checked; unchanged" if result["unchanged"] else "Map captured",
                    {"id": result["id"]},
                    automatic,
                )
            except Exception:
                logging.getLogger(__name__).error(
                    "Map snapshot succeeded but its audit event could not be persisted"
                )
            if not result["unchanged"]:
                try:
                    ha.publish_message(
                        {"type": "map_snapshot_created", "snapshot_id": result["id"]}
                    )
                except Exception:
                    logging.getLogger(__name__).error(
                        "Map snapshot succeeded but its live notification could not be published"
                    )
            return result

    async def auto_capture():
        while True:
            await asyncio.sleep(10)
            try:
                await archive_active_job_route()
            except Exception:
                logging.getLogger(__name__).warning(
                    "Live job route could not be archived; retrying on the next sample"
                )
            await jobs.advance_sequence()
            selected = ha.selected()
            if ha.connected and selected:
                jobs.observe_schedules()
            if (
                ha.connected
                and time.monotonic() - last_capture >= max(30, settings.capture_interval)
                and store.preferences().get("auto_capture", settings.auto_capture)
            ):
                try:
                    await capture(Capture(), automatic=True)
                except (ValueError, HAError):
                    pass
                except Exception:
                    logging.getLogger(__name__).error(
                        "Automatic map capture failed; archive preserved"
                    )

    async def sample_network():
        while True:
            try:
                live = connections.enrich(ha.telemetry())
                device = physical_name(ha, ha.selected())
                await asyncio.to_thread(network_history.record, device, live)
            except Exception:
                logging.getLogger(__name__).warning("Network sample unavailable")
            await asyncio.sleep(15)

    @asynccontextmanager
    async def lifespan(app):
        configure_logging(settings.redaction_secrets)
        # Complete cold imports before accepting requests or launching any
        # background consumers. Concurrent SDK circular imports can deadlock.
        try:
            preload_sdk()
        except Exception:
            # Keep the offline archive accessible; the cloud adapter exposes
            # its controlled initialization error if this dependency is broken.
            logging.getLogger(__name__).warning("Mammotion SDK preload failed")
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        # Upstream background exceptions can contain session material.
        # Do not render third-party task/exception reprs into container logs.
        loop.set_exception_handler(
            lambda _loop, _context: logging.getLogger(__name__).error(
                "Provider background task failed; inspect connection diagnostics"
            )
        )
        tasks.extend([asyncio.create_task(ha.run()), asyncio.create_task(auto_capture())])
        logging.getLogger(__name__).info('{"event":"started","version":"1.0.0"}')
        notification_tasks = [
            asyncio.create_task(connections.monitor()),
            asyncio.create_task(sample_network()),
            asyncio.create_task(telegram.monitor()),
            asyncio.create_task(telegram.poll()),
        ]
        yield
        for task in notification_tasks:
            task.cancel()
        await asyncio.gather(*notification_tasks, return_exceptions=True)
        await telegram.close()
        for camera_task in list(camera_tasks):
            camera_task.cancel()
        await asyncio.gather(*list(camera_tasks), return_exceptions=True)
        await connections.close()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await ha.close()
        loop.set_exception_handler(previous_handler)

    app = FastAPI(title="Mammotion Console", version="1.0.0", lifespan=lifespan)
    app.include_router(access_point_routes(store))
    app.add_middleware(RequestSizeLimit)
    app.state.ha, app.state.store, app.state.archive, app.state.jobs = ha, store, archive, jobs
    app.state.connections = connections
    app.state.network_history = network_history
    app.state.archive_active_job_route = archive_active_job_route

    def origin_allowed(origin: str | None, host: str) -> bool:
        if not origin:
            return True
        parsed = urlsplit(origin)
        return parsed.scheme in {"http", "https"} and (
            parsed.netloc == host or origin == settings.app_origin
        )

    @app.middleware("http")
    async def security(request: Request, call_next):
        started = time.monotonic()
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if (
                not origin_allowed(request.headers.get("origin"), request.headers.get("host", ""))
                or request.headers.get("sec-fetch-site") == "cross-site"
            ):
                return JSONResponse(
                    {"detail": "Cross-origin mutations are not allowed"}, status_code=403
                )
            if request.headers.get("x-console-request") != "1":
                return JSONResponse(
                    {"detail": "X-Console-Request header is required"}, status_code=403
                )
        try:
            content_length = int(request.headers.get("content-length", "0"))
        except ValueError:
            return JSONResponse({"detail": "Invalid content length"}, status_code=400)
        if content_length > MAX_BYTES:
            return JSONResponse({"detail": "Request too large"}, status_code=413)
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not request.url.path.endswith(
            "/stop"
        ):
            async with mutation_lock:
                response = await call_next(request)
        else:
            response = await call_next(request)
        response.headers["Server-Timing"] = f"app;dur={(time.monotonic() - started) * 1000:.1f}"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = (
            "no-store" if request.url.path.startswith("/api") else "no-cache"
        )
        return response

    @app.exception_handler(ValueError)
    async def bad_input(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({"detail": "Resource not found"}, status_code=404)

    @app.exception_handler(HAError)
    async def upstream(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.get("/api/health")
    def health():
        with store.connect() as db:
            db.execute("SELECT 1")
        return {
            "status": "ok",
            "features": {"persistent_job_routes": 1},
            "version": "1.0.0",
            "ha_connected": ha.connected if settings.connection_mode == "home_assistant" else False,
            "provider": settings.connection_mode,
            "connected": ha.connected,
        }

    @app.get("/api/ready")
    def ready():
        return {
            "ready": True,
            "provider": settings.connection_mode,
            "provider_configured": bool(
                settings.mammotion_username.get_secret_value()
                and settings.mammotion_password.get_secret_value()
            )
            if isinstance(ha, CloudClient)
            else bool(settings.ha_base_url and settings.ha_token.get_secret_value()),
            "ha_configured": bool(settings.ha_base_url and settings.ha_token.get_secret_value()),
            "ha_connected": ha.connected if not isinstance(ha, CloudClient) else False,
        }

    @app.get("/api/state")
    def state():
        return connections.enrich(ha.telemetry())

    @app.websocket("/api/live")
    async def live(ws: WebSocket):
        if not origin_allowed(ws.headers.get("origin"), ws.headers.get("host", "")):
            await ws.close(code=1008)
            return
        await ws.accept()
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        ha.listeners.add(queue)
        try:
            await ws.send_json(connections.enrich(ha.telemetry()))
            while True:
                try:
                    value = await asyncio.wait_for(queue.get(), 20)
                except TimeoutError:
                    value = ha.telemetry()
                await ws.send_json(connections.enrich(value))
        except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
            pass
        finally:
            ha.listeners.discard(queue)

    @app.get("/api/diagnostics")
    def diagnostics():
        cloud = ha if isinstance(ha, CloudClient) else connections.secondary
        route = (
            cloud.overlay_diagnostics()
            if isinstance(cloud, CloudClient) and connections.match(cloud)
            else None
        )
        return ha.clean(
            {
                "app_version": "1.0.0",
                "provider": settings.connection_mode,
                "ha_version": ha.version,
                "versions": ha.cache.versions(),
                "camera_connection": connections.status(),
                "mqtt": connections.enrich(ha.telemetry()).get("mqtt"),
                "mowing_route": route,
                "map_capture": cloud.map_capture_diagnostics()
                if isinstance(cloud, CloudClient) and connections.match(cloud)
                else None,
                "mqtt_monitor": connections.monitor_diagnostics(),
                "connected": ha.connected,
                "error": ha.error,
                "retry_seconds": ha.backoff,
                "last_http_status": ha.last_http_status,
                "rate_limit_seconds": max(0, round(ha.rate_limited_until - time.monotonic())),
                "last_event": ha.last_event,
                "mowers": ha.mowers(),
                "entities": ha.entities(),
                "services": ha.services.get("mammotion", {}),
                "capabilities": ha.capabilities(),
                "map_sync": store.events("map_sync"),
            }
        )

    @app.get("/api/events")
    def events():
        return store.events()

    @app.get("/api/settings")
    def preferences():
        return {
            "preferences": Preferences(
                **(
                    {"auto_capture": settings.auto_capture}
                    | {
                        k: v
                        for k, v in store.preferences().items()
                        if k in Preferences.model_fields
                    }
                )
            ).model_dump(),
            "server": {
                "telegram_configured": bool(settings.telegram_bot_token.get_secret_value()),
                "telegram_error": telegram.error or telegram.poll_error,
                "telegram_camera_capture": settings.telegram_camera_capture,
                "camera_snapshot_configured": bool(settings.camera_snapshot_url.get_secret_value()),
                "connection_mode": settings.connection_mode,
                "mammotion_configured": bool(
                    settings.mammotion_username.get_secret_value()
                    and settings.mammotion_password.get_secret_value()
                ),
                "bluetooth_enabled": settings.bluetooth_enabled,
                "ha_base_url": settings.ha_base_url,
                "ha_configured": bool(settings.ha_token.get_secret_value()),
                "cache_enabled": bool(settings.ha_config_path),
                "allow_manual_movement": settings.allow_manual_movement,
                "map_calibration": {
                    "rotation_degrees": settings.map_display_rotation_degrees,
                    "offset_east_m": settings.map_display_offset_east_m,
                    "offset_north_m": settings.map_display_offset_north_m,
                    "origin_latitude": settings.map_display_origin_latitude,
                    "origin_longitude": settings.map_display_origin_longitude,
                },
            },
            "mowers": ha.mowers(),
        }

    @app.post("/api/camera/frame")
    async def camera_frame(request: Request):
        if not telegram.enabled():
            raise ValueError("Telegram snapshots are disabled")
        identity = telegram.identity(ha.telemetry())
        if (
            request.headers.get("x-camera-provider") != identity[0]
            or request.headers.get("x-camera-device") != identity[1]
        ):
            raise ValueError("Camera frame belongs to a different provider or mower")
        content = await request.body()
        if len(content) > 2_000_000:
            raise ValueError("Frame exceeds limit")
        frame = await asyncio.to_thread(telegram.validate_frame, content)
        telegram.frame = (telegram.identity(ha.telemetry()), time.monotonic(), frame)
        return {"accepted": True}

    @app.put("/api/connection")
    async def switch_provider(body: ProviderRequest):
        nonlocal ha, jobs, last_capture
        if body.mode == settings.connection_mode:
            return preferences()
        configured = (
            bool(
                settings.mammotion_username.get_secret_value()
                and settings.mammotion_password.get_secret_value()
            )
            if body.mode == "mammotion"
            else bool(settings.ha_base_url and settings.ha_token.get_secret_value())
        )
        if not configured:
            raise ValueError("Configure this provider's credentials in the server .env first")
        async with capture_lock:
            jobs.cancel_sequence("provider_switched")
            await telegram.close()
            telegram.pending.clear()
            telegram.frame = None
            telegram.previous = None
            if isinstance(ha, CloudClient):
                await ha.move("stop", "wifi")
                if ha.movement.fault:
                    raise HAError("Check the mower before switching after a failed movement stop")
            for task in list(camera_tasks):
                task.cancel()
            await asyncio.gather(*list(camera_tasks), return_exceptions=True)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            connections.paused = True
            await connections.close()
            await ha.close()
            listeners = ha.listeners
            settings.connection_mode = body.mode
            ha = (
                CloudClient(settings, store)
                if body.mode == "mammotion"
                else HaClient(settings, store)
            )
            ha.listeners = listeners
            jobs = Jobs(ha, archive)
            app.state.ha, app.state.jobs = ha, jobs
            last_capture = 0.0
            store.put("connection", {"mode": body.mode}, "active")
            tasks[:] = [asyncio.create_task(ha.run()), asyncio.create_task(auto_capture())]
            connections.paused = False
            ha.publish()
        return preferences()

    @app.put("/api/settings")
    def put_preferences(body: Preferences):
        if body.mower_entity and body.mower_entity not in {m["entity_id"] for m in ha.mowers()}:
            raise ValueError("Mower is not discovered")
        if body.default_snapshot:
            archive.get(body.default_snapshot)
        store.put("settings", body.model_dump(), "preferences")
        ha.publish()
        return preferences()

    @app.post("/api/mower/{action}")
    async def control(action: str, body: Action):
        if not body.confirmed:
            raise ValueError("Explicit confirmation required")
        if action == "wake" and not isinstance(ha, CloudClient):
            provider = connections.secondary
            if not isinstance(provider, CloudClient) or not connections.match(provider):
                raise HAError("Configure a matching Mammotion account to wake the mower")
            return await provider.control("wake")
        if action in {"stop", "dock"}:
            jobs.cancel_sequence(f"mower_{action}_requested")
        result = await ha.control(action)
        jobs.record_control(action)
        return result

    @app.get("/api/maps")
    def maps():
        return archive.list()

    @app.post("/api/maps/capture")
    async def capture_map(body: Capture):
        return await capture(body)

    @app.post("/api/maps/import")
    async def import_map(file: Annotated[UploadFile, File()]):
        content = await file.read(MAX_BYTES + 1)
        if len(content) > MAX_BYTES:
            raise ValueError("Map exceeds import limit")
        if content.startswith(b"PK"):
            try:
                return await asyncio.to_thread(archive.import_zip, content)
            except (zipfile.BadZipFile, EOFError, OSError, KeyError) as exc:
                raise ValueError("Invalid map archive") from exc
        payload = ha.clean(json.loads(content))
        if not isinstance(payload, dict):
            raise ValueError("Map import must be a JSON object")
        return await asyncio.to_thread(
            archive.capture,
            payload,
            str(payload.get("device", "import"))[:200],
            str(payload.get("name", "Imported map"))[:200],
        )

    @app.get("/api/maps/{key}/geojson")
    def geojson(key: str):
        return FileResponse(
            archive.directory(key) / "normalized.geojson", media_type="application/geo+json"
        )

    @app.get("/api/maps/{key}/preview")
    def map_preview(key: str):
        return FileResponse(archive.directory(key) / "preview.svg", media_type="image/svg+xml")

    @app.get("/api/maps/{key}/export")
    def export(key: str):
        return Response(
            archive.export(key),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="map-{key}.zip"'},
        )

    @app.patch("/api/maps/{key}")
    def metadata(key: str, body: Metadata):
        return archive.metadata(key, body.name, body.notes)

    @app.delete("/api/maps/{key}")
    def delete_map(key: str, confirmed: bool = False):
        if not confirmed:
            raise ValueError("Explicit delete confirmation required")
        archive.delete(key)
        prefs = store.preferences()
        if prefs.get("default_snapshot") == key:
            store.put("settings", prefs | {"default_snapshot": ""}, "preferences")
        return {"deleted": True}

    @app.get("/api/network/history")
    async def network_samples(
        radio: Literal["wifi", "cellular"] = "wifi",
        days: Annotated[int, Field(ge=1, le=90)] = 7,
        device: Annotated[str, Field(max_length=200)] = "",
    ):
        selected = device or physical_name(ha, ha.selected())
        return await asyncio.to_thread(network_history.history, selected, radio, days)

    @app.get("/api/overlay")
    async def overlay(
        snapshot_id: Annotated[str, Field(max_length=80)] = "", include_route: bool = True
    ):
        result: dict = {}
        source = route_source()
        snapshot_matches = None
        if snapshot_id:
            snapshot = archive.get(snapshot_id)
            snapshot_matches = zones_match_snapshot(
                provider_zones(source, archive), snapshot["manifest"].get("zones", [])
            )
        diagnostics = source.overlay_diagnostics() if isinstance(source, CloudClient) else {}
        result["status"] = {
            "source": "mammotion" if isinstance(source, CloudClient) else "home_assistant",
            "stale": not source.telemetry()["online"],
            # Home Assistant does not expose the cloud route-generation
            # lifecycle.  Keep it unknown there instead of showing a
            # permanent "synchronizing" message for a valid HA overlay.
            "route_current": diagnostics.get("route_current"),
            "map_matches": diagnostics.get("map_matches"),
            "snapshot_matches": snapshot_matches,
        }
        # Fetching/drawing a route over an unrelated or unverified archive is
        # misleading and can also start an unnecessary cloud route saga.
        if snapshot_id and snapshot_matches is not True:
            return ha.clean(result)
        if not include_route:
            return ha.clean(result)
        result.update(await current_route(source))
        jobs.observe(source)
        if jobs.active_history:
            await asyncio.to_thread(jobs.capture_overlay, result, source)
            result = await asyncio.to_thread(jobs.merge_active_overlay, result)
        return ha.clean(result)

    @app.get("/api/jobs")
    def job_info():
        cap = ha.capabilities()["job"]
        cap["fields"] = {k: v for k, v in cap["fields"].items() if k in JOB_FIELDS}
        return {
            "capability": cap,
            "route_history": {"persistent": True, "version": 1},
            "zones": jobs.zones(),
            "presets": store.documents("preset"),
            "zone_profiles": store.documents("zone_profile"),
            "zone_sequences": jobs.sequences(),
            "history": jobs.history(),
            "resume": ha.capabilities()["resume"],
        }

    @app.get("/api/jobs/history/{key}/fields")
    def history_fields(key: str):
        return {"fields": jobs.reusable_fields(key)}

    @app.get("/api/jobs/history/{key}/overlay")
    def history_overlay(key: str):
        return ha.clean(jobs.route(key))

    @app.post("/api/jobs/resume")
    async def resume_job(body: Action):
        if not body.confirmed:
            raise ValueError("Confirm resuming the current paused job")
        selected = ha.selected()
        if not selected:
            raise ValueError("Select a mower")
        current = await ha.request("GET", f"/api/states/{selected['entity_id']}")
        if current.get("state") != "paused":
            raise ValueError("Only the currently paused mower job can be resumed")
        result = await ha.control("resume")
        jobs.record_control("resume")
        store.event("job", "Resume of current paused job requested")
        return result

    def archived_zone_for(current: dict, archived_zones: list[dict]):
        exact = [
            zone
            for zone in archived_zones
            if str(current.get("hash")) == str(zone.get("source_id"))
        ]
        if len(exact) == 1:
            return exact[0]
        fingerprint = str(current.get("geometry_fingerprint") or "")
        geometry = [
            zone
            for zone in archived_zones
            if fingerprint and fingerprint == str(zone.get("geometry_fingerprint") or "")
        ]
        if len(geometry) == 1:
            return geometry[0]
        current_name = str(current.get("name", "")).strip()
        generic = current_name.casefold().startswith(("zone ", "area "))
        names = [
            zone
            for zone in archived_zones
            if current_name
            and not generic
            and current_name.casefold() == str(zone.get("name", "")).strip().casefold()
        ]
        return names[0] if len(names) == 1 else None

    def validate_task_map(map_id, fields):
        if not map_id:
            return
        snapshot = archive.get(map_id)
        selected = ha.selected()
        if not selected or snapshot["device"] not in {
            selected.get("device_name"),
            selected["entity_id"],
        }:
            raise ValueError("Choose a map belonging to the selected mower")
        zones = {z["entity_id"]: z for z in jobs.zones()}
        if any(
            key not in zones or not archived_zone_for(zones[key], snapshot["manifest"]["zones"])
            for key in fields.get("areas", [])
        ):
            raise ValueError(
                "Selected task zones do not belong to this map; synchronize the correct active map"
            )

    @app.put("/api/jobs/zone-profile")
    def save_zone_profile(body: ZoneProfile):
        snapshot = archive.get(body.map_id)
        current = next((z for z in jobs.zones() if str(z.get("hash")) == body.zone_hash), None)
        direct = next(
            (
                archived
                for archived in snapshot["manifest"]["zones"]
                if str(archived["source_id"]) == body.zone_hash
            ),
            None,
        )
        zone = archived_zone_for(current, snapshot["manifest"]["zones"]) if current else direct
        if zone is None:
            raise ValueError("Choose a zone belonging to this archived map")
        allowed = JOB_FIELDS - {"areas", "modify", "start_progress", "plan_only"}
        schema = {k: v for k, v in ha.capabilities()["job"]["fields"].items() if k in allowed}
        validate_fields(body.fields, schema, allowed)
        if "ha_bypass_mode" in body.fields and "ultra_wave" in body.fields:
            raise ValueError("Choose one obstacle detection setting")
        return store.put(
            "zone_profile",
            body.model_dump()
            | {
                "geometry_fingerprint": str(zone.get("geometry_fingerprint", "")),
                "map_zone_hash": str(zone["source_id"]),
                "zone_name": zone["name"],
                "device": snapshot["device"],
            },
            f"{body.map_id}:{zone.get('geometry_fingerprint') or body.zone_hash}",
        )

    @app.post("/api/jobs/presets")
    def save_preset(body: Preset):
        jobs.validate_job(body.fields)
        validate_task_map(body.map_id, body.fields)
        return store.put("preset", body.model_dump())

    @app.put("/api/jobs/presets/{key}")
    def update_preset(key: str, body: Preset):
        if key not in {p["id"] for p in store.documents("preset")}:
            raise KeyError(key)
        jobs.validate_job(body.fields)
        validate_task_map(body.map_id, body.fields)
        return store.put("preset", body.model_dump(), key)

    @app.delete("/api/jobs/presets/{key}")
    def delete_preset(key: str):
        store.delete("preset", key)
        return {"deleted": True}

    @app.post("/api/jobs/start")
    async def start_job(body: Action):
        if not body.confirmed:
            raise ValueError("Review and confirm the job before launch")
        validate_task_map(body.map_id, body.fields)
        return await jobs.start(body.fields)

    @app.post("/api/jobs/zone-sequence")
    async def start_zone_sequence(body: ZoneSequence):
        if not body.confirmed:
            raise ValueError("Review and confirm the per-zone sequence before launch")
        snapshot = archive.get(body.map_id)
        selected = ha.selected()
        if not selected or snapshot["device"] not in {
            selected.get("device_name"),
            selected.get("entity_id"),
        }:
            raise ValueError("Choose a map belonging to the selected mower")
        current = {str(zone.get("hash")): zone for zone in jobs.zones()}
        if any(
            zone_hash not in current
            or not archived_zone_for(current[zone_hash], snapshot["manifest"]["zones"])
            for zone_hash in body.zone_hashes
        ):
            raise ValueError("Every sequence zone must belong to the selected map")
        return await jobs.start_sequence(
            body.map_id, body.zone_hashes, store.documents("zone_profile")
        )

    @app.post("/api/jobs/zone-sequence/cancel")
    def cancel_zone_sequence(body: Action):
        if not body.confirmed:
            raise ValueError("Confirm cancelling the remaining per-zone sequence")
        return {"cancelled": jobs.cancel_sequence()}

    @app.get("/api/schedules")
    def schedules():
        caps = {k: v for k, v in ha.capabilities().items() if k.startswith("schedule_")}
        for cap in caps.values():
            cap["fields"] = {k: v for k, v in cap["fields"].items() if k in TASK_FIELDS}
        return {
            "schedules": jobs.schedules(),
            "zones": jobs.zones(),
            "capabilities": caps,
            "history": jobs.schedule_history(),
            "discovery": "reported" if jobs.schedules() else "unverified",
        }

    @app.post("/api/schedules/{action}")
    async def schedule_action(action: str, body: Action):
        if not body.confirmed:
            raise ValueError("Confirm schedule operation")
        return await jobs.schedule_action(action, body.target, body.fields)

    @app.post("/api/movement/{direction}")
    async def movement(direction: str, body: MovementRequest):
        if isinstance(ha, CloudClient):
            return await ha.move(direction, body.transport)
        raise HTTPException(409, detail=ha.capabilities()["movement"]["reason"])

    @app.get("/api/camera/settings")
    async def camera_settings():
        if connections.configured(connections.mode()):
            connections.ensure(connections.mode())
        return connections.status()

    @app.put("/api/camera/settings")
    async def set_camera_settings(body: ProviderRequest):
        if not connections.configured(body.mode):
            raise ValueError("Configure this camera provider in the server .env first")
        for task in list(camera_tasks):
            task.cancel()
        await asyncio.gather(*list(camera_tasks), return_exceptions=True)
        telegram.frame = None
        store.put("camera_connection", {"mode": body.mode}, "active")
        connections.ensure(body.mode)
        ha.publish()
        return connections.status()

    @app.websocket("/api/camera/relay")
    async def camera_relay(ws: WebSocket):
        from backend.camera_relay import available, relay

        if not origin_allowed(ws.headers.get("origin"), ws.headers.get("host", "")):
            await ws.close(code=1008)
            return
        await ws.accept()
        if not available() or mutation_lock.locked():
            await ws.send_json(
                {"type": "error", "message": "Native video is unavailable or already in use"}
            )
            await ws.close()
            return
        task = asyncio.current_task()
        assert task is not None
        camera_tasks.add(task)
        acquired = False
        try:
            # A stopped viewer's native worker needs time to release the subscription.
            # Allow an immediate restart to wait for that cleanup, without overlapping workers.
            async with asyncio.timeout(10):
                await relay_lock.acquire()
            acquired = True
            await relay(ws)
        except (asyncio.CancelledError, WebSocketDisconnect):
            pass
        except Exception:
            with suppress(Exception):
                await ws.send_json(
                    {"type": "error", "message": "Native video stopped. Reconnect to retry."}
                )
        finally:
            if acquired:
                relay_lock.release()
            camera_tasks.discard(task)
            with suppress(Exception):
                await ws.close()

    @app.websocket("/api/camera")
    async def camera(ws: WebSocket):
        if mutation_lock.locked():
            await ws.close(code=1013)
            return
        task = asyncio.current_task()
        assert task is not None
        camera_tasks.add(task)
        try:
            await camera_session(ws)
        finally:
            camera_tasks.discard(task)
            with suppress(Exception):
                await ws.close()

    async def camera_session(ws: WebSocket):
        if not origin_allowed(ws.headers.get("origin"), ws.headers.get("host", "")):
            await ws.close(code=1008)
            return
        await ws.accept()
        try:
            camera_provider = await connections.video_provider()
        except HAError as exc:
            await ws.send_json({"type": "error", "message": str(exc)})
            return
        if isinstance(camera_provider, CloudClient):
            try:
                await camera_provider.camera_session(ws)
            except (WebSocketDisconnect, asyncio.CancelledError):
                return
            except Exception as exc:
                with suppress(Exception):
                    await ws.send_json(
                        {
                            "type": "error",
                            "message": str(exc)
                            if isinstance(exc, HAError)
                            else "Direct camera connection failed; check device connectivity and camera availability",
                        }
                    )
                    await ws.close()
            return
        camera_id = camera_provider.capabilities()["camera"].get("entity_id")
        if not camera_id or not camera_provider.connected:
            await ws.send_json({"type": "error", "message": "Camera is unavailable"})
            await ws.close()
            return
        if camera_provider.capabilities()["camera"].get("protocol") == "agora":
            from backend.ha_camera import camera_session as ha_agora_session

            if ha_camera_lock.locked():
                await ws.send_json(
                    {"type": "error", "message": "A camera viewer is already connected"}
                )
                return
            async with ha_camera_lock:
                try:
                    await ha_agora_session(camera_provider, ws, camera_id)
                except (WebSocketDisconnect, asyncio.CancelledError):
                    return
                except Exception as exc:
                    with suppress(Exception):
                        await ws.send_json(
                            {
                                "type": "error",
                                "message": str(exc)
                                if isinstance(exc, HAError)
                                else "HA camera subscription failed; check Mammotion-HA connectivity and logs",
                            }
                        )
            return
        try:
            async with camera_provider.socket() as upstream_ws:
                session_id = None

                async def receive_ha():
                    nonlocal session_id
                    async for raw in upstream_ws:
                        msg = json.loads(raw)
                        if msg.get("event", {}).get("type") == "session":
                            session_id = msg["event"]["session_id"]
                        # ICE credentials are short-lived and necessary for WebRTC, never persisted.
                        secret = settings.ha_token.get_secret_value()
                        await ws.send_json(
                            json.loads(json.dumps(msg).replace(secret, "[redacted]"))
                            if secret
                            else msg
                        )

                async def receive_browser():
                    next_id = 1
                    while True:
                        msg = await ws.receive_json()
                        kind = msg.get("type")
                        out = {"id": next_id, "entity_id": camera_id}
                        if kind == "config":
                            out["type"] = "camera/webrtc/get_client_config"
                        elif (
                            kind == "offer"
                            and isinstance(msg.get("offer"), str)
                            and len(msg["offer"]) < 100000
                        ):
                            out |= {"type": "camera/webrtc/offer", "offer": msg["offer"]}
                        elif kind == "candidate" and session_id:
                            candidate = msg.get("candidate", {})
                            if len(json.dumps(candidate)) > 10000:
                                raise ValueError("ICE candidate too large")
                            out |= {
                                "type": "camera/webrtc/candidate",
                                "session_id": session_id,
                                "candidate": candidate,
                            }
                        else:
                            raise ValueError("Unsupported camera signaling message")
                        next_id += 1
                        await upstream_ws.send(json.dumps(out))

                tasks = [asyncio.create_task(receive_ha()), asyncio.create_task(receive_browser())]
                try:
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except (WebSocketDisconnect, asyncio.CancelledError):
            return
        except Exception:
            with suppress(Exception):
                await ws.send_json(
                    {
                        "type": "error",
                        "message": "WebRTC connection failed. Check HA camera support and network reachability.",
                    }
                )
                await ws.close()

    if settings.frontend_dir.exists():
        app.mount("/assets", StaticFiles(directory=settings.frontend_dir / "assets"), name="assets")
        if (settings.frontend_dir / "images").is_dir():
            app.mount(
                "/images", StaticFiles(directory=settings.frontend_dir / "images"), name="images"
            )

    @app.get("/{path:path}")
    def spa(path: str):
        if path.startswith("api/") or not (settings.frontend_dir / "index.html").exists():
            raise HTTPException(
                404,
                "Build the frontend with npm run build"
                if not path.startswith("api/")
                else "Not found",
            )
        return FileResponse(settings.frontend_dir / "index.html")

    return app


app = create_app()
