#!/usr/bin/env python3
"""
Usage:
  python crsfproxy.py --device /dev/ttyUSB0 --baud 921600 --host 0.0.0.0 --port 60000 --loop_hz 250 --tx_rate 100 --telemetry_udp 192.168.4.2:40042 --config_udp 60001

CRSF TX side bridge:
- Listens on a UDP port for 40-byte RC packets: <uint32 t_ms><16 x uint16 us><uint32 crc32>.
- Converts to CRSF RC_CHANNELS_PACKED and writes to the serial CRSF port at tx_rate.
- Parses inbound CRSF telemetry and can forward raw frames to a UDP target (e.g. MWP).

Telemetry output:
- Optional raw CRSF frames to --telemetry_udp (host:port)

ELRS configuration:
- Optional JSON or shell-style Lua configuration commands on --config_udp.
- Configuration is TX-only and uses the same DEVICE/PARAMETER protocol as elrs.lua.

Failsafe (proxy):
- If RC updates stop for < failsafe_time_ms, repeat last_valid_channels_us.
- If RC updates stop for >= failsafe_time_ms, send --failsafe_channels_us (defaults to throttle/arm low).
- Radio link failsafe (TX<->RX loss) is handled by the receiver, not this proxy.
- Failsafe channel values are configurable via --failsafe_channels_us.
"""

import argparse
import json
import queue
import shlex
import socket
import struct
import threading
import time
import zlib
from enum import IntEnum

import serial

from crsf_protocol import (
    CRSF_ADDRESS_ELRS_LUA,
    CRSF_ADDRESS_RADIO_TRANSMITTER,
    CRSF_ADDRESS_TRANSMITTER as ELRS_ADDRESS_TRANSMITTER,
    DeviceInfo,
    Frame as CrsfFrame,
    FrameType,
    Parameter,
    ParameterType,
    make_extended_frame,
)
from elrs_config import ParameterClient

CRSF_SYNC = 0xC8
CRSF_TRANSMITTER = 0xEE
CHANNEL_COUNT = 16
MIN_US = 900
MAX_US = 2100
ARM_LOW_US = 900
MID_US = 1500
UDP_PAYLOAD_LEN = 4 + CHANNEL_COUNT * 2
UDP_PACKET_LEN = UDP_PAYLOAD_LEN + 4
WRITE_TIMEOUT_S = 0.1
DEFAULT_BAUD = 115200
SERIAL_RX_HEADERS = (CRSF_SYNC, CRSF_TRANSMITTER)
FAILSAFE_DEFAULT_US = [
    1500, 1500, 900, 1500, 900, 1500, 1500, 1500,
    1500, 1500, 1500, 1500, 1500, 1500, 1500, 1500,
]
CONFIG_RESPONSE_LIMIT = 65507
CONFIG_STATUS_INTERVAL_S = 1.0
CONFIG_PARAMETER_TIMEOUT_S = 3.0
CONFIG_DEVICE_DISCOVERY_S = 2.0


def parameter_value(parameter: Parameter):
    if parameter.type == ParameterType.SELECTION:
        if parameter.value < len(parameter.options):
            return parameter.options[parameter.value]
    return parameter.value


def parameter_dict(parameter: Parameter) -> dict:
    return {
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
        "options": list(parameter.options),
        "children": list(parameter.children),
        "command_status": parameter.command_status,
        "command_timeout": parameter.command_timeout,
        "command_info": parameter.command_info,
    }


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


class ProxyConfigTransport:
    """Thread-safe ELRS Lua transport driven by crsfproxy's serial loop."""

    def __init__(self) -> None:
        self.inbound: queue.Queue[CrsfFrame] = queue.Queue()
        self.outbound: queue.Queue[bytes] = queue.Queue()
        self.state_lock = threading.Lock()
        self.radio_rate_hz = None
        self.status = None

    def origin(self, device_address: int) -> int:
        if device_address == ELRS_ADDRESS_TRANSMITTER:
            return CRSF_ADDRESS_ELRS_LUA
        return CRSF_ADDRESS_RADIO_TRANSMITTER

    def queue(self, frame: bytes) -> None:
        self.outbound.put(frame)

    def flush(self) -> None:
        while not self.outbound.empty():
            time.sleep(0.001)

    def poll(self) -> list[CrsfFrame]:
        frames = []
        while True:
            try:
                frames.append(self.inbound.get_nowait())
            except queue.Empty:
                return frames

    def drain_outbound(self) -> list[bytes]:
        frames = []
        while True:
            try:
                frames.append(self.outbound.get_nowait())
            except queue.Empty:
                return frames

    def observe(self, frame: CrsfFrame) -> None:
        if frame.type == FrameType.RADIO_ID and len(frame.payload) >= 11 \
                and frame.payload[2] == 0x10:
            interval = int.from_bytes(frame.payload[3:7], "big", signed=True)
            if interval > 0:
                with self.state_lock:
                    self.radio_rate_hz = 10_000_000 / interval
        if frame.type == FrameType.ELRS_STATUS and len(frame.extended_payload) >= 4:
            data = frame.extended_payload
            message = data[4:].split(bytes([0]), 1)[0].decode("utf-8", errors="replace")
            with self.state_lock:
                self.status = {
                    "connected": bool(data[3] & 1),
                    "packets_bad": data[0],
                    "packets_good": int.from_bytes(data[1:3], "big"),
                    "flags": f"0x{data[3]:02X}",
                    "message": message,
                }
        if frame.origin == ELRS_ADDRESS_TRANSMITTER and frame.type in (
                FrameType.DEVICE_INFO,
                FrameType.PARAMETER_SETTINGS_ENTRY,
                FrameType.PARAMETER_WRITE,
                FrameType.ELRS_STATUS):
            self.inbound.put(frame)

    def snapshot(self) -> dict:
        with self.state_lock:
            return {
                "radio_rate_hz": self.radio_rate_hz,
                "binding": self.status,
            }


class ConfigService:
    """Execute one-shot Lua-style operations against the ELRS transmitter."""

    def __init__(self, transport: ProxyConfigTransport) -> None:
        self.transport = transport
        self.client = ParameterClient(transport, CONFIG_PARAMETER_TIMEOUT_S)
        self.device = None

    def transmitter(self) -> DeviceInfo:
        if self.device is None:
            devices = self.client.discover(CONFIG_DEVICE_DISCOVERY_S)
            self.device = devices[ELRS_ADDRESS_TRANSMITTER]
        return self.device

    def execute(self, request: dict) -> dict:
        command = request["command"].casefold()
        if command == "devices":
            devices = self.client.discover(CONFIG_DEVICE_DISCOVERY_S)
            return {"devices": [device_dict(devices[address]) for address in sorted(devices)]}

        transmitter = self.transmitter()
        if command == "params":
            result = {
                "device": device_dict(transmitter),
                "parameters": [parameter_dict(parameter)
                               for parameter in self.client.read_all(transmitter)],
            }
            result.update(self.transport.snapshot())
            return result
        if command == "info":
            parameters = self.client.read_all(transmitter)
            values = {
                parameter.name: parameter_value(parameter)
                for parameter in parameters
                if parameter.value is not None
            }
            firmware_hash = None
            for name, value in values.items():
                if "hash" in name.casefold() or "git" in name.casefold():
                    firmware_hash = value
                    break
            result = {
                "device": device_dict(transmitter),
                "current_band": values.get("RF Band"),
                "packet_rate": values.get("Packet Rate"),
                "mode": values.get("Link Mode"),
                "model_id": values.get("Model Id"),
                "telemetry_ratio": values.get("Telem Ratio"),
                "firmware_hash": firmware_hash,
                "configuration": values,
                "lua_info": {
                    parameter.name: parameter_value(parameter)
                    for parameter in parameters
                    if parameter.type in (ParameterType.INFO, ParameterType.STRING)
                },
            }
            result.update(self.transport.snapshot())
            return result

        parameter = self.client.find(transmitter, str(request["parameter"]))
        if command == "get":
            return {"parameter": parameter_dict(parameter)}
        if command == "set":
            result = self.client.write(transmitter.address, parameter, str(request["value"]))
            return {
                "verified": result.verified,
                "old_value": result.old_value,
                "parameter": parameter_dict(result.parameter),
            }
        if command == "command":
            result = self.client.command(
                transmitter.address, parameter, bool(request.get("confirm", False)))
            return {"parameter": parameter_dict(result)}
        raise ValueError(f"unknown configuration command {request['command']!r}")


def parse_config_request(data: bytes) -> dict:
    text = data.decode("utf-8").strip()
    if text.startswith("{"):
        return json.loads(text)
    words = shlex.split(text)
    command = words[0].casefold()
    if command in ("info", "devices", "params"):
        return {"command": command}
    if command == "get":
        return {"command": command, "parameter": words[1]}
    if command == "set":
        return {"command": command, "parameter": words[1], "value": words[2]}
    if command == "command":
        return {
            "command": command,
            "parameter": words[1],
            "confirm": "--confirm" in words[2:],
        }
    raise ValueError(f"unknown configuration command {words[0]!r}")


class ConfigUdpServer(threading.Thread):
    def __init__(self, host: str, port: int, transport: ProxyConfigTransport,
                 debug: bool = False) -> None:
        super().__init__(name="config-udp", daemon=True)
        self.host = host
        self.port = port
        self.transport = transport
        self.debug = debug
        self.stop_event = threading.Event()
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind((host, port))
        self.socket.settimeout(0.1)

    def run(self) -> None:
        service = ConfigService(self.transport)
        while not self.stop_event.is_set():
            try:
                data, sender = self.socket.recvfrom(CONFIG_RESPONSE_LIMIT)
            except socket.timeout:
                continue
            try:
                request = parse_config_request(data)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, IndexError) as error:
                response = {"ok": False, "error": f"{type(error).__name__}: {error}"}
            else:
                if self.debug:
                    print(f"Config request sender={sender} command={request.get('command')!r}")
                try:
                    response = {"ok": True, "result": service.execute(request)}
                except (KeyError, ValueError, TimeoutError, RuntimeError) as error:
                    response = {"ok": False, "error": f"{type(error).__name__}: {error}"}
            payload = json.dumps(response, separators=(",", ":")).encode("utf-8")
            self.socket.sendto(payload, sender)

    def close(self) -> None:
        self.stop_event.set()
        self.join()
        self.socket.close()

class PacketsTypes(IntEnum):
    GPS = 0x02
    VARIO = 0x07
    BATTERY_SENSOR = 0x08
    BARO_ALT = 0x09
    HEARTBEAT = 0x0B
    VIDEO_TRANSMITTER = 0x0F
    LINK_STATISTICS = 0x14
    RC_CHANNELS_PACKED = 0x16
    ATTITUDE = 0x1E
    FLIGHT_MODE = 0x21
    DEVICE_INFO = 0x29
    CONFIG_READ = 0x2C
    CONFIG_WRITE = 0x2D
    RADIO_ID = 0x3A

def crc8_dvb_s2(crc, a) -> int:
    crc ^= a
    for _ in range(8):
        if crc & 0x80:
            crc = (crc << 1) ^ 0xD5
        else:
            crc <<= 1
    return crc & 0xFF

def crc8_data(data) -> int:
    crc = 0
    for a in data:
        crc = crc8_dvb_s2(crc, a)
    return crc

def crsf_validate_frame(frame) -> bool:
    # frame = [0]sync [1]len [2]type ... [n]crc
    if len(frame) < 4:
        return False
    return crc8_data(frame[2:-1]) == frame[-1]

def signed_byte(b: int) -> int:
    return b - 256 if b >= 128 else b

def us_to_crsf(us: int) -> int:
    # 1000..2000 us -> 172..1811 (1639 steps)
    us = max(1000, min(2000, int(us)))
    return 172 + round((us - 1000) * 1639 / 1000)

def crsf_to_us(v: int) -> int:
    v = max(172, min(1811, int(v)))
    return 1000 + round((v - 172) * 1000 / 1639)

def packCrsfToBytes(channels) -> bytes:
    # channels are 16 ints in CRSF range (0..1811, typical 172..1811)
    if len(channels) != CHANNEL_COUNT:
        raise ValueError(f"CRSF must have {CHANNEL_COUNT} channels")
    result = bytearray()
    destShift = 0
    newVal = 0
    for ch in channels:
        newVal |= (ch << destShift) & 0xFF
        result.append(newVal)
        srcBitsLeft = 11 - 8 + destShift
        newVal = ch >> (11 - srcBitsLeft)
        if srcBitsLeft >= 8:
            result.append(newVal & 0xFF)
            newVal >>= 8
            srcBitsLeft -= 8
        destShift = srcBitsLeft
    return result  # 22 bytes

def channelsCrsfToChannelsPacket(channels_crsf) -> bytes:
    # channels_crsf: list of 16 values already in CRSF units
    payload = bytearray([PacketsTypes.RC_CHANNELS_PACKED])
    payload += packCrsfToBytes(channels_crsf)
    length = len(payload) + 1  # +1 for CRC
    frame = bytearray([CRSF_SYNC, length]) + payload
    frame.append(crc8_data(frame[2:]))
    return frame

def channelsUsToPacket(us_channels) -> bytes:
    # us_channels: list of 16 microsecond values
    if len(us_channels) != CHANNEL_COUNT:
        raise ValueError(f"Need {CHANNEL_COUNT} channels")
    crsf_ch = [us_to_crsf(v) for v in us_channels]
    return channelsCrsfToChannelsPacket(crsf_ch)

def unpack_rc_channels(payload: bytes) -> list:
    # payload is the 22 data bytes after type
    ch = []
    acc = 0
    bits = 0
    for b in payload[:22]:
        acc |= (b & 0xFF) << bits
        bits += 8
        if bits >= 11:
            ch.append(acc & 0x7FF)
            acc >>= 11
            bits -= 11
            if len(ch) == CHANNEL_COUNT:
                break
    if len(ch) != CHANNEL_COUNT:
        ch += [172] * (CHANNEL_COUNT - len(ch))
    return ch

def handleCrsfPacket(ptype: int, data: bytes, verbose=False):
    """
    Parse CRSF telemetry packet. Returns a dict, or None if unhandled.
    data is the whole frame, including sync and crc.
    """
    try:
        pkt_type = PacketsTypes(ptype)
    except ValueError:
        pkt_type = None

    now = time.time()
    out = {"t": now, "ptype": int(ptype)}

    if pkt_type == PacketsTypes.RADIO_ID:
        out["type"] = "RADIO_ID"
        out["raw"] = data.hex()
        return out

    if pkt_type == PacketsTypes.LINK_STATISTICS:
        out.update({
            "type": "LINK_STATISTICS",
            "rssi1": signed_byte(data[3]),
            "rssi2": signed_byte(data[4]),
            "lq": data[5],
            "snr": signed_byte(data[6]),
            "antenna": data[7],
            "mode": data[8],
            "power": data[9],
            "downlink_rssi": signed_byte(data[10]),
            "downlink_lq": data[11],
            "downlink_snr": signed_byte(data[12]),
        })
        return out

    if pkt_type == PacketsTypes.ATTITUDE:
        out.update({
            "type": "ATTITUDE",
            "pitch_rad": int.from_bytes(data[3:5], "big", signed=True) / 10000.0,
            "roll_rad": int.from_bytes(data[5:7], "big", signed=True) / 10000.0,
            "yaw_rad": int.from_bytes(data[7:9], "big", signed=True) / 10000.0,
        })
        return out

    if pkt_type == PacketsTypes.FLIGHT_MODE:
        raw_mode = bytes(data[3:-1]).decode("ascii", errors="ignore")
        clean = []
        for ch in raw_mode:
            if ch == "\x00":
                break
            if ch in ("*", " "):
                break
            if ch.isalpha() or ch.isdigit() or ch in ("!", "_"):
                clean.append(ch)
            else:
                break
        mode = "".join(clean)
        out.update({
            "type": "FLIGHT_MODE",
            "mode": mode,
            "raw_mode": raw_mode
        })
        return out

    if pkt_type == PacketsTypes.BATTERY_SENSOR:
        out.update({
            "type": "BATTERY_SENSOR",
            "vbat_v": int.from_bytes(data[3:5], "big", signed=True) / 10.0,
            "current_a": int.from_bytes(data[5:7], "big", signed=True) / 10.0,
            "mah": (data[7] << 16) | (data[8] << 8) | data[9],
            "pct": data[10],
        })
        return out

    if pkt_type == PacketsTypes.BARO_ALT:
        out.update({
            "type": "BARO_ALT",
            "alt_m": int.from_bytes(data[3:7], "big", signed=True) / 100.0,
        })
        return out

    if pkt_type == PacketsTypes.DEVICE_INFO:
        out.update({
            "type": "DEVICE_INFO",
            "raw": data.hex()
        })
        return out

    if pkt_type == PacketsTypes.GPS:
        out.update({
            "type": "GPS",
            "lat": int.from_bytes(data[3:7], "big", signed=True) / 1e7,
            "lon": int.from_bytes(data[7:11], "big", signed=True) / 1e7,
            "gspd_ms": int.from_bytes(data[11:13], "big", signed=True) / 36.0,
            "hdg_deg": int.from_bytes(data[13:15], "big", signed=True) / 100.0,
            "alt_m": int.from_bytes(data[15:17], "big", signed=True) - 1000,
            "sats": data[17],
        })
        return out

    if pkt_type == PacketsTypes.VARIO:
        out.update({
            "type": "VARIO",
            "vspd_ms": int.from_bytes(data[3:5], "big", signed=True) / 10.0,
        })
        return out

    if pkt_type == PacketsTypes.RC_CHANNELS_PACKED:
        payload = data[3:-1]  # type already at [2], CRC at [-1]
        ch_crsf = unpack_rc_channels(payload)
        out.update({
            "type": "RC_CHANNELS_PACKED",
            "ch_crsf": ch_crsf,
            "ch_us": [crsf_to_us(v) for v in ch_crsf],
        })
        return out

    out.update({"type": "UNKNOWN", "raw": data.hex()})
    if verbose:
        print(f"Telemetry UNKNOWN type=0x{ptype:02x} raw={data.hex()}")
    return out

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='0.0.0.0', required=False, help="UDP bind host for RC packets")
    parser.add_argument('--port', type=int, default=60000, required=False, help="UDP port for RC packets")
    parser.add_argument('--device', default='/dev/ttyUSB0', required=False, help="Serial device")
    parser.add_argument('--baud', type=int, default=DEFAULT_BAUD, required=False, help="CRSF serial baudrate")
    parser.add_argument('--tx_rate', type=float, default=100.0, help="RC frame rate Hz")
    parser.add_argument('--loop_hz', type=float, default=250.0, help="Max main loop rate Hz")
    parser.add_argument('--failsafe_time_ms', type=int, default=1000, help="Enter failsafe after this")
    parser.add_argument('--failsafe_channels_us', nargs=CHANNEL_COUNT, type=int, default=FAILSAFE_DEFAULT_US, help="Failsafe channels in microseconds (16 values, space-separated)")
    parser.add_argument('--telemetry_udp', help="Send raw CRSF telemetry frames to udp://host:port (e.g. MWP)")
    parser.add_argument('--config_udp', type=int, help="UDP port for TX elrs.lua configuration commands")
    parser.add_argument('--debug', action='store_true', help="Verbose RC/telemetry logging")
    args = parser.parse_args()

    HOST = args.host
    PORT = args.port
    tx_rate = float(args.tx_rate)
    loop_hz = float(args.loop_hz)
    loop_period = 1.0 / loop_hz
    failsafe_time_ms = int(args.failsafe_time_ms)

    # RC channel state (microseconds). Initialize throttle and arm low.
    channels_us = [MID_US] * CHANNEL_COUNT
    channels_us[2] = MIN_US   # throttle
    channels_us[4] = ARM_LOW_US   # arm

    failsafe_us = list(args.failsafe_channels_us)

    print(f"Listening for UDP RC on {HOST}:{PORT}")

    # Open serial without asserting modem control lines during startup.
    ser = serial.Serial()
    ser.port = args.device
    ser.baudrate = args.baud
    ser.timeout = 0
    ser.write_timeout = WRITE_TIMEOUT_S
    ser.bytesize = serial.EIGHTBITS
    ser.parity = serial.PARITY_NONE
    ser.stopbits = serial.STOPBITS_ONE
    ser.xonxoff = False
    ser.rtscts = False
    ser.dsrdtr = False
    ser.rts = False
    ser.dtr = False
    ser.open()
    ser.reset_input_buffer()
    ser.reset_output_buffer()
    if args.debug:
        print(
            f"Opened serial device={args.device} "
            f"baud_request={args.baud} "
            f"baud_open={ser.baudrate} "
            f"timeout={ser.timeout} "
            f"write_timeout={ser.write_timeout} "
            f"rts={ser.rts} "
            f"dtr={ser.dtr}"
        )
    input_buf = bytearray()

    # UDP socket for RC input
    rc_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rc_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    rc_sock.bind((HOST, PORT))
    rc_sock.setblocking(False)

    # Optional UDP socket for telemetry (raw CRSF frames)
    tele_sock = None
    tele_target = None
    if args.telemetry_udp is not None:
        if ":" not in args.telemetry_udp:
            raise ValueError("telemetry_udp must be host:port")
        host, port_s = args.telemetry_udp.rsplit(":", 1)
        tele_target = (host, int(port_s))
        tele_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        tele_sock.setblocking(False)
        print(f"Telemetry UDP target {tele_target}")
        if args.debug:
            print("Debug telemetry forwarding enabled")

    config_transport = None
    config_server = None
    next_config_status_t = 0.0
    if args.config_udp is not None:
        config_transport = ProxyConfigTransport()
        config_server = ConfigUdpServer(HOST, args.config_udp, config_transport, args.debug)
        config_server.start()
        print(f"ELRS configuration UDP listening on {HOST}:{args.config_udp}")

    last_rc_sender = None
    last_rc_update_t = 0.0
    last_tx_t = 0.0
    last_valid_channels_us = None
    last_control_state = None
    last_tx_debug_t = 0.0

    try:
        while True:
            loop_start = time.time()
            now = time.time()

            # Read RC updates from UDP (drain socket, keep latest)
            while True:
                try:
                    data, sender = rc_sock.recvfrom(128)
                except (BlockingIOError, InterruptedError):
                    break
                if len(data) != UDP_PACKET_LEN:
                    print(f"Bad RC packet length {len(data)} from {sender}")
                    continue
                payload = data[:UDP_PAYLOAD_LEN]
                crc_rx = struct.unpack_from("<I", data, UDP_PAYLOAD_LEN)[0]
                crc_calc = zlib.crc32(payload) & 0xFFFFFFFF
                if crc_calc != crc_rx:
                    print(f"CRC mismatch from {sender}: got {crc_rx:08x} expected {crc_calc:08x}")
                    continue
                _ts_ms = struct.unpack_from("<I", payload, 0)[0]
                ch = list(struct.unpack_from("<16H", payload, 4))
                if args.debug and sender != last_rc_sender:
                    print(f"Receiving RC from {sender} ch0-3={ch[:4]} ch4-7={ch[4:8]}")
                channels_us = ch
                last_valid_channels_us = ch
                last_rc_update_t = now
                last_rc_sender = sender

            # Read from serial
            try:
                waiting = ser.in_waiting
            except OSError as e:
                print(f"Serial in_waiting failed port={args.device} baud={args.baud} err={e}")
                raise
            if waiting > 0:
                try:
                    chunk = ser.read(waiting)
                except OSError as e:
                    print(f"Serial read failed port={args.device} baud={args.baud} waiting={waiting} err={e}")
                    raise
                if chunk.startswith(b"$X"):
                    # Some devices prepend 8 bytes of junk
                    chunk = chunk[8:]
                input_buf.extend(chunk)
                if args.debug and len(chunk) > 0:
                    print(
                        f"Serial rx chunk_len={len(chunk)} "
                        f"buffer_len={len(input_buf)} "
                        f"chunk_hex={chunk.hex()}"
                    )

            # Parse CRSF frames from buffer
            while len(input_buf) > 2:
                if input_buf[0] not in SERIAL_RX_HEADERS:
                    sync_index = -1
                    for header_byte in SERIAL_RX_HEADERS:
                        header_index = input_buf.find(bytes((header_byte,)))
                        if header_index != -1 and (sync_index == -1 or header_index < sync_index):
                            sync_index = header_index
                    if sync_index == -1:
                        if args.debug:
                            print(
                                f"Serial discard reason=no_sync "
                                f"discard_len={len(input_buf)} "
                                f"discard_hex={bytes(input_buf).hex()}"
                            )
                        input_buf.clear()
                        break
                    if args.debug and sync_index > 0:
                        print(
                            f"Serial discard reason=resync "
                            f"discard_len={sync_index} "
                            f"discard_hex={bytes(input_buf[:sync_index]).hex()}"
                        )
                    del input_buf[:sync_index]
                    if len(input_buf) <= 2:
                        break
                expected_len = input_buf[1] + 2  # length includes type..crc, plus sync+len here
                if expected_len > 64 or expected_len < 4:
                    if args.debug:
                        print(
                            f"Serial discard reason=bad_len "
                            f"len_byte={input_buf[1]} "
                            f"buffer_len={len(input_buf)} "
                            f"buffer_hex={bytes(input_buf).hex()}"
                        )
                    del input_buf[0]
                    continue
                if len(input_buf) < expected_len:
                    break
                frame = bytes(input_buf[:expected_len])
                del input_buf[:expected_len]
                if not crsf_validate_frame(frame):
                    if args.debug:
                        print(
                            f"Serial discard reason=crc_error "
                            f"frame_len={len(frame)} "
                            f"type=0x{frame[2]:02x} "
                            f"frame_hex={frame.hex()}"
                        )
                    continue
                if args.debug:
                    print(
                        f"Serial frame_header=0x{frame[0]:02x} "
                        f"Serial frame_ok len={len(frame)} "
                        f"type=0x{frame[2]:02x} "
                        f"frame_hex={frame.hex()}"
                    )
                if config_transport is not None:
                    config_transport.observe(
                        CrsfFrame(frame[0], frame[2], frame[3:-1], frame))
                if tele_sock is not None:
                    try:
                        tele_sock.sendto(frame, tele_target)
                        if args.debug:
                            print(f"Sent telemetry frame len={len(frame)} type=0x{frame[2]:02x} to {tele_target}")
                    except BlockingIOError:
                        pass
                pkt = handleCrsfPacket(frame[2], frame, verbose=args.debug)
                if args.debug and pkt is not None:
                    ptype = pkt.get("type")
                    if ptype == "LINK_STATISTICS":
                        print(f"TEL LINK rssi1={pkt['rssi1']} rssi2={pkt['rssi2']} lq={pkt['lq']} snr={pkt['snr']} pwr={pkt['power']} down_rssi={pkt['downlink_rssi']} down_lq={pkt['downlink_lq']} down_snr={pkt['downlink_snr']}")
                    elif ptype == "ATTITUDE":
                        print(f"TEL ATTI p={pkt['pitch_rad']:.3f} r={pkt['roll_rad']:.3f} y={pkt['yaw_rad']:.3f}")
                    elif ptype == "FLIGHT_MODE":
                        print(f"TEL MODE {pkt['mode']} raw='{pkt.get('raw_mode','')}'")
                    elif ptype == "BATTERY_SENSOR":
                        print(f"TEL BATT {pkt['vbat_v']:.1f}V {pkt['current_a']:.1f}A {pkt['mah']}mAh {pkt['pct']}%")
                    elif ptype == "GPS":
                        print(f"TEL GPS lat={pkt['lat']:.6f} lon={pkt['lon']:.6f} alt={pkt['alt_m']}m gspd={pkt['gspd_ms']:.2f}m/s hdg={pkt['hdg_deg']:.2f} sats={pkt['sats']}")
                    elif ptype == "VARIO":
                        print(f"TEL VARIO vspd={pkt['vspd_ms']:.1f}m/s")
                    elif ptype == "BARO_ALT":
                        print(f"TEL BARO alt={pkt['alt_m']:.2f}m")
                    elif ptype == "RC_CHANNELS_PACKED":
                        print(f"TEL RC ch0-3={pkt['ch_us'][:4]} ch4-7={pkt['ch_us'][4:8]}")
                    #elif ptype == "RADIO_ID": # skip printing
                    #    print(f"TEL RADIO_ID {pkt['raw']}")
                    elif ptype == "DEVICE_INFO":
                        print(f"TEL DEVICE_INFO {pkt['raw']}")

            # Determine which channels to send
            if (now - last_tx_t) >= (1.0 / tx_rate):
                if config_transport is not None and now >= next_config_status_t:
                    config_transport.queue(make_extended_frame(
                        FrameType.PARAMETER_WRITE,
                        ELRS_ADDRESS_TRANSMITTER,
                        CRSF_ADDRESS_ELRS_LUA,
                        bytes([0, 0]),
                    ))
                    next_config_status_t = now + CONFIG_STATUS_INTERVAL_S
                elapsed_ms = (now - last_rc_update_t) * 1000.0
                if last_valid_channels_us is None:
                    active = failsafe_us
                    control_state = "no_rc"
                elif elapsed_ms >= failsafe_time_ms:
                    active = failsafe_us
                    control_state = "failsafe"
                else:
                    active = last_valid_channels_us
                    control_state = "live_rc"
                if args.debug and control_state != last_control_state:
                    print(
                        f"RC state={control_state} "
                        f"sender={last_rc_sender} "
                        f"elapsed_ms={elapsed_ms:.1f} "
                        f"ch0-3={active[:4]} "
                        f"ch4-7={active[4:8]}"
                    )
                    last_control_state = control_state
                if args.debug and (now - last_tx_debug_t) >= 0.5:
                    print(
                        f"Serial tx state={control_state} "
                        f"elapsed_ms={elapsed_ms:.1f} "
                        f"ch0-3={active[:4]} "
                        f"ch4-7={active[4:8]}"
                    )
                    last_tx_debug_t = now
                try:
                    frame = channelsUsToPacket(active)
                    control_frames = config_transport.drain_outbound() \
                        if config_transport is not None else []
                    burst = frame + b"".join(control_frames)
                    written = ser.write(burst)
                    if args.debug:
                        print(
                            f"Serial tx frame_len={len(frame)} control_frames={len(control_frames)} "
                            f"burst_len={len(burst)} written={written} "
                            f"type=0x{frame[2]:02x} frame_hex={frame.hex()}"
                        )
                except serial.SerialTimeoutException as e:
                    print(f"Serial write timeout port={args.device} baud={args.baud} burst_len={len(burst)} elapsed_ms={elapsed_ms:.1f} err={e}")
                last_tx_t = now

            loop_elapsed = time.time() - loop_start
            if loop_elapsed < loop_period:
                time.sleep(loop_period - loop_elapsed)

    except KeyboardInterrupt:
        print("Shutdown requested.")
    finally:
        try:
            rc_sock.close()
        except OSError:
            pass
        if config_server is not None:
            config_server.close()
        try:
            ser.close()
        except OSError:
            pass
        if tele_sock is not None:
            try:
                tele_sock.close()
            except OSError:
                pass

if __name__ == "__main__":
    main()
