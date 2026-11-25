"""Serve MJPEG through HA's camera proxy and a private FFmpeg input."""

import asyncio
from secrets import token_urlsafe

from aiohttp import web

from .video import FRAME_TIMEOUT, START_TIMEOUT

BOUNDARY = "valore"
HEADERS = {
    "Content-Type": f"multipart/x-mixed-replace; boundary={BOUNDARY}",
    "Cache-Control": "no-store",
}


class LoopbackMedia:
    """Keep the HomeKit Bridge input off the LAN."""

    def __init__(self, video):
        self.video = video
        self.source = None
        self.runner = None

    async def start(self):
        """Create a process-local input for HomeKit Bridge's FFmpeg."""
        path = f"/{token_urlsafe(24)}/stream.mjpg"
        app = web.Application()
        app.router.add_get(path, self.stream, allow_head=False)
        self.runner = web.AppRunner(app, access_log=None, handler_cancellation=True)
        await self.runner.setup()
        try:
            await web.TCPSite(self.runner, "127.0.0.1", 0).start()
        except BaseException:
            await self.close()
            raise
        port = self.runner.addresses[0][1]
        self.source = f"http://127.0.0.1:{port}{path}"

    async def close(self):
        """Disconnect local clients and release the listening socket."""
        self.source = None
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None

    async def stream(self, request):
        """Write one MJPEG stream while this client is watching."""
        self.video.acquire()
        response = web.StreamResponse(headers=HEADERS)
        try:
            try:
                async with asyncio.timeout(START_TIMEOUT + FRAME_TIMEOUT):
                    sequence, frame = await self.video.wait_frame()
            except TimeoutError:
                raise web.HTTPServiceUnavailable(text="No live video available") from None
            await response.prepare(request)
            while True:
                header = f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(frame)}\r\n\r\n"
                await response.write(header.encode() + frame + b"\r\n")
                sequence, frame = await self.video.wait_frame(sequence)
        except web.HTTPServiceUnavailable:
            if not response.prepared:
                raise
            return response
        except ConnectionError:
            return response
        finally:
            self.video.release()
