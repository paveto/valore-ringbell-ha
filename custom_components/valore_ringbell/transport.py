"""Compatibility adapter for the pinned Tuya SDK."""

import asyncio
from importlib.metadata import version

from tuya_ipc_p2p_sdk.exceptions import TuyaIpcP2pSessionError
from tuya_ipc_p2p_sdk.signaling import moto_client
from tuya_ipc_p2p_sdk.stream_session import StreamSession


class DeviceAnswerTimeout(TimeoutError):
    """The signaling broker connected and the device did not answer."""


def enable_verified_mqtt(context):
    """Use the verified SSL context for the SDK's signaling connection."""
    if version("tuya-ipc-p2p-sdk") != "0.1.3":
        raise RuntimeError("Unexpected version of the Tuya P2P SDK")
    moto_client._UNVERIFIED_TLS = context


class ValoreSession(StreamSession):
    """Distinguish a sleeping device or closed relay from task cancellation."""

    async def _async_await_answer(self):
        try:
            return await super()._async_await_answer()
        except TuyaIpcP2pSessionError as exc:
            if isinstance(exc.__cause__, TimeoutError):
                raise DeviceAnswerTimeout from exc
            raise

    async def async_start(self):
        try:
            await super().async_start()
        except asyncio.CancelledError:
            # SDK cancels its video-ready future when the relay closes during setup.
            if asyncio.current_task().cancelling() or not self._ended.done():
                raise
