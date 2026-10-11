"""Isolated native Agora receiver. Stdout contains only length-prefixed JPEGs."""

import asyncio
import base64
import os
import signal
import struct
import sys
import time
from io import BytesIO


async def run(base_url, output):
    import aiohttp
    from agora.rtc.agora_base import (
        ClientRoleType,
        EncryptionConfig,
        RTCConnConfig,
        RtcConnectionPublishConfig,
        VideoSubscriptionOptions,
    )
    from agora.rtc.agora_service import AgoraService, AgoraServiceConfig
    from agora.rtc.video_frame_observer import IVideoFrameObserver
    from PIL import Image

    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, stopped.set)
    loop.add_signal_handler(signal.SIGINT, stopped.set)

    class Observer(IVideoFrameObserver):
        latest = None
        last_copy = 0.0

        def on_frame(self, channel, uid, frame):
            now = time.monotonic()
            if now - self.last_copy < 0.2 or not 0 < frame.width <= 4096:
                return True
            if not 0 < frame.height <= 4096:
                return True
            self.last_copy = now
            self.latest = (
                frame.width,
                frame.height,
                frame.y_stride,
                frame.u_stride,
                frame.v_stride,
                bytes(frame.y_buffer),
                bytes(frame.u_buffer),
                bytes(frame.v_buffer),
                frame.rotation,
            )
            return True

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(base_url + "/api/camera", origin=base_url) as ws:
            data = await ws.receive_json(timeout=70)
            if data.get("type") != "agora":
                raise RuntimeError("Unsupported native subscription")
            service = AgoraService()
            config = AgoraServiceConfig(
                appid=data["appid"],
                enable_video=1,
                log_path="/dev/null",
                log_level=0,
                data_dir="/tmp",
                config_dir="/tmp",
            )
            if service.initialize(config) != 0:
                raise RuntimeError("Native receiver initialization failed")
            connection = None
            try:
                connection = service.create_rtc_connection(
                    RTCConnConfig(client_role_type=ClientRoleType.CLIENT_ROLE_AUDIENCE),
                    RtcConnectionPublishConfig(is_publish_audio=False, is_publish_video=False),
                )
                observer = Observer()
                if encryption := data.get("encryption"):
                    salt = base64.b64decode(encryption["salt"], validate=True)
                    if encryption.get("mode") != "aes-256-gcm2" or len(salt) != 32:
                        raise ValueError("Unsupported camera encryption")
                    if (
                        connection.enable_encryption(
                            1,
                            EncryptionConfig(
                                encryption_mode=8,
                                encryption_key=encryption["key"],
                                encryption_kdf_salt=bytearray(salt),
                            ),
                        )
                        != 0
                    ):
                        raise RuntimeError("Native encryption initialization failed")
                connection.register_video_frame_observer(observer)
                connection.get_local_user().subscribe_all_video(
                    VideoSubscriptionOptions(encodedFrameOnly=False)
                )
                if connection.connect(data["token"], data["channel"], str(data["uid"])) != 0:
                    raise RuntimeError("Native receiver join failed")
                started = last_heartbeat = last_frame = time.monotonic()
                while not stopped.is_set() and time.monotonic() - started < 600:
                    now = time.monotonic()
                    if now - last_heartbeat >= 10:
                        await ws.send_json({"type": "heartbeat"})
                        last_heartbeat = now
                    frame, observer.latest = observer.latest, None
                    if frame:
                        w, h, ys, us, vs, y, u, v, rotation = frame
                        yp = Image.frombytes("L", (w, h), y, "raw", "L", ys)
                        size = ((w + 1) // 2, (h + 1) // 2)
                        up = Image.frombytes("L", size, u, "raw", "L", us).resize((w, h))
                        vp = Image.frombytes("L", size, v, "raw", "L", vs).resize((w, h))
                        image = Image.merge("YCbCr", (yp, up, vp)).convert("RGB")
                        if rotation in (90, 180, 270):
                            image = image.rotate(-rotation, expand=True)
                        image.thumbnail((960, 720))
                        buffer = BytesIO()
                        image.save(buffer, format="JPEG", quality=70)
                        jpeg = buffer.getvalue()
                        output.write(struct.pack("!I", len(jpeg)) + jpeg)
                        output.flush()
                        last_frame = now
                    elif now - last_frame > 40:
                        raise TimeoutError("No native video frames")
                    await asyncio.sleep(0.05)
            finally:
                if connection:
                    connection.disconnect()
                    connection.release()
                service.release()


if __name__ == "__main__":
    # Native libraries may print session metadata even with logging disabled.
    # Only our private binary descriptor reaches the parent process.
    stream = os.fdopen(os.dup(1), "wb", buffering=0)
    sink = os.open(os.devnull, os.O_WRONLY)
    os.dup2(sink, 1)
    os.dup2(sink, 2)
    try:
        asyncio.run(run(sys.argv[1], stream))
    except Exception:
        sys.exit(1)
