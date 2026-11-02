#!/usr/bin/env python3
"""crsfproxy protocol v1 server for ExpressLRS and mLRS radios.

One UDP port carries every user↔server datagram:

- CRSF frames: RC_CHANNELS_PACKED drives RC, every other frame is either a
  device command from the RC owner or telemetry from the device;
- CONTROL JSON requests and CONTROL_REPLY answers for device configuration;
- STATUS records for link health.

Telemetry downlink goes only to the configured --telemetry (envelope) and
--telemetry_raw (bare CRSF frame) destinations. A CONTROL_REPLY goes back to
the peer that sent the request.

Normal defaults:
  device      /dev/ttyUSB0
  baud        115200 (elrs), 400000 (mlrs)
  UDP         0.0.0.0:60000
  radio       elrs

Radio selection:
  --radio elrs   ExpressLRS configuration backend (default)
  --radio mlrs   mLRS mBridge-over-CRSF configuration backend; defaults to 400000 baud
"""

from __future__ import annotations

import argparse
import socket
import sys

import serial

from lib.elrs_backend import ELRS_RADIO
from lib.mlrs_backend import MLRS_RADIO
from lib.server import ProxyOptions, run_proxy


RADIOS = {"elrs": ELRS_RADIO, "mlrs": MLRS_RADIO}
DEFAULT_RADIO = "elrs"


def host_port(value: str) -> tuple[str, int]:
    host, separator, port_text = value.rpartition(":")
    if not separator or not host or not port_text.isdigit():
        raise argparse.ArgumentTypeError(f"{value!r} expected host:port")
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"{value!r} port must be 1..65535")
    try:
        socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
    except socket.gaierror as error:
        raise argparse.ArgumentTypeError(f"{value!r} host does not resolve: {error}") from error
    return host, port


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--radio", choices=RADIOS, default=DEFAULT_RADIO,
                        help="Radio configuration backend")
    parser.add_argument("--host", default="0.0.0.0",
                        help="UDP bind address")
    parser.add_argument("--port", type=int, default=60000,
                        help="UDP port")
    parser.add_argument("--device", default="/dev/ttyUSB0",
                        help="Serial device or pySerial URL, e.g. socket://127.0.0.1:5762")
    parser.add_argument("--baud", type=int,
                        help="CRSF serial baudrate (default: the radio's, 115200 elrs / 400000 mlrs)")
    parser.add_argument("--tx_rate", type=float, default=100.0, help="Maximum RC frame rate Hz")
    parser.add_argument("--loop_hz", type=float, default=250.0, help="Max main loop rate Hz")
    parser.add_argument("--rc-hold-ms", type=int, default=0, metavar="MS",
                        help="Repeat the latest RC sample for up to MS after input stops; 0 disables hold (default)")
    parser.add_argument("--telemetry", type=host_port, action="append",
                        metavar="HOST:PORT",
                        help="Envelope downlink destination (repeatable)")
    parser.add_argument("--telemetry_raw", type=host_port, action="append",
                        metavar="HOST:PORT",
                        help="Bare CRSF downlink destination (repeatable)")
    parser.add_argument("--rc_owner_timeout_ms", type=int, default=1000,
                        metavar="MS",
                        help="RC owner release time in milliseconds (default 1000)")
    parser.add_argument("--status_hz", type=float, default=5.0,
                        help="STATUS rate in Hz (default 5)")
    parser.add_argument("--debug", action="store_true", help="Verbose RC/telemetry logging")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.rc_hold_ms < 0:
        parser.error("--rc-hold-ms must be >= 0")
    if not args.tx_rate > 0:
        parser.error("--tx_rate must be > 0")
    if not args.loop_hz > 0:
        parser.error("--loop_hz must be > 0")
    if not args.rc_owner_timeout_ms > 0:
        parser.error("--rc_owner_timeout_ms must be > 0")
    if not args.status_hz > 0:
        parser.error("--status_hz must be > 0")

    radio = RADIOS[args.radio]
    try:
        run_proxy(
            ProxyOptions(
                host=args.host,
                port=args.port,
                device=args.device,
                baud=radio.default_baud if args.baud is None else args.baud,
                tx_rate=args.tx_rate,
                loop_hz=args.loop_hz,
                rc_hold_ms=args.rc_hold_ms,
                telemetry=tuple(args.telemetry or ()),
                telemetry_raw=tuple(args.telemetry_raw or ()),
                rc_owner_timeout_ms=args.rc_owner_timeout_ms,
                status_hz=args.status_hz,
                debug=args.debug,
            ),
            radio,
        )
    except (serial.SerialException, OSError) as error:
        print(f"cannot start: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
