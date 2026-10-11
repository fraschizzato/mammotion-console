import copy
import io
import json
import sqlite3
import zipfile
from unittest.mock import patch

import httpx
import pytest

from backend.config import Settings
from backend.ha import HaClient, HAError
from backend.jobs import validate_fields
from backend.maps import MapArchive
from backend.storage import Store


def test_file_write_failure_leaves_existing_snapshots_untouched(tmp_path, map_a, map_b):
    archive = MapArchive(Store(tmp_path))
    old = archive.capture(map_a, "m", "A")
    original = archive.export(old["id"])
    with (
        patch("backend.maps.os.replace", side_effect=OSError("disk failure")),
        pytest.raises(OSError),
    ):
        archive.capture(map_b, "m", "B")
    assert len(archive.list()) == 1
    assert archive.export(old["id"]) == original


def test_import_preserves_version_group_and_rejects_tampering(tmp_path, map_a):
    source, target = MapArchive(Store(tmp_path / "a")), MapArchive(Store(tmp_path / "b"))
    one = source.capture(map_a, "m", "A")
    changed = copy.deepcopy(map_a)
    changed["geojson"]["features"][0]["properties"]["Name"] = "Renamed zone"
    two = source.capture(changed, "m", "A2", set_id=one["set_id"])
    first = target.import_zip(source.export(one["id"]))
    second = target.import_zip(source.export(two["id"]))
    assert first["set_id"] == second["set_id"] and second["version"] == 2
    blob = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(source.export(one["id"]))) as src,
        zipfile.ZipFile(blob, "w") as dst,
    ):
        for name in src.namelist():
            content = src.read(name)
            if name == "manifest.json":
                manifest = json.loads(content)
                manifest["fingerprint"] = "tampered"
                content = json.dumps(manifest).encode()
            dst.writestr(name, content)
    with pytest.raises(ValueError, match="integrity"):
        target.import_zip(blob.getvalue())


def test_future_database_version_is_refused(tmp_path):
    store = Store(tmp_path)
    with sqlite3.connect(store.database) as db:
        db.execute("UPDATE schema_version SET version=999")
    with pytest.raises(RuntimeError, match="Unsupported database"):
        Store(tmp_path)


def test_repeated_automatic_events_coalesce_across_store_restart(tmp_path):
    store = Store(tmp_path)
    assert store.event_coalesced("map_sync", "Map unavailable", {"attempt": 1}) is True
    assert store.event_coalesced("map_sync", "Map unavailable", {"attempt": 2}) is False
    assert len(store.events("map_sync")) == 1
    assert store.events("map_sync")[0]["details"] == {"attempt": 1}

    reopened = Store(tmp_path)
    assert reopened.event_coalesced("map_sync", "Map unavailable") is False
    assert reopened.event_coalesced("map_sync", "Map changed") is True
    # Manual events are deliberately never coalesced.
    reopened.event("map_sync", "Map changed", {"manual": True})
    assert [event["message"] for event in reopened.events("map_sync")] == [
        "Map changed",
        "Map changed",
        "Map unavailable",
    ]


def test_malformed_upload_is_a_client_error(client):
    for content in [
        b"[]",
        b"null",
        b"PKbroken",
        b'{"geojson":{"type":"FeatureCollection","features":null}}',
    ]:
        response = client.post("/api/maps/import", files={"file": ("invalid.json", content)})
        assert response.status_code == 422, response.text


async def test_rate_limit_prevents_command_flood(tmp_path):
    ha = HaClient(Settings(data_dir=tmp_path, _env_file=None), Store(tmp_path))
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "60"})

    await ha.http.aclose()
    ha.http = httpx.AsyncClient(base_url="http://ha.test", transport=httpx.MockTransport(transport))
    with pytest.raises(HAError, match="429"):
        await ha.request("GET", "/api/states")
    with pytest.raises(HAError, match="rate limit"):
        await ha.request("GET", "/api/states")
    assert len(calls) == 1
    await ha.http.aclose()


def test_actual_upstream_week_schema_accepts_sunday_zero_not_seven():
    schema = {"weeks": {"selector": {"object": {}}}}
    assert validate_fields({"weeks": [0, 1, 6]}, schema, {"weeks"}) == {"weeks": [0, 1, 6]}
    with pytest.raises(ValueError):
        validate_fields({"weeks": [7]}, schema, {"weeks"})


def test_streamed_request_limit_is_enforced(client, monkeypatch):
    monkeypatch.setattr("backend.middleware.MAX_BYTES", 10)
    response = client.post(
        "/api/maps/capture",
        content='{"name":"too long"}',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413


def test_structured_logger_redacts_configured_token():
    import logging

    from backend.logging_config import JsonFormatter

    record = logging.LogRecord(
        "backend", logging.WARNING, "", 0, "failed: %s", ("secret-canary",), None
    )
    rendered = JsonFormatter("secret-canary").format(record)
    assert "secret-canary" not in rendered
    assert json.loads(rendered)["level"] == "WARNING"
