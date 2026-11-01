"""Shared protocol v1 configuration objects and request helpers."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeVar

from .crsf_protocol import (
    COMMAND_STEPS,
    CommandStatus,
    DeviceInfo,
    Parameter,
    ParameterType,
)

DEFAULT_PARAMETER_TIMEOUT_S = 3.0
PARAMETER_PAGE_BYTES = 800

Item = TypeVar("Item")


def parameter_value(parameter: Parameter):
    if parameter.type == ParameterType.SELECTION:
        if parameter.value < len(parameter.options):
            return parameter.options[parameter.value]
    return parameter.value


def parameter_dict(parameter: Parameter, compact: bool = False) -> dict:
    result = {
        "id": parameter.id,
        "parent": parameter.parent,
        "type": parameter.type.name,
        "hidden": parameter.hidden,
        "name": parameter.name,
        "value": parameter_value(parameter),
        "raw_value": parameter.value,
        "minimum": parameter.minimum,
        "maximum": parameter.maximum,
        "default": parameter.default,
        "unit": parameter.unit,
    }
    if not compact:
        result["options"] = list(parameter.options)
    result.update({
        "children": list(parameter.children),
        "command_status": parameter.command_status,
        "command_timeout": parameter.command_timeout,
        "command_info": parameter.command_info,
    })
    return result


def device_dict(device: DeviceInfo) -> dict:
    software = device.software_version
    return {
        "address": device.address,
        "name": device.name,
        "serial": device.serial,
        "hardware_version": f"0x{device.hardware_version:08X}",
        "software_version": f"0x{software:08X}",
        "version": f"{software >> 16}.{software >> 8 & 0xFF}.{software & 0xFF}",
        "parameter_count": device.parameter_count,
        "parameter_version": device.parameter_version,
    }


class ControlCommand(StrEnum):
    """CONTROL request commands (PROTOCOL.md)."""
    DEVICES = "devices"
    INFO = "info"
    PARAMS = "params"
    GET = "get"
    SET = "set"
    COMMAND = "command"


class ControlErrorCode(StrEnum):
    """CONTROL_REPLY error codes (PROTOCOL.md)."""
    BAD_REQUEST = "bad_request"
    BUSY = "busy"
    FAILED = "failed"
    STALE = "stale"


PARAMETER_COMMANDS = frozenset({ControlCommand.GET, ControlCommand.SET, ControlCommand.COMMAND})


@dataclass(frozen=True)
class ControlRequest:
    """One parsed CONTROL request. JSON exists only at the network edge."""
    command: ControlCommand
    parameter: str | None = None
    value: str | None = None
    step: CommandStatus | None = None
    offset: int = 0

    @classmethod
    def from_json(cls, data: dict) -> ControlRequest:
        """Validate a decoded request object; raises ValueError naming the problem."""
        try:
            command = ControlCommand(str(data.get("command", "")).casefold())
        except ValueError as error:
            raise ValueError(f"unknown command {data.get('command')!r}; expected one of "
                             f"{[c.value for c in ControlCommand]}") from error
        parameter = None
        if command in PARAMETER_COMMANDS:
            if data.get("parameter") is None:
                raise ValueError(f"command={command.value} requires parameter")
            parameter = str(data["parameter"])
        value = None
        if command == ControlCommand.SET:
            if data.get("value") is None:
                raise ValueError("command=set requires value")
            value = str(data["value"])
        step = None
        if command == ControlCommand.COMMAND:
            name = str(data.get("step", ""))
            step = CommandStatus.__members__.get(name.upper())
            if step not in COMMAND_STEPS:
                raise ValueError(f"command step={name!r} expected one of "
                                 f"{sorted(s.name.lower() for s in COMMAND_STEPS)}")
        offset = 0
        if command == ControlCommand.PARAMS:
            try:
                offset = int(data.get("offset", 0))
            except (TypeError, ValueError) as error:
                raise ValueError(f"offset={data.get('offset')!r} must be an integer") from error
            if offset < 0:
                raise ValueError(f"offset={offset} must be >= 0")
        return cls(command, parameter, value, step, offset)

    def to_json(self) -> dict:
        data: dict = {"command": self.command.value}
        if self.parameter is not None:
            data["parameter"] = self.parameter
        if self.value is not None:
            data["value"] = self.value
        if self.step is not None:
            data["step"] = self.step.name.lower()
        if self.command == ControlCommand.PARAMS:
            data["offset"] = self.offset
        return data


@dataclass(frozen=True)
class ControlReply:
    """One CONTROL_REPLY: a JSON-ready result, or an error code and message."""
    result: dict | None = None
    error_code: ControlErrorCode | None = None
    message: str = ""

    @classmethod
    def failure(cls, code: ControlErrorCode, error: Exception | str) -> ControlReply:
        message = error if isinstance(error, str) else f"{type(error).__name__}: {error}"
        return cls(error_code=code, message=message)

    @property
    def ok(self) -> bool:
        return self.error_code is None

    def to_json(self) -> dict:
        if self.ok:
            return {"ok": True, "result": self.result}
        return {"ok": False, "error": {"code": self.error_code.value, "message": self.message}}

    @classmethod
    def from_json(cls, data: dict) -> ControlReply:
        if not isinstance(data, dict):
            raise ValueError("CONTROL_REPLY payload is not a JSON object")
        if data.get("ok") is True:
            result = data.get("result")
            return cls(result=result if isinstance(result, dict) else {})
        error = data.get("error")
        if not isinstance(error, dict):
            return cls(error_code=ControlErrorCode.FAILED, message=str(error))
        message = str(error.get("message", ""))
        try:
            code = ControlErrorCode(str(error.get("code")))
        except ValueError:
            code = ControlErrorCode.FAILED
            message = f"unknown error code {error.get('code')!r}: {message}"
        return cls(error_code=code, message=message)


def paginate(
    items: Sequence[Item],
    offset: int,
    render: Callable[[Item], dict],
    *,
    budget: int = PARAMETER_PAGE_BYTES,
) -> tuple[list[dict], bool]:
    """Render items from offset into a page that fits the byte budget.

    The first item is always included, so an oversized object cannot make the
    page empty and the client loop forever.
    """
    if offset < 0:
        raise ValueError(f"offset={offset} must be >= 0")
    total = len(items)
    if offset >= total:
        return [], False
    page: list[dict] = []
    used = 0
    for item in items[offset:]:
        rendered = render(item)
        size = len(json.dumps(rendered, separators=(",", ":")).encode("utf-8")) + 1
        if page and used + size > budget:
            break
        page.append(rendered)
        used += size
    return page, offset + len(page) < total


def parameter_page(parameters: Sequence[Parameter], offset: int = 0) -> tuple[list[dict], bool]:
    """One compact page of the parameter table."""
    return paginate(
        parameters, offset,
        lambda parameter: parameter_dict(parameter, compact=True))
