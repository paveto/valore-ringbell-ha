"""Valore Ringbell integration."""

import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path
import ssl

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.components.http import StaticPathConfig
from homeassistant.const import CONF_DEVICE_ID, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send
from tuya_mobile import TuyaMobileInvalidAuth

from .cloud import Cloud, UnsupportedDevice
from .const import CONF_COUNTRY_CODE, CONF_DEVICE_NAME, DOMAIN, PLATFORMS
from .media import LoopbackMedia
from .rings import watch_rings
from .transport import enable_verified_mqtt
from .video import VideoSession

_LOGGER = logging.getLogger(__name__)
CARD_URL = "/valore_ringbell/card.js"


async def async_setup(hass, config):
    """Make the dashboard card available without exposing any camera data."""
    await hass.http.async_register_static_paths([
        StaticPathConfig(
            CARD_URL, str(Path(__file__).with_name("frontend") / "card.js"), cache_headers=False,
        ),
    ])
    if "frontend" in hass.config.components:
        from homeassistant.components.frontend import add_extra_js_url

        add_extra_js_url(hass, CARD_URL)
    return True


@dataclass
class Runtime:
    """Resources for one configured Valore doorbell."""

    video: VideoSession
    media: LoopbackMedia
    ring_task: asyncio.Task | None = None

    async def close(self):
        """Stop cloud polling and close active video during unload."""
        if self.ring_task is not None:
            self.ring_task.cancel()
            await asyncio.gather(self.ring_task, return_exceptions=True)
        await self.video.stop("shutdown")
        await self.media.close()


type ValoreEntry = ConfigEntry[Runtime]


@callback
def device_info(entry: ValoreEntry) -> DeviceInfo:
    """Group the camera and doorbell event on the same device."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.data[CONF_DEVICE_ID])},
        name=entry.data[CONF_DEVICE_NAME], manufacturer="Valore", model="IP07 / X6",
    )


def ring_signal(entry):
    """Return the dispatcher channel for this entry."""
    return f"{DOMAIN}_{entry.entry_id}_ring"


async def async_setup_entry(hass: HomeAssistant, entry: ValoreEntry) -> bool:
    """Start a native integration without an auxiliary app."""
    try:
        # SSL trust-store loading touches the filesystem.
        await hass.async_add_executor_job(
            lambda: enable_verified_mqtt(ssl.create_default_context())
        )
        data = entry.data
        cloud = Cloud(
            async_get_clientsession(hass), data[CONF_USERNAME], data[CONF_PASSWORD],
            data[CONF_COUNTRY_CODE],
        )
        async with asyncio.timeout(45):
            await cloud.authenticate()
            await cloud.select(data[CONF_DEVICE_ID])
    except TuyaMobileInvalidAuth:
        raise ConfigEntryAuthFailed("Tuya rejected the account credentials") from None
    except (aiohttp.ClientError, RuntimeError, ValueError, TimeoutError, UnsupportedDevice):
        raise ConfigEntryNotReady("Tuya account or selected device is currently unavailable") from None

    video = VideoSession(cloud.open_video)
    media = LoopbackMedia(video)
    await media.start()
    entry.runtime_data = Runtime(video, media)
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await entry.runtime_data.close()
        raise

    if "camera" not in hass.config.components:
        _LOGGER.error(
            "The Home Assistant camera component failed to start. "
            "Check the camera and stream errors in the system log"
        )

    async def deliver(event):
        video.prepare_for_ring()
        async_dispatcher_send(hass, ring_signal(entry), event)

    async def fetch(earliest):
        try:
            return await cloud.rings(earliest)
        except TuyaMobileInvalidAuth:
            entry.async_start_reauth(hass)
            raise asyncio.CancelledError from None

    entry.runtime_data.ring_task = entry.async_create_background_task(
        hass, watch_rings(fetch, deliver), "Valore ring polling"
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ValoreEntry) -> bool:
    """Unload entities before releasing their shared resources."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    await entry.runtime_data.close()
    return True
