"""Home Assistant camera entity and HomeKit Bridge video source."""

import asyncio
import logging

from aiohttp import web
from homeassistant.components.camera import Camera
from homeassistant.const import CONF_DEVICE_ID

from . import device_info
from .video import FRAME_TIMEOUT

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    """Register the entity under the selected device."""
    async_add_entities([ValoreCamera(entry)])


class ValoreCamera(Camera):
    """Present one on-demand MJPEG camera."""

    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, entry):
        super().__init__()
        self.runtime = entry.runtime_data
        self._attr_device_info = device_info(entry)
        self._attr_unique_id = f"{entry.data[CONF_DEVICE_ID]}_camera"

    async def async_added_to_hass(self):
        """Log the entity ID after the camera has been registered."""
        await super().async_added_to_hass()
        _LOGGER.info("Valore camera registered as %s", self.entity_id)

    async def async_camera_image(self, width=None, height=None):
        """Use a recent live frame and bound waiting on a sleeping doorbell."""
        video = self.runtime.video
        if (frame := video.frame or video.recent_ring_image()) is not None:
            return frame
        try:
            await video.async_acquire()
            try:
                async with asyncio.timeout(FRAME_TIMEOUT):
                    _, frame = await video.wait_frame()
                    return frame
            finally:
                video.release()
        except (TimeoutError, web.HTTPServiceUnavailable):
            return None

    async def handle_async_mjpeg_stream(self, request):
        """Stream video in native Home Assistant camera cards."""
        return await self.runtime.media.stream(request)

    async def stream_source(self):
        """Provide the private FFmpeg input to HomeKit Bridge."""
        return self.runtime.media.source
