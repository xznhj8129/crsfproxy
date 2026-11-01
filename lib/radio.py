"""Radio backend contract: what ``crsfproxy.py --radio`` selects.

Shared by every radio: the envelope and CRSF codecs, the CONTROL service path,
the RC deadline schedule and hold, CRSF serial I/O and frame parsing, and
telemetry forwarding.

Per radio, and nothing else:

  name               label in log lines ("ELRS", "mLRS")
  default_baud       CRSF serial baud when --baud is not given
  frame_addresses    first bytes that may start an inbound serial frame
  transport_type     ProxyConfigTransport (sub)class: which inbound frames
                     reach the configuration service
  service_type       constructed with the transport; execute(request) runs a
                     typed ControlRequest (info, devices, params, get, set,
                     and one COMMAND step per request, see
                     crsf_protocol.COMMAND_STEPS) against the module and
                     returns the JSON-ready result
  status_poll_frame  frame queued once per status interval to keep the
                     module's status fresh, or None for no poll

The server loop never checks which radio is active; it only uses these fields.
The radios are defined next to their protocol code: ELRS_RADIO in
lib/elrs_backend.py, MLRS_RADIO in lib/mlrs_backend.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .config import ControlRequest
    from .transport import ProxyConfigTransport


class ConfigService(Protocol):
    def execute(self, request: ControlRequest) -> dict: ...


@dataclass(frozen=True)
class RadioBackend:
    name: str
    default_baud: int
    frame_addresses: frozenset[int]
    transport_type: type[ProxyConfigTransport]
    service_type: type[ConfigService]
    status_poll_frame: bytes | None
