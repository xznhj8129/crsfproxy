#!/usr/bin/env python3
"""crsfproxy core and the ExpressLRS radio backend. Run crsfproxy.py.

Usage:
  python3 crsfproxy.py --device /dev/ttyUSB0 --baud 921600 --host 0.0.0.0 --port 60000 --loop_hz 250 --tx_rate 100 --telemetry_udp 192.168.4.2:40042 --config_udp 60001

CRSF TX side bridge:
- Listens on a UDP port for 40-byte RC packets: <uint32 t_ms><16 x uint16 us><uint32 crc32>.
- Converts fresh UDP RC state to CRSF RC_CHANNELS_PACKED and writes it to the serial CRSF port.
- Parses inbound CRSF telemetry and can forward raw frames to a UDP target (e.g. MWP).

Telemetry output:
- Optional raw CRSF frames to --telemetry_udp (host:port)

Configuration:
- JSON or shell-style configuration commands on --config_udp (RC port + 1 by default).
- The radio backend (lib/radio.py) executes them; ELRS_RADIO uses the same
  DEVICE/PARAMETER protocol as elrs.lua.

RC hold:
- Disabled by default: each fresh UDP RC sample is forwarded once, subject to --tx_rate.
- --rc-hold-ms N optionally repeats the latest RC sample for up to N ms to smooth short input gaps.
- After the hold expires, crsfproxy stops emitting RC channel frames until fresh UDP RC arrives.
- Actual RF/receiver/flight-controller failsafe logic remains downstream of crsfproxy.
"""

import json
import queue
import shlex
import socket
import threading
import time
from dataclasses import dataclass

import serial

from .crsf_protocol import (
    CRSF_ADDRESS_ELRS_LUA,
    CRSF_ADDRESS_RADIO_TRANSMITTER,
    CRSF_ADDRESS_TRANSMITTER as ELRS_ADDRESS_TRANSMITTER,
    CRSF_CHANNEL_PAYLOAD_SIZE,
    CRSF_SYNC,
    DeviceInfo,
    Frame as CrsfFrame,
    FrameParser,
    FrameType,
    Parameter,
    ParameterType,
    crsf_to_us,
    make_extended_frame,
    make_rc_frame,
    unpack_channels,
)
from .elrs_config import ParameterClient
from .radio import RadioBackend
from .rc_packet import RcPacket, RcPacketError
from .schedule import DeadlineSchedule

WRITE_TIMEOUT_S = 0.1
DEFAULT_BAUD = 115200
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


class ElrsConfigService:
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
    def __init__(self, host: str, port: int, service, debug: bool = False) -> None:
        super().__init__(name="config-udp", daemon=True)
        self.host = host
        self.port = port
        self.service = service
        self.debug = debug
        self.stop_event = threading.Event()
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind((host, port))
        self.socket.settimeout(0.1)

    def run(self) -> None:
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
                    response = {"ok": True, "result": self.service.execute(request)}
                except (KeyError, ValueError, TimeoutError, RuntimeError) as error:
                    response = {"ok": False, "error": f"{type(error).__name__}: {error}"}
            payload = json.dumps(response, separators=(",", ":")).encode("utf-8")
            self.socket.sendto(payload, sender)

    def close(self) -> None:
        self.stop_event.set()
        self.join()
        self.socket.close()


ELRS_RADIO = RadioBackend(
    name="ELRS",
    default_baud=DEFAULT_BAUD,
    frame_addresses=frozenset((CRSF_SYNC, ELRS_ADDRESS_TRANSMITTER)),
    transport_type=ProxyConfigTransport,
    service_type=ElrsConfigService,
    # Empty PARAMETER_WRITE from the Lua address: asks the TX for ELRS_STATUS.
    status_poll_frame=make_extended_frame(
        FrameType.PARAMETER_WRITE, ELRS_ADDRESS_TRANSMITTER, CRSF_ADDRESS_ELRS_LUA, bytes([0, 0])),
)


def signed_byte(b: int) -> int:
    return b - 256 if b >= 128 else b

def handleCrsfPacket(ptype: int, data: bytes, verbose=False):
    """
    Parse CRSF telemetry packet. Returns a dict, or None if unhandled.
    data is the whole frame, including sync and crc.
    """
    try:
        pkt_type = FrameType(ptype)
    except ValueError:
        pkt_type = None

    now = time.time()
    out = {"t": now, "ptype": int(ptype)}

    if pkt_type == FrameType.RADIO_ID:
        out["type"] = "RADIO_ID"
        out["raw"] = data.hex()
        return out

    if pkt_type == FrameType.LINK_STATISTICS:
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

    if pkt_type == FrameType.ATTITUDE:
        out.update({
            "type": "ATTITUDE",
            "pitch_rad": int.from_bytes(data[3:5], "big", signed=True) / 10000.0,
            "roll_rad": int.from_bytes(data[5:7], "big", signed=True) / 10000.0,
            "yaw_rad": int.from_bytes(data[7:9], "big", signed=True) / 10000.0,
        })
        return out

    if pkt_type == FrameType.FLIGHT_MODE:
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

    if pkt_type == FrameType.BATTERY_SENSOR:
        out.update({
            "type": "BATTERY_SENSOR",
            "vbat_v": int.from_bytes(data[3:5], "big", signed=True) / 10.0,
            "current_a": int.from_bytes(data[5:7], "big", signed=True) / 10.0,
            "mah": (data[7] << 16) | (data[8] << 8) | data[9],
            "pct": data[10],
        })
        return out

    if pkt_type == FrameType.BARO_ALTITUDE:
        out.update({
            "type": "BARO_ALT",
            "alt_m": int.from_bytes(data[3:7], "big", signed=True) / 100.0,
        })
        return out

    if pkt_type == FrameType.DEVICE_INFO:
        out.update({
            "type": "DEVICE_INFO",
            "raw": data.hex()
        })
        return out

    if pkt_type == FrameType.GPS:
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

    if pkt_type == FrameType.VARIO:
        out.update({
            "type": "VARIO",
            "vspd_ms": int.from_bytes(data[3:5], "big", signed=True) / 10.0,
        })
        return out

    payload = data[3:-1]  # type already at [2], CRC at [-1]
    if pkt_type == FrameType.RC_CHANNELS_PACKED and len(payload) == CRSF_CHANNEL_PAYLOAD_SIZE:
        ch_crsf = unpack_channels(payload)
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

@dataclass(frozen=True)
class ProxyOptions:
    """Validated settings for one proxy; crsfproxy.py builds this from its arguments."""
    host: str
    port: int
    device: str
    baud: int
    tx_rate: float
    loop_hz: float
    rc_hold_ms: int
    telemetry_udp: tuple[str, int] | None
    config_udp: int
    debug: bool


def run_proxy(options: ProxyOptions, radio: RadioBackend) -> None:
    HOST = options.host
    PORT = options.port
    loop_period = 1.0 / options.loop_hz
    rc_hold_ms = options.rc_hold_ms

    print(f"Listening for UDP RC on {HOST}:{PORT}")
    print("RC hold disabled" if rc_hold_ms == 0 else f"RC hold {rc_hold_ms} ms")

    # Open a local serial device or pySerial URL without asserting modem control lines.
    ser = serial.serial_for_url(options.device, do_not_open=True)
    ser.baudrate = options.baud
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
    if options.debug:
        print(
            f"Opened serial device={options.device} "
            f"baud_request={options.baud} "
            f"baud_open={ser.baudrate} "
            f"timeout={ser.timeout} "
            f"write_timeout={ser.write_timeout} "
            f"rts={ser.rts} "
            f"dtr={ser.dtr}"
        )
    frame_parser = FrameParser(radio.frame_addresses)

    # UDP socket for RC input
    rc_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rc_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    rc_sock.bind((HOST, PORT))
    rc_sock.setblocking(False)

    # Optional UDP socket for telemetry (raw CRSF frames)
    tele_sock = None
    tele_target = None
    if options.telemetry_udp is not None:
        tele_target = options.telemetry_udp
        tele_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        tele_sock.setblocking(False)
        print(f"Telemetry UDP target {tele_target}")
        if options.debug:
            print("Debug telemetry forwarding enabled")

    next_config_status_t = 0.0
    config_transport = radio.transport_type()
    config_server = ConfigUdpServer(
        HOST, options.config_udp, radio.service_type(config_transport), options.debug)
    config_server.start()
    print(f"{radio.name} configuration UDP listening on {HOST}:{options.config_udp}")

    last_rc_sender = None
    last_rc_update_t = 0.0
    last_valid_channels_us = None
    rc_update_seq = 0
    last_sent_rc_seq = 0
    last_control_state = None
    last_tx_debug_t = 0.0
    rc_schedule = DeadlineSchedule(options.tx_rate, time.monotonic())

    try:
        while True:
            loop_start = time.monotonic()
            now = loop_start

            # Read RC updates from UDP (drain socket, keep latest)
            while True:
                try:
                    data, sender = rc_sock.recvfrom(128)
                except (BlockingIOError, InterruptedError):
                    break
                try:
                    ch = list(RcPacket.decode(data).channels_us)
                except RcPacketError as error:
                    print(f"RC packet rejected sender={sender} {error}")
                    continue
                if options.debug and sender != last_rc_sender:
                    print(f"Receiving RC from {sender} ch0-3={ch[:4]} ch4-7={ch[4:8]}")
                last_valid_channels_us = ch
                last_rc_update_t = now
                last_rc_sender = sender
                rc_update_seq += 1

            # Read from serial
            try:
                waiting = ser.in_waiting
            except OSError as e:
                print(f"Serial in_waiting failed port={options.device} baud={options.baud} err={e}")
                raise
            frames = []
            if waiting > 0:
                try:
                    chunk = ser.read(waiting)
                except OSError as e:
                    print(f"Serial read failed port={options.device} baud={options.baud} waiting={waiting} err={e}")
                    raise
                if chunk.startswith(b"$X"):
                    # Some devices prepend 8 bytes of junk
                    chunk = chunk[8:]
                discarded_before = frame_parser.bytes_discarded
                crc_errors_before = frame_parser.crc_errors
                frames = frame_parser.feed(chunk)
                if options.debug and len(chunk) > 0:
                    print(
                        f"Serial rx chunk_len={len(chunk)} "
                        f"buffer_len={len(frame_parser.buffer)} "
                        f"chunk_hex={chunk.hex()}"
                    )
                if options.debug and frame_parser.bytes_discarded != discarded_before:
                    print(
                        f"Serial discard "
                        f"discard_len={frame_parser.bytes_discarded - discarded_before} "
                        f"crc_errors={frame_parser.crc_errors - crc_errors_before} "
                        f"(bytes are in the chunk_hex above)"
                    )

            # Handle complete, CRC-valid CRSF frames
            for frame in frames:
                if options.debug:
                    print(
                        f"Serial frame_header=0x{frame.address:02x} "
                        f"Serial frame_ok len={len(frame.raw)} "
                        f"type=0x{frame.type:02x} "
                        f"frame_hex={frame.raw.hex()}"
                    )
                config_transport.observe(frame)
                if tele_sock is not None:
                    try:
                        tele_sock.sendto(frame.raw, tele_target)
                        if options.debug:
                            print(f"Sent telemetry frame len={len(frame.raw)} type=0x{frame.type:02x} to {tele_target}")
                    except BlockingIOError:
                        pass
                pkt = handleCrsfPacket(frame.type, frame.raw, verbose=options.debug)
                if options.debug and pkt is not None:
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

            # Send fresh RC, or optionally hold the latest sample for a short gap.
            if rc_schedule.due(now):
                if radio.status_poll_frame is not None and now >= next_config_status_t:
                    config_transport.queue(radio.status_poll_frame)
                    next_config_status_t = now + CONFIG_STATUS_INTERVAL_S

                elapsed_ms = (now - last_rc_update_t) * 1000.0
                active = None
                if last_valid_channels_us is None:
                    control_state = 'waiting_rc'
                elif rc_update_seq != last_sent_rc_seq:
                    active = last_valid_channels_us
                    control_state = 'live_rc'
                elif rc_hold_ms > 0 and elapsed_ms < rc_hold_ms:
                    active = last_valid_channels_us
                    control_state = 'rc_hold'
                else:
                    control_state = 'rc_idle'

                if options.debug and control_state != last_control_state:
                    line = (
                        f"RC state={control_state} "
                        f"sender={last_rc_sender} "
                        f"elapsed_ms={elapsed_ms:.1f}"
                    )
                    if active is not None:
                        line += f" ch0-3={active[:4]} ch4-7={active[4:8]}"
                    print(line)
                    last_control_state = control_state

                if options.debug and (now - last_tx_debug_t) >= 0.5:
                    line = f"Serial tx state={control_state} elapsed_ms={elapsed_ms:.1f}"
                    if active is not None:
                        line += f" ch0-3={active[:4]} ch4-7={active[4:8]}"
                    print(line)
                    last_tx_debug_t = now

                rc_frame = make_rc_frame(active) if active is not None else b''
                control_frames = config_transport.drain_outbound()
                burst = rc_frame + b''.join(control_frames)
                try:
                    written = ser.write(burst) if burst else 0
                    if rc_frame:
                        last_sent_rc_seq = rc_update_seq
                    if options.debug:
                        rc_type = f"0x{rc_frame[2]:02x}" if rc_frame else 'none'
                        print(
                            f"Serial tx rc_frame_len={len(rc_frame)} control_frames={len(control_frames)} "
                            f"burst_len={len(burst)} written={written} "
                            f"rc_type={rc_type}"
                            + (f" frame_hex={rc_frame.hex()}" if rc_frame else '')
                        )
                except serial.SerialTimeoutException as e:
                    print(f"Serial write timeout port={options.device} baud={options.baud} burst_len={len(burst)} elapsed_ms={elapsed_ms:.1f} err={e}")

            loop_elapsed = time.monotonic() - loop_start
            if loop_elapsed < loop_period:
                time.sleep(loop_period - loop_elapsed)

    except KeyboardInterrupt:
        print("Shutdown requested.")
    finally:
        try:
            rc_sock.close()
        except OSError:
            pass
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
