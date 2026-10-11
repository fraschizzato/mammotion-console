import asyncio
import json
from contextlib import asynccontextmanager

from tests.conftest import seed_ha


def test_camera_signaling_is_scoped_and_closes_session(client):
    ha = client.app.state.ha
    seed_ha(ha)
    sent = []
    closed = []

    class MockSocket:
        def __init__(self):
            self.queue = asyncio.Queue()

        def __aiter__(self):
            return self

        async def __anext__(self):
            return await self.queue.get()

        async def send(self, raw):
            msg = json.loads(raw)
            sent.append(msg)
            if msg["type"] == "camera/webrtc/get_client_config":
                await self.queue.put(
                    json.dumps(
                        {
                            "id": msg["id"],
                            "type": "result",
                            "success": True,
                            "result": {"configuration": {"iceServers": []}},
                        }
                    )
                )
            elif msg["type"] == "camera/webrtc/offer":
                await self.queue.put(
                    json.dumps(
                        {
                            "id": msg["id"],
                            "type": "event",
                            "event": {"type": "session", "session_id": "owned-session"},
                        }
                    )
                )
                await self.queue.put(
                    json.dumps(
                        {
                            "id": msg["id"],
                            "type": "event",
                            "event": {"type": "answer", "answer": "mock-sdp"},
                        }
                    )
                )
            else:
                await self.queue.put(
                    json.dumps({"id": msg["id"], "type": "result", "success": True})
                )

    @asynccontextmanager
    async def socket():
        try:
            yield MockSocket()
        finally:
            closed.append(True)

    ha.socket = socket
    with client.websocket_connect("/api/camera") as ws:
        ws.send_json({"type": "config", "entity_id": "camera.other"})
        assert ws.receive_json()["result"]["configuration"] == {"iceServers": []}
        ws.send_json({"type": "offer", "offer": "mock-offer"})
        assert ws.receive_json()["event"]["type"] == "session"
        assert ws.receive_json()["event"]["answer"] == "mock-sdp"
        ws.send_json(
            {
                "type": "candidate",
                "session_id": "foreign-session",
                "candidate": {"candidate": "candidate:fixture"},
            }
        )
        assert ws.receive_json()["success"]
    assert all(m["entity_id"] == "camera.luba" for m in sent)
    assert sent[-1]["session_id"] == "owned-session"
    assert closed


def test_camera_rejects_cross_origin(client):
    import pytest
    from starlette.websockets import WebSocketDisconnect

    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/api/camera", headers={"origin": "https://evil.example"}),
    ):
        pass
