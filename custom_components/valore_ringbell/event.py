"""A native doorbell event for automations and HomeKit Bridge."""

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.const import CONF_DEVICE_ID
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from . import device_info, ring_signal


async def async_setup_entry(hass, entry, async_add_entities):
    """Register the event entity for the selected doorbell."""
    async_add_entities([ValoreDoorbell(entry)])


class ValoreDoorbell(EventEntity):
    """Represent each physical button press as a doorbell event."""

    _attr_has_entity_name = True
    _attr_translation_key = "ring"
    _attr_device_class = EventDeviceClass.DOORBELL
    _attr_event_types = ["ring"]

    def __init__(self, entry):
        self._entry = entry
        self._attr_device_info = device_info(entry)
        self._attr_unique_id = f"{entry.data[CONF_DEVICE_ID]}_ring"

    async def async_added_to_hass(self):
        """Connect to the entry's event stream."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, ring_signal(self._entry), self._handle_ring)
        )

    @callback
    def _handle_ring(self, event):
        self._trigger_event("ring", {"source_timestamp": event["timestamp"]})
        self.async_write_ha_state()
