"""One short nudge at a time, pinned transport and best-effort zero-speed stop."""

import asyncio
from typing import Any

from backend.ha import HAError


class Movement:
    def __init__(self):
        self.task: asyncio.Task | None = None
        self.release = asyncio.Event()
        self.fault = ""

    async def nudge(self, handle: Any, transport: Any, direction: str):
        mapping = {
            "forward": "move_forward",
            "backward": "move_back",
            "left": "move_left",
            "right": "move_right",
        }
        if direction not in mapping:
            raise ValueError("Unknown movement direction")
        if self.fault:
            raise HAError(self.fault)
        if self.task and not self.task.done():
            raise HAError("A movement is already active; release before another nudge")
        self.release = asyncio.Event()
        payload = getattr(handle.commands, mapping[direction])(0.25)

        async def send(payload: bytes):
            async with asyncio.timeout(3):
                sender = getattr(transport, "send_user", transport.send)
                await sender(
                    payload,
                    iot_id=handle.iot_id,
                    firmware_version=handle.snapshot.raw.device_firmwares.device_version
                    or "1.0.0.0",
                )

        async def execute():
            failure = False
            try:
                await send(payload)
                try:
                    await asyncio.wait_for(self.release.wait(), 0.25)
                except TimeoutError:
                    pass
            except Exception:
                failure = True
            finally:
                try:
                    await send(handle.commands.send_movement(linear_speed=0, angular_speed=0))
                except Exception:
                    self.fault = "Movement stop was not confirmed. Use the mower's physical stop; restart the console only after checking it."
                    raise HAError(self.fault) from None
            if failure:
                raise HAError("Movement delivery was uncertain; a zero-speed command was sent")
            return {"accepted": True, "stopped": True}

        self.task = asyncio.create_task(execute())
        # A browser disconnect must not cancel the automatic stop.
        return await asyncio.shield(self.task)

    async def stop(self):
        self.release.set()
        if self.task and not self.task.done():
            await asyncio.shield(self.task)
        if self.fault:
            raise HAError(self.fault)
        return {"stopped": True}
