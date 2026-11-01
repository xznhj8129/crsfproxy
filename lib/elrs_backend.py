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
    """Execute one-shot Lua-style operations against the ELRS transmitter."""

    def __init__(self, transport: ProxyConfigTransport) -> None:
        self.transport = transport
        self.client = ParameterClient(transport, DEFAULT_PARAMETER_TIMEOUT_S)
        self.device = None
        self._parameters: list[Parameter] | None = None
        self._parameters_at = 0.0

    def transmitter(self) -> DeviceInfo:
        if self.device is None:
            devices = self.client.discover(CONFIG_DEVICE_DISCOVERY_S)
            self.device = devices[ELRS_ADDRESS_TRANSMITTER]
        return self.device

    def _parameter_table(self, allow_stale: bool = False) -> list[Parameter]:
        """The transmitted parameter table, read once per cache lifetime.

        A paginated ``params`` sweep uses allow_stale for offset > 0 so a slow
        client cannot trigger a full module re-read between pages.
        """
        now = time.monotonic()
        if (self._parameters is None
                or (not allow_stale
                    and now - self._parameters_at >= PARAMETER_CACHE_TTL_S)):
            self._parameters = list(self.client.read_all(self.transmitter()))
            self._parameters_at = time.monotonic()
        return self._parameters

    def _invalidate_parameters(self) -> None:
        self._parameters = None

    def _find_parameter(self, name_or_id) -> Parameter:
        text = str(name_or_id)
        parameters = self._parameter_table()
        if text.isdecimal():
            parameter_id = int(text)
            for parameter in parameters:
                if parameter.id == parameter_id:
                    return parameter
        folded = text.casefold()
        for parameter in parameters:
            if parameter.name.casefold() == folded:
                return parameter
        raise KeyError(
            f"parameter {text!r} not found on device 0x{self.transmitter().address:02X}")

    def execute(self, request: ControlRequest) -> dict:
        if request.command == ControlCommand.DEVICES:
            devices = self.client.discover(CONFIG_DEVICE_DISCOVERY_S)
            return {
                "protocol": "elrs",
                "devices": [device_dict(devices[address]) for address in sorted(devices)],
            }

        transmitter = self.transmitter()
        if request.command == ControlCommand.PARAMS:
            parameters = self._parameter_table(allow_stale=request.offset > 0)
            page, more = parameter_page(parameters, request.offset)
            result = {
                "protocol": "elrs",
                "device": device_dict(transmitter),
                "parameters": page,
                "offset": request.offset,
                "total": len(parameters),
                "more": more,
            }
            result.update(self.transport.snapshot())
            return result
        if request.command == ControlCommand.INFO:
            parameters = self._parameter_table()
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

        parameter = self._find_parameter(request.parameter)
        if request.command == ControlCommand.GET:
            # The table only locates the parameter; its value is read live.
            live = self.client.read(transmitter.address, parameter.id)
            return {"protocol": "elrs", "parameter": parameter_dict(live)}
        if request.command == ControlCommand.SET:
            try:
                result = self.client.write(transmitter.address, parameter, request.value)
            finally:
                self._invalidate_parameters()
            return {
                "protocol": "elrs",
                "verified": result.verified,
                "old_value": result.old_value,
                "parameter": parameter_dict(result.parameter),
            }
        try:
            parameter = self.client.command_step(transmitter.address, parameter, request.step)
        finally:
            self._invalidate_parameters()
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
