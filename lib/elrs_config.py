"""CRSF parameter protocol client shared by the radio backends.

ParameterClient drives DEVICE_PING / PARAMETER_READ / PARAMETER_WRITE /
COMMAND over a configuration transport. WriteResult and current_value_bytes
support the set/verify flow.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .crsf_protocol import (
    CRSF_ADDRESS_BROADCAST,
    CRSF_ADDRESS_TRANSMITTER,
    COMMAND_STEPS,
    CommandStatus,
    DeviceInfo,
    Frame,
    FrameType,
    Parameter,
    ParameterType,
    decode_parameter,
    encode_parameter_value,
    make_extended_frame,
    parse_device_info,
)


RESEND_S = 0.5
# elrs.lua fieldTimeout for a device other than the TX module: its replies
# cross the air link, and the TX queues every request it is given.
REMOTE_RESEND_S = 5.0
REMOTE_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class WriteResult:
    parameter: Parameter
    old_value: int | str | None
    verified: bool


def current_value_bytes(parameter: Parameter) -> bytes | None:
    """The parameter's current value encoded as it would be for a write."""
    if parameter.type == ParameterType.SELECTION:
        return bytes([parameter.value])
    if parameter.type.value <= ParameterType.INT64:
        size = 1 << (parameter.type.value // 2)
        return parameter.value.to_bytes(size, "big", signed=bool(parameter.type.value % 2))
    if parameter.type == ParameterType.FLOAT:
        return parameter.value.to_bytes(4, "big", signed=True)
    return None


class ParameterClient:
    """The Lua-script parameter protocol over a configuration transport."""

    def __init__(self, transport, timeout_seconds: float = 3.0) -> None:
        self.transport = transport
        self.timeout_seconds = timeout_seconds

    def _pump(self, deadline: float, want) -> Frame | None:
        while time.monotonic() < deadline:
            for frame in self.transport.poll():
                if want(frame):
                    return frame
            time.sleep(0.002)
        return None

    def discover(self, seconds: float = 2.0) -> dict[int, DeviceInfo]:
        ping = make_extended_frame(FrameType.DEVICE_PING, CRSF_ADDRESS_BROADCAST,
                                   self.transport.origin(CRSF_ADDRESS_BROADCAST))
        self.transport.queue(ping)
        devices: dict[int, DeviceInfo] = {}
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            for frame in self.transport.poll():
                if frame.type == FrameType.DEVICE_INFO:
                    device = parse_device_info(frame)
                    devices[device.address] = device
            time.sleep(0.002)
        return devices

    def _timing(self, device_address: int) -> tuple[float, float]:
        """(resend interval, per-chunk timeout) for a device."""
        if device_address == CRSF_ADDRESS_TRANSMITTER:
            return RESEND_S, self.timeout_seconds
        return REMOTE_RESEND_S, REMOTE_TIMEOUT_S

    def _read_chunks(self, device_address: int, parameter_id: int,
                     query_type: int, query_value: int | None = None,
                     first_value: int | None = None) -> bytes:
        """first_value, when given, replaces the value of the very first query only
        (a COMMAND step, sent once); resends and later chunks use query_value."""
        origin = self.transport.origin(device_address)
        resend_s, timeout_s = self._timing(device_address)
        chunk = 0
        remaining = None
        assembled = bytearray()
        while True:
            deadline = time.monotonic() + timeout_s
            frame = None
            attempts = 0
            while frame is None and time.monotonic() < deadline:
                value = chunk if query_value is None else query_value
                if first_value is not None and chunk == 0 and attempts == 0:
                    value = first_value
                attempts += 1
                self.transport.queue(make_extended_frame(
                    query_type, device_address, origin, bytes([parameter_id, value])))
                frame = self._pump(
                    min(deadline, time.monotonic() + resend_s),
                    lambda f: f.type == FrameType.PARAMETER_SETTINGS_ENTRY
                    and f.origin == device_address and f.extended_payload[0] == parameter_id
                    and (remaining is None or f.extended_payload[1] == remaining - 1),
                )
            if frame is None:
                raise TimeoutError(
                    f"device=0x{device_address:02X} parameter_id={parameter_id} "
                    f"chunk={chunk} attempts={attempts} timeout_seconds={timeout_s}")
            response = frame.extended_payload
            assembled.extend(response[2:])
            remaining = response[1]
            if remaining == 0:
                return bytes(assembled)
            chunk += 1

    def read(self, device_address: int, parameter_id: int) -> Parameter:
        return decode_parameter(parameter_id,
                                self._read_chunks(device_address, parameter_id, FrameType.PARAMETER_READ))

    def read_all(self, device: DeviceInfo) -> list[Parameter]:
        return [self.read(device.address, parameter_id)
                for parameter_id in range(1, device.parameter_count + 1)]

    def find(self, device: DeviceInfo, name_or_id: str) -> Parameter:
        if name_or_id.isdecimal():
            return self.read(device.address, int(name_or_id))
        for parameter_id in range(1, device.parameter_count + 1):
            parameter = self.read(device.address, parameter_id)
            if parameter.name.casefold() == name_or_id.casefold():
                return parameter
        raise KeyError(f"parameter {name_or_id!r} not found on device 0x{device.address:02X}")

    def write(self, device_address: int, parameter: Parameter, value: str) -> WriteResult:
        encoded = encode_parameter_value(parameter, value)
        origin = self.transport.origin(device_address)
        write_frame = make_extended_frame(FrameType.PARAMETER_WRITE, device_address, origin,
                                          bytes([parameter.id]) + encoded)
        updated = parameter
        for _ in range(3):
            self.transport.queue(write_frame)
            self.transport.flush()
            self._pump(
                time.monotonic() + 0.1,
                lambda f: f.type == FrameType.PARAMETER_WRITE
                and f.origin == device_address and f.extended_payload[0] == parameter.id
                and f.extended_payload[1:] == encoded,
            )
            time.sleep(0.5)
            try:
                updated = self.read(device_address, parameter.id)
            except TimeoutError:
                continue
            if current_value_bytes(updated) == encoded:
                return WriteResult(updated, parameter.value, True)
        return WriteResult(updated, parameter.value, False)

    def command_step(self, device_address: int, parameter: Parameter, step: CommandStatus) -> Parameter:
        """Send one COMMAND step and return the device's resulting status.

        The UI runs the loop (see COMMAND_STEPS in crsf_protocol). The step is
        sent once, as the first query of the read; START, CONFIRM and CANCEL
        have side effects, so if the device does not answer within the resend
        interval the read repeats POLL instead, which is safe to repeat. Each
        step therefore draws one answer, not a stale extra one.
        """
        if parameter.type != ParameterType.COMMAND:
            raise ValueError(f"parameter={parameter.name!r} type={parameter.type.name} expected=COMMAND")
        if step not in COMMAND_STEPS:
            raise ValueError(f"command step={step!r} expected one of {sorted(s.name for s in COMMAND_STEPS)}")
        self.transport.poll()  # drop late answers to earlier steps
        return decode_parameter(
            parameter.id,
            self._read_chunks(device_address, parameter.id, FrameType.PARAMETER_WRITE,
                              query_value=CommandStatus.POLL, first_value=step))
