#!/usr/bin/env python3
"""mLRS frontend for crsfproxy.

This keeps crsfproxy's UDP RC, CRSF serial, telemetry forwarding, and failsafe
logic unchanged while replacing the ExpressLRS parameter service with mLRS's
mBridge-over-CRSF configurator protocol.

Usage is the same as crsfproxy.py. mLRS only supports 400000 baud on the radio
CRSF interface, so this wrapper supplies 400000 when --baud is not specified.

Example:
  python3 mlrsproxy.py --device /dev/ttyUSB0 --config_udp 60001 \
      --telemetry_udp 127.0.0.1:40042

The existing config_client.py can then be used for info/params/get/set and
synthetic command parameters such as Save and Bind:
  python3 config_client.py --port 60001 params
  python3 config_client.py --port 60001 set "Tx Ch Source" crsf
  python3 config_client.py --port 60001 command Save --confirm

mLRS parameter writes are live changes. They are not persisted until Save is
issued, matching the official mLRS Lua configurator.
"""

from __future__ import annotations

import builtins
import sys
import time
from collections import deque
from dataclasses import dataclass
from enum import IntEnum

from . import elrs_backend as base
from .crsf_protocol import (
    CRSF_ADDRESS_RADIO_TRANSMITTER,
    CRSF_ADDRESS_RECEIVER,
    CRSF_ADDRESS_TRANSMITTER,
    Frame,
    Parameter,
    ParameterType,
    make_frame,
)


MLRS_CRSF_TO_MODULE = 0x81
MLRS_CRSF_TO_RADIO = 0x82
MBRIDGE_COMMAND_STX = 0xA0
MLRS_SAVE_DEADTIME_S = 4.0
SYNTHETIC_PARAMETER_BASE = 240


class MlrsCommand(IntEnum):
    TX_LINK_STATS = 2
    REQUEST_INFO = 3
    DEVICE_ITEM_TX = 4
    DEVICE_ITEM_RX = 5
    PARAM_REQUEST_LIST = 6  # deprecated upstream, retained for wire numbering
    PARAM_ITEM = 7
    PARAM_ITEM2 = 8
    PARAM_ITEM3_4 = 9
    REQUEST_CMD = 10
    INFO = 11
    PARAM_SET = 12
    PARAM_STORE = 13
    BIND_START = 14
    BIND_STOP = 15
    MODELID_SET = 16
    SYSTEM_BOOTLOADER = 17
    FLASH_ESPBRIDGE = 18


COMMAND_LENGTHS = {
    MlrsCommand.TX_LINK_STATS: 22,
    MlrsCommand.REQUEST_INFO: 0,
    MlrsCommand.DEVICE_ITEM_TX: 24,
    MlrsCommand.DEVICE_ITEM_RX: 24,
    MlrsCommand.PARAM_REQUEST_LIST: 0,
    MlrsCommand.PARAM_ITEM: 24,
    MlrsCommand.PARAM_ITEM2: 24,
    MlrsCommand.PARAM_ITEM3_4: 24,
    MlrsCommand.REQUEST_CMD: 18,
    MlrsCommand.INFO: 24,
    MlrsCommand.PARAM_SET: 7,
    MlrsCommand.PARAM_STORE: 0,
    MlrsCommand.BIND_START: 0,
    MlrsCommand.BIND_STOP: 0,
    MlrsCommand.MODELID_SET: 3,
    MlrsCommand.SYSTEM_BOOTLOADER: 0,
    MlrsCommand.FLASH_ESPBRIDGE: 0,
}


class MlrsParameterType(IntEnum):
    UINT8 = 0
    INT8 = 1
    UINT16 = 2
    INT16 = 3
    LIST = 4
    STR6 = 5


PARAMETER_TYPE_MAP = {
    MlrsParameterType.UINT8: ParameterType.UINT8,
    MlrsParameterType.INT8: ParameterType.INT8,
    MlrsParameterType.UINT16: ParameterType.UINT16,
    MlrsParameterType.INT16: ParameterType.INT16,
    MlrsParameterType.LIST: ParameterType.SELECTION,
    MlrsParameterType.STR6: ParameterType.STRING,
}


@dataclass(frozen=True)
class MlrsWireCommand:
    command: MlrsCommand
    payload: bytes


@dataclass(frozen=True)
class MlrsDevice:
    address: int
    kind: str
    name: str
    firmware_u16: int
    setup_layout_u16: int

    @property
    def version(self) -> str:
        major = (self.firmware_u16 & 0xF000) >> 12
        minor = (self.firmware_u16 & 0x0FC0) >> 6
        patch = self.firmware_u16 & 0x003F
        return f"v{major}.{minor}.{patch:02d}"

    @property
    def setup_layout(self) -> str:
        major = (self.setup_layout_u16 & 0xF000) >> 12
        minor = (self.setup_layout_u16 & 0x0FC0) >> 6
        patch = self.setup_layout_u16 & 0x003F
        return f"v{major}.{minor}.{patch:02d}"

    def as_dict(self, parameter_count: int) -> dict:
        return {
            "address": self.address,
            "kind": self.kind,
            "name": self.name,
            "serial": "",
            "hardware_version": f"0x{self.setup_layout_u16:04X}",
            "software_version": f"0x{self.firmware_u16:04X}",
            "version": self.version,
            "setup_layout": self.setup_layout,
            "parameter_count": parameter_count,
            "parameter_version": self.setup_layout_u16,
        }


@dataclass(frozen=True)
class MlrsInfo:
    receiver_sensitivity: int
    status_flags: int
    tx_power_dbm: int
    rx_power_dbm: int
    rx_available: bool
    tx_config_id: int
    tx_diversity: int
    rx_diversity: int
    parameter_count: int

    @property
    def has_status(self) -> bool:
        return bool(self.status_flags & 0x01)

    @property
    def binding(self) -> bool:
        return bool(self.status_flags & 0x02)

    def status_dict(self) -> dict:
        if self.binding:
            message = "Binding"
        elif self.rx_available:
            message = "Connected"
        else:
            message = "Receiver unavailable"
        return {
            "connected": bool(self.rx_available and not self.binding),
            "packets_bad": 0,
            "packets_good": 0,
            "flags": f"0x{self.status_flags:02X}",
            "message": message,
        }

    def as_dict(self) -> dict:
        return {
            "receiver_sensitivity": self.receiver_sensitivity,
            "has_status": self.has_status,
            "binding": self.binding,
            "tx_power_dbm": self.tx_power_dbm,
            "rx_power_dbm": self.rx_power_dbm,
            "rx_available": self.rx_available,
            "tx_config_id": self.tx_config_id,
            "tx_diversity": self.tx_diversity,
            "rx_diversity": self.rx_diversity,
            "parameter_count": self.parameter_count,
        }


@dataclass(frozen=True)
class MlrsParameterRecord:
    parameter: Parameter
    mlrs_type: MlrsParameterType | None
    allowed_mask: int = 0xFFFF
    raw_options: tuple[str, ...] = ()
    editable: bool = True
    command: MlrsCommand | None = None


@dataclass(frozen=True)
class MlrsWriteResult:
    parameter: Parameter
    old_value: int | str | None
    verified: bool


@dataclass(frozen=True)
class MlrsSummary:
    tx: MlrsDevice
    rx: MlrsDevice | None
    info: MlrsInfo


def _cstring(data: bytes, offset: int, length: int) -> str:
    return data[offset:offset + length].split(b"\x00", 1)[0].decode(
        "utf-8", errors="replace")


def _u16(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 2], "little", signed=False)


def _i16(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 2], "little", signed=True)


def _i8(data: bytes, offset: int) -> int:
    value = data[offset]
    return value - 256 if value >= 128 else value


def _decode_value(data: bytes, offset: int, value_type: MlrsParameterType) -> int:
    if value_type in (MlrsParameterType.UINT8, MlrsParameterType.LIST):
        return data[offset]
    if value_type == MlrsParameterType.INT8:
        return _i8(data, offset)
    if value_type == MlrsParameterType.UINT16:
        return _u16(data, offset)
    if value_type == MlrsParameterType.INT16:
        return _i16(data, offset)
    raise ValueError(f"mLRS parameter type {value_type.name} is not numeric")


def _encode_value(value: int, value_type: MlrsParameterType) -> bytes:
    if value_type in (MlrsParameterType.UINT8, MlrsParameterType.LIST):
        return int(value).to_bytes(1, "little", signed=False)
    if value_type == MlrsParameterType.INT8:
        return int(value).to_bytes(1, "little", signed=True)
    if value_type == MlrsParameterType.UINT16:
        return int(value).to_bytes(2, "little", signed=False)
    if value_type == MlrsParameterType.INT16:
        return int(value).to_bytes(2, "little", signed=True)
    raise ValueError(f"mLRS parameter type {value_type.name} is not numeric")


def _options_from_bytes(data: bytes) -> tuple[str, ...]:
    text = data.split(b"\x00", 1)[0].decode("utf-8", errors="replace")
    return tuple(text.split(",")) if text else ()


def _mask_is_editable(mask: int) -> bool:
    return mask != 0 and (mask & (mask - 1)) != 0


def _parameter_value(parameter: Parameter):
    return base.parameter_value(parameter)


class MlrsConfigTransport(base.ProxyConfigTransport):
    """Use crsfproxy's scheduled serial writes for mBridge-over-CRSF traffic."""

    def queue(self, frame: bytes) -> None:
        if frame:
            self.outbound.put(frame)

    def observe(self, frame: Frame) -> None:
        if frame.type == MLRS_CRSF_TO_RADIO:
            self.inbound.put(frame)

    def snapshot(self) -> dict:
        return {"radio_rate_hz": None, "binding": None}


class MlrsClient:
    """mBridge command client carried in mLRS CRSF frame types 0x81/0x82."""

    def __init__(self, transport: MlrsConfigTransport, timeout_seconds: float = 3.0) -> None:
        self.transport = transport
        self.timeout_seconds = timeout_seconds
        self.pending: deque[MlrsWireCommand] = deque()
        self.summary: MlrsSummary | None = None
        self.parameter_cache: list[MlrsParameterRecord] | None = None
        self.command_dead_until = 0.0

    def _wait_for_deadtime(self) -> None:
        delay = self.command_dead_until - time.monotonic()
        if delay > 0:
            time.sleep(delay)

    def _queue_command(self, command: MlrsCommand, payload: bytes = b"") -> None:
        self._wait_for_deadtime()
        command_length = COMMAND_LENGTHS[command]
        if len(payload) > command_length:
            raise ValueError(
                f"mLRS command={command.name} payload_len={len(payload)} max={command_length}")
        body = bytearray(command_length)
        body[:len(payload)] = payload
        crsf_payload = b"OW" + bytes([MBRIDGE_COMMAND_STX + int(command)]) + bytes(body)
        self.transport.queue(make_frame(
            MLRS_CRSF_TO_MODULE,
            crsf_payload,
            address=CRSF_ADDRESS_TRANSMITTER,
        ))

    @staticmethod
    def _decode_frame(frame: Frame) -> MlrsWireCommand | None:
        if frame.type != MLRS_CRSF_TO_RADIO or not frame.payload:
            return None
        marker = frame.payload[0]
        if marker < MBRIDGE_COMMAND_STX or marker > MBRIDGE_COMMAND_STX + 31:
            return None
        raw_command = marker - MBRIDGE_COMMAND_STX
        try:
            command = MlrsCommand(raw_command)
        except ValueError:
            return None
        return MlrsWireCommand(command, bytes(frame.payload[1:]))

    def _poll(self) -> None:
        for frame in self.transport.poll():
            command = self._decode_frame(frame)
            if command is not None:
                self.pending.append(command)

    def _take(self, predicate) -> MlrsWireCommand | None:
        found = None
        count = len(self.pending)
        for _ in range(count):
            item = self.pending.popleft()
            if found is None and predicate(item):
                found = item
            else:
                self.pending.append(item)
        return found

    def _discard(self, predicate) -> None:
        kept: deque[MlrsWireCommand] = deque()
        while self.pending:
            item = self.pending.popleft()
            if not predicate(item):
                kept.append(item)
        self.pending = kept

    def _wait(self, predicate, deadline: float) -> MlrsWireCommand:
        while time.monotonic() < deadline:
            item = self._take(predicate)
            if item is not None:
                return item
            self._poll()
            item = self._take(predicate)
            if item is not None:
                return item
            time.sleep(0.002)
        raise TimeoutError("mLRS configuration response timeout")

    def _parse_device(self, command: MlrsWireCommand, kind: str) -> MlrsDevice:
        if len(command.payload) < 24:
            raise ValueError(f"mLRS {command.command.name} payload is short")
        address = CRSF_ADDRESS_TRANSMITTER if kind == "tx" else CRSF_ADDRESS_RECEIVER
        return MlrsDevice(
            address=address,
            kind=kind,
            name=_cstring(command.payload, 4, 20),
            firmware_u16=_u16(command.payload, 0),
            setup_layout_u16=_u16(command.payload, 2),
        )

    @staticmethod
    def _parse_info(command: MlrsWireCommand) -> MlrsInfo:
        if len(command.payload) < 24:
            raise ValueError("mLRS INFO payload is short")
        diversity = command.payload[7]
        return MlrsInfo(
            receiver_sensitivity=_i16(command.payload, 0),
            status_flags=command.payload[2],
            tx_power_dbm=_i8(command.payload, 3),
            rx_power_dbm=_i8(command.payload, 4),
            rx_available=bool(command.payload[5] & 0x01),
            tx_config_id=command.payload[6],
            tx_diversity=diversity & 0x0F,
            rx_diversity=(diversity >> 4) & 0x0F,
            parameter_count=command.payload[8],
        )

    def request_info(self, force: bool = False) -> MlrsSummary:
        if self.summary is not None and not force:
            return self.summary

        target_commands = {
            MlrsCommand.DEVICE_ITEM_TX,
            MlrsCommand.DEVICE_ITEM_RX,
            MlrsCommand.INFO,
        }
        self._poll()
        self._discard(lambda item: item.command in target_commands)
        self._queue_command(MlrsCommand.REQUEST_INFO)

        deadline = time.monotonic() + self.timeout_seconds
        tx_command = self._wait(
            lambda item: item.command == MlrsCommand.DEVICE_ITEM_TX, deadline)
        info_command = self._wait(
            lambda item: item.command == MlrsCommand.INFO, deadline)

        self._poll()
        rx_command = self._take(lambda item: item.command == MlrsCommand.DEVICE_ITEM_RX)
        if rx_command is None:
            grace_deadline = min(deadline, time.monotonic() + 0.15)
            try:
                rx_command = self._wait(
                    lambda item: item.command == MlrsCommand.DEVICE_ITEM_RX,
                    grace_deadline,
                )
            except TimeoutError:
                rx_command = None

        tx = self._parse_device(tx_command, "tx")
        rx = self._parse_device(rx_command, "rx") if rx_command is not None else None
        info = self._parse_info(info_command)
        self.summary = MlrsSummary(tx, rx, info)
        return self.summary

    @staticmethod
    def _response_index(command: MlrsWireCommand) -> int | None:
        return command.payload[0] if command.payload else None

    def _clear_parameter_responses(self, index: int) -> None:
        param_commands = {
            MlrsCommand.PARAM_ITEM,
            MlrsCommand.PARAM_ITEM2,
            MlrsCommand.PARAM_ITEM3_4,
        }
        self._poll()
        self._discard(
            lambda item: item.command in param_commands
            and self._response_index(item) in (index, index + 128))

    def _decode_parameter(
        self,
        item1: MlrsWireCommand,
        item2: MlrsWireCommand,
        item3: MlrsWireCommand | None,
        item4: MlrsWireCommand | None,
    ) -> MlrsParameterRecord:
        if len(item1.payload) < 24 or len(item2.payload) < 24:
            raise ValueError("mLRS parameter response payload is short")

        index = item1.payload[0]
        try:
            mlrs_type = MlrsParameterType(item1.payload[1])
        except ValueError as error:
            raise ValueError(f"mLRS parameter={index} unknown type={item1.payload[1]}") from error

        name = _cstring(item1.payload, 2, 16)
        allowed_mask = 0xFFFF
        raw_options: tuple[str, ...] = ()
        editable = True
        minimum = None
        maximum = None
        default = None
        unit = ""

        if mlrs_type == MlrsParameterType.STR6:
            value: int | str = _cstring(item1.payload, 18, 6)
        else:
            value = _decode_value(item1.payload, 18, mlrs_type)

        parameter_type = PARAMETER_TYPE_MAP[mlrs_type]

        if mlrs_type.value < MlrsParameterType.LIST.value:
            minimum = _decode_value(item2.payload, 1, mlrs_type)
            maximum = _decode_value(item2.payload, 3, mlrs_type)
            default = _decode_value(item2.payload, 5, mlrs_type)
            unit = _cstring(item2.payload, 7, 6)
        elif mlrs_type == MlrsParameterType.LIST:
            allowed_mask = _u16(item2.payload, 1)
            option_bytes = bytearray(item2.payload[3:24])
            if item3 is not None:
                if len(item3.payload) < 24:
                    raise ValueError("mLRS PARAM_ITEM3 payload is short")
                option_bytes.extend(item3.payload[1:24])
            if item4 is not None:
                if len(item4.payload) < 24:
                    raise ValueError("mLRS PARAM_ITEM4 payload is short")
                option_bytes.extend(item4.payload[1:24])
            raw_options = _options_from_bytes(bytes(option_bytes))
            minimum = 0
            maximum = max(0, len(raw_options) - 1)
            default = None
            editable = _mask_is_editable(allowed_mask)
            if editable:
                options = tuple(
                    option if allowed_mask & (1 << option_index) else ""
                    for option_index, option in enumerate(raw_options)
                )
            else:
                parameter_type = ParameterType.INFO
                options = ()
                if isinstance(value, int) and 0 <= value < len(raw_options):
                    value = raw_options[value]
        else:
            options = ()

        if mlrs_type != MlrsParameterType.LIST:
            options = ()

        parameter = Parameter(
            id=index,
            parent=0,
            type=parameter_type,
            hidden=False,
            name=name,
            value=value,
            minimum=minimum,
            maximum=maximum,
            default=default,
            unit=unit,
            options=options,
        )
        return MlrsParameterRecord(
            parameter=parameter,
            mlrs_type=mlrs_type,
            allowed_mask=allowed_mask,
            raw_options=raw_options,
            editable=editable,
        )

    def read_parameter(self, index: int) -> MlrsParameterRecord | None:
        if not 0 <= index < 128:
            raise ValueError(f"mLRS parameter index out of range: {index}")

        self._clear_parameter_responses(index)
        request_payload = bytes([MlrsCommand.PARAM_ITEM, index])
        self._queue_command(MlrsCommand.REQUEST_CMD, request_payload)
        deadline = time.monotonic() + self.timeout_seconds

        item1 = self._wait(
            lambda item: item.command == MlrsCommand.PARAM_ITEM
            and self._response_index(item) in (index, 255),
            deadline,
        )
        if item1.payload[0] == 255:
            return None

        item2 = self._wait(
            lambda item: item.command == MlrsCommand.PARAM_ITEM2
            and self._response_index(item) == index,
            deadline,
        )

        item3 = None
        item4 = None
        try:
            mlrs_type = MlrsParameterType(item1.payload[1])
        except (IndexError, ValueError) as error:
            raise ValueError(f"mLRS parameter={index} has invalid type") from error

        if mlrs_type == MlrsParameterType.LIST and b"\x00" not in item2.payload[3:24]:
            item3 = self._wait(
                lambda item: item.command == MlrsCommand.PARAM_ITEM3_4
                and self._response_index(item) == index,
                deadline,
            )
            if b"\x00" not in item3.payload[1:24]:
                item4 = self._wait(
                    lambda item: item.command == MlrsCommand.PARAM_ITEM3_4
                    and self._response_index(item) == index + 128,
                    deadline,
                )

        return self._decode_parameter(item1, item2, item3, item4)

    @staticmethod
    def _synthetic_commands() -> list[MlrsParameterRecord]:
        definitions = (
            (SYNTHETIC_PARAMETER_BASE + 0, "Save", MlrsCommand.PARAM_STORE,
             "Persist current mLRS parameters to nonvolatile storage"),
            (SYNTHETIC_PARAMETER_BASE + 1, "Bind", MlrsCommand.BIND_START,
             "Start mLRS binding"),
            (SYNTHETIC_PARAMETER_BASE + 2, "Bind Stop", MlrsCommand.BIND_STOP,
             "Stop mLRS binding"),
            (SYNTHETIC_PARAMETER_BASE + 3, "System Bootloader", MlrsCommand.SYSTEM_BOOTLOADER,
             "Reboot the transmitter module into its system bootloader"),
            (SYNTHETIC_PARAMETER_BASE + 4, "Flash ESP", MlrsCommand.FLASH_ESPBRIDGE,
             "Enter the mLRS ESP bridge flashing function"),
        )
        result = []
        for parameter_id, name, command, info in definitions:
            result.append(MlrsParameterRecord(
                parameter=Parameter(
                    id=parameter_id,
                    parent=0,
                    type=ParameterType.COMMAND,
                    hidden=False,
                    name=name,
                    command_info=info,
                ),
                mlrs_type=None,
                editable=True,
                command=command,
            ))
        return result

    def read_all(self, force: bool = False, include_commands: bool = True) -> list[MlrsParameterRecord]:
        if self.parameter_cache is None or force:
            summary = self.request_info(force=force)
            records: list[MlrsParameterRecord] = []
            if summary.info.parameter_count:
                indexes = range(summary.info.parameter_count)
                for index in indexes:
                    record = self.read_parameter(index)
                    if record is None:
                        break
                    records.append(record)
            else:
                for index in range(128):
                    record = self.read_parameter(index)
                    if record is None:
                        break
                    records.append(record)
            self.parameter_cache = records
        records = list(self.parameter_cache)
        if include_commands:
            records.extend(self._synthetic_commands())
        return records

    def find(self, name_or_id: str) -> MlrsParameterRecord:
        folded = name_or_id.casefold()
        records = self.read_all(include_commands=True)
        if name_or_id.isdecimal():
            parameter_id = int(name_or_id)
            for record in records:
                if record.parameter.id == parameter_id:
                    return record
        for record in records:
            if record.parameter.name.casefold() == folded:
                return record
        raise KeyError(f"mLRS parameter {name_or_id!r} not found")

    def write(self, record: MlrsParameterRecord, value_text: str) -> MlrsWriteResult:
        parameter = record.parameter
        if record.command is not None or record.mlrs_type is None:
            raise ValueError(f"mLRS parameter {parameter.name!r} is not writable")
        if not record.editable:
            raise ValueError(f"mLRS parameter {parameter.name!r} is read-only")

        mlrs_type = record.mlrs_type
        payload = bytearray(COMMAND_LENGTHS[MlrsCommand.PARAM_SET])
        payload[0] = parameter.id

        if mlrs_type == MlrsParameterType.LIST:
            if value_text in record.raw_options:
                new_value = record.raw_options.index(value_text)
            else:
                new_value = int(value_text)
            if not 0 <= new_value < len(record.raw_options):
                raise ValueError(f"mLRS selection index {new_value} out of range")
            if not (record.allowed_mask & (1 << new_value)):
                raise ValueError(
                    f"mLRS selection {record.raw_options[new_value]!r} is not allowed")
            encoded = _encode_value(new_value, mlrs_type)
            expected_value: int | str = new_value
        elif mlrs_type == MlrsParameterType.STR6:
            encoded = value_text.encode("ascii")
            if len(encoded) != 6:
                raise ValueError("mLRS STR6 values must be exactly 6 ASCII characters")
            expected_value = value_text
        else:
            new_value = int(value_text)
            if parameter.minimum is not None and new_value < parameter.minimum:
                raise ValueError(f"value {new_value} is below minimum {parameter.minimum}")
            if parameter.maximum is not None and new_value > parameter.maximum:
                raise ValueError(f"value {new_value} is above maximum {parameter.maximum}")
            encoded = _encode_value(new_value, mlrs_type)
            expected_value = new_value

        payload[1:1 + len(encoded)] = encoded
        old_value = parameter.value
        self._queue_command(MlrsCommand.PARAM_SET, bytes(payload))
        self.transport.flush()
        self.parameter_cache = None
        self.summary = None

        updated = parameter
        for attempt in range(3):
            if attempt:
                time.sleep(0.1)
            try:
                refreshed = self.read_parameter(parameter.id)
            except TimeoutError:
                continue
            if refreshed is None:
                continue
            updated = refreshed.parameter
            if refreshed.mlrs_type == MlrsParameterType.LIST:
                raw_value = refreshed.parameter.value
                if refreshed.parameter.type == ParameterType.INFO:
                    raw_value = (
                        refreshed.raw_options.index(refreshed.parameter.value)
                        if refreshed.parameter.value in refreshed.raw_options else None
                    )
                verified = raw_value == expected_value
            else:
                verified = refreshed.parameter.value == expected_value
            if verified:
                return MlrsWriteResult(updated, old_value, True)
        return MlrsWriteResult(updated, old_value, False)

    def run_command(self, record: MlrsParameterRecord, confirm: bool) -> Parameter:
        if record.command is None or record.parameter.type != ParameterType.COMMAND:
            raise ValueError(f"mLRS parameter {record.parameter.name!r} is not a command")
        if not confirm:
            raise ValueError(f"command {record.parameter.name!r} requires --confirm")
        self._queue_command(record.command)
        self.transport.flush()
        self.summary = None
        self.parameter_cache = None
        if record.command == MlrsCommand.PARAM_STORE:
            self.command_dead_until = time.monotonic() + MLRS_SAVE_DEADTIME_S
        return record.parameter


class MlrsConfigService:
    """Expose mLRS configuration through crsfproxy's existing UDP JSON API."""

    def __init__(self, transport: MlrsConfigTransport) -> None:
        self.transport = transport
        self.client = MlrsClient(transport, base.CONFIG_PARAMETER_TIMEOUT_S)

    @staticmethod
    def _record_dict(record: MlrsParameterRecord) -> dict:
        result = base.parameter_dict(record.parameter)
        result["editable"] = record.editable
        if record.mlrs_type is not None:
            result["mlrs_type"] = record.mlrs_type.name
            result["allowed_mask"] = f"0x{record.allowed_mask:04X}"
        return result

    def _summary_result(self, summary: MlrsSummary) -> dict:
        result = {
            "protocol": "mlrs",
            "device": summary.tx.as_dict(summary.info.parameter_count),
            "binding": summary.info.status_dict(),
            "mlrs": summary.info.as_dict(),
        }
        if summary.rx is not None:
            result["mlrs"]["rx_device"] = summary.rx.as_dict(summary.info.parameter_count)
        return result

    def execute(self, request: dict) -> dict:
        command = request["command"].casefold()

        if command == "devices":
            summary = self.client.request_info(force=True)
            devices = [summary.tx.as_dict(summary.info.parameter_count)]
            if summary.rx is not None:
                devices.append(summary.rx.as_dict(summary.info.parameter_count))
            return {"protocol": "mlrs", "devices": devices}

        if command in ("params", "info"):
            records = self.client.read_all(force=True, include_commands=(command == "params"))
            summary = self.client.request_info()
            result = self._summary_result(summary)
            if command == "params":
                result["parameters"] = [self._record_dict(record) for record in records]
                return result

            values = {
                record.parameter.name: _parameter_value(record.parameter)
                for record in records
                if record.parameter.value is not None
            }
            result.update({
                "current_band": values.get("RF Band") or values.get("Tx RF Band"),
                "packet_rate": values.get("Mode") or values.get("Tx Mode"),
                "mode": values.get("Mode") or values.get("Tx Mode"),
                "model_id": summary.info.tx_config_id,
                "telemetry_ratio": None,
                "firmware_hash": None,
                "configuration": values,
                "lua_info": {},
            })
            return result

        record = self.client.find(str(request["parameter"]))
        if command == "get":
            return {"protocol": "mlrs", "parameter": self._record_dict(record)}
        if command == "set":
            result = self.client.write(record, str(request["value"]))
            updated_record = MlrsParameterRecord(
                parameter=result.parameter,
                mlrs_type=record.mlrs_type,
                allowed_mask=record.allowed_mask,
                raw_options=record.raw_options,
                editable=record.editable,
            )
            return {
                "protocol": "mlrs",
                "verified": result.verified,
                "old_value": result.old_value,
                "parameter": self._record_dict(updated_record),
                "persisted": False,
                "note": "Run command Save --confirm to persist mLRS parameter changes",
            }
        if command == "command":
            parameter = self.client.run_command(
                record, bool(request.get("confirm", False)))
            return {
                "protocol": "mlrs",
                "parameter": base.parameter_dict(parameter),
            }
        raise ValueError(f"unknown configuration command {request['command']!r}")


def _suppress_elrs_status_frame(frame_type, destination, origin, payload=b"") -> bytes:
    """crsfproxy periodically asks ELRS for status; mLRS neither needs nor uses it."""
    if (
        frame_type == base.FrameType.PARAMETER_WRITE
        and destination == base.ELRS_ADDRESS_TRANSMITTER
        and origin == base.CRSF_ADDRESS_ELRS_LUA
        and payload == bytes([0, 0])
    ):
        return b""
    return _ORIGINAL_MAKE_EXTENDED_FRAME(frame_type, destination, origin, payload)


def _proxy_print(*args, **kwargs) -> None:
    if args and isinstance(args[0], str):
        first = args[0].replace(
            "ELRS configuration UDP listening",
            "mLRS configuration UDP listening",
        )
        args = (first,) + args[1:]
    builtins.print(*args, **kwargs)


def _patch_crsfproxy() -> None:
    base.ProxyConfigTransport = MlrsConfigTransport
    base.ConfigService = MlrsConfigService
    base.make_extended_frame = _suppress_elrs_status_frame
    base.SERIAL_RX_HEADERS = tuple(dict.fromkeys(
        tuple(base.SERIAL_RX_HEADERS) + (CRSF_ADDRESS_RADIO_TRANSMITTER,)
    ))
    base.print = _proxy_print


def _apply_mlrs_defaults(argv: list[str]) -> None:
    has_baud = any(arg == "--baud" or arg.startswith("--baud=") for arg in argv[1:])
    if not has_baud:
        argv.extend(["--baud", "400000"])


_ORIGINAL_MAKE_EXTENDED_FRAME = base.make_extended_frame


def activate(argv: list[str]) -> None:
    _patch_crsfproxy()
    _apply_mlrs_defaults(argv)


def main() -> int:
    activate(sys.argv)
    base.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
