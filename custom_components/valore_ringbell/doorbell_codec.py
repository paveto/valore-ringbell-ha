"""Audio frame decoder for the Valore IP07/X6 microphone."""

from struct import pack

SAMPLE_RATE = 8000
HEADER_LENGTH = 48
FIRST_RECORD_LENGTH = 1300
TAIL_RECORD_LENGTH = 28
FRAME_PREFIX = b"\x05\x00\x01\x00\x00\x00\x00\x00"
FORMAT_ID = b"\x08\x00\x00\x00"


def alaw_sample(byte):
    code = byte ^ 0x55
    magnitude = (code & 0x0F) << 4
    segment = (code >> 4) & 7
    if segment == 0:
        magnitude += 8
    else:
        magnitude = (magnitude + 0x108) << (segment - 1)
    return magnitude if code & 0x80 else -magnitude


ALAW_TO_PCM = tuple(pack("<h", alaw_sample(byte)) for byte in range(256))


class DoorbellAudioDecoder:
    def __init__(self):
        self.partial = None

    def decode(self, record):
        """Assemble one microphone block and return mono PCM16."""
        if (
            len(record) == FIRST_RECORD_LENGTH
            and record.startswith(FRAME_PREFIX)
            and record[16:20] == FORMAT_ID
        ):
            self.partial = record[HEADER_LENGTH:]
            return None
        partial, self.partial = self.partial, None
        if len(record) != TAIL_RECORD_LENGTH or partial is None:
            return None
        payload = partial + record
        return SAMPLE_RATE, b"".join(ALAW_TO_PCM[byte] for byte in payload)
