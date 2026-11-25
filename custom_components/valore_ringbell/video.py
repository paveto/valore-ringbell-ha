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
from tuya_ipc_p2p_sdk.exceptions import TuyaIpcP2pDeviceBusyError

from .transport import DeviceAnswerTimeout
from .live_audio import LiveAudio

_LOGGER = logging.getLogger(__name__)

COOLDOWN = 20
RING_VIDEO_WINDOW = 180
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
    def __init__(self, open_session, on_call_state=None):
        self.open_session = open_session
        self.on_call_state = on_call_state
        self.call_state = "idle"
        self.ring_deadline = None
        self.ring_snapshot = None
        self.ring_snapshot_until = 0
        self.active_session = None
        self.audio = LiveAudio()
        self.task = None
        self.idle = None
        self.ring_preview = None
        self.ring_answered = None
        self.interactive_viewers = 0
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

    def set_call_state(self, state):
        if state == self.call_state:
            return
        self.call_state = state
        if self.on_call_state is not None:
            self.on_call_state(state, self.ring_deadline)

    def recent_ring_image(self):
        if self.ring_snapshot_until > time.monotonic():
            return self.ring_snapshot
        self.ring_snapshot = None
        return None

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
        if self.call_state != "idle" and self.ring_snapshot is None:
            self.ring_snapshot = frame
            self.ring_snapshot_until = now + RING_VIDEO_WINDOW
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
        except TuyaIpcP2pDeviceBusyError:
            result = {"end": "device_busy"}
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
            if not metrics["frames"] and result["end"] not in {
                "idle_timeout", "shutdown", "cancelled"
            }:
                _LOGGER.warning(
                    "Valore video ended before the first frame after %s s: %s (%s)",
                    metrics["duration_seconds"], result["end"],
                    result.get("error_type", "no additional error"),
                )
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
            self.audio.close()
            self.set_state("idle")
            self.set_call_state("idle")

    @property
    def retry_in(self):
        return max(0, math.ceil(self.cooldown_until - time.monotonic()))

    def acquire(self):
        task = self.task
        # A cancelled task retains a nonzero cancellation count after it completes.
        closing = self.state == "closing" or (
            task is not None and not task.done() and task.cancelling()
        )
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

    async def async_acquire(self, *, wait=False):
        """Join the stream, waiting briefly if a ring is preparing it."""
        async with asyncio.timeout(COOLDOWN + CLOSE_TIMEOUT):
            while True:
                try:
                    self.acquire()
                    return
                except web.HTTPServiceUnavailable:
                    if self.shutdown or not (
                        wait or (self.ring_preview is not None and not self.ring_preview.done())
                    ):
                        raise
                    changed = self.changed
                    delay = max(0.1, self.cooldown_until - time.monotonic()) if self.retry_in else COOLDOWN
                    try:
                        await asyncio.wait_for(changed.wait(), timeout=delay)
                    except TimeoutError:
                        continue

    def prepare_for_ring(self):
        """Open one short preview when a doorbell press is received."""
        if not self.shutdown and (self.ring_preview is None or self.ring_preview.done()):
            self.ring_answered = asyncio.Event()
            self.ring_deadline = int(time.time() + RING_VIDEO_WINDOW)
            self.ring_snapshot = self.frame
            self.ring_snapshot_until = time.monotonic() + RING_VIDEO_WINDOW
            self.set_call_state("ringing")
            if self.interactive_viewers:
                self.set_call_state("answered")
                self.ring_answered.set()
            self.ring_preview = asyncio.create_task(self._prepare_for_ring(self.ring_answered))

    async def _prepare_for_ring(self, ring_answered):
        try:
            async with asyncio.timeout(RING_VIDEO_WINDOW):
                await self.async_acquire(wait=True)
                try:
                    await self.wait_frame()
                    _LOGGER.debug("Valore video is ready for the caller")
                    answered = asyncio.create_task(ring_answered.wait())
                    try:
                        await asyncio.wait((answered, self.task), return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        answered.cancel()
                finally:
                    self.release()
        except (TimeoutError, web.HTTPServiceUnavailable):
            _LOGGER.debug("Valore ring video window ended")
            if not self.interactive_viewers:
                self.set_call_state("idle")
                await self.stop("unanswered")

    def answer_ring(self):
        self.interactive_viewers += 1
        if self.call_state == "ringing" and self.ring_answered is not None:
            self.ring_answered.set()
            self.set_call_state("answered")

    async def close_ring(self):
        """Release the unanswered hold and disconnect the current relay."""
        self.ring_answered = None
        self.set_call_state("idle")
        await self.cancel_ring_preview()
        await self.stop("hangup")

    async def cancel_ring_preview(self):
        preview = self.ring_preview
        if preview is not None:
            preview.cancel()
            await asyncio.gather(preview, return_exceptions=True)
            if self.ring_preview is preview:
                self.ring_preview = None

    async def wait_ring_frame(self, after=-1):
        """Allow the background preview to acquire video first."""
        while self.state == "idle" and self.ring_preview is not None and not self.ring_preview.done():
            await self.changed.wait()
        return await self.wait_frame(after)

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
            self.set_call_state("idle")
            await self.cancel_ring_preview()
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
