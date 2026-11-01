"""Shared protocol v1 configuration objects and request helpers."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
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


def command_step_from_request(request: dict) -> CommandStatus:
    """The COMMAND step named in a "command" request ("start", "poll", "confirm", "cancel")."""
    name = str(request["step"])
    step = CommandStatus.__members__.get(name.upper())
    if step not in COMMAND_STEPS:
        raise ValueError(f"command step={name!r} expected one of "
                         f"{sorted(s.name.lower() for s in COMMAND_STEPS)}")
    return step


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
