"""crsfproxy protocol v1 envelope and STATUS payload (see PROTOCOL.md).

Every user↔server datagram is one envelope: a 10-byte little-endian header
(version, kind, session, seq) followed by a kind-specific payload.
"""

from __future__ import annotations

import secrets
import struct
from dataclasses import dataclass
from enum import IntEnum

VERSION = 1
MAX_DATAGRAM = 65507

HEADER_STRUCT = struct.Struct("<BBII")
HEADER_SIZE = HEADER_STRUCT.size
STATUS_STRUCT = struct.Struct("<7IBBHI")
STATUS_SIZE = STATUS_STRUCT.size
STATUS_SIZE_V03 = struct.Struct("<7I").size

# rc_age_ms / device_rx_age_ms / link_age_ms sentinel: nothing since start.
STATUS_NO_VALUE = 0xFFFFFFFF


class Kind(IntEnum):
    CRSF = 1
    CONTROL = 2
    CONTROL_REPLY = 3
    STATUS = 4


@dataclass(frozen=True)
class Envelope:
    version: int
    kind: Kind
    session: int
    seq: int
    payload: bytes


def encode(kind: Kind | int, session: int, seq: int, payload: bytes = b"") -> bytes:
    """Build one datagram. Rejects values decode() would reject."""
    try:
        kind = Kind(kind)
    except ValueError as error:
        raise ValueError(f"envelope kind={kind!r} is not valid") from error
    if session == 0:
        raise ValueError("envelope session must be nonzero")
    if not 0 <= session <= 0xFFFFFFFF:
        raise ValueError(f"envelope session={session} out of u32 range")
    if not 0 <= seq <= 0xFFFFFFFF:
        raise ValueError(f"envelope seq={seq} out of u32 range")
    if HEADER_SIZE + len(payload) > MAX_DATAGRAM:
        raise ValueError(
            f"envelope length={HEADER_SIZE + len(payload)} exceeds {MAX_DATAGRAM}")
    return HEADER_STRUCT.pack(VERSION, int(kind), session, seq) + payload


def decode(data: bytes) -> Envelope:
    """Parse one datagram, rejecting anything PROTOCOL.md says to drop."""
    if len(data) < HEADER_SIZE:
        raise ValueError(
            f"envelope length={len(data)} is shorter than the {HEADER_SIZE}-byte header")
    version, kind_value, session, seq = HEADER_STRUCT.unpack_from(data)
    if version != VERSION:
        raise ValueError(f"envelope version={version} expected={VERSION}")
    if session == 0:
        raise ValueError("envelope session must be nonzero")
    try:
        kind = Kind(kind_value)
    except ValueError as error:
        raise ValueError(f"envelope kind={kind_value} is reserved or unknown") from error
    return Envelope(version, kind, session, seq, bytes(data[HEADER_SIZE:]))


def new_session() -> int:
    """A random nonzero u32 session."""
    return secrets.randbelow(0xFFFFFFFF) + 1


def is_newer(a: int, b: int) -> bool:
    """True when u32 sequence a is newer than b (half-range comparison)."""
    delta = (a - b) & 0xFFFFFFFF
    return 1 <= delta < 0x80000000


@dataclass(frozen=True)
class Status:
    """The 36-byte STATUS payload (28-byte v0.3 payloads also unpack)."""

    rc_age_ms: int = STATUS_NO_VALUE
    rc_session: int = 0
    rc_seq: int = 0
    rc_accepted: int = 0
    rc_stale: int = 0
    rc_foreign: int = 0
    device_rx_age_ms: int = STATUS_NO_VALUE
    link_flags: int | None = 0
    link_pkts_bad: int | None = 0
    link_pkts_good: int | None = 0
    link_age_ms: int | None = STATUS_NO_VALUE

    def pack(self) -> bytes:
        u32_fields = (
            ("rc_age_ms", self.rc_age_ms),
            ("rc_session", self.rc_session),
            ("rc_seq", self.rc_seq),
            ("rc_accepted", self.rc_accepted),
            ("rc_stale", self.rc_stale),
            ("rc_foreign", self.rc_foreign),
            ("device_rx_age_ms", self.device_rx_age_ms),
        )
        for name, value in u32_fields:
            if not 0 <= value <= 0xFFFFFFFF:
                raise ValueError(f"status.{name}={value} out of u32 range")
        link_fields = (
            ("link_flags", self.link_flags if self.link_flags is not None else 0, 0xFF),
            ("link_pkts_bad", self.link_pkts_bad if self.link_pkts_bad is not None else 0, 0xFF),
            ("link_pkts_good", self.link_pkts_good if self.link_pkts_good is not None else 0, 0xFFFF),
            ("link_age_ms", self.link_age_ms if self.link_age_ms is not None else STATUS_NO_VALUE, 0xFFFFFFFF),
        )
        for name, value, limit in link_fields:
            if not 0 <= value <= limit:
                raise ValueError(f"status.{name}={value} out of range")
        return STATUS_STRUCT.pack(
            *(value for _, value in u32_fields),
            *(value for _, value, _ in link_fields),
        )

    @classmethod
    def unpack(cls, data: bytes) -> Status:
        if len(data) == STATUS_SIZE_V03:
            values = struct.unpack("<7I", data)
            return cls(*values, link_flags=None, link_pkts_bad=None,
                       link_pkts_good=None, link_age_ms=None)
        if len(data) != STATUS_SIZE:
            raise ValueError(f"status length={len(data)} expected={STATUS_SIZE}")
        return cls(*STATUS_STRUCT.unpack(data))
