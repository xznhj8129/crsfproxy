from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


CRSF_SYNC = 0xC8
CRSF_ADDRESS_BROADCAST = 0x00
CRSF_ADDRESS_USB = 0x10
CRSF_ADDRESS_FLIGHT_CONTROLLER = 0xC8
CRSF_ADDRESS_RADIO_TRANSMITTER = 0xEA
CRSF_ADDRESS_RECEIVER = 0xEC
CRSF_ADDRESS_TRANSMITTER = 0xEE
CRSF_ADDRESS_ELRS_LUA = 0xEF
CRSF_MAX_FRAME_SIZE = 64
CRSF_CHANNEL_COUNT = 16
CRSF_CHANNEL_PAYLOAD_SIZE = 22
CRSF_EXTENDED_TYPE_MIN = 0x28
CRSF_KNOWN_ADDRESSES = {
    0x00, 0x10, 0x12, 0x80, 0x8A, 0xC0, 0xC2, 0xC4, 0xC8, 0xCA,
    0xCC, 0xEA, 0xEC, 0xEE, 0xEF,
}


class FrameType(IntEnum):
    GPS = 0x02
    VARIO = 0x07
    BATTERY_SENSOR = 0x08
    BARO_ALTITUDE = 0x09
    HEARTBEAT = 0x0B
    LINK_STATISTICS = 0x14
    RC_CHANNELS_PACKED = 0x16
    ATTITUDE = 0x1E
    FLIGHT_MODE = 0x21
    DEVICE_PING = 0x28
    DEVICE_INFO = 0x29
    PARAMETER_SETTINGS_ENTRY = 0x2B
    PARAMETER_READ = 0x2C
    PARAMETER_WRITE = 0x2D
    ELRS_STATUS = 0x2E
    COMMAND = 0x32
    RADIO_ID = 0x3A


class ParameterType(IntEnum):
    UINT8 = 0
    INT8 = 1
    UINT16 = 2
    INT16 = 3
    UINT32 = 4
    INT32 = 5
    UINT64 = 6
    INT64 = 7
    FLOAT = 8
    SELECTION = 9
    STRING = 10
    FOLDER = 11
    INFO = 12
    COMMAND = 13


@dataclass(frozen=True)
class Frame:
    address: int
    type: int
    payload: bytes
    raw: bytes

    @property
    def destination(self) -> int | None:
        return self.payload[0] if self.type >= CRSF_EXTENDED_TYPE_MIN else None

    @property
    def origin(self) -> int | None:
        return self.payload[1] if self.type >= CRSF_EXTENDED_TYPE_MIN else None

    @property
    def extended_payload(self) -> bytes:
        return self.payload[2:] if self.type >= CRSF_EXTENDED_TYPE_MIN else self.payload


@dataclass(frozen=True)
class DeviceInfo:
    address: int
    name: str
    serial: str
    hardware_version: int
    software_version: int
    parameter_count: int
    parameter_version: int


@dataclass(frozen=True)
class Parameter:
    id: int
    parent: int
    type: ParameterType
    hidden: bool
    name: str
    value: int | str | None = None
    minimum: int | None = None
    maximum: int | None = None
    default: int | None = None
    unit: str = ""
    options: tuple[str, ...] = ()
    precision: int | None = None
    step: int | None = None
    children: tuple[int, ...] = ()
    command_status: int | None = None
    command_timeout: int | None = None
    command_info: str = ""


def crc8(data: bytes) -> int:
    crc = 0
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = ((crc << 1) ^ 0xD5) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def make_frame(frame_type: int, payload: bytes = b"", address: int = CRSF_SYNC) -> bytes:
    body = bytes([frame_type]) + payload
    return bytes([address, len(body) + 1]) + body + bytes([crc8(body)])


def make_extended_frame(frame_type: int, destination: int, origin: int, payload: bytes = b"") -> bytes:
    return make_frame(frame_type, bytes([destination, origin]) + payload)


def validate_frame(raw: bytes) -> bool:
    return 4 <= len(raw) <= CRSF_MAX_FRAME_SIZE and raw[1] + 2 == len(raw) and crc8(raw[2:-1]) == raw[-1]


class FrameParser:
    def __init__(self) -> None:
        self.buffer = bytearray()
        self.bytes_discarded = 0
        self.crc_errors = 0

    def feed(self, data: bytes) -> list[Frame]:
        self.buffer.extend(data)
        frames: list[Frame] = []
        while len(self.buffer) >= 4:
            if self.buffer[0] not in CRSF_KNOWN_ADDRESSES:
                del self.buffer[0]
                self.bytes_discarded += 1
                continue
            total = self.buffer[1] + 2
            if total < 4 or total > CRSF_MAX_FRAME_SIZE:
                del self.buffer[0]
                self.bytes_discarded += 1
                continue
            if len(self.buffer) < total:
                break
            raw = bytes(self.buffer[:total])
            if not validate_frame(raw):
                del self.buffer[0]
                self.bytes_discarded += 1
                self.crc_errors += 1
                continue
            del self.buffer[:total]
            frames.append(Frame(raw[0], raw[2], raw[3:-1], raw))
        return frames


def us_to_crsf(value: int) -> int:
    value = max(1000, min(2000, int(value)))
    return 172 + round((value - 1000) * 1639 / 1000)


def crsf_to_us(value: int) -> int:
    value = max(172, min(1811, int(value)))
    return 1000 + round((value - 172) * 1000 / 1639)


def pack_channels_us(channels_us: list[int]) -> bytes:
    if len(channels_us) != CRSF_CHANNEL_COUNT:
        raise ValueError(f"channel_count={len(channels_us)} expected={CRSF_CHANNEL_COUNT}")
    packed = 0
    for index, value in enumerate(channels_us):
        packed |= (us_to_crsf(value) & 0x7FF) << (index * 11)
    return packed.to_bytes(CRSF_CHANNEL_PAYLOAD_SIZE, "little")


def unpack_channels_us(payload: bytes) -> list[int]:
    if len(payload) != CRSF_CHANNEL_PAYLOAD_SIZE:
        raise ValueError(f"channel_payload_size={len(payload)} expected={CRSF_CHANNEL_PAYLOAD_SIZE}")
    packed = int.from_bytes(payload, "little")
    return [crsf_to_us((packed >> (index * 11)) & 0x7FF) for index in range(CRSF_CHANNEL_COUNT)]


def make_rc_frame(channels_us: list[int]) -> bytes:
    return make_frame(FrameType.RC_CHANNELS_PACKED, pack_channels_us(channels_us))


def make_battery_frame(voltage_decivolts: int, current_deciamps: int, capacity_mah: int, remaining_percent: int) -> bytes:
    payload = (
        voltage_decivolts.to_bytes(2, "big")
        + current_deciamps.to_bytes(2, "big")
        + capacity_mah.to_bytes(3, "big")
        + bytes([remaining_percent])
    )
    return make_frame(FrameType.BATTERY_SENSOR, payload)


def parse_device_info(frame: Frame) -> DeviceInfo:
    data = frame.extended_payload
    end = data.index(0)
    name = data[:end].decode("utf-8")
    values = data[end + 1:]
    return DeviceInfo(
        address=frame.origin,
        name=name,
        serial=values[:4].decode("ascii", errors="replace"),
        hardware_version=int.from_bytes(values[4:8], "big"),
        software_version=int.from_bytes(values[8:12], "big"),
        parameter_count=values[12],
        parameter_version=values[13],
    )


def _cstring(data: bytes, offset: int) -> tuple[str, int]:
    end = data.index(0, offset)
    text = data[offset:end].replace(b"\xC0", b"+").replace(b"\xC1", b"-")
    return text.decode("utf-8", errors="replace"), end + 1


def decode_parameter(parameter_id: int, data: bytes) -> Parameter:
    parent = data[0]
    raw_type = data[1]
    parameter_type = ParameterType(raw_type & 0x7F)
    name, offset = _cstring(data, 2)
    common = {
        "id": parameter_id,
        "parent": parent,
        "type": parameter_type,
        "hidden": bool(raw_type & 0x80),
        "name": name,
    }

    if parameter_type.value <= ParameterType.INT64:
        size = 1 << (parameter_type.value // 2)
        signed = bool(parameter_type.value % 2)
        values = [int.from_bytes(data[offset + size * index:offset + size * (index + 1)], "big", signed=signed) for index in range(4)]
        unit, _ = _cstring(data, offset + size * 4)
        return Parameter(**common, value=values[0], minimum=values[1], maximum=values[2], default=values[3], unit=unit)

    if parameter_type == ParameterType.FLOAT:
        values = [int.from_bytes(data[offset + 4 * index:offset + 4 * (index + 1)], "big", signed=True) for index in range(4)]
        precision = data[offset + 16]
        step = int.from_bytes(data[offset + 17:offset + 21], "big")
        unit, _ = _cstring(data, offset + 21)
        return Parameter(**common, value=values[0], minimum=values[1], maximum=values[2], default=values[3], unit=unit, precision=precision, step=step)

    if parameter_type == ParameterType.SELECTION:
        options, offset = _cstring(data, offset)
        value, minimum, maximum, default = data[offset:offset + 4]
        unit, _ = _cstring(data, offset + 4)
        return Parameter(**common, value=value, minimum=minimum, maximum=maximum, default=default, unit=unit, options=tuple(options.split(";")))

    if parameter_type in (ParameterType.STRING, ParameterType.INFO):
        value, _ = _cstring(data, offset)
        return Parameter(**common, value=value)

    if parameter_type == ParameterType.FOLDER:
        children = tuple(value for value in data[offset:] if value != 0xFF)
        return Parameter(**common, children=children)

    if parameter_type == ParameterType.COMMAND:
        status = data[offset]
        timeout = data[offset + 1]
        info, _ = _cstring(data, offset + 2)
        return Parameter(**common, command_status=status, command_timeout=timeout, command_info=info)

    raise ValueError(f"parameter_type={parameter_type}")


def encode_parameter_value(parameter: Parameter, value: str) -> bytes:
    if parameter.type == ParameterType.SELECTION:
        if value in parameter.options:
            return bytes([parameter.options.index(value)])
        return bytes([int(value)])
    if parameter.type.value <= ParameterType.INT64:
        size = 1 << (parameter.type.value // 2)
        signed = bool(parameter.type.value % 2)
        return int(value).to_bytes(size, "big", signed=signed)
    if parameter.type == ParameterType.FLOAT:
        scale = 10 ** parameter.precision
        return round(float(value) * scale).to_bytes(4, "big", signed=True)
    raise ValueError(f"parameter={parameter.name!r} type={parameter.type.name} writable=0")


def describe_frame(frame: Frame) -> str:
    type_name = FrameType(frame.type).name if frame.type in FrameType._value2member_map_ else f"0x{frame.type:02X}"
    if frame.type == FrameType.RC_CHANNELS_PACKED:
        channels = unpack_channels_us(frame.payload)
        return f"TEL RC ch0-3={channels[:4]} ch4-7={channels[4:8]} ch8-11={channels[8:12]} ch12-15={channels[12:16]}"
    if frame.type == FrameType.LINK_STATISTICS:
        data = frame.payload
        return (
            f"TEL LINK rssi1={int.from_bytes(data[0:1], 'big', signed=True)} "
            f"rssi2={int.from_bytes(data[1:2], 'big', signed=True)} lq={data[2]} "
            f"snr={int.from_bytes(data[3:4], 'big', signed=True)} antenna={data[4]} "
            f"mode={data[5]} pwr={data[6]} down_rssi={int.from_bytes(data[7:8], 'big', signed=True)} "
            f"down_lq={data[8]} down_snr={int.from_bytes(data[9:10], 'big', signed=True)}"
        )
    if frame.type == FrameType.GPS:
        data = frame.payload
        return (
            f"TEL GPS lat={int.from_bytes(data[0:4], 'big', signed=True) / 1e7:.6f} "
            f"lon={int.from_bytes(data[4:8], 'big', signed=True) / 1e7:.6f} "
            f"alt={int.from_bytes(data[12:14], 'big') - 1000}m "
            f"gspd={int.from_bytes(data[8:10], 'big') / 36:.2f}m/s "
            f"hdg={int.from_bytes(data[10:12], 'big') / 100:.2f} sats={data[14]}"
        )
    if frame.type == FrameType.VARIO:
        return f"TEL VARIO vspd={int.from_bytes(frame.payload[0:2], 'big', signed=True) / 10:.1f}m/s"
    if frame.type == FrameType.BATTERY_SENSOR:
        data = frame.payload
        return (
            f"TEL BATT {int.from_bytes(data[0:2], 'big') / 10:.1f}V "
            f"{int.from_bytes(data[2:4], 'big') / 10:.1f}A "
            f"{int.from_bytes(data[4:7], 'big')}mAh {data[7]}%"
        )
    if frame.type == FrameType.BARO_ALTITUDE:
        return f"TEL BARO alt={int.from_bytes(frame.payload[0:4], 'big', signed=True) / 100:.2f}m"
    if frame.type == FrameType.ATTITUDE:
        data = frame.payload
        return (
            f"TEL ATTI p={int.from_bytes(data[0:2], 'big', signed=True) / 10000:.3f} "
            f"r={int.from_bytes(data[2:4], 'big', signed=True) / 10000:.3f} "
            f"y={int.from_bytes(data[4:6], 'big', signed=True) / 10000:.3f}"
        )
    if frame.type == FrameType.FLIGHT_MODE:
        raw_mode = frame.payload.decode("ascii", errors="replace")
        mode = frame.payload.split(bytes([0]), 1)[0].decode("ascii", errors="replace")
        return f"TEL MODE {mode} raw={raw_mode!r}"
    if frame.type == FrameType.DEVICE_INFO:
        device = parse_device_info(frame)
        return (
            f"TEL DEVICE_INFO destination=0x{frame.destination:02X} origin=0x{device.address:02X} "
            f"name={device.name!r} serial={device.serial!r} hardware_version=0x{device.hardware_version:08X} "
            f"software_version=0x{device.software_version:08X} parameter_count={device.parameter_count} "
            f"parameter_version={device.parameter_version}"
        )
    if frame.type == FrameType.ELRS_STATUS:
        data = frame.extended_payload
        message = data[4:].split(b"\x00", 1)[0].decode("utf-8", errors="replace")
        return (
            f"TEL ELRS_STATUS destination=0x{frame.destination:02X} origin=0x{frame.origin:02X} "
            f"packets_bad={data[0]} packets_good={int.from_bytes(data[1:3], 'big')} flags=0x{data[3]:02X} message={message!r}"
        )
    if frame.type == FrameType.RADIO_ID and len(frame.payload) >= 11 and frame.payload[2] == 0x10:
        interval = int.from_bytes(frame.payload[3:7], "big", signed=True)
        shift = int.from_bytes(frame.payload[7:11], "big", signed=True)
        return (
            f"TEL RADIO_ID destination=0x{frame.destination:02X} origin=0x{frame.origin:02X} subtype=0x10 "
            f"rate_hz={10_000_000 / interval:.1f} interval_ticks={interval} shift_ticks={shift}"
        )
    if frame.type >= CRSF_EXTENDED_TYPE_MIN:
        return f"TEL {type_name} destination=0x{frame.destination:02X} origin=0x{frame.origin:02X} payload={frame.extended_payload.hex()}"
    return f"TEL {type_name} payload={frame.payload.hex()}"
