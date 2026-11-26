"""Opt-in microphone audio for the active Tuya P2P session."""

import asyncio
import audioop
from contextlib import suppress
import logging
from math import pi, sin
from struct import pack, unpack_from
import time

from aiohttp import web, WSMsgType
from tuya_ipc_p2p_sdk.control import CONTROL_MAGIC, build_command
from tuya_ipc_p2p_sdk.crypto import encrypt_record

QUERY_AUDIO = 2
# The pinned StreamSession already sends this query during its channel-0 burst.
STARTUP_AUDIO_REQUEST = 2
SPEAKER_CONTROL = 8
SPEAKER_START = 0
SPEAKER_STOP = 1
SAMPLE_RATES = {0: 8000, 3: 16000, 8000: 8000, 16000: 16000}
PCM_IDS = {101, 0x81}
CODECS = {
    105: ("G.711 µ-law", audioop.lin2ulaw),
    0x85: ("G.711 µ-law", audioop.lin2ulaw),
    106: ("G.711 A-law", audioop.lin2alaw),
    0x86: ("G.711 A-law", audioop.lin2alaw),
}
MAX_PRESS_SECONDS = 30
TALK_RATE = 8000
VOICE_LEVEL = 360
VOICE_RELEASE = 0.5
WAITING_TONE_INTERVAL = 8
LOG = logging.getLogger(__name__)


def speaker_frame(request_id, timestamp_ms, payload):
    return pack("<IIQIIII", CONTROL_MAGIC, request_id, timestamp_ms, 0, 0, 0, len(payload)) + payload


def pcm16(pcm, _width):
    return pcm


class TalkError(Exception):
    pass


class Speaker:
    """Negotiate a device-specific speaker format, then send short voice frames."""

    def __init__(self, relay, send_key):
        self.relay = relay
        self.send_key = send_key
        self.next_id = 32
        self.pending = {}
        self.owner = None
        self.started = None
        self.encoder = None
        self.sample_rate = None
        self.next_audio_at = 0
        self.frames = 0
        self.phase = "idle"
        self.formats = []
        self.startup_formats = []
        self.error = None
        self.start_result = None

    def status(self):
        return {
            "phase": self.phase,
            "formats": self.formats or self.startup_formats,
            "format_from_startup": bool(self.startup_formats),
            "start_result": self.start_result,
            "error": self.error,
        }

    def _send_command(self, code, payload):
        request_id = self.next_id
        self.next_id += 1
        command = build_command(request_id, 0, code, payload)
        self.relay.control.send(encrypt_record(self.send_key, command))
        return request_id

    async def _request(self, code, payload):
        response = asyncio.get_running_loop().create_future()
        self.pending[self.next_id] = (code, response)
        request_id = self.next_id
        try:
            self._send_command(code, payload)
            async with asyncio.timeout(3):
                return await response
        except TimeoutError:
            if code == QUERY_AUDIO:
                raise TalkError("Zvonek neodpověděl na dotaz o formátu reproduktoru.") from None
            raise TalkError("Zvonek nepotvrdil zapnutí reproduktoru.") from None
        finally:
            self.pending.pop(request_id, None)

    @staticmethod
    def parse_formats(payload):
        if len(payload) < 24:
            return []
        channel, count = unpack_from("<II", payload)
        if channel != 0 or not 0 < count <= (len(payload) - 8) // 16:
            return []
        return [unpack_from("<IIII", payload, 8 + 16 * index) for index in range(min(count, 8))]

    def on_control(self, packet):
        if packet is None or packet.flag != 1:
            return
        if packet.type == STARTUP_AUDIO_REQUEST and packet.sub_command == QUERY_AUDIO:
            self.startup_formats = self.parse_formats(packet.payload)
        match = self.pending.get(packet.type)
        if match is not None:
            code, response = match
            if packet.sub_command == code and not response.done():
                response.set_result(packet.payload)

    async def begin(self, owner):
        if self.owner is not None:
            raise TalkError("Mikrofon už používá jiný divák.")
        self.owner = owner
        self.phase = "query_audio"
        self.formats = []
        self.error = self.start_result = None
        try:
            if self.startup_formats:
                self.formats = self.startup_formats
            else:
                response = await self._request(QUERY_AUDIO, pack("<I", 0))
                self.formats = self.parse_formats(response)
            if not self.formats:
                raise TalkError("Zvonek neoznámil formát reproduktoru.")
            for codec, rate, bits, channels in self.formats:
                sample_rate = SAMPLE_RATES.get(rate)
                if sample_rate is None or channels not in (0, 1):
                    continue
                if codec in CODECS and bits in (0, 1, 8, 16):
                    name, self.encoder = CODECS[codec]
                elif codec in PCM_IDS and bits in (1, 16):
                    name, self.encoder = "PCM16", pcm16
                else:
                    continue
                self.sample_rate = sample_rate
                break
            else:
                detail = ", ".join(f"{codec:#x}/{rate}/{bits}/{channels}" for codec, rate, bits, channels in self.formats[:3])
                raise TalkError(f"Nepodporovaný formát reproduktoru ({detail}).")

            self.started = self.next_id
            self.phase = "start_speaker"
            result = await self._request(SPEAKER_CONTROL, pack("<II", 0, SPEAKER_START))
            if len(result) >= 8:
                self.start_result = unpack_from("<II", result)
            if self.start_result not in ((0, 1), (0, 3), (0, 4)):
                raise TalkError("Zvonek odmítl zapnout reproduktor.")
            if self.owner is not owner:
                raise TalkError("Relace zvonku už skončila.")
            self.next_audio_at = time.monotonic()
            self.phase = "transmitting"
            return name, self.sample_rate
        except BaseException:
            self.end(owner)
            raise

    def send(self, owner, pcm):
        if self.owner is not owner or self.started is None or self.encoder is None:
            return
        channel = self.relay._conversation(2)
        now = time.monotonic()
        if len(channel._pending) >= 6 or self.next_audio_at > now + 0.06:
            return
        self.next_audio_at = max(now, self.next_audio_at) + 0.02
        payload = self.encoder(pcm, 2)
        frame = speaker_frame(self.started, time.time_ns() // 1_000_000, payload)
        channel.send(encrypt_record(self.send_key, frame))
        self.frames += 1

    def end(self, owner):
        if self.owner is not owner:
            return
        active = self.started is not None
        self.owner = self.started = self.encoder = self.sample_rate = None
        if self.phase == "transmitting":
            self.phase = "released"
        if active:
            with suppress(RuntimeError):
                self._send_command(SPEAKER_CONTROL, pack("<II", 0, SPEAKER_STOP))


async def play_waiting_tone(camera):
    """Play a short chime periodically while waiting for an answer."""
    owner = object()
    reported = False
    while camera.call_state == "ringing":
        session = camera.active_session
        speaker = getattr(session, "speaker", None)
        if speaker is None:
            await camera.changed.wait()
            continue
        if speaker.owner is None:
            try:
                _, rate = await speaker.begin(owner)
                packet_samples = rate // 50
                samples = packet_samples * 18
                sent_before = speaker.frames
                for offset in range(0, samples, packet_samples):
                    if camera.call_state != "ringing" or camera.active_session is not session:
                        break
                    packet = pack(f"<{packet_samples}h", *(
                        int(5200 * min(1, index / (rate / 100), (samples - index) / (rate / 100))
                            * sin(2 * pi * 520 * index / rate))
                        for index in range(offset, offset + packet_samples)
                    ))
                    speaker.send(owner, packet)
                    await asyncio.sleep(0.02)
                if not reported and speaker.frames > sent_before:
                    LOG.info("Valore waiting tone sent to the doorbell")
                    reported = True
            except (TalkError, RuntimeError) as exc:
                if not reported:
                    LOG.warning("Valore waiting tone could not start: %s", type(exc).__name__)
                    reported = True
            finally:
                speaker.end(owner)
        await asyncio.sleep(WAITING_TONE_INTERVAL)


async def gated_talk(camera, session, speaker, socket):
    """Release the device speaker between speech bursts on Apple Home."""
    await socket.send_json({"ready": True, "codec": "PCM16", "sample_rate": TALK_RATE})
    clock = asyncio.get_running_loop().time
    deadline = None
    rate = None
    rate_state = None
    while camera.active_session is session and camera.state == "streaming":
        try:
            async with asyncio.timeout_at(deadline):
                message = await socket.receive()
        except TimeoutError:
            LOG.debug("Valore speaker paused; camera audio chunks: %s", camera.audio.chunks)
            speaker.end(socket)
            deadline = rate = rate_state = None
            continue
        if message.type != WSMsgType.BINARY or len(message.data) != TALK_RATE // 25:
            break
        pcm = message.data
        if audioop.rms(pcm, 2) >= VOICE_LEVEL:
            if rate is None:
                if speaker.owner is not None:
                    continue
                _, rate = await speaker.begin(socket)
                LOG.debug("Valore speaker resumed; camera audio chunks: %s", camera.audio.chunks)
            deadline = clock() + VOICE_RELEASE
        if rate == 16000:
            pcm, rate_state = audioop.ratecv(pcm, 2, 1, TALK_RATE, rate, rate_state)
            # ratecv emits one sample fewer on the first packet of a new burst.
            pcm = pcm.ljust(rate // 25, b"\0")
        if rate is not None:
            speaker.send(socket, pcm)


async def talk_websocket(request, camera, *, max_seconds=MAX_PRESS_SECONDS, voice_gate=False):
    session = camera.active_session
    speaker = getattr(session, "speaker", None)
    if camera.state != "streaming" or not camera.viewers or speaker is None:
        raise web.HTTPServiceUnavailable(text="Nejdřív otevři živý obraz.")

    socket = web.WebSocketResponse(max_msg_size=1024)
    await socket.prepare(request)
    try:
        async with asyncio.timeout(max_seconds):
            if voice_gate:
                await gated_talk(camera, session, speaker, socket)
            else:
                codec, sample_rate = await speaker.begin(socket)
                await socket.send_json({"ready": True, "codec": codec, "sample_rate": sample_rate})
                pcm_bytes = sample_rate // 25
                async for message in socket:
                    if camera.active_session is not session or camera.state != "streaming":
                        break
                    if message.type != WSMsgType.BINARY or len(message.data) != pcm_bytes:
                        break
                    speaker.send(socket, message.data)
    except (TalkError, RuntimeError) as exc:
        if isinstance(exc, TalkError):
            message = str(exc)
        else:
            message = "Řídicí kanál zvonku ukončil přenos řeči."
        if speaker.owner is None or speaker.owner is socket:
            speaker.error = message
        LOG.info("Speaker negotiation: %s", speaker.status())
        with suppress(ConnectionError):
            await socket.send_json({"error": message})
    except (TimeoutError, ConnectionError):
        pass
    finally:
        speaker.end(socket)
        await socket.close()
    return socket
