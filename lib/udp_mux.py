"""One-byte system addressing for crsfproxy UDP transports.

System ID 0 addresses every proxy. IDs 1 through 254 address one proxy.
ID 255 is reserved and invalid.
"""

SYS_ID_BROADCAST = 0
SYS_ID_MIN = 1
SYS_ID_MAX = 254
SYS_ID_RESERVED = 255
HEADER_LEN = 1


def validate_target_sys_id(sys_id: int) -> int:
    """Validate a destination system ID. Broadcast (0) is allowed."""
    value = int(sys_id)
    if not SYS_ID_BROADCAST <= value <= SYS_ID_MAX:
        raise ValueError(f"sys_id must be 0..{SYS_ID_MAX}")
    return value


def validate_local_sys_id(sys_id: int) -> int:
    """Validate a proxy's own system ID. Broadcast (0) is not a valid identity."""
    value = int(sys_id)
    if not SYS_ID_MIN <= value <= SYS_ID_MAX:
        raise ValueError(f"local sys_id must be {SYS_ID_MIN}..{SYS_ID_MAX}")
    return value


def accepts_sys_id(local_sys_id: int, target_sys_id: int) -> bool:
    """Return True when a datagram is addressed to this proxy."""
    local = validate_local_sys_id(local_sys_id)
    target = validate_target_sys_id(target_sys_id)
    return target == SYS_ID_BROADCAST or target == local


def wrap(sys_id: int, payload: bytes) -> bytes:
    """Prefix a UDP payload with its one-byte system ID."""
    return bytes((validate_target_sys_id(sys_id),)) + bytes(payload)


def unwrap(datagram: bytes) -> tuple[int, bytes]:
    """Split a routed UDP datagram into system ID and payload."""
    if not datagram:
        raise ValueError("routed UDP datagram is empty")
    sys_id = validate_target_sys_id(datagram[0])
    return sys_id, datagram[HEADER_LEN:]
