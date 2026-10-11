"""Use HA's Mammotion subscription services with the real Agora browser SDK."""

import asyncio
import base64
from contextlib import suppress

from backend.ha import HAError


def agora_payload(data: dict) -> dict:
    if not isinstance(data, dict) or not all(
        data.get(k) for k in ("appid", "channelName", "token")
    ):
        raise HAError(
            "Home Assistant returned no Mammotion camera subscription after refresh. "
            "Check Mammotion-HA logs for get_stream_subscription (device reachability/quota), "
            "or select the configured Mammotion account provider in Settings."
        )
    payload = {
        "type": "agora",
        "appid": data["appid"],
        "channel": data["channelName"],
        "token": data["token"],
        "uid": data.get("uid", 0),
        "available_seconds": data.get("availableTime"),
    }
    if data.get("openEncrypt") not in (None, False, 0, "0"):
        key, salt = data.get("key"), data.get("salt")
        try:
            valid = (
                isinstance(key, str)
                and bool(key)
                and isinstance(salt, str)
                and len(base64.b64decode(salt, validate=True)) == 32
            )
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise HAError(
                "HA camera subscription is missing encryption key/salt; update Mammotion-HA"
            )
        payload["encryption"] = {"mode": "aes-256-gcm2", "key": key, "salt": salt}
    return payload


async def camera_session(ha, ws, camera_id: str):
    """Refresh before reading tokens; stop the encoder even after failed negotiation."""
    target = {"entity_id": camera_id}

    async def subscription():
        async with asyncio.timeout(35):
            await ha.call("mammotion", "refresh_stream", target)
            response = await ha.call("mammotion", "get_tokens", target, response=True)
        data = response.get("service_response", response)
        await ws.send_json(agora_payload(data))

    try:
        await subscription()
        while True:
            message = await asyncio.wait_for(ws.receive_json(), 45)
            if message.get("type") == "renew":
                await subscription()
            elif message.get("type") != "heartbeat":
                raise ValueError("Unsupported camera message")
    finally:
        with suppress(Exception):
            async with asyncio.timeout(10):
                await ha.call("mammotion", "stop_video", target)
