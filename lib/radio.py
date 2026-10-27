"""Radio backend contract: what ``crsfproxy.py --radio`` selects.

Shared by every radio (the proxy core in lib/elrs_backend.py): UDP RC input,
the RC deadline schedule and hold, CRSF serial I/O and frame parsing, raw
telemetry forwarding, and the UDP configuration API (request parsing, one
request at a time, JSON replies).

Per radio, and nothing else:

  name               label in log lines ("ELRS", "mLRS")
  default_baud       CRSF serial baud when --baud is not given
  frame_addresses    first bytes that may start an inbound serial frame
  transport_type     ProxyConfigTransport (sub)class: which inbound frames
                     reach the configuration client
  service_type       constructed with the transport; execute(request) runs a
                     parsed configuration request (info, devices, params,
                     get, set, and one COMMAND step per request, see
                     crsf_protocol.COMMAND_STEPS) against the module and
                     returns the JSON-ready result
  status_poll_frame  frame queued once per CONFIG_STATUS_INTERVAL_S to keep
                     the module's status fresh, or None for no poll

The proxy core never checks which radio is active; it only uses these fields.
The radios are defined next to their protocol code: ELRS_RADIO in
lib/elrs_backend.py, MLRS_RADIO in lib/mlrs_backend.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .elrs_backend import ProxyConfigTransport


class ConfigService(Protocol):
    def execute(self, request: dict) -> dict: ...


@dataclass(frozen=True)
class RadioBackend:
    name: str
    default_baud: int
    frame_addresses: frozenset[int]
    transport_type: type[ProxyConfigTransport]
    service_type: type[ConfigService]
    status_poll_frame: bytes | None
