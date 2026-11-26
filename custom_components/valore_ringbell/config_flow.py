"""Set up Valore from a Tuya account and a named device selection."""

import asyncio
import re

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_DEVICE_ID, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectSelector, SelectSelectorConfig, SelectSelectorMode, TextSelector,
    TextSelectorConfig, TextSelectorType,
)
from tuya_mobile import (
    TuyaMobileAccountLocked, TuyaMobileCaptchaRequired,
    TuyaMobileInvalidAuth, TuyaMobileMFARequired,
)

from .cloud import Cloud, UnsupportedDevice
from .const import CONF_COUNTRY_CODE, CONF_DEVICE_NAME, CONF_WAITING_TONE, DOMAIN

NETWORK_ERRORS = (aiohttp.ClientError, TimeoutError, RuntimeError, ValueError)


class ValoreConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Set up one doorbell per config entry."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return ValoreOptionsFlow()

    def __init__(self):
        self.cloud = None
        self.devices = {}

    async def async_step_user(self, user_input=None):
        """Verify the account and fetch its list of devices."""
        errors = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            code = user_input[CONF_COUNTRY_CODE].strip().lstrip("+")
            if not username or not user_input[CONF_PASSWORD] or not re.fullmatch(r"[0-9]{1,4}", code):
                errors["base"] = "invalid_input"
            else:
                self.cloud = Cloud(
                    async_get_clientsession(self.hass), username, user_input[CONF_PASSWORD], code
                )
                try:
                    async with asyncio.timeout(45):
                        await self.cloud.authenticate()
                        self.devices = await self.cloud.devices()
                except TuyaMobileInvalidAuth:
                    errors["base"] = "invalid_auth"
                except (TuyaMobileCaptchaRequired, TuyaMobileMFARequired):
                    errors["base"] = "verification_required"
                except TuyaMobileAccountLocked:
                    errors["base"] = "account_locked"
                except NETWORK_ERRORS:
                    errors["base"] = "cannot_connect"
                else:
                    if self.devices:
                        return await self.async_step_device()
                    errors["base"] = "no_devices"
                self.cloud = None
        schema = vol.Schema({
            vol.Required(CONF_USERNAME): TextSelector(TextSelectorConfig(type=TextSelectorType.EMAIL)),
            vol.Required(CONF_PASSWORD): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
            vol.Required(CONF_COUNTRY_CODE, default="420"): TextSelector(),
        })
        return self.async_show_form(
            step_id="user", data_schema=self.add_suggested_values_to_schema(
                schema,
                {key: user_input[key] for key in (CONF_USERNAME, CONF_COUNTRY_CODE)} if user_input else {},
            ), errors=errors,
        )

    async def async_step_device(self, user_input=None):
        """Verify the selected device supports this stream."""
        if self.cloud is None:
            return await self.async_step_user()
        errors = {}
        if user_input is not None:
            device_id = user_input[CONF_DEVICE_ID]
            try:
                await self.cloud.select(device_id, self.devices, validate=True)
            except UnsupportedDevice:
                errors["base"] = "unsupported_device"
            except NETWORK_ERRORS:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(device_id)
                self._abort_if_unique_id_configured()
                device = self.devices[device_id][0]
                return self.async_create_entry(
                    title=device.name,
                    data={
                        CONF_DEVICE_ID: device.id, CONF_DEVICE_NAME: device.name,
                        CONF_USERNAME: self.cloud.username, CONF_PASSWORD: self.cloud.password,
                        CONF_COUNTRY_CODE: self.cloud.country_code,
                    },
                )
        devices = sorted((device for device, _ in self.devices.values()), key=lambda item: item.name.casefold())
        options = [{"label": f"{device.name} · {device.id[-6:]}", "value": device.id} for device in devices]
        return self.async_show_form(step_id="device", data_schema=vol.Schema({
            vol.Required(CONF_DEVICE_ID): SelectSelector(
                SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
            )
        }), errors=errors)

    async def async_step_reauth(self, entry_data):
        """Ask for the current Tuya password."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        """Verify a replacement password against the existing device."""
        entry = self._get_reauth_entry()
        errors = {}
        if user_input is not None:
            cloud = Cloud(
                async_get_clientsession(self.hass), entry.data[CONF_USERNAME],
                user_input[CONF_PASSWORD], entry.data[CONF_COUNTRY_CODE],
            )
            try:
                async with asyncio.timeout(45):
                    await cloud.authenticate()
                    await cloud.select(entry.data[CONF_DEVICE_ID])
            except TuyaMobileInvalidAuth:
                errors["base"] = "invalid_auth"
            except (TuyaMobileCaptchaRequired, TuyaMobileMFARequired):
                errors["base"] = "verification_required"
            except TuyaMobileAccountLocked:
                errors["base"] = "account_locked"
            except UnsupportedDevice:
                errors["base"] = "no_devices"
            except NETWORK_ERRORS:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: cloud.password}
                )
        return self.async_show_form(step_id="reauth_confirm", data_schema=vol.Schema({
            vol.Required(CONF_PASSWORD): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
        }), errors=errors)

class ValoreOptionsFlow(config_entries.OptionsFlowWithReload):
    """Configure the waiting tone for this doorbell."""

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(data=self.config_entry.options | user_input)
        defaults = self.hass.data.get(DOMAIN, {})
        enabled = self.config_entry.options.get(
            CONF_WAITING_TONE, defaults.get(CONF_WAITING_TONE, False),
        )
        return self.async_show_form(step_id="init", data_schema=vol.Schema({
            vol.Required(CONF_WAITING_TONE, default=enabled): bool,
        }))
