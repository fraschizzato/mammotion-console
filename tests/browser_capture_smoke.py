"""Run through stdin inside the production image; no physical camera or Internet."""

import asyncio
import io
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from PIL import Image

from backend.camera_capture import capture_frame

HTML = b"""<!doctype html><button>Drive</button><button onclick="start()">Connect camera</button>
<section class="camera-panel"><video autoplay muted playsinline></video></section>
<script>
function start() {
  const c = document.createElement('canvas'); c.width=320; c.height=180;
  function frame() { const x=c.getContext('2d'); x.fillStyle='green'; x.fillRect(0,0,320,180); requestAnimationFrame(frame); }
  frame(); const v=document.querySelector('video'); v.srcObject=c.captureStream(10); v.play();
}
</script>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(HTML)

    def log_message(self, *_args):
        pass


async def main():
    server = HTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        frame = await capture_frame(f"http://127.0.0.1:{server.server_port}")
        image = Image.open(io.BytesIO(frame))
        assert image.size == (320, 180)
        r, g, b = image.getpixel((160, 90))
        assert g > 100 and r < 30 and b < 30
        print("Headless camera capture and cleanup passed (synthetic video, no network)")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    asyncio.run(main())
