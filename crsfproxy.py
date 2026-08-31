#!/usr/bin/env python3
"""Unified UDP <-> CRSF proxy for ExpressLRS and mLRS radios.

The RC bridge, telemetry forwarding, UDP configuration API, and failsafe behavior
are shared. Select the radio-specific configuration backend with:

  --radio elrs    ExpressLRS CRSF device-parameter protocol (default)
  --radio mlrs    mLRS mBridge-over-CRSF protocol

Normal defaults are intended to work without extra flags:

  device      /dev/ttyUSB0
  baud        115200
  RC UDP      0.0.0.0:60000
  config UDP  0.0.0.0:60001
  sys_id      1
  radio       elrs

mLRS requires 400000 baud on the radio-side CRSF interface; when --radio mlrs
is selected and --baud is omitted, the mLRS backend supplies that value.
"""

from __future__ import annotations

import argparse
import sys

from lib import elrs_backend, mlrs_backend


RADIO_CHOICES = ("elrs", "mlrs")
DEFAULT_RADIO = "elrs"
DEFAULT_CONFIG_PORT = 60001


def _select_radio(argv: list[str]) -> tuple[str, list[str]]:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--radio", choices=RADIO_CHOICES, default=DEFAULT_RADIO)
    args, forwarded = parser.parse_known_args(argv[1:])
    return args.radio, [argv[0], *forwarded]


def _apply_common_defaults(argv: list[str]) -> list[str]:
    """Supply defaults that belong to the public crsfproxy interface."""
    if not any(
        arg == "--config_udp" or arg.startswith("--config_udp=")
        for arg in argv[1:]
    ):
        argv.extend(["--config_udp", str(DEFAULT_CONFIG_PORT)])
    return argv


def main() -> int:
    radio, forwarded = _select_radio(sys.argv)
    forwarded = _apply_common_defaults(forwarded)

    if any(arg in ("-h", "--help") for arg in forwarded[1:]):
        print(
            "Radio selection:\n"
            "  --radio elrs   ExpressLRS configuration backend (default)\n"
            "  --radio mlrs   mLRS mBridge-over-CRSF configuration backend; "
            "defaults to 400000 baud\n"
            "Common defaults:\n"
            "  RC UDP         0.0.0.0:60000\n"
            "  config UDP     0.0.0.0:60001\n"
            "  sys_id         1\n"
            "  device         /dev/ttyUSB0\n"
        )

    if radio == "mlrs":
        mlrs_backend.activate(forwarded)

    sys.argv[:] = forwarded
    elrs_backend.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
