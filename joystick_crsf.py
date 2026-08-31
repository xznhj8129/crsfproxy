#!/usr/bin/env python3
"""
Usage:
  python joystick_crsf.py --target 192.168.4.1 --port 60000 --sys-id 1 --rate 50
  python joystick_crsf.py --target 127.0.0.1 --port 60000 --sys-id 1 \
                          --target 127.0.0.1 --port 60010 --sys-id 2

Each --target starts one destination. The following --port and --sys-id apply to
that target until the next --target. Defaults per target are port 60000 and
sys_id 1.

Sends joystick channels as addressed UDP packets:
<uint8 sys_id><uint32 t_ms><16 x uint16 us><uint32 crc32>.
"""

import argparse
import socket
import struct
import sys
import time
import zlib

import pygame

from lib import validate_target_sys_id, wrap

MIN_US = 900
MAX_US = 2100
MID_US = 1500
DEFAULT_PORT = 60000
DEFAULT_SYS_ID = 1
DEFAULT_RATE_HZ = 50.0
AXIS_COUNT = 4
BUTTON_COUNT = 12
GAMEPAD_AUX_BUTTON_COUNT = 8
GAMEPAD_LATCHED_BUTTON_COUNT = 4


def axis_to_us(value: float) -> int:
    clamped = max(-1.0, min(1.0, float(value)))
    span = MAX_US - MIN_US
    return int(MIN_US + ((clamped + 1.0) * 0.5 * span))


def button_to_us(pressed: int) -> int:
    return MAX_US if pressed else MIN_US


def get_joystick_state(joystick, min_axes):
    pygame.event.pump()
    axes = [round(joystick.get_axis(i), 3) for i in range(joystick.get_numaxes())]
    buttons = [joystick.get_button(i) for i in range(joystick.get_numbuttons())]
    while len(axes) < min_axes:
        axes.append(0.0)
    while len(buttons) < BUTTON_COUNT:
        buttons.append(0)
    return axes[:min_axes], buttons[:BUTTON_COUNT]


def parse_target(value: str, default_port: int) -> tuple[str, int]:
    if not 1 <= default_port <= 65535:
        raise ValueError("default UDP port must be 1..65535")
    if value.count(":") > 1:
        raise ValueError("IPv6 targets are not supported by joystick_crsf")
    if ":" not in value:
        if not value:
            raise ValueError("target host cannot be empty")
        return value, default_port
    host, port_s = value.rsplit(":", 1)
    if not host or not port_s.isdigit():
        raise ValueError(f"invalid target {value!r}; expected host or host:port")
    port = int(port_s)
    if not 1 <= port <= 65535:
        raise ValueError(f"target UDP port must be 1..65535: {value!r}")
    return host, port


def _option_value(argv: list[str], index: int, name: str) -> tuple[str, int]:
    arg = argv[index]
    if arg.startswith(name + "="):
        return arg.split("=", 1)[1], index + 1
    if index + 1 >= len(argv):
        raise ValueError(f"{name} requires a value")
    return argv[index + 1], index + 2


def parse_target_specs(argv: list[str]) -> tuple[list[tuple[str, int, int]], list[str]]:
    """Extract ordered per-target options and leave global options for argparse."""
    targets: list[dict[str, object]] = []
    remaining: list[str] = []
    current = None
    index = 0

    while index < len(argv):
        arg = argv[index]

        if arg == "--target" or arg.startswith("--target="):
            value, index = _option_value(argv, index, "--target")
            host, port = parse_target(value, DEFAULT_PORT)
            current = {"host": host, "port": port, "sys_id": DEFAULT_SYS_ID}
            targets.append(current)
            continue

        if arg == "--port" or arg.startswith("--port="):
            if current is None:
                raise ValueError("--port must follow a --target")
            value, index = _option_value(argv, index, "--port")
            try:
                port = int(value)
            except ValueError as error:
                raise ValueError(f"invalid target UDP port {value!r}") from error
            if not 1 <= port <= 65535:
                raise ValueError("target UDP port must be 1..65535")
            current["port"] = port
            continue

        if arg in ("--sys-id", "--sys_id") or arg.startswith("--sys-id=") or arg.startswith("--sys_id="):
            if current is None:
                raise ValueError("--sys-id must follow a --target")
            option_name = "--sys_id" if arg.startswith("--sys_id") else "--sys-id"
            value, index = _option_value(argv, index, option_name)
            try:
                current["sys_id"] = validate_target_sys_id(int(value))
            except ValueError as error:
                raise ValueError(f"invalid target sys_id {value!r}: {error}") from error
            continue

        remaining.append(arg)
        index += 1

    if not targets:
        raise ValueError("at least one --target is required")

    return [
        (str(target["host"]), int(target["port"]), int(target["sys_id"]))
        for target in targets
    ], remaining


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", action="append", metavar="HOST[:PORT]",
                        help="Start a target; repeat for multiple proxies")
    parser.add_argument("--port", type=int, metavar="PORT",
                        help="Port for the most recent --target (default 60000)")
    parser.add_argument("--sys-id", "--sys_id", dest="sys_id", type=int, metavar="ID",
                        help="System ID for the most recent --target (default 1)")
    parser.add_argument("--rate", type=float, default=DEFAULT_RATE_HZ, help="Send rate in Hz")
    parser.add_argument("--joystick-index", type=int, default=0,
                        help="Joystick index reported by pygame")
    parser.add_argument("--debugch", action="store_true", help="Print channels each send")
    return parser


def main():
    parser = build_parser()
    try:
        targets, remaining = parse_target_specs(sys.argv[1:])
    except ValueError as error:
        parser.error(str(error))
    args = parser.parse_args(remaining)

    period = 1.0 / args.rate

    pygame.init()
    pygame.joystick.init()

    if pygame.joystick.get_count() == 0:
        raise RuntimeError("No joystick detected.")
    if args.joystick_index >= pygame.joystick.get_count():
        raise RuntimeError(f"Joystick index {args.joystick_index} not available.")

    joystick = pygame.joystick.Joystick(args.joystick_index)
    joystick.init()
    js_name = joystick.get_name()
    print(f"Joystick '{js_name}' ready on index {args.joystick_index}.")

    is_tx12 = ("tx12" in js_name.lower()) or ("radiomaster" in js_name.lower())
    required_axes = 8 if is_tx12 else AXIS_COUNT

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    print(
        "Sending UDP RC to "
        + ", ".join(f"{host}:{port} sys_id={sys_id}" for host, port, sys_id in targets)
        + "."
    )

    channels = [MID_US] * 16
    channels[2] = MIN_US
    channels[4] = MIN_US
    armed = False
    last_arm_btn = 0
    btn_latched = [False] * GAMEPAD_AUX_BUTTON_COUNT
    last_btns = [0] * GAMEPAD_AUX_BUTTON_COUNT

    dbg_t = 0.0
    try:
        while True:
            loop_start = time.time()
            axes, buttons = get_joystick_state(joystick, required_axes)

            if is_tx12:
                channels[0] = axis_to_us(axes[0])
                channels[1] = axis_to_us(axes[1])
                channels[2] = axis_to_us(axes[2])
                channels[3] = axis_to_us(axes[3])
                for i in range(4):
                    channels[4 + i] = axis_to_us(axes[4 + i])
                for i in range(4):
                    channels[8 + i] = button_to_us(buttons[i])
            else:
                channels[0] = axis_to_us(axes[2])
                channels[1] = axis_to_us(axes[3])
                channels[2] = axis_to_us(-axes[1])
                channels[3] = axis_to_us(axes[0])

                arm_btn = buttons[9]
                if arm_btn and not last_arm_btn:
                    armed = not armed
                last_arm_btn = arm_btn
                channels[4] = MAX_US if armed else MIN_US

                for i in range(GAMEPAD_AUX_BUTTON_COUNT):
                    if i < GAMEPAD_LATCHED_BUTTON_COUNT and buttons[i] and not last_btns[i]:
                        btn_latched[i] = not btn_latched[i]
                    last_btns[i] = buttons[i]
                    channels[5 + i] = MAX_US if (
                        buttons[i] if i >= GAMEPAD_LATCHED_BUTTON_COUNT else btn_latched[i]
                    ) else MIN_US

            if args.debugch:
                now = time.time()
                if now - dbg_t >= 0.5:
                    print(f"buttons={buttons} channels={channels}")
                    dbg_t = now

            payload = struct.pack("<I16H", int(time.time() * 1000) & 0xFFFFFFFF, *channels)
            crc = zlib.crc32(payload) & 0xFFFFFFFF
            routed_payload = payload + struct.pack("<I", crc)
            packets: dict[int, bytes] = {}
            for host, port, sys_id in targets:
                packet = packets.get(sys_id)
                if packet is None:
                    packet = wrap(sys_id, routed_payload)
                    packets[sys_id] = packet
                sock.sendto(packet, (host, port))

            elapsed = time.time() - loop_start
            if elapsed < period:
                time.sleep(period - elapsed)
    finally:
        sock.close()
        pygame.quit()


if __name__ == "__main__":
    main()
