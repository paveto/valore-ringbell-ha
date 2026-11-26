"""Valore Ringbell integration."""

import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path
import ssl
import time

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.components.http import StaticPathConfig
from homeassistant.const import CONF_DEVICE_ID, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import config_validation as cv, device_registry, entity_registry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.storage import Store
from tuya_mobile import TuyaMobileInvalidAuth

from .archive import RingArchive
from .cloud import Cloud, UnsupportedDevice
from .const import CONF_COUNTRY_CODE, CONF_DEVICE_NAME, DOMAIN, PLATFORMS
from .media import LoopbackMedia
from .live_audio import LiveAudio
from .rings import watch_rings
from .transport import enable_verified_mqtt
from .video import VideoSession
from .views import DoorbellAudioView, DoorbellHangupView, get_camera_runtime

_LOGGER = logging.getLogger(__name__)
CARD_URL = "/valore_ringbell/card.js"
BRIDGE_KEY = "valore_ringbell.scrypted"
BRIDGE_CAMERA = "scrypted_camera"
WAITING_TONE = "waiting_tone"
CONFIG_SCHEMA = vol.Schema(
    {vol.Optional(DOMAIN): vol.Schema({
        vol.Optional(BRIDGE_CAMERA): cv.entity_domain("camera"),
        vol.Optional(WAITING_TONE, default=False): cv.boolean,
    })},
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass, config):
    """Make the dashboard card available without exposing any camera data."""
    hass.data[DOMAIN] = config.get(DOMAIN, {})

    async def hangup(call):
        runtime = get_camera_runtime(hass, call.data["entity_id"])
        if runtime is not None and runtime.video.call_id == call.data["call_id"]:
            await runtime.video.close_ring()

    async_register_admin_service(hass, DOMAIN, "hangup", hangup, vol.Schema({
        vol.Required("entity_id"): cv.entity_domain("camera"),
        vol.Required("call_id"): vol.Match(r"^[a-f0-9]{24}$"),
    }))

    await hass.http.async_register_static_paths([
        StaticPathConfig(
            f"/valore_ringbell/{name}", str(Path(__file__).with_name("frontend") / name),
            cache_headers=False,
        ) for name in ("card.js", "audio.js", "talk.js", "microphone.js")
    ])
    hass.http.register_view(DoorbellAudioView())
    hass.http.register_view(DoorbellHangupView())
    if "frontend" in hass.config.components:
        from homeassistant.components.frontend import add_extra_js_url

        add_extra_js_url(hass, CARD_URL)
    return True


@dataclass
class Runtime:
    """Resources for one configured Valore doorbell."""

    video: VideoSession
    media: LoopbackMedia
    audio: LiveAudio
    archive: RingArchive
    ring_task: asyncio.Task | None = None

    async def close(self):
        """Stop cloud polling and close active video during unload."""
        if self.ring_task is not None:
            self.ring_task.cancel()
            await asyncio.gather(self.ring_task, return_exceptions=True)
        await self.video.stop("shutdown")
        await self.archive.close()
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

    @callback
    def call_state_changed(state, deadline):
        registry = device_registry.async_get(hass)
        if device := registry.async_get_device(identifiers={(DOMAIN, data[CONF_DEVICE_ID])}):
            hass.bus.async_fire(
                "valore_ringbell_call_state",
                {"device_id": device.id, "state": state, "deadline": deadline},
            )

    options = hass.data[DOMAIN]
    video = VideoSession(cloud.open_video, call_state_changed, waiting_tone=options.get(WAITING_TONE, False))
    media = LoopbackMedia(video)
    registry = entity_registry.async_get(hass)
    camera_id = registry.async_get_entity_id("camera", DOMAIN, f"{data[CONF_DEVICE_ID]}_camera")
    configured_camera = options.get(BRIDGE_CAMERA)
    bridge_enabled = configured_camera is not None and camera_id == configured_camera
    ffmpeg = get_ffmpeg_manager(hass).binary
    await media.start(ffmpeg if bridge_enabled else None)
    cloud.on_audio = video.audio.publish
    archive = RingArchive(hass, video, ffmpeg)
    entry.runtime_data = Runtime(video, media, video.audio, archive)
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        if media.bridge_url:
            await Store(hass, 1, BRIDGE_KEY, private=True).async_save({"url": media.bridge_url})
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
        call_id = video.call_id
        await media.notify_ring()
        image = await archive.capture(event)
        async_dispatcher_send(hass, ring_signal(entry), event | {
            "snapshot_url": image,
            "call_id": call_id,
            "notification_prepare_seconds": max(0, int(time.time()) - event["detected_at"]),
        })

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
