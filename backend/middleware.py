from fastapi import HTTPException

from backend.maps import MAX_BYTES


class RequestSizeLimit:
    """Bound streamed/chunked uploads as well as declared Content-Length."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        size = 0

        async def limited_receive():
            nonlocal size
            message = await receive()
            if message["type"] == "http.request":
                size += len(message.get("body", b""))
                if size > MAX_BYTES:
                    raise HTTPException(413, "Request exceeds the upload limit")
            return message

        await self.app(scope, limited_receive if scope["type"] == "http" else receive, send)
