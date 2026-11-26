"""Compatibility adapter for the pinned Tuya SDK."""

import asyncio
from importlib.metadata import version

from aiohttp.web import WebSocketResponse
from tuya_ipc_p2p_sdk.control import parse_control
from tuya_ipc_p2p_sdk.crypto import decrypt_record
from tuya_ipc_p2p_sdk.exceptions import TuyaIpcP2pError, TuyaIpcP2pSessionError
from tuya_ipc_p2p_sdk.signaling import moto_client
from tuya_ipc_p2p_sdk.stream_session import StreamSession

from .talkback import Speaker


class DeviceAnswerTimeout(TimeoutError):
    """The signaling broker connected and the device did not answer."""


def enable_verified_mqtt(context):
    """Use the verified SSL context for the SDK's signaling connection."""
    if version("tuya-ipc-p2p-sdk") != "0.1.3":
        raise RuntimeError("Unexpected version of the Tuya P2P SDK")
    moto_client._UNVERIFIED_TLS = context


class ValoreSession(StreamSession):
    """Distinguish a sleeping device or closed relay from task cancellation."""

    def __init__(self, config, identity, uid, on_frame, on_audio=None):
        super().__init__(config, identity, uid, on_frame)
        self.on_audio = on_audio
        self.speaker = None

    def _send_channel_zero(self, send_key, receive_key):
        relay = self._require_relay()
        self.speaker = Speaker(relay, send_key)
        if self.on_audio is not None:
            audio = relay._conversation(2)
            audio.set_message_handler(lambda record: self._on_audio_record(record, receive_key))
        super()._send_channel_zero(send_key, receive_key)

    def _on_control_record(self, record, receive_key):
        if self.speaker is not None:
            try:
                self.speaker.on_control(parse_control(decrypt_record(receive_key, record)))
            except TuyaIpcP2pError:
                pass
        super()._on_control_record(record, receive_key)

    async def async_close(self):
        owner = self.speaker.owner if self.speaker is not None else None
        if owner is not None:
            self.speaker.end(owner)
        try:
            await super().async_close()
        finally:
            if isinstance(owner, WebSocketResponse):
                await owner.close(drain=False)

    def _on_audio_record(self, record, receive_key):
        try:
            plaintext = decrypt_record(receive_key, record)
        except TuyaIpcP2pError:
            return
        self.on_audio(plaintext)

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
