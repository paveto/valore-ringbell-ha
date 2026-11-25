"""Save a ring's first JPEG and record its live relay to private HA media."""

import asyncio
from contextlib import suppress
from datetime import datetime, UTC
import logging
from pathlib import Path

from aiohttp import web

_LOGGER = logging.getLogger(__name__)
MEDIA_FOLDER = "valore_ringbell"
SNAPSHOT_TIMEOUT = 8
VIDEO_START_TIMEOUT = 45
MAX_RECORD_SECONDS = 600


class RingArchive:
    """Persist recordings without holding the video session open."""

    def __init__(self, hass, video, ffmpeg):
        self.hass = hass
        self.video = video
        self.ffmpeg = ffmpeg
        self.tasks = set()

    async def capture(self, event):
        root = self.hass.config.media_dirs.get("local")
        if not root:
            return None
        stamp = datetime.fromtimestamp(event["timestamp"], UTC).strftime("%Y%m%d-%H%M%S")
        name = f'{stamp}-{event["event_id"][:32]}'
        folder = Path(root) / MEDIA_FOLDER
        photo = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(self._save(folder, name, photo))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        try:
            async with asyncio.timeout(SNAPSHOT_TIMEOUT):
                return await asyncio.shield(photo)
        except TimeoutError:
            return None

    async def _save(self, folder, name, photo):
        try:
            async with asyncio.timeout(VIDEO_START_TIMEOUT):
                sequence, jpeg = await self.video.wait_ring_frame()

            def save_jpeg():
                folder.mkdir(mode=0o750, parents=True, exist_ok=True)
                staging = folder / f".{name}.jpg.tmp"
                staging.write_bytes(jpeg)
                staging.replace(folder / f"{name}.jpg")

            await self.hass.async_add_executor_job(save_jpeg)
            if not photo.done():
                photo.set_result(f"/media/local/{MEDIA_FOLDER}/{name}.jpg")
            await self._record(folder, name, sequence, jpeg)
        except (TimeoutError, web.HTTPServiceUnavailable):
            _LOGGER.debug("No live frames to save for the doorbell ring")
        except (OSError, RuntimeError):
            _LOGGER.warning("Could not save the Valore ring to local media")
        finally:
            if not photo.done():
                photo.set_result(None)

    async def _record(self, folder, name, sequence, frame):
        temp = folder / f".{name}.mp4"
        final = folder / f"{name}.mp4"
        process = await asyncio.create_subprocess_exec(
            self.ffmpeg, "-v", "error", "-nostats",
            "-use_wallclock_as_timestamps", "1", "-f", "image2pipe", "-framerate", "25",
            "-vcodec", "mjpeg", "-i", "pipe:0", "-an",
            "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", "-fps_mode", "vfr",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", "-y", str(temp),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            async with asyncio.timeout(MAX_RECORD_SECONDS):
                while True:
                    process.stdin.write(frame)
                    await process.stdin.drain()
                    sequence, frame = await self.video.wait_frame(sequence)
        except (TimeoutError, web.HTTPServiceUnavailable, ConnectionError):
            pass
        finally:
            process.stdin.close()
            try:
                async with asyncio.timeout(5):
                    status = await process.wait()
            except TimeoutError:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
                status = -1
            if status == 0:
                await self.hass.async_add_executor_job(temp.replace, final)
            else:
                await self.hass.async_add_executor_job(temp.unlink, True)
                _LOGGER.warning("FFmpeg did not finalize the Valore ring recording")

    async def close(self):
        tasks = list(self.tasks)
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=8)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
