"""Authenticated Tuya cloud access for one selected Valore device."""

import asyncio
from dataclasses import dataclass
from urllib.parse import urlsplit

from tuya_ipc_p2p_sdk.exceptions import TuyaIpcP2pError
from tuya_ipc_p2p_sdk.models import StreamConfig
from tuya_mobile import TuyaMobileApp, get_mobile_app_profile

from .mqtt import MobileClient, get_mqtt_identity
from .rings import fetch_rings
from .transport import ValoreSession


@dataclass(frozen=True)
class Device:
    """A device as presented in the setup form."""

    id: str
    name: str


class UnsupportedDevice(Exception):
    """The selected device has no usable Tuya RTC configuration."""


class VerifiedHttp:
    """Keep mobile requests on HTTPS and reject redirects."""

    def __init__(self, session):
        self.session = session

    def post(self, url, **kwargs):
        if urlsplit(url).scheme != "https":
            raise ValueError("Tuya did not supply an HTTPS endpoint")
        return self.session.post(url, allow_redirects=False, ssl=True, **kwargs)


class Cloud:
    """Own mobile credentials and the shared, renewable login."""

    def __init__(self, http, username, password, country_code):
        self.http = VerifiedHttp(http)
        self.username = username
        self.password = password
        self.country_code = country_code
        self.mobile = None
        self.login = None
        self.selected = None
        self.refresh_lock = asyncio.Lock()

    async def authenticate(self):
        profile = get_mobile_app_profile(TuyaMobileApp.TUYA_SMART)
        mobile = MobileClient(
            profile, self.http, username=self.username,
            request_timeout=12, max_login_attempts=1,
        )
        login = await mobile.login_with_password(self.password, self.country_code)
        self.mobile, self.login = mobile, login

    async def devices(self, requested_id=None):
        records = await self.mobile.get_local_keys([requested_id] if requested_id else [])
        devices = {}
        for row in records:
            device_id = row.get("device_id")
            key = row.get("local_key")
            if not isinstance(device_id, str) or not device_id or not isinstance(key, str):
                continue
            if len(key.encode()) != 16 or (requested_id and requested_id != device_id):
                continue
            if device_id in devices and devices[device_id][1] != key:
                raise ValueError("Tuya returned conflicting device keys")
            name = str(row.get("name") or "Valore")[:100]
            devices[device_id] = (Device(device_id, name), key)
        return devices

    async def rtc_config(self, mobile, device_id, key):
        result = await mobile._call(
            "m.ipc.v4.rtc.config.get", {"devId": device_id}, version="1.0"
        )
        if not result.get("success"):
            self.check_expired(result)
            raise UnsupportedDevice
        try:
            return StreamConfig.from_json(result["result"], device_id, key)
        except (KeyError, AttributeError, ValueError, TypeError, TuyaIpcP2pError):
            raise UnsupportedDevice from None

    @staticmethod
    def check_expired(response):
        marker = str(response.get("errorCode") or response.get("code") or "").upper()
        if any(code in marker for code in ("SESSION_INVALID", "SESSION_EXPIRED", "USER_SESSION")):
            raise RuntimeError("SESSION_EXPIRED")

    async def refresh_if_expired(self, mobile, error):
        marker = str(error).upper()
        if not any(code in marker for code in ("SESSION_INVALID", "SESSION_EXPIRED", "USER_SESSION")):
            return False
        async with self.refresh_lock:
            if self.mobile is mobile:
                await self.authenticate()
        return True

    async def select(self, device_id, lookup=None, *, validate=False):
        if lookup is None:
            lookup = await self.devices(device_id)
        if device_id not in lookup:
            raise UnsupportedDevice
        self.selected = lookup[device_id]
        if validate:
            device, key = self.selected
            await self.rtc_config(self.mobile, device.id, key)

    async def open_video(self, on_frame):
        device, key = self.selected
        mobile = self.mobile
        try:
            config = await self.rtc_config(mobile, device.id, key)
        except RuntimeError as exc:
            if not await self.refresh_if_expired(mobile, exc):
                raise
            config = await self.rtc_config(self.mobile, device.id, key)
        return ValoreSession(config, get_mqtt_identity(self.mobile, self.login), self.login.uid, on_frame)

    async def rings(self, earliest):
        mobile = self.mobile
        try:
            return await fetch_rings(mobile, self.selected[0].id, earliest)
        except RuntimeError as exc:
            if not await self.refresh_if_expired(mobile, exc):
                raise
            return await fetch_rings(self.mobile, self.selected[0].id, earliest)
