"""Run inside the application container to verify a genuinely decoded camera frame."""

import argparse
import asyncio
import json
import sys
from importlib.metadata import version
from urllib.request import Request, urlopen

from backend.camera_capture import capture_frame


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=("home_assistant", "mammotion"))
    args = parser.parse_args()
    url = "http://127.0.0.1:5000/api/camera/settings"
    if args.source:
        request = Request(
            url,
            data=json.dumps({"mode": args.source}).encode(),
            method="PUT",
            headers={"X-Console-Request": "1", "Content-Type": "application/json"},
        )
        with urlopen(request, timeout=15) as response:
            response.read()
    print("PyMammotion:", version("pymammotion"))
    with urlopen(url, timeout=15) as response:
        settings = json.load(response)
    print("Video source:", settings["mode"])
    print("Credentials configured:", settings["configured"][settings["mode"]])
    frame = await capture_frame()
    print("Decoded camera frame:", bool(frame), "JPEG bytes:", len(frame))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:
        # Never dump exceptions that might carry camera/session material.
        print("Video verification failed:", type(error).__name__)
        print("Inspect the status in Drive; do not publish camera tokens or account credentials.")
        sys.exit(1)
