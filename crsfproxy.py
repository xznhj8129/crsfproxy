#!/usr/bin/env python3
"""Unified UDP <-> CRSF proxy for ExpressLRS and mLRS radios.

Normal defaults:
  device      /dev/ttyUSB0
  baud        115200 (elrs), 400000 (mlrs)
  RC UDP      0.0.0.0:60000
  config UDP  RC port + 1 (60001 by default)
  radio       elrs

Radio selection:
  --radio elrs   ExpressLRS configuration backend (default)
  --radio mlrs   mLRS mBridge-over-CRSF configuration backend; defaults to 400000 baud
"""

from __future__ import annotations

import argparse
import socket

from lib.elrs_backend import ELRS_RADIO, ProxyOptions, run_proxy
from lib.mlrs_backend import MLRS_RADIO


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
    parser.add_argument("--host", default="0.0.0.0", help="UDP bind host for RC packets")
    parser.add_argument("--port", type=int, default=60000, help="UDP port for RC packets")
    parser.add_argument("--device", default="/dev/ttyUSB0",
                        help="Serial device or pySerial URL, e.g. socket://127.0.0.1:5762")
    parser.add_argument("--baud", type=int,
                        help="CRSF serial baudrate (default: the radio's, 115200 elrs / 400000 mlrs)")
    parser.add_argument("--tx_rate", type=float, default=100.0, help="Maximum RC frame rate Hz")
    parser.add_argument("--loop_hz", type=float, default=250.0, help="Max main loop rate Hz")
    parser.add_argument("--rc-hold-ms", type=int, default=0, metavar="MS",
                        help="Repeat the latest RC sample for up to MS after input stops; 0 disables hold (default)")
    parser.add_argument("--telemetry_udp", type=host_port,
                        help="Send raw CRSF telemetry frames to host:port (e.g. MWP)")
    parser.add_argument("--config_udp", type=int,
                        help="UDP port for TX configuration commands (default: RC port + 1)")
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
    config_udp = args.port + 1 if args.config_udp is None else args.config_udp
    if not 1 <= config_udp <= 65535:
        parser.error(f"config UDP port {config_udp} out of range; specify --config_udp explicitly")

    radio = RADIOS[args.radio]
    run_proxy(
        ProxyOptions(
            host=args.host,
            port=args.port,
            device=args.device,
            baud=radio.default_baud if args.baud is None else args.baud,
            tx_rate=args.tx_rate,
            loop_hz=args.loop_hz,
            rc_hold_ms=args.rc_hold_ms,
            telemetry_udp=args.telemetry_udp,
            config_udp=config_udp,
            debug=args.debug,
        ),
        radio,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
