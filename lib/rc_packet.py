"""The 40-byte UDP RC packet (``udp_crsf`` format).

This is the only definition of the packet in the repository: joystick_crsf.py
encodes it, crsfproxy (lib/elrs_backend.py) and crsf_rx_sim.py decode it.

Layout, little-endian:
    uint32 t_ms | 16 x uint16 channels_us | uint32 crc32
The zlib CRC32 covers the first 36 bytes.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

from .crsf_protocol import CRSF_CHANNEL_COUNT


_BODY = struct.Struct(f"<I{CRSF_CHANNEL_COUNT}H")
_CRC = struct.Struct("<I")
RC_PACKET_SIZE = _BODY.size + _CRC.size


class RcPacketError(ValueError):
    """A received datagram is not a valid RC packet."""


@dataclass(frozen=True)
class RcPacket:
    timestamp_ms: int
    channels_us: tuple[int, ...]

    def encode(self) -> bytes:
        body = _BODY.pack(self.timestamp_ms, *self.channels_us)
        return body + _CRC.pack(zlib.crc32(body))

    @classmethod
    def decode(cls, data: bytes) -> RcPacket:
        if len(data) != RC_PACKET_SIZE:
            raise RcPacketError(f"packet_len={len(data)} expected={RC_PACKET_SIZE}")
        (received_crc,) = _CRC.unpack_from(data, _BODY.size)
        calculated_crc = zlib.crc32(data[:_BODY.size])
        if received_crc != calculated_crc:
            raise RcPacketError(
                f"packet_crc=0x{received_crc:08X} expected_crc=0x{calculated_crc:08X}"
            )
        timestamp_ms, *channels_us = _BODY.unpack_from(data)
        return cls(timestamp_ms, tuple(channels_us))
