"""Serve MJPEG through HA's camera proxy and a private FFmpeg input."""

import asyncio
import audioop
import logging
from contextlib import suppress
from secrets import token_urlsafe

from aiohttp import web, WSMsgType

from .talkback import talk_websocket
from .video import FRAME_TIMEOUT, START_TIMEOUT

_LOGGER = logging.getLogger(__name__)

BOUNDARY = "valore"
AUDIO_RATE = 8000
AUDIO_PACKET_BYTES = 320
AV_CLIENT_LIMIT = 2
HEADERS = {
    "Content-Type": f"multipart/x-mixed-replace; boundary={BOUNDARY}",
    "Cache-Control": "no-store",
}


class LoopbackMedia:
    """Keep the HomeKit Bridge input off the LAN."""

    def __init__(self, video):
        self.video = video
        self.source = None
        self.audio_source = None
        self.bridge_url = None
        self.bridge_listeners = set()
        self.av_clients = 0
        self.runner = None

    async def start(self, bridge_ffmpeg=None):
        """Serve local media and optionally enable the Scrypted bridge."""
        prefix = f"/{token_urlsafe(24)}"
        app = web.Application()
        app.router.add_get(prefix + "/stream.mjpg", self.stream, allow_head=False)
        if bridge_ffmpeg:
            async def mux(request):
                return await self.av(request, bridge_ffmpeg)

            async def talk(request):
                return await talk_websocket(request, self.video, max_seconds=600, voice_gate=True)

            app.router.add_get(prefix + "/audio.pcm", self.pcm, allow_head=False)
            app.router.add_get(prefix + "/av", mux, allow_head=False)
            app.router.add_get(prefix + "/snapshot", self.snapshot, allow_head=False)
            app.router.add_get(prefix + "/rings", self.rings, allow_head=False)
            app.router.add_get(prefix + "/talk", talk, allow_head=False)
        self.runner = web.AppRunner(app, access_log=None, handler_cancellation=True)
        await self.runner.setup()
        try:
            await web.TCPSite(self.runner, "127.0.0.1", 0).start()
        except BaseException:
            await self.close()
            raise
        port = self.runner.addresses[0][1]
        base = f"http://127.0.0.1:{port}{prefix}"
        self.source = base + "/stream.mjpg"
        if bridge_ffmpeg:
            self.audio_source = base + "/audio.pcm"
            self.bridge_url = base

    async def close(self):
        """Disconnect local clients and release the listening socket."""
        self.source = None
        self.audio_source = None
        self.bridge_url = None
        for socket in tuple(self.bridge_listeners):
            await socket.close()
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None

    async def stream(self, request):
        """Write one MJPEG stream while this client is watching."""
        try:
            await self.video.async_acquire()
        except TimeoutError:
            raise web.HTTPServiceUnavailable(text="Camera did not become available") from None
        interactive = request.query.get("valore_preview") != "1"
        if interactive:
            self.video.answer_ring()
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
            await self.release_viewer(interactive)

    async def release_viewer(self, interactive):
        if interactive:
            self.video.interactive_viewers -= 1
        self.video.release()
        if (interactive and not self.video.interactive_viewers and self.video.ring_answered is not None
                and self.video.ring_answered.is_set()):
            self.video.ring_answered = None
            await self.video.close_ring()

    async def pcm(self, request):
        """Supply fixed-rate mono PCM to FFmpeg for the current relay."""
        audio = self.video.audio
        queue = asyncio.Queue(maxsize=8)
        audio.listeners.add(queue)
        response = web.StreamResponse(headers={"Cache-Control": "no-store"})
        buffer = bytearray()
        rate_state = None
        previous_rate = None
        clock = asyncio.get_running_loop().time
        deadline = clock()
        try:
            await response.prepare(request)
            while True:
                while not queue.empty():
                    chunk = queue.get_nowait()
                    if chunk is None:
                        return response
                    rate, payload = chunk
                    if rate != previous_rate:
                        rate_state = None
                        previous_rate = rate
                    if rate != AUDIO_RATE:
                        payload, rate_state = audioop.ratecv(
                            payload, 2, 1, rate, AUDIO_RATE, rate_state,
                        )
                    buffer.extend(payload)
                if len(buffer) > AUDIO_PACKET_BYTES * 16:
                    del buffer[:-AUDIO_PACKET_BYTES * 16]
                await response.write(bytes(buffer[:AUDIO_PACKET_BYTES]).ljust(AUDIO_PACKET_BYTES, b"\0"))
                del buffer[:AUDIO_PACKET_BYTES]
                deadline = max(deadline + 0.02, clock())
                await asyncio.sleep(max(0, deadline - clock()))
        except ConnectionError:
            return response
        finally:
            audio.listeners.discard(queue)

    async def feed_mjpeg(self, writer, sequence, frame):
        try:
            while True:
                writer.write(frame)
                await writer.drain()
                sequence, frame = await self.video.wait_frame(sequence)
        except (ConnectionError, web.HTTPServiceUnavailable):
            pass
        finally:
            writer.close()

    async def av(self, request, ffmpeg):
        """Mux the active video and microphone into one FFmpeg input."""
        if not self.audio_source or self.av_clients >= AV_CLIENT_LIMIT:
            raise web.HTTPServiceUnavailable()
        self.av_clients += 1
        acquired = False
        interactive = False
        process = None
        feeder = None
        phase = "acquire"
        response = web.StreamResponse(headers={
            "Content-Type": "video/mp2t", "Cache-Control": "no-store",
        })
        try:
            await self.video.async_acquire(wait=True)
            acquired = True
            phase = "wait_first_jpeg"
            async with asyncio.timeout(START_TIMEOUT + FRAME_TIMEOUT):
                sequence, frame = await self.video.wait_frame()
            phase = "start_muxer"
            process = await asyncio.create_subprocess_exec(
                ffmpeg, "-v", "error", "-nostdin",
                "-use_wallclock_as_timestamps", "1", "-probesize", "32", "-analyzeduration", "1",
                "-f", "image2pipe", "-framerate", "25", "-c:v", "mjpeg", "-i", "pipe:0",
                "-probesize", "32", "-analyzeduration", "1",
                "-f", "s16le", "-ar", str(AUDIO_RATE), "-ac", "1", "-i", self.audio_source,
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-profile:v", "baseline",
                "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", "-fps_mode", "vfr", "-pix_fmt", "yuv420p",
                "-g", "15", "-af", "aresample=async=1", "-c:a", "aac", "-ar", "16000",
                "-muxdelay", "0", "-muxpreload", "0", "-flush_packets", "1",
                "-f", "mpegts", "pipe:1",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            feeder = asyncio.create_task(self.feed_mjpeg(process.stdin, sequence, frame))
            async with asyncio.timeout(8):
                first = await process.stdout.read(1316)
            if not first:
                raise web.HTTPServiceUnavailable()
            await response.prepare(request)
            await response.write(first)
            self.video.answer_ring()
            interactive = True
            while chunk := await process.stdout.read(8192):
                await response.write(chunk)
            return response
        except (TimeoutError, web.HTTPServiceUnavailable):
            _LOGGER.warning("Valore Scrypted stream could not start: %s", phase)
            if not response.prepared:
                raise web.HTTPServiceUnavailable() from None
            return response
        except ConnectionError:
            return response
        finally:
            if feeder is not None:
                feeder.cancel()
                await asyncio.gather(feeder, return_exceptions=True)
            if process is not None and process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.communicate()
            self.av_clients -= 1
            if acquired:
                await self.release_viewer(interactive)

    async def snapshot(self, request):
        """Use the current or recent call to avoid waking a sleeping doorbell."""
        frame = self.video.frame or self.video.recent_ring_image()
        if frame is None and self.video.call_state != "idle":
            try:
                async with asyncio.timeout(5):
                    _, frame = await self.video.wait_ring_frame()
            except (TimeoutError, web.HTTPServiceUnavailable):
                pass
        if frame is None:
            raise web.HTTPServiceUnavailable()
        return web.Response(body=frame, content_type="image/jpeg", headers={"Cache-Control": "no-store"})

    async def rings(self, request):
        """Send physical button presses to the local bridge."""
        socket = web.WebSocketResponse(heartbeat=30, max_msg_size=512)
        await socket.prepare(request)
        self.bridge_listeners.add(socket)
        try:
            async for message in socket:
                if message.type == WSMsgType.TEXT:
                    break
        finally:
            self.bridge_listeners.discard(socket)
        return socket

    async def notify_ring(self):
        for socket in tuple(self.bridge_listeners):
            try:
                async with asyncio.timeout(0.5):
                    await socket.send_json({"event": "ring"})
            except (TimeoutError, ConnectionError):
                self.bridge_listeners.discard(socket)
