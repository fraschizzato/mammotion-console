"""Browser-compatible video relay with a bounded native worker lifetime."""

import asyncio
import importlib.util
import struct
import sys
from contextlib import suppress


def available():
    return sys.platform == "linux" and importlib.util.find_spec("agora") is not None


async def relay(ws):
    port = ws.scope.get("server", ("127.0.0.1", 5000))[1]
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "backend.native_camera",
        f"http://127.0.0.1:{port}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )

    async def frames():
        assert process.stdout is not None
        first = True
        while True:
            async with asyncio.timeout(110 if first else 45):
                header = await process.stdout.readexactly(4)
                length = struct.unpack("!I", header)[0]
                if not 0 < length <= 2_000_000:
                    raise ValueError("Invalid native frame length")
                frame = await process.stdout.readexactly(length)
            if not frame.startswith(b"\xff\xd8"):
                raise ValueError("Invalid native frame")
            await ws.send_bytes(frame)
            first = False

    async def browser():
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                return

    tasks = [asyncio.create_task(frames()), asyncio.create_task(browser())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 8)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
