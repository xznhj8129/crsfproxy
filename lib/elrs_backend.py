#!/usr/bin/env python3
"""ExpressLRS radio backend for crsfproxy.

This module supplies the ExpressLRS parameter client and configuration service
over the CRSF DEVICE/PARAMETER protocol used by elrs.lua. The shared server loop
in lib/server.py owns the serial device, RC, envelopes, scheduling, and
telemetry; this backend only defines the radio-specific pieces.
"""

import time

from . import transport
from .config import (
    DEFAULT_PARAMETER_TIMEOUT_S,
    ControlCommand,
    ControlRequest,
    device_dict,
    parameter_dict,
    parameter_page,
    parameter_value,
)
from .crsf_protocol import (
    CRSF_ADDRESS_ELRS_LUA,
    CRSF_ADDRESS_TRANSMITTER as ELRS_ADDRESS_TRANSMITTER,
    CRSF_SYNC,
    DeviceInfo,
    FrameType,
    Parameter,
    make_extended_frame,
)
from .elrs_config import ParameterClient
from .radio import RadioBackend
from .transport import ProxyConfigTransport

DEFAULT_BAUD = 115200
CONFIG_DEVICE_DISCOVERY_S = 2.0
PARAMETER_CACHE_TTL_S = 30.0


class ElrsConfigService:
    """Execute one-shot Lua-style operations against an ELRS device (the TX by default)."""

    def __init__(self, transport: ProxyConfigTransport) -> None:
        self.transport = transport
        self.client = ParameterClient(transport, DEFAULT_PARAMETER_TIMEOUT_S)
        self.devices: dict[int, DeviceInfo] = {}
        # address -> (parameter table, read time)
        self._tables: dict[int, tuple[list[Parameter], float]] = {}

    def _discover(self) -> dict[int, DeviceInfo]:
        self.devices = self.client.discover(CONFIG_DEVICE_DISCOVERY_S)
        return self.devices

    def device(self, address: int | None = None) -> DeviceInfo:
        """The device at address (None: the transmitter), pinging once if it is not known yet."""
        address = ELRS_ADDRESS_TRANSMITTER if address is None else address
        if address not in self.devices:
            self._discover()
        if address not in self.devices:
            raise KeyError(f"no device 0x{address:02X} answered the ping (devices={len(self.devices)})")
        return self.devices[address]

    def _parameter_table(self, device: DeviceInfo, allow_stale: bool = False) -> list[Parameter]:
        """The device's parameter table, read once per cache lifetime.

        A paginated ``params`` sweep uses allow_stale for offset > 0 so a slow
        client cannot trigger a full device re-read between pages.
        """
        cached = self._tables.get(device.address)
        if (cached is None
                or (not allow_stale
                    and time.monotonic() - cached[1] >= PARAMETER_CACHE_TTL_S)):
            cached = (list(self.client.read_all(device)), time.monotonic())
            self._tables[device.address] = cached
        return cached[0]

    def _invalidate_parameters(self, device: DeviceInfo) -> None:
        self._tables.pop(device.address, None)

    def _find_parameter(self, device: DeviceInfo, name_or_id) -> Parameter:
        text = str(name_or_id)
        parameters = self._parameter_table(device)
        if text.isdecimal():
            parameter_id = int(text)
            for parameter in parameters:
                if parameter.id == parameter_id:
                    return parameter
        folded = text.casefold()
        for parameter in parameters:
            if parameter.name.casefold() == folded:
                return parameter
        raise KeyError(f"parameter {text!r} not found on device 0x{device.address:02X}")

    def _info(self) -> dict:
        """The transmitter summary; info never addresses another device."""
        transmitter = self.device()
        parameters = self._parameter_table(transmitter)
        values = {
            parameter.name: parameter_value(parameter)
            for parameter in parameters
            if parameter.value is not None
        }
        firmware_hash = None
        for name, value in values.items():
            if "hash" in name.casefold() or "git" in name.casefold():
                firmware_hash = value
                break
        result = {
            "protocol": "elrs",
            "device": device_dict(transmitter),
            "current_band": values.get("RF Band"),
            "packet_rate": values.get("Packet Rate"),
            "mode": values.get("Link Mode"),
            "model_id": values.get("Model Id"),
            "telemetry_ratio": values.get("Telem Ratio"),
            "firmware_hash": firmware_hash,
        }
        result.update(self.transport.snapshot())
        return result

    def execute(self, request: ControlRequest) -> dict:
        if request.command == ControlCommand.DEVICES:
            devices = self._discover()
            return {
                "protocol": "elrs",
                "devices": [device_dict(devices[address]) for address in sorted(devices)],
            }

        if request.command == ControlCommand.INFO:
            return self._info()

        device = self.device(request.address)
        if request.command == ControlCommand.PARAMS:
            parameters = self._parameter_table(device, allow_stale=request.offset > 0)
            page, more = parameter_page(parameters, request.offset)
            result = {
                "protocol": "elrs",
                "device": device_dict(device),
                "parameters": page,
                "offset": request.offset,
                "total": len(parameters),
                "more": more,
            }
            result.update(self.transport.snapshot())
            return result
        parameter = self._find_parameter(device, request.parameter)
        if request.command == ControlCommand.GET:
            # The table only locates the parameter; its value is read live.
            live = self.client.read(device.address, parameter.id)
            return {"protocol": "elrs", "parameter": parameter_dict(live)}
        if request.command == ControlCommand.SET:
            try:
                result = self.client.write(device.address, parameter, request.value)
            finally:
                self._invalidate_parameters(device)
            return {
                "protocol": "elrs",
                "verified": result.verified,
                "old_value": result.old_value,
                "parameter": parameter_dict(result.parameter),
            }
        try:
            parameter = self.client.command_step(device.address, parameter, request.step)
        finally:
            self._invalidate_parameters(device)
        return {"protocol": "elrs", "parameter": parameter_dict(parameter)}


ELRS_RADIO = RadioBackend(
    name="ELRS",
    default_baud=DEFAULT_BAUD,
    frame_addresses=frozenset((CRSF_SYNC, ELRS_ADDRESS_TRANSMITTER)),
    transport_type=transport.ProxyConfigTransport,
    service_type=ElrsConfigService,
    # Empty PARAMETER_WRITE from the Lua address: asks the TX for ELRS_STATUS.
    status_poll_frame=make_extended_frame(
        FrameType.PARAMETER_WRITE, ELRS_ADDRESS_TRANSMITTER, CRSF_ADDRESS_ELRS_LUA, bytes([0, 0])),
)
