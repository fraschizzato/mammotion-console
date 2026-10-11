"""Explicit test-only HA simulator. Never imported by the production application.

Run: python -m uvicorn tests.mock_ha:app --host 0.0.0.0 --port 8129
"""

import asyncio
import json
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect

from tests.conftest import FIXTURES, seed_ha

app = FastAPI()
fixture = SimpleNamespace()
seed_ha(fixture)
active = "a"
calls: list[dict] = []
SECRET = "mock-ha-secret-canary"


@app.middleware("http")
async def auth(request: Request, call_next):
    if (
        request.url.path.startswith("/api/")
        and request.headers.get("Authorization") != f"Bearer {SECRET}"
    ):
        raise HTTPException(401)
    return await call_next(request)


@app.get("/api/states")
def states():
    return list(fixture.states.values())


@app.get("/api/states/{entity}")
def state(entity: str):
    return fixture.states.get(entity, {"state": "unavailable"})


@app.get("/api/services")
def services():
    return [{"domain": k, "services": v} for k, v in fixture.services.items()]


@app.post("/api/services/{domain}/{service}")
async def service(domain: str, service: str, request: Request):
    body = await request.json()
    calls.append({"domain": domain, "service": service, "data": body})
    payload = json.loads((FIXTURES / f"map-{active}.json").read_text())
    if service == "get_geojson":
        return {"service_response": payload["geojson"]}
    if service == "get_map_data":
        return {
            "service_response": {
                "area": {
                    key: {"total_frame": 1, "data": [{"current_frame": 1}]}
                    for key in payload["expected_zone_ids"]
                },
                "area_name": [],
            }
        }
    return []


@app.post("/testing/map/{name}")
def switch_map(name: str):
    global active
    if name not in {"a", "b"}:
        raise HTTPException(422)
    active = name
    return {"active": active}


@app.get("/testing/calls")
def service_calls():
    return calls


@app.websocket("/api/websocket")
async def socket(ws: WebSocket):
    await ws.accept()
    await ws.send_json({"type": "auth_required"})
    auth = await ws.receive_json()
    if auth.get("access_token") != SECRET:
        await ws.send_json({"type": "auth_invalid"})
        await ws.close()
        return
    await ws.send_json({"type": "auth_ok", "ha_version": "2026.9.fixture"})
    event_task = None

    async def push_events(subscription):
        while True:
            await asyncio.sleep(2)
            await ws.send_json(
                {
                    "id": subscription,
                    "type": "event",
                    "event": {
                        "event_type": "state_changed",
                        "data": {
                            "entity_id": "sensor.luba_battery",
                            "new_state": fixture.states["sensor.luba_battery"] | {"state": "63"},
                        },
                    },
                }
            )

    try:
        while True:
            message = await ws.receive_json()
            result = None
            if message["type"] == "config/entity_registry/list":
                result = fixture.registry
            elif message["type"] == "config/device_registry/list":
                result = fixture.devices
            elif (
                message["type"] == "subscribe_events"
                and message.get("event_type") == "state_changed"
            ):
                event_task = asyncio.create_task(push_events(message["id"]))
            elif message["type"].startswith("camera/"):
                await ws.send_json(
                    {
                        "id": message["id"],
                        "type": "result",
                        "success": False,
                        "error": {
                            "code": "camera_unavailable",
                            "message": "Test-only simulator has no camera media",
                        },
                    }
                )
                continue
            await ws.send_json(
                {"id": message["id"], "type": "result", "success": True, "result": result}
            )
    except WebSocketDisconnect:
        pass
    finally:
        if event_task:
            event_task.cancel()
            await asyncio.gather(event_task, return_exceptions=True)
