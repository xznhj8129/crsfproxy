#!/usr/bin/env python3
"""
Usage:
  python joystick_crsf.py --target 192.168.4.1 --port 60000 --sys-id 1 --rate 50
  python joystick_crsf.py --target 192.168.4.1:60000 --target 192.168.4.2:60000 --sys-id 0

Sends joystick channels as addressed UDP packets:
<uint8 sys_id><uint32 t_ms><16 x uint16 us><uint32 crc32>.
"""

import argparse
import socket
import struct
import time
import zlib

import pygame

from lib import validate_target_sys_id, wrap

MIN_US = 900
MAX_US = 2100
MID_US = 1500
DEFAULT_PORT = 60000
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", action="append", required=True,
                        help="UDP target host/IP or host:port; repeat for multiple proxies")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="Default UDP target port when --target omits one")
    parser.add_argument("--sys-id", "--sys_id", dest="sys_id", type=int, default=1,
                        help="Target system ID: 0=all, 1..254=specific proxy")
    parser.add_argument("--rate", type=float, default=DEFAULT_RATE_HZ, help="Send rate in Hz")
    parser.add_argument("--joystick-index", type=int, default=0, help="Joystick index reported by pygame")
    parser.add_argument("--debugch", action="store_true", help="Print channels each send")
    args = parser.parse_args()

    period = 1.0 / args.rate
    try:
        sys_id = validate_target_sys_id(args.sys_id)
        targets = [parse_target(value, args.port) for value in args.target]
    except ValueError as error:
        parser.error(str(error))

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
    print(f"Sending UDP RC sys_id={sys_id} to {targets}.")

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
                    channels[5 + i] = MAX_US if (buttons[i] if i >= GAMEPAD_LATCHED_BUTTON_COUNT else btn_latched[i]) else MIN_US

            if args.debugch:
                now = time.time()
                if now - dbg_t >= 0.5:  # ~2 Hz
                    print(f"buttons={buttons} channels={channels}")
                    dbg_t = now
            payload = struct.pack("<I16H", int(time.time() * 1000) & 0xFFFFFFFF, *channels)
            crc = zlib.crc32(payload) & 0xFFFFFFFF
            packet = wrap(sys_id, payload + struct.pack("<I", crc))
            for target in targets:
                sock.sendto(packet, target)

            elapsed = time.time() - loop_start
            if elapsed < period:
                time.sleep(period - elapsed)
    finally:
        sock.close()
        pygame.quit()


if __name__ == "__main__":
    main()
