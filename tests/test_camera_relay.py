import asyncio
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.camera_relay import relay


@pytest.mark.asyncio
async def test_relay_forwards_frame_and_reaps_worker_on_disconnect(monkeypatch):
    jpeg = b"\xff\xd8fixture\xff\xd9"
    reader = asyncio.StreamReader()
    reader.feed_data(struct.pack("!I", len(jpeg)) + jpeg)
    process = SimpleNamespace(stdout=reader, returncode=None, terminate=Mock(), kill=Mock())
    process.wait = AsyncMock(return_value=0)
    launch = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    sent = asyncio.Event()

    async def send(frame):
        assert frame == jpeg
        sent.set()

    async def receive():
        await sent.wait()
        return {"type": "websocket.disconnect"}

    ws = SimpleNamespace(scope={"server": ("0.0.0.0", 5011)}, send_bytes=send, receive=receive)
    await relay(ws)
    process.terminate.assert_called_once()
    process.wait.assert_awaited_once()
    process.kill.assert_not_called()
    assert launch.call_args.args[-1] == "http://127.0.0.1:5011"


@pytest.mark.asyncio
async def test_relay_rejects_oversize_frame_and_cleans_up(monkeypatch):
    reader = asyncio.StreamReader()
    reader.feed_data(struct.pack("!I", 2_000_001))
    process = SimpleNamespace(stdout=reader, returncode=None, terminate=Mock(), kill=Mock())
    process.wait = AsyncMock(return_value=0)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    ws = SimpleNamespace(scope={}, send_bytes=AsyncMock(), receive=lambda: asyncio.sleep(30))
    with pytest.raises(ValueError, match="length"):
        await relay(ws)
    ws.send_bytes.assert_not_awaited()
    process.terminate.assert_called_once()


def test_relay_rejects_foreign_origin(client):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/api/camera/relay", headers={"origin": "https://foreign.test"}
        ):
            pass


def test_immediate_restart_waits_for_previous_worker_cleanup(client, monkeypatch):
    count = 0
    active = 0

    async def receiver(ws):
        nonlocal count, active
        assert active == 0
        count += 1
        active += 1
        try:
            await ws.send_text(str(count))
            await ws.receive_text()
            await asyncio.sleep(0.1)
        finally:
            active -= 1

    monkeypatch.setattr("backend.camera_relay.available", lambda: True)
    monkeypatch.setattr("backend.camera_relay.relay", receiver)
    with client.websocket_connect("/api/camera/relay") as first:
        assert first.receive_text() == "1"
        first.send_text("stop")
        with client.websocket_connect("/api/camera/relay") as second:
            assert second.receive_text() == "2"
            second.send_text("stop")
    assert count == 2
