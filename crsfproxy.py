#!/usr/bin/env python3
"""Unified UDP <-> CRSF proxy for ExpressLRS and mLRS radios.

The RC bridge, telemetry forwarding, UDP configuration API, and failsafe behavior
are shared. Select the radio-specific configuration backend with:

  --radio elrs    ExpressLRS CRSF device-parameter protocol (default)
  --radio mlrs    mLRS mBridge-over-CRSF protocol

mLRS requires 400000 baud on the radio-side CRSF interface; when --radio mlrs
is selected and --baud is omitted, the mLRS backend supplies that value.
"""

from __future__ import annotations

import argparse
import sys

import _elrs_backend as _backend
from _elrs_backend import *  # noqa: F401,F403 - preserve the historical import API


RADIO_CHOICES = ("elrs", "mlrs")
DEFAULT_RADIO = "elrs"


def _select_radio(argv: list[str]) -> tuple[str, list[str]]:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--radio", choices=RADIO_CHOICES, default=DEFAULT_RADIO)
    args, forwarded = parser.parse_known_args(argv[1:])
    return args.radio, [argv[0], *forwarded]


def main() -> int:
    radio, forwarded = _select_radio(sys.argv)

    if any(arg in ("-h", "--help") for arg in forwarded[1:]):
        print(
            "Radio selection:\n"
            "  --radio elrs   ExpressLRS configuration backend (default)\n"
            "  --radio mlrs   mLRS mBridge-over-CRSF configuration backend; "
            "defaults to 400000 baud\n"
        )

    sys.argv[:] = forwarded

    if radio == "mlrs":
        # _mlrs_backend was originally a thin mLRS frontend around crsfproxy.
        # Point its historical `import crsfproxy as base` at the internal ELRS
        # implementation, then let it replace only the radio-specific pieces.
        sys.modules["crsfproxy"] = _backend
        import _mlrs_backend as mlrs_backend

        mlrs_backend._patch_crsfproxy()
        mlrs_backend._apply_mlrs_defaults(sys.argv)

    _backend.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
