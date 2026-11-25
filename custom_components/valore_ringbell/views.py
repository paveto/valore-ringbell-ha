"""Authenticated browser controls for the doorbell conversation."""

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.const import KEY_HASS, KEY_HASS_USER
from homeassistant.helpers import entity_registry

from .const import DOMAIN
from .talkback import talk_websocket


class DoorbellAudioView(HomeAssistantView):
    url = "/api/valore_ringbell/{entity_id}/{mode:audio|talk}"
    name = "api:valore_ringbell:audio"

    async def get(self, request, entity_id, mode):
        runtime = get_runtime(request, entity_id)
        origin = request.headers.get("Origin")
        if origin and origin != f"{request.scheme}://{request.host}":
            raise web.HTTPForbidden
        if runtime.video.state != "streaming":
            raise web.HTTPServiceUnavailable()
        if mode == "talk":
            return await talk_websocket(request, runtime.video)
        return await runtime.audio.websocket(request)


class DoorbellHangupView(HomeAssistantView):
    url = "/api/valore_ringbell/{entity_id}/hangup"
    name = "api:valore_ringbell:hangup"

    async def post(self, request, entity_id):
        runtime = get_runtime(request, entity_id)
        await runtime.video.close_ring()
        return web.Response(status=204)


def get_runtime(request, entity_id):
    """Scope controls to an authenticated administrator and the configured camera."""
    user = request[KEY_HASS_USER]
    if not user or not user.is_admin:
        raise web.HTTPForbidden
    hass = request.app[KEY_HASS]
    entity = entity_registry.async_get(hass).async_get(entity_id)
    if entity and entity.domain == "camera" and entity.platform == DOMAIN and entity.config_entry_id:
        entry = hass.config_entries.async_get_entry(entity.config_entry_id)
        if entry and (runtime := getattr(entry, "runtime_data", None)):
            return runtime
    raise web.HTTPNotFound
