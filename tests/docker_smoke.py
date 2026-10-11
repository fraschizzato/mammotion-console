"""Explicit integration smoke against tests.mock_ha and an isolated running console."""

import asyncio
import json
import os

import httpx


async def main():
    base = os.environ.get("CONSOLE_TEST_URL", "http://127.0.0.1:5010")
    mock = os.environ.get("MOCK_HA_URL", "http://127.0.0.1:8129")
    async with (
        httpx.AsyncClient(base_url=base, headers={"X-Console-Request": "1"}, timeout=30) as console,
        httpx.AsyncClient(base_url=mock) as ha,
    ):
        health = (await console.get("/api/health")).json()
        assert health["status"] == "ok" and health["ha_connected"]
        assert '<div id="root">' in (await console.get("/")).text
        before = (await console.get("/api/maps")).json()
        assert len(before) >= 2, "Capture fixture A and B first"
        original = next(
            m
            for m in before
            if m["device"] == "Luba-fixture" and m["manifest"]["zones"][0]["source_id"] == "101"
        )
        original_bytes = (await console.get(f"/api/maps/{original['id']}/geojson")).content
        await ha.post("/testing/map/b")
        b = await console.post("/api/maps/capture", json={"name": "Docker B"})
        assert b.status_code == 200, b.text
        assert (await console.get(f"/api/maps/{original['id']}/geojson")).content == original_bytes
        await asyncio.sleep(31)
        await ha.post("/testing/map/a")
        a = await console.post("/api/maps/capture", json={"name": "Docker A"})
        assert a.status_code == 200, a.text
        assert a.json()["unchanged"] and a.json()["id"] == original["id"]
        assert (await console.get(f"/api/maps/{original['id']}/geojson")).content == original_bytes
        diagnostics = await console.get("/api/diagnostics")
        assert "mock-ha-secret-canary" not in diagnostics.text
        state = (await console.get("/api/state")).json()
        assert next(m for m in state["metrics"] if m["key"] == "battery_percent")["value"] == "63"
        print(
            json.dumps(
                {
                    "container_health": "ok",
                    "ha_websocket_battery": 63,
                    "map_switch": "A → B → A preserved",
                    "unchanged_recapture": True,
                    "map_count": len((await console.get("/api/maps")).json()),
                    "secret_in_diagnostics": False,
                }
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
