#!/usr/bin/env python3
"""Virtual CRSF receiver for INAV SITL and hardware-in-the-loop testing.

The simulator accepts the same 40-byte UDP RC packet used by crsfproxy,
generates CRSF RC_CHANNELS_PACKED and LINK_STATISTICS frames, and exchanges
raw CRSF frames with INAV over a TCP socket.

Examples:
  python crsf_rx_sim.py --endpoint tcp://127.0.0.1:5763
  python crsf_rx_sim.py --endpoint tcp-listen://127.0.0.1:5763

Run multiple independent receivers by starting multiple instances with distinct
endpoint, RC UDP, and control UDP ports.
"""

from __future__ import annotations

import argparse
import json
import select
import shlex
import socket
import struct
import sys
import time
import zlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from crsf_protocol import (
    CRSF_ADDRESS_FLIGHT_CONTROLLER,
    FrameParser,
    FrameType,
    describe_frame,
    make_frame,
    make_rc_frame,
)

CHANNEL_COUNT = 16
RC_UDP_PAYLOAD_LEN = 4 + CHANNEL_COUNT * 2
RC_UDP_PACKET_LEN = RC_UDP_PAYLOAD_LEN + 4
DEFAULT_RC_RATE_HZ = 150.0
DEFAULT_LINK_RATE_HZ = 10.0
DEFAULT_LOOP_RATE_HZ = 500.0
DEFAULT_RECONNECT_DELAY_S = 0.25
DEFAULT_CHANNELS_US = [
    1500, 1500, 1000, 1500, 1000, 1500, 1500, 1500,
    1500, 1500, 1500, 1500, 1500, 1500, 1500, 1500,
]
DEFAULT_FAILSAFE_CHANNELS_US = list(DEFAULT_CHANNELS_US)
CAPTURE_HEADER = struct.Struct("<QI")


class ConfigurationError(ValueError):
    """Invalid command-line or runtime configuration."""


@dataclass(frozen=True)
class EndpointSpec:
    mode: str
    host: str
    port: int


def parse_endpoint(value: str) -> EndpointSpec:
    parsed = urlparse(value)
    if parsed.scheme not in ("tcp", "tcp-listen"):
        raise ConfigurationError(
            f"endpoint={value!r} unsupported_scheme={parsed.scheme!r}; "
            "use tcp://host:port or tcp-listen://host:port"
        )
    if parsed.hostname is None or parsed.port is None:
        raise ConfigurationError(f"endpoint={value!r} requires host and port")
    return EndpointSpec(parsed.scheme, parsed.hostname, parsed.port)


def parse_host_port(value: str) -> tuple[str, int]:
    host, separator, port_text = value.rpartition(":")
    if not separator or not host or not port_text:
        raise ConfigurationError(f"address={value!r} expected=host:port")
    try:
        port = int(port_text)
    except ValueError as error:
        raise ConfigurationError(f"address={value!r} invalid_port={port_text!r}") from error
    if not 1 <= port <= 65535:
        raise ConfigurationError(f"address={value!r} port_out_of_range={port}")
    return host, port


def parse_channels(values: list[str] | tuple[str, ...]) -> list[int]:
    if len(values) != CHANNEL_COUNT:
        raise ConfigurationError(
            f"channel_count={len(values)} expected={CHANNEL_COUNT} values={values}"
        )
    channels = [int(value) for value in values]
    for index, value in enumerate(channels):
        if not 750 <= value <= 2250:
            raise ConfigurationError(
                f"channel_index={index} channel_value_us={value} valid_range=750..2250"
            )
    return channels


def decode_udp_rc_packet(data: bytes) -> tuple[int, list[int]]:
    if len(data) != RC_UDP_PACKET_LEN:
        raise ValueError(f"packet_len={len(data)} expected={RC_UDP_PACKET_LEN}")
    payload = data[:RC_UDP_PAYLOAD_LEN]
    received_crc = struct.unpack_from("<I", data, RC_UDP_PAYLOAD_LEN)[0]
    calculated_crc = zlib.crc32(payload) & 0xFFFFFFFF
    if received_crc != calculated_crc:
        raise ValueError(
            f"packet_crc=0x{received_crc:08X} expected_crc=0x{calculated_crc:08X}"
        )
    unpacked = struct.unpack("<I16H", payload)
    timestamp_ms = unpacked[0]
    channels = list(unpacked[1:])
    for index, value in enumerate(channels):
        if not 750 <= value <= 2250:
            raise ValueError(
                f"channel_index={index} channel_value_us={value} valid_range=750..2250"
            )
    return timestamp_ms, channels


def encode_signed_byte(value: int) -> int:
    if not -128 <= value <= 127:
        raise ValueError(f"signed_byte={value} valid_range=-128..127")
    return value & 0xFF


@dataclass
class LinkStatistics:
    """CRSF LINK_STATISTICS values as observed by the flight controller.

    RSSI fields are positive dBm magnitudes as defined by CRSF. For example,
    55 represents -55 dBm. SNR fields are signed dB values.
    """

    uplink_rssi_ant1: int = 55
    uplink_rssi_ant2: int = 58
    uplink_lq: int = 100
    uplink_snr: int = 9
    active_antenna: int = 0
    rf_mode: int = 7
    uplink_tx_power: int = 3
    downlink_rssi: int = 62
    downlink_lq: int = 100
    downlink_snr: int = 7

    def validate(self) -> None:
        unsigned_fields = {
            "uplink_rssi_ant1": self.uplink_rssi_ant1,
            "uplink_rssi_ant2": self.uplink_rssi_ant2,
            "active_antenna": self.active_antenna,
            "rf_mode": self.rf_mode,
            "uplink_tx_power": self.uplink_tx_power,
            "downlink_rssi": self.downlink_rssi,
        }
        for name, value in unsigned_fields.items():
            if not 0 <= value <= 255:
                raise ValueError(f"{name}={value} valid_range=0..255")
        for name, value in (("uplink_lq", self.uplink_lq), ("downlink_lq", self.downlink_lq)):
            if not 0 <= value <= 100:
                raise ValueError(f"{name}={value} valid_range=0..100")
        for name, value in (("uplink_snr", self.uplink_snr), ("downlink_snr", self.downlink_snr)):
            if not -128 <= value <= 127:
                raise ValueError(f"{name}={value} valid_range=-128..127")

    def frame(self, rf_link: bool) -> bytes:
        self.validate()
        uplink_lq = self.uplink_lq if rf_link else 0
        downlink_lq = self.downlink_lq if rf_link else 0
        payload = bytes([
            self.uplink_rssi_ant1,
            self.uplink_rssi_ant2,
            uplink_lq,
            encode_signed_byte(self.uplink_snr),
            self.active_antenna,
            self.rf_mode,
            self.uplink_tx_power,
            self.downlink_rssi,
            downlink_lq,
            encode_signed_byte(self.downlink_snr),
        ])
        return make_frame(
            FrameType.LINK_STATISTICS,
            payload,
            address=CRSF_ADDRESS_FLIGHT_CONTROLLER,
        )


@dataclass
class ReceiverState:
    channels_us: list[int] = field(default_factory=lambda: list(DEFAULT_CHANNELS_US))
    failsafe_channels_us: list[int] = field(
        default_factory=lambda: list(DEFAULT_FAILSAFE_CHANNELS_US)
    )
    link: LinkStatistics = field(default_factory=LinkStatistics)
    rf_link: bool = True
    rf_loss_policy: str = "stop"
    source_timeout_s: float = 1.0
    source_timeout_action: str = "hold"
    last_rc_input_at: float | None = None
    last_rc_source_timestamp_ms: int | None = None
    accepted_rc_packets: int = 0
    rejected_rc_packets: int = 0
    transmitted_rc_frames: int = 0
    transmitted_link_frames: int = 0
    received_telemetry_frames: int = 0
    received_telemetry_bytes: int = 0

    def validate(self) -> None:
        parse_channels(tuple(str(value) for value in self.channels_us))
        parse_channels(tuple(str(value) for value in self.failsafe_channels_us))
        self.link.validate()
        if self.rf_loss_policy not in ("stop", "failsafe"):
            raise ValueError(f"rf_loss_policy={self.rf_loss_policy!r}")
        if self.source_timeout_action not in ("hold", "failsafe", "stop"):
            raise ValueError(f"source_timeout_action={self.source_timeout_action!r}")
        if self.source_timeout_s < 0:
            raise ValueError(f"source_timeout_s={self.source_timeout_s}")

    def accept_rc(self, timestamp_ms: int, channels_us: list[int], now: float) -> None:
        self.channels_us = list(channels_us)
        self.last_rc_source_timestamp_ms = timestamp_ms
        self.last_rc_input_at = now
        self.accepted_rc_packets += 1

    def output_channels(self, now: float) -> list[int] | None:
        if not self.rf_link:
            if self.rf_loss_policy == "stop":
                return None
            return self.failsafe_channels_us

        source_timed_out = (
            self.last_rc_input_at is not None
            and self.source_timeout_s > 0
            and now - self.last_rc_input_at >= self.source_timeout_s
        )
        if not source_timed_out or self.source_timeout_action == "hold":
            return self.channels_us
        if self.source_timeout_action == "failsafe":
            return self.failsafe_channels_us
        return None

    def snapshot(self, connected: bool) -> dict[str, Any]:
        return {
            "connected": connected,
            "rf_link": self.rf_link,
            "rf_loss_policy": self.rf_loss_policy,
            "source_timeout_s": self.source_timeout_s,
            "source_timeout_action": self.source_timeout_action,
            "channels_us": list(self.channels_us),
            "failsafe_channels_us": list(self.failsafe_channels_us),
            "last_rc_source_timestamp_ms": self.last_rc_source_timestamp_ms,
            "link": asdict(self.link),
            "counters": {
                "accepted_rc_packets": self.accepted_rc_packets,
                "rejected_rc_packets": self.rejected_rc_packets,
                "transmitted_rc_frames": self.transmitted_rc_frames,
                "transmitted_link_frames": self.transmitted_link_frames,
                "received_telemetry_frames": self.received_telemetry_frames,
                "received_telemetry_bytes": self.received_telemetry_bytes,
            },
        }


class TcpEndpoint:
    def __init__(self, spec: EndpointSpec, timeout_s: float = 1.0) -> None:
        self.spec = spec
        self.timeout_s = timeout_s

    def open(self) -> socket.socket:
        if self.spec.mode == "tcp":
            connection = socket.create_connection(
                (self.spec.host, self.spec.port), timeout=self.timeout_s
            )
        else:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind((self.spec.host, self.spec.port))
                listener.listen(1)
                listener.settimeout(self.timeout_s)
                connection, _ = listener.accept()
            finally:
                listener.close()
        connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        connection.setblocking(False)
        return connection


class CaptureWriter:
    """Timestamped raw-frame capture: <uint64 unix_ns><uint32 len><frame>."""

    def __init__(self, path: Path | None) -> None:
        self.file = path.open("ab", buffering=0) if path else None

    def write(self, raw: bytes) -> None:
        if self.file is not None:
            self.file.write(CAPTURE_HEADER.pack(time.time_ns(), len(raw)))
            self.file.write(raw)

    def close(self) -> None:
        if self.file is not None:
            self.file.close()


def parse_control_request(data: bytes) -> dict[str, Any]:
    text = data.decode("utf-8").strip()
    if not text:
        raise ValueError("empty control request")
    if text.startswith("{"):
        request = json.loads(text)
        if not isinstance(request, dict):
            raise ValueError("JSON control request must be an object")
        return request

    words = shlex.split(text)
    action = words[0].replace("-", "_").casefold()
    if action in ("status", "drop_rf", "restore_rf") and len(words) == 1:
        return {"action": action}
    if action == "set" and len(words) == 3:
        return {"action": "set_link", words[1]: int(words[2])}
    raise ValueError(f"unsupported control request={text!r}")


def apply_control_request(
    state: ReceiverState,
    request: dict[str, Any],
    now: float,
    connected: bool,
) -> dict[str, Any]:
    action = str(request.get("action", "")).replace("-", "_").casefold()
    if action == "status":
        return state.snapshot(connected)
    if action == "drop_rf":
        state.rf_link = False
        return state.snapshot(connected)
    if action == "restore_rf":
        state.rf_link = True
        return state.snapshot(connected)
    if action == "set_channels":
        values = request.get("channels")
        if not isinstance(values, list):
            raise ValueError("set_channels requires channels array")
        channels = parse_channels(tuple(str(value) for value in values))
        state.accept_rc(int(now * 1000), channels, now)
        return state.snapshot(connected)
    if action == "set_link":
        valid_names = set(LinkStatistics.__dataclass_fields__)
        updates = {key: value for key, value in request.items() if key != "action"}
        if not updates:
            raise ValueError("set_link requires at least one link field")
        unknown = sorted(set(updates) - valid_names)
        if unknown:
            raise ValueError(f"unknown_link_fields={unknown}")
        for name, value in updates.items():
            setattr(state.link, name, int(value))
        state.link.validate()
        return state.snapshot(connected)
    raise ValueError(f"unknown action={action!r}")


def open_udp_listener(host: str, port: int) -> socket.socket:
    udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    udp_socket.bind((host, port))
    udp_socket.setblocking(False)
    return udp_socket


def receive_rc_packets(udp_socket: socket.socket, state: ReceiverState, now: float, debug: bool) -> None:
    while True:
        try:
            data, sender = udp_socket.recvfrom(65535)
        except BlockingIOError:
            return
        try:
            timestamp_ms, channels = decode_udp_rc_packet(data)
        except ValueError as error:
            state.rejected_rc_packets += 1
            if debug:
                print(f"RC reject sender={sender} error={error}", file=sys.stderr)
            continue
        state.accept_rc(timestamp_ms, channels, now)
        if debug:
            print(
                f"RC accept sender={sender} timestamp_ms={timestamp_ms} "
                f"channels_1_4={channels[:4]}"
            )


def receive_control_packets(
    udp_socket: socket.socket,
    state: ReceiverState,
    now: float,
    connected: bool,
    debug: bool,
) -> None:
    while True:
        try:
            data, sender = udp_socket.recvfrom(65535)
        except BlockingIOError:
            return
        try:
            request = parse_control_request(data)
            result = apply_control_request(state, request, now, connected)
            response = {"ok": True, "result": result}
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as error:
            response = {"ok": False, "error": f"{type(error).__name__}: {error}"}
        payload = json.dumps(response, separators=(",", ":")).encode("utf-8")
        udp_socket.sendto(payload, sender)
        if debug:
            print(f"CONTROL sender={sender} response={response}")


def read_connection(
    connection: socket.socket,
    parser: FrameParser,
    state: ReceiverState,
    telemetry_target: tuple[str, int] | None,
    telemetry_socket: socket.socket,
    capture: CaptureWriter,
    debug: bool,
) -> None:
    while True:
        readable, _, _ = select.select([connection], [], [], 0)
        if not readable:
            return
        try:
            data = connection.recv(4096)
        except BlockingIOError:
            return
        if not data:
            raise ConnectionError("peer closed CRSF TCP connection")
        state.received_telemetry_bytes += len(data)
        for frame in parser.feed(data):
            state.received_telemetry_frames += 1
            capture.write(frame.raw)
            if telemetry_target is not None:
                telemetry_socket.sendto(frame.raw, telemetry_target)
            if debug:
                try:
                    description = describe_frame(frame)
                except (ValueError, IndexError) as error:
                    description = f"type=0x{frame.type:02X} decode_error={error}"
                print(f"RX {description} frame_hex={frame.raw.hex()}")


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--endpoint",
        required=True,
        help="tcp://host:port to connect or tcp-listen://host:port to accept INAV",
    )
    parser.add_argument("--name", default="rx0", help="Receiver name used in logs")
    parser.add_argument("--host", default="0.0.0.0", help="UDP RC bind host")
    parser.add_argument("--port", type=int, default=60000, help="UDP RC bind port")
    parser.add_argument("--control_host", default="127.0.0.1", help="Control UDP bind host")
    parser.add_argument("--control_port", type=int, default=60001, help="Control UDP bind port; 0 disables")
    parser.add_argument("--rc_rate", type=float, default=DEFAULT_RC_RATE_HZ, help="RC frame rate Hz")
    parser.add_argument("--link_rate", type=float, default=DEFAULT_LINK_RATE_HZ, help="Link statistics rate Hz; 0 disables")
    parser.add_argument("--loop_rate", type=float, default=DEFAULT_LOOP_RATE_HZ, help="Main loop rate Hz")
    parser.add_argument("--reconnect_delay", type=float, default=DEFAULT_RECONNECT_DELAY_S, help="Seconds between TCP retries")
    parser.add_argument("--source_timeout_ms", type=int, default=1000, help="UDP RC silence timeout; 0 disables")
    parser.add_argument(
        "--source_timeout_action",
        choices=("hold", "failsafe", "stop"),
        default="hold",
        help="Action after UDP RC timeout",
    )
    parser.add_argument(
        "--rf_loss_policy",
        choices=("stop", "failsafe"),
        default="stop",
        help="RC output while RF link is dropped",
    )
    parser.add_argument(
        "--channels",
        nargs=CHANNEL_COUNT,
        default=[str(value) for value in DEFAULT_CHANNELS_US],
        metavar="US",
        help="Initial 16 channel values in microseconds",
    )
    parser.add_argument(
        "--failsafe_channels",
        nargs=CHANNEL_COUNT,
        default=[str(value) for value in DEFAULT_FAILSAFE_CHANNELS_US],
        metavar="US",
        help="16 channel values used by failsafe policies",
    )
    parser.add_argument("--telemetry_udp", help="Forward raw inbound CRSF frames to host:port")
    parser.add_argument("--capture", type=Path, help="Append timestamped inbound CRSF frames to this file")
    parser.add_argument("--debug", action="store_true", help="Verbose frame and control logging")
    return parser


def run(args: argparse.Namespace) -> None:
    endpoint_spec = parse_endpoint(args.endpoint)
    if args.rc_rate <= 0:
        raise ConfigurationError(f"rc_rate={args.rc_rate} must_be_positive=1")
    if args.link_rate < 0:
        raise ConfigurationError(f"link_rate={args.link_rate} must_be_nonnegative=1")
    if args.loop_rate <= 0:
        raise ConfigurationError(f"loop_rate={args.loop_rate} must_be_positive=1")
    if args.reconnect_delay < 0:
        raise ConfigurationError(f"reconnect_delay={args.reconnect_delay} must_be_nonnegative=1")

    telemetry_target = parse_host_port(args.telemetry_udp) if args.telemetry_udp else None
    state = ReceiverState(
        channels_us=parse_channels(args.channels),
        failsafe_channels_us=parse_channels(args.failsafe_channels),
        rf_loss_policy=args.rf_loss_policy,
        source_timeout_s=args.source_timeout_ms / 1000.0,
        source_timeout_action=args.source_timeout_action,
    )
    state.validate()

    rc_socket = open_udp_listener(args.host, args.port)
    control_socket = (
        open_udp_listener(args.control_host, args.control_port)
        if args.control_port
        else None
    )
    telemetry_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    capture = CaptureWriter(args.capture)
    endpoint = TcpEndpoint(endpoint_spec)
    connection: socket.socket | None = None
    parser = FrameParser()
    last_connect_attempt = 0.0
    next_rc_at = time.monotonic()
    next_link_at = next_rc_at
    next_status_at = next_rc_at + 1.0
    rc_period = 1.0 / args.rc_rate
    link_period = 1.0 / args.link_rate if args.link_rate else None
    loop_period = 1.0 / args.loop_rate

    print(
        f"Virtual receiver name={args.name!r} endpoint={args.endpoint!r} "
        f"rc_udp={args.host}:{args.port} control_udp="
        f"{args.control_host}:{args.control_port if args.control_port else 'disabled'} "
        f"rc_rate_hz={args.rc_rate} link_rate_hz={args.link_rate}"
    )

    try:
        while True:
            loop_started_at = time.monotonic()
            receive_rc_packets(rc_socket, state, loop_started_at, args.debug)
            if control_socket is not None:
                receive_control_packets(
                    control_socket,
                    state,
                    loop_started_at,
                    connection is not None,
                    args.debug,
                )

            if connection is None and loop_started_at - last_connect_attempt >= args.reconnect_delay:
                last_connect_attempt = loop_started_at
                try:
                    connection = endpoint.open()
                except (OSError, TimeoutError) as error:
                    if args.debug:
                        print(
                            f"TCP pending name={args.name!r} endpoint={args.endpoint!r} "
                            f"error={type(error).__name__}: {error}",
                            file=sys.stderr,
                        )
                else:
                    parser = FrameParser()
                    print(f"TCP connected name={args.name!r} endpoint={args.endpoint!r}")

            if connection is not None:
                try:
                    read_connection(
                        connection,
                        parser,
                        state,
                        telemetry_target,
                        telemetry_socket,
                        capture,
                        args.debug,
                    )

                    if loop_started_at >= next_rc_at:
                        channels = state.output_channels(loop_started_at)
                        if channels is not None:
                            frame = make_rc_frame(channels)
                            connection.sendall(frame)
                            state.transmitted_rc_frames += 1
                            if args.debug:
                                print(
                                    f"TX RC rf_link={state.rf_link} "
                                    f"channels_1_4={channels[:4]} frame_hex={frame.hex()}"
                                )
                        while next_rc_at <= loop_started_at:
                            next_rc_at += rc_period

                    if link_period is not None and loop_started_at >= next_link_at:
                        frame = state.link.frame(state.rf_link)
                        connection.sendall(frame)
                        state.transmitted_link_frames += 1
                        if args.debug:
                            print(
                                f"TX LINK rf_link={state.rf_link} "
                                f"uplink_lq={state.link.uplink_lq if state.rf_link else 0} "
                                f"frame_hex={frame.hex()}"
                            )
                        while next_link_at <= loop_started_at:
                            next_link_at += link_period
                except (BrokenPipeError, ConnectionError, ConnectionResetError, OSError) as error:
                    print(
                        f"TCP disconnected name={args.name!r} endpoint={args.endpoint!r} "
                        f"error={type(error).__name__}: {error}",
                        file=sys.stderr,
                    )
                    connection.close()
                    connection = None
                    last_connect_attempt = loop_started_at

            if args.debug and loop_started_at >= next_status_at:
                print(f"STATUS {json.dumps(state.snapshot(connection is not None), separators=(',', ':'))}")
                next_status_at = loop_started_at + 1.0

            elapsed = time.monotonic() - loop_started_at
            if elapsed < loop_period:
                time.sleep(loop_period - elapsed)
    finally:
        if connection is not None:
            connection.close()
        rc_socket.close()
        if control_socket is not None:
            control_socket.close()
        telemetry_socket.close()
        capture.close()


def main() -> None:
    parser = build_argument_parser()
    args = parser.parse_args()
    try:
        run(args)
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)


if __name__ == "__main__":
    main()
