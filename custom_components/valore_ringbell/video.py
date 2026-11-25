"""Share one on-demand relay between camera clients."""

import asyncio
from collections import deque
import io
import logging
import math
import re
import time

from aiohttp import web
from PIL import Image

from .transport import DeviceAnswerTimeout

_LOGGER = logging.getLogger(__name__)

COOLDOWN = 20
IDLE_TIMEOUT = 10
START_TIMEOUT = 75
FRAME_TIMEOUT = 15
CLOSE_TIMEOUT = 15
MAX_RECONNECTS = 2
RETRYABLE_ENDS = {
    "relay_eof", "relay_closed", "relay_error", "first_frame_timeout", "frame_stall",
}
RELAY_EOF = re.compile(r"(\d+) bytes read on a total of (\d+) expected bytes")


def session_result(reason):
    if reason.startswith("relay closed"):
        match = RELAY_EOF.search(reason)
        if match:
            partial, expected = map(int, match.groups())
            return {"end": "relay_eof", "partial_bytes": partial, "expected_bytes": expected}
        return {"end": "relay_closed" if reason == "relay closed" else "relay_error"}
    if reason.startswith("device disconnect"):
        match = re.search(r"close_reason=(\d+)$", reason)
        return {"end": "device_disconnect", "device_close_reason": int(match[1]) if match else None}
    return {"end": "session_closed"}


class VideoSession:
    def __init__(self, open_session):
        self.open_session = open_session
        self.active_session = None
        self.task = None
        self.idle = None
        self.viewers = 0
        self.state = "idle"
        self.changed = asyncio.Event()
        self.frame = None
        self.sequence = 0
        self.resolution = None
        self.cooldown_until = 0
        self.error = None
        self.reconnects = 0
        self.session_count = 0
        self.history = deque(maxlen=10)
        self.stopping = None
        self.shutdown = False
        self.reset_metrics()

    def reset_metrics(self):
        self.session_started = None
        self.first_frame_at = None
        self.last_frame_at = None
        self.session_frames = 0
        self.frame_times = deque(maxlen=240)

    def notify(self):
        self.changed.set()
        self.changed = asyncio.Event()

    def set_state(self, state):
        self.state = state
        self.notify()

    def on_frame(self, frame):
        if self.state not in ("connecting", "streaming"):
            return
        if not (frame.startswith(b"\xff\xd8\xff") and frame.endswith(b"\xff\xd9")):
            return
        now = time.monotonic()
        if self.first_frame_at is None:
            try:
                with Image.open(io.BytesIO(frame)) as image:
                    image.load()
                    self.resolution = image.size
            except (OSError, ValueError):
                return
            self.first_frame_at = now
        self.last_frame_at = now
        self.frame_times.append(now)
        self.session_frames += 1
        self.sequence += 1
        self.frame = frame
        self.set_state("streaming")

    def session_metrics(self, ended):
        first, last = self.first_frame_at, self.last_frame_at
        media_seconds = last - first if first is not None else 0
        return {
            "session": self.session_count,
            "duration_seconds": round(ended - self.session_started, 2),
            "first_frame_seconds": round(first - self.session_started, 2) if first is not None else None,
            "media_seconds": round(media_seconds, 2),
            "frames": self.session_frames,
            "average_fps": round((self.session_frames - 1) / media_seconds, 2) if media_seconds else 0,
        }

    async def wait_for_end(self, stream):
        closed = asyncio.create_task(stream.async_wait_closed())
        try:
            deadline = time.monotonic() + FRAME_TIMEOUT
            while True:
                if closed.done():
                    return session_result(closed.result())
                done, _ = await asyncio.wait({closed}, timeout=max(0, deadline - time.monotonic()))
                if done:
                    return session_result(closed.result())
                if self.last_frame_at is None:
                    return {"end": "first_frame_timeout"}
                deadline = self.last_frame_at + FRAME_TIMEOUT
                if deadline <= time.monotonic():
                    return {"end": "frame_stall"}
        finally:
            closed.cancel()
            await asyncio.gather(closed, return_exceptions=True)

    async def run_attempt(self):
        self.reset_metrics()
        self.session_started = time.monotonic()
        self.session_count += 1
        self.set_state("connecting")
        stream = None
        result = {"end": "setup_error"}
        try:
            async with asyncio.timeout(START_TIMEOUT):
                stream = await self.open_session(self.on_frame)
                self.active_session = stream
                await stream.async_start()
            result = await self.wait_for_end(stream)
        except asyncio.CancelledError:
            result = {"end": self.stopping or "cancelled"}
            raise
        except DeviceAnswerTimeout:
            result = {"end": "no_device_answer"}
        except TimeoutError:
            result = {"end": "setup_timeout"}
        except Exception as exc:
            result = {"end": "setup_error", "error_type": type(exc).__name__}
        finally:
            metrics = self.session_metrics(time.monotonic())
            self.active_session = None
            self.frame = None
            self.set_state("closing")
            if stream is not None:
                try:
                    async with asyncio.timeout(CLOSE_TIMEOUT):
                        await stream.async_close()
                except Exception as exc:
                    result["cleanup_error"] = type(exc).__name__
            self.history.appendleft(metrics | result)
            _LOGGER.debug("Video session metrics: %s", self.history[0])
            self.session_started = None
            self.cooldown_until = time.monotonic() + COOLDOWN
        return result

    async def run(self):
        try:
            for attempt in range(MAX_RECONNECTS + 1):
                if self.shutdown or not self.viewers:
                    break
                self.reconnects = attempt
                result = await self.run_attempt()
                self.error = result["end"]
                if self.shutdown or result.get("cleanup_error") or not self.viewers or attempt == MAX_RECONNECTS:
                    break
                if self.error not in RETRYABLE_ENDS:
                    break
                self.set_state("backoff")
                await asyncio.sleep(max(0, self.cooldown_until - time.monotonic()))
        finally:
            self.set_state("idle")

    @property
    def retry_in(self):
        return max(0, math.ceil(self.cooldown_until - time.monotonic()))

    def acquire(self):
        closing = self.state == "closing" or (self.task is not None and self.task.cancelling())
        if self.shutdown or closing or (self.state == "idle" and self.retry_in):
            raise web.HTTPServiceUnavailable(
                text="Camera unavailable. Retry shortly.",
                headers={"Retry-After": str(self.retry_in or COOLDOWN)},
            )
        if self.idle is not None:
            self.idle.cancel()
            self.idle = None
        if self.task is None or self.task.done():
            self.error = None
            self.set_state("connecting")
            self.task = asyncio.create_task(self.run())
        self.viewers += 1

    def release(self):
        self.viewers -= 1
        if self.viewers == 0 and self.task is not None and not self.task.done() and not self.shutdown:
            if self.state == "backoff":
                self.task.cancel()
            elif self.state != "closing":
                self.idle = asyncio.create_task(self.stop_when_idle())

    async def stop_when_idle(self):
        await asyncio.sleep(IDLE_TIMEOUT)
        self.idle = None
        if not self.viewers:
            await self.stop("idle_timeout")

    async def stop(self, reason):
        if reason == "shutdown":
            self.shutdown = True
            if self.idle is not None:
                self.idle.cancel()
                self.idle = None
        if self.task is not None and not self.task.done():
            if self.state != "closing" and not self.task.cancelling():
                self.stopping = reason
                self.task.cancel()
            await asyncio.shield(asyncio.gather(self.task, return_exceptions=True))
            self.stopping = None

    async def wait_frame(self, after=-1):
        while True:
            if self.frame is not None and self.sequence > after:
                return self.sequence, self.frame
            if self.state == "idle" or self.shutdown:
                raise web.HTTPServiceUnavailable(text="No live video available")
            await self.changed.wait()
