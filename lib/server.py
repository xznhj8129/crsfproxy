#!/usr/bin/env python3
"""crsfproxy protocol v1 server loop (run it with crsfproxy.py).

One UDP port carries every user↔server datagram:

- CRSF frames, including RC_CHANNELS_PACKED for RC and the device's own
  telemetry frames;
- CONTROL JSON requests and their CONTROL_REPLY answers;
- STATUS link-health records to the configured downlink destinations.

The server owns one serial or socket:// CRSF device. It serializes CONTROL
operations through one worker thread, enforces one RC owner, writes RC from a
fixed-rate slot schedule, and forwards every valid device frame to the
configured telemetry destinations. See PROTOCOL.md for the contract.
"""

from __future__ import annotations

import json
import queue
import socket
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from enum import Enum, auto

import serial

from . import envelope
from .config import ControlErrorCode, ControlReply, ControlRequest
from .transport import ProxyConfigTransport
from .crsf_protocol import (
    CRSF_CHANNEL_PAYLOAD_SIZE,
    FrameParser,
    FrameType,
    describe_frame,
    validate_frame,
)
from .radio import ConfigService, RadioBackend
from .schedule import DeadlineSchedule

WRITE_TIMEOUT_S = 0.1
CONTROL_CACHE_TTL_S = 60.0
CONTROL_CACHE_SIZE = 64
RAW_QUEUE_SIZE = 64
STATUS_POLL_INTERVAL_S = 1.0
U32_MASK = 0xFFFFFFFF

Peer = tuple[str, int]


class RcResult(Enum):
    ACCEPTED = auto()
    STALE = auto()
    FOREIGN = auto()


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
    telemetry: tuple[tuple[str, int], ...]
    telemetry_raw: tuple[tuple[str, int], ...]
    rc_owner_timeout_ms: int = 1000
    status_hz: float = 5.0
    debug: bool = False


@dataclass
class _ControlState:
    high_water: int | None = None
    cache: OrderedDict[int, tuple[float, ControlReply]] = field(default_factory=OrderedDict)


class ControlSessions:
    """CONTROL exactly-once state, per (peer, session).

    The high-water mark remembers the newest completed request even after its
    reply leaves the bounded cache. The cache itself holds recent replies for
    at least CONTROL_CACHE_TTL_S seconds, capped at CONTROL_CACHE_SIZE entries.
    """

    def __init__(
        self,
        ttl_s: float = CONTROL_CACHE_TTL_S,
        capacity: int = CONTROL_CACHE_SIZE,
    ) -> None:
        self.ttl_s = ttl_s
        self.capacity = capacity
        self._states: dict[tuple[Peer, int], _ControlState] = {}

    def cached(self, peer: Peer, session: int, seq: int, now: float) -> ControlReply | None:
        state = self._states.get((peer, session))
        if state is None:
            return None
        entry = state.cache.get(seq)
        if entry is None:
            return None
        timestamp, reply = entry
        if now - timestamp > self.ttl_s:
            del state.cache[seq]
            return None
        return reply

    def is_new(self, peer: Peer, session: int, seq: int) -> bool:
        state = self._states.get((peer, session))
        if state is None or state.high_water is None:
            return True
        return envelope.is_newer(seq, state.high_water)

    def complete(self, peer: Peer, session: int, seq: int, reply: ControlReply, now: float) -> None:
        key = (peer, session)
        state = self._states.get(key)
        if state is None:
            state = _ControlState()
            self._states[key] = state
        if state.high_water is None or envelope.is_newer(seq, state.high_water):
            state.high_water = seq
        state.cache[seq] = (now, reply)
        state.cache.move_to_end(seq)
        while len(state.cache) > self.capacity:
            state.cache.popitem(last=False)

    def prune(self, now: float) -> None:
        for state in self._states.values():
            expired = [
                seq for seq, (timestamp, _) in state.cache.items()
                if now - timestamp > self.ttl_s
            ]
            for seq in expired:
                del state.cache[seq]


class RcOwner:
    """The one RC sender allowed to drive the device.

    After a release the previous owner's last accepted seq is kept so an old
    retransmission is stale while a newer frame can reacquire ownership.
    """

    def __init__(self, timeout_ms: int) -> None:
        if not timeout_ms > 0:
            raise ValueError(f"rc_owner_timeout_ms={timeout_ms} must_be_positive=1")
        self.timeout_ms = timeout_ms
        self.timeout_s = timeout_ms / 1000.0
        self._owner: Peer | None = None
        self._released_owner: Peer | None = None
        self._released_seq: int | None = None
        self._last_frame: bytes | None = None
        self._last_seq: int | None = None
        self._last_session: int | None = None
        self._last_at: float | None = None

    @property
    def owner(self) -> Peer | None:
        return self._owner

    @property
    def last_frame(self) -> bytes | None:
        return self._last_frame

    @property
    def last_seq(self) -> int | None:
        return self._last_seq

    @property
    def last_session(self) -> int | None:
        return self._last_session

    @property
    def last_at(self) -> float | None:
        return self._last_at

    def is_owner(self, peer: Peer, session: int) -> bool:
        return self._owner == (peer, session)

    def accept(
        self,
        peer: Peer,
        session: int,
        seq: int,
        frame: bytes,
        now: float,
    ) -> RcResult:
        key = (peer, session)
        if self._owner is not None:
            if key != self._owner:
                return RcResult.FOREIGN
            if self._last_seq is not None and not envelope.is_newer(seq, self._last_seq):
                return RcResult.STALE
        elif (self._released_owner == key
              and self._released_seq is not None
              and not envelope.is_newer(seq, self._released_seq)):
            return RcResult.STALE

        self._owner = key
        self._released_owner = None
        self._released_seq = None
        self._last_frame = frame
        self._last_seq = seq
        self._last_session = session
        self._last_at = now
        return RcResult.ACCEPTED

    def release_if_stale(self, now: float) -> bool:
        if self._owner is None or self._last_at is None:
            return False
        if now - self._last_at < self.timeout_s:
            return False
        self._released_owner = self._owner
        self._released_seq = self._last_seq
        self._owner = None
        return True


@dataclass(frozen=True)
class ControlJob:
    peer: Peer
    session: int
    seq: int
    request: ControlRequest


@dataclass(frozen=True)
class ControlDone:
    job: ControlJob
    reply: ControlReply


def _reply_payload(reply: ControlReply) -> bytes:
    """The datagram payload, or a failed reply if it cannot be serialized or fit."""
    try:
        payload = json.dumps(reply.to_json(), separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError) as error:
        return _reply_payload(ControlReply.failure(
            ControlErrorCode.FAILED, f"reply could not be serialized: {error}"))
    if envelope.HEADER_SIZE + len(payload) > envelope.MAX_DATAGRAM:
        return _reply_payload(ControlReply.failure(
            ControlErrorCode.FAILED,
            f"reply length={envelope.HEADER_SIZE + len(payload)} exceeds {envelope.MAX_DATAGRAM}"))
    return payload


# Only a completed success or failure is cached and advances the high-water mark.
CACHEABLE_ERROR_CODES = frozenset({None, ControlErrorCode.FAILED})


class ControlWorker(threading.Thread):
    """Execute CONTROL requests one at a time, off the UDP/serial loop."""

    def __init__(self, service: ConfigService, transport: ProxyConfigTransport) -> None:
        super().__init__(name="control-worker", daemon=True)
        self.service = service
        self.transport = transport
        self.jobs: queue.Queue[ControlJob | None] = queue.Queue()
        self.done: queue.Queue[ControlDone] = queue.Queue()
        self._stopping = threading.Event()
        self.start()

    def submit(self, job: ControlJob) -> None:
        self.jobs.put(job)

    def stop(self) -> None:
        self._stopping.set()
        self.jobs.put(None)
        self.join(timeout=1.0)

    def run(self) -> None:
        while True:
            try:
                job = self.jobs.get(timeout=0.1)
            except queue.Empty:
                if self._stopping.is_set():
                    return
                continue
            if job is None:
                return
            self.done.put(ControlDone(job, self._execute(job)))

    def _execute(self, job: ControlJob) -> ControlReply:
        self.transport.begin_operation()
        try:
            result = self.service.execute(job.request)
        except (KeyError, ValueError) as error:  # unknown parameter, invalid value
            return ControlReply.failure(ControlErrorCode.BAD_REQUEST, error)
        except Exception as error:  # TimeoutError, RuntimeError, device failures
            return ControlReply.failure(ControlErrorCode.FAILED, error)
        finally:
            self.transport.end_operation()
        return ControlReply(result=result)


def _parse_control_request(payload: bytes) -> ControlRequest:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"CONTROL payload is not UTF-8: {error}") from error
    try:
        request = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"CONTROL payload is not JSON: {error}") from error
    except RecursionError as error:
        raise ValueError("CONTROL payload is nested too deeply") from error
    if not isinstance(request, dict):
        raise ValueError("CONTROL payload is not a JSON object")
    return ControlRequest.from_json(request)


def _send_control_reply(
    udp_socket: socket.socket,
    peer: Peer,
    session: int,
    seq: int,
    reply: ControlReply,
    debug: bool,
) -> None:
    try:
        data = envelope.encode(
            envelope.Kind.CONTROL_REPLY, session, seq, _reply_payload(reply))
        udp_socket.sendto(data, peer)
    except (OSError, ValueError) as error:
        print(
            f"CONTROL_REPLY send failed peer={peer} session={session} "
            f"seq={seq} err={error}")
        return
    if debug:
        print(
            f"CONTROL reply peer={peer} session={session} seq={seq} "
            f"ok={reply.ok} code={reply.error_code}")


def _send_datagram(udp_socket: socket.socket, data: bytes, destination: Peer) -> None:
    try:
        udp_socket.sendto(data, destination)
    except OSError as error:
        print(
            f"UDP send failed target={destination} len={len(data)} err={error}")


def _age_ms(timestamp: float | None, now: float) -> int:
    if timestamp is None:
        return envelope.STATUS_NO_VALUE
    age = int(max(0.0, now - timestamp) * 1000.0)
    return min(age, envelope.STATUS_NO_VALUE - 1)


def run_proxy(options: ProxyOptions, radio: RadioBackend) -> None:
    loop_period = 1.0 / options.loop_hz

    print(f"Listening for UDP on {options.host}:{options.port}")
    print(
        f"{radio.name} on {options.device} at {options.baud} baud, "
        f"{options.tx_rate:g} Hz max")
    print(
        "RC hold disabled" if options.rc_hold_ms == 0
        else f"RC hold {options.rc_hold_ms} ms")
    print(f"Telemetry to: {', '.join(options.telemetry) or 'none'}")
    print(f"Raw telemetry to: {', '.join(options.telemetry_raw) or 'none'}")

    serial_port = None
    udp_socket = None
    worker = None
    try:
        serial_port = serial.serial_for_url(options.device, do_not_open=True)
        serial_port.baudrate = options.baud
        serial_port.timeout = 0
        serial_port.write_timeout = WRITE_TIMEOUT_S
        serial_port.bytesize = serial.EIGHTBITS
        serial_port.parity = serial.PARITY_NONE
        serial_port.stopbits = serial.STOPBITS_ONE
        serial_port.xonxoff = False
        serial_port.rtscts = False
        serial_port.dsrdtr = False
        serial_port.rts = False
        serial_port.dtr = False
        serial_port.open()
        serial_port.reset_input_buffer()
        serial_port.reset_output_buffer()
        if options.debug:
            print(
                f"Opened serial device={options.device} "
                f"baud_request={options.baud} "
                f"baud_open={serial_port.baudrate} "
                f"timeout={serial_port.timeout} "
                f"write_timeout={serial_port.write_timeout} "
                f"rts={serial_port.rts} "
                f"dtr={serial_port.dtr}")

        udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        udp_socket.bind((options.host, options.port))
        udp_socket.setblocking(False)

        frame_parser = FrameParser(radio.frame_addresses)
        config_transport = radio.transport_type()
        service = radio.service_type(config_transport)
        worker = ControlWorker(service, config_transport)

        server_session = envelope.new_session()
        crsf_seq = 0
        status_seq = 0
        sessions = ControlSessions()
        owner = RcOwner(options.rc_owner_timeout_ms)
        running: ControlJob | None = None

        rc_accepted = 0
        rc_stale = 0
        rc_foreign = 0
        device_rx_at = None
        raw_frames: deque[bytes] = deque()

        last_written_session = None
        last_written_seq = None
        last_rc_state = None
        last_slot_debug_at = 0.0

        start = time.monotonic()
        rc_schedule = DeadlineSchedule(options.tx_rate, start)
        status_schedule = DeadlineSchedule(options.status_hz, start)
        next_status_poll_at = start

        if options.debug:
            print(f"Server session={server_session}")

        while True:
            loop_start = time.monotonic()
            now = loop_start

            released_owner = owner.owner
            if owner.release_if_stale(now):
                if options.debug:
                    print(
                        f"RC owner released peer={released_owner} "
                        f"session={owner.last_session} seq={owner.last_seq} "
                        f"idle_ms={_age_ms(owner.last_at, now)}")
                last_rc_state = None

            # Drain every waiting datagram before reading the device.
            while True:
                try:
                    data, sender = udp_socket.recvfrom(envelope.MAX_DATAGRAM)
                except (BlockingIOError, InterruptedError):
                    break
                peer = (sender[0], sender[1])
                try:
                    message = envelope.decode(data)
                except ValueError as error:
                    print(f"Datagram rejected peer={peer} reason={error}")
                    continue

                if message.kind in (envelope.Kind.CONTROL_REPLY, envelope.Kind.STATUS):
                    print(
                        f"Datagram rejected peer={peer} kind={message.kind.name} "
                        f"reason=invalid_direction")
                    continue

                if message.kind == envelope.Kind.CRSF:
                    raw = message.payload
                    if not validate_frame(raw):
                        print(
                            f"CRSF frame rejected peer={peer} len={len(raw)} "
                            f"reason=invalid_frame")
                        continue
                    if (raw[2] == FrameType.RC_CHANNELS_PACKED
                            and len(raw) - 4 != CRSF_CHANNEL_PAYLOAD_SIZE):
                        print(
                            f"CRSF frame rejected peer={peer} len={len(raw)} "
                            f"reason=rc_payload_size={len(raw) - 4}")
                        continue

                    if raw[2] == FrameType.RC_CHANNELS_PACKED:
                        was_owner = owner.owner
                        result = owner.accept(
                            peer, message.session, message.seq, raw, now)
                        if result == RcResult.ACCEPTED:
                            rc_accepted = (rc_accepted + 1) & U32_MASK
                            if options.debug and owner.owner != was_owner:
                                print(
                                    f"RC owner peer={peer} "
                                    f"session={message.session} seq={message.seq}")
                        elif result == RcResult.STALE:
                            rc_stale = (rc_stale + 1) & U32_MASK
                            if options.debug:
                                print(
                                    f"RC stale peer={peer} "
                                    f"session={message.session} seq={message.seq}")
                        else:
                            rc_foreign = (rc_foreign + 1) & U32_MASK
                            if options.debug:
                                print(
                                    f"RC foreign peer={peer} "
                                    f"session={message.session} seq={message.seq}")
                        continue

                    if running is not None:
                        print(
                            f"CRSF non-RC dropped peer={peer} "
                            f"type=0x{raw[2]:02X} reason=control_running")
                        continue
                    if not owner.is_owner(peer, message.session):
                        print(
                            f"CRSF non-RC dropped peer={peer} "
                            f"type=0x{raw[2]:02X} reason=not_rc_owner")
                        continue
                    if len(raw_frames) >= RAW_QUEUE_SIZE:
                        print(
                            f"CRSF non-RC dropped peer={peer} "
                            f"type=0x{raw[2]:02X} reason=raw_queue_full")
                        continue
                    raw_frames.append(raw)
                    if options.debug:
                        print(
                            f"CRSF non-RC queued peer={peer} "
                            f"type=0x{raw[2]:02X} len={len(raw)}")
                    continue

                # message.kind == envelope.Kind.CONTROL
                if (running is not None
                        and running.peer == peer
                        and running.session == message.session
                        and running.seq == message.seq):
                    if options.debug:
                        print(
                            f"CONTROL duplicate-running ignored peer={peer} "
                            f"session={message.session} seq={message.seq}")
                    continue
                cached = sessions.cached(
                    peer, message.session, message.seq, now)
                if cached is not None:
                    if options.debug:
                        print(
                            f"CONTROL cached replay peer={peer} "
                            f"session={message.session} seq={message.seq}")
                    _send_control_reply(
                        udp_socket, peer, message.session, message.seq,
                        cached, options.debug)
                    continue
                if not sessions.is_new(peer, message.session, message.seq):
                    _send_control_reply(
                        udp_socket, peer, message.session, message.seq,
                        ControlReply.failure(
                            ControlErrorCode.STALE,
                            "request seq is older than a completed request"),
                        options.debug)
                    continue
                try:
                    request = _parse_control_request(message.payload)
                except ValueError as error:
                    _send_control_reply(
                        udp_socket, peer, message.session, message.seq,
                        ControlReply.failure(ControlErrorCode.BAD_REQUEST, error),
                        options.debug)
                    continue
                if running is not None:
                    _send_control_reply(
                        udp_socket, peer, message.session, message.seq,
                        ControlReply.failure(ControlErrorCode.BUSY, "another CONTROL request is running"),
                        options.debug)
                    continue

                job = ControlJob(peer, message.session, message.seq, request)
                running = job
                worker.submit(job)
                if options.debug:
                    print(
                        f"CONTROL request peer={peer} session={message.session} "
                        f"seq={message.seq} command={request.command.value}")

            # Read the device with a zero-timeout block read. pyserial's
            # socket:// reports in_waiting as 1 for any readable socket, so
            # in_waiting would force one-byte reads.
            try:
                chunk = serial_port.read(256)
            except OSError as error:
                print(
                    f"Serial read failed port={options.device} "
                    f"baud={options.baud} err={error}")
                raise

            frames = []
            if chunk:
                if chunk.startswith(b"$X"):
                    # Some devices prepend 8 bytes of junk.
                    chunk = chunk[8:]
                discarded_before = frame_parser.bytes_discarded
                crc_errors_before = frame_parser.crc_errors
                frames = frame_parser.feed(chunk)
                if options.debug and chunk:
                    print(
                        f"Serial rx chunk_len={len(chunk)} "
                        f"buffer_len={len(frame_parser.buffer)} "
                        f"chunk_hex={chunk.hex()}")
                if options.debug and frame_parser.bytes_discarded != discarded_before:
                    print(
                        f"Serial discard "
                        f"discard_len={frame_parser.bytes_discarded - discarded_before} "
                        f"crc_errors={frame_parser.crc_errors - crc_errors_before}")

            for frame in frames:
                try:
                    config_transport.observe(frame)
                except (IndexError, ValueError, ZeroDivisionError) as error:
                    print(
                        f"Device frame observe failed address=0x{frame.address:02X} "
                        f"type=0x{frame.type:02X} len={len(frame.raw)} err={error}")
                device_rx_at = now
                if options.debug:
                    try:
                        description = describe_frame(frame)
                    except (IndexError, ValueError, ZeroDivisionError) as error:
                        description = (
                            f"TEL unparsed type=0x{frame.type:02X} "
                            f"len={len(frame.raw)} err={error}")
                    print(
                        f"Serial frame address=0x{frame.address:02X} "
                        f"type=0x{frame.type:02X} frame_hex={frame.raw.hex()} "
                        f"{description}")

                for destination in options.telemetry:
                    _send_datagram(
                        udp_socket,
                        envelope.encode(
                            envelope.Kind.CRSF, server_session, crsf_seq,
                            frame.raw),
                        destination)
                for destination in options.telemetry_raw:
                    _send_datagram(udp_socket, frame.raw, destination)
                crsf_seq = (crsf_seq + 1) & U32_MASK

            while True:
                try:
                    done = worker.done.get_nowait()
                except queue.Empty:
                    break
                reply = done.reply
                if reply.error_code in CACHEABLE_ERROR_CODES:
                    sessions.complete(
                        done.job.peer, done.job.session, done.job.seq,
                        reply, now)
                _send_control_reply(
                    udp_socket, done.job.peer, done.job.session,
                    done.job.seq, reply, options.debug)
                if running is not None and (
                        running.peer == done.job.peer
                        and running.session == done.job.session
                        and running.seq == done.job.seq):
                    running = None

            sessions.prune(now)

            if status_schedule.due(now):
                status = envelope.Status(
                    rc_age_ms=_age_ms(owner.last_at, now),
                    rc_session=owner.last_session or 0,
                    rc_seq=owner.last_seq or 0,
                    rc_accepted=rc_accepted,
                    rc_stale=rc_stale,
                    rc_foreign=rc_foreign,
                    device_rx_age_ms=_age_ms(device_rx_at, now),
                )
                payload = status.pack()
                for destination in options.telemetry:
                    _send_datagram(
                        udp_socket,
                        envelope.encode(
                            envelope.Kind.STATUS, server_session, status_seq,
                            payload),
                        destination)
                status_seq = (status_seq + 1) & U32_MASK

            if rc_schedule.due(now):
                if (radio.status_poll_frame is not None
                        and now >= next_status_poll_at):
                    config_transport.queue(radio.status_poll_frame)
                    next_status_poll_at = now + STATUS_POLL_INTERVAL_S

                active_frame = None
                elapsed_ms = 0.0
                if owner.last_frame is None:
                    control_state = "waiting_rc"
                else:
                    elapsed_ms = max(0.0, now - owner.last_at) * 1000.0
                    sample_changed = (
                        owner.last_seq != last_written_seq
                        or owner.last_session != last_written_session)
                    if sample_changed:
                        control_state = "live"
                        active_frame = owner.last_frame
                    elif (options.rc_hold_ms > 0
                          and elapsed_ms < options.rc_hold_ms):
                        control_state = "hold"
                        active_frame = owner.last_frame
                    else:
                        control_state = "idle"

                if options.debug and control_state != last_rc_state:
                    print(
                        f"RC state={control_state} "
                        f"peer={owner.owner} "
                        f"session={owner.last_session} seq={owner.last_seq} "
                        f"elapsed_ms={elapsed_ms:.1f}")
                    last_rc_state = control_state

                raw_count = len(raw_frames)
                raw_burst = b"".join(raw_frames)
                raw_frames.clear()
                control_frames = config_transport.drain_outbound()
                parts = []
                if active_frame is not None:
                    parts.append(active_frame)
                if raw_burst:
                    parts.append(raw_burst)
                parts.extend(control_frames)
                burst = b"".join(parts)

                if burst:
                    try:
                        written = serial_port.write(burst)
                    except serial.SerialTimeoutException as error:
                        print(
                            f"Serial write timeout port={options.device} "
                            f"baud={options.baud} burst_len={len(burst)} "
                            f"elapsed_ms={elapsed_ms:.1f} err={error}")
                        written = 0
                    if active_frame is not None:
                        last_written_session = owner.last_session
                        last_written_seq = owner.last_seq
                    if options.debug:
                        print(
                            f"Serial tx state={control_state} "
                            f"rc_len={len(active_frame) if active_frame else 0} "
                            f"raw_frames={raw_count} "
                            f"control_frames={len(control_frames)} "
                            f"burst_len={len(burst)} written={written}"
                            + (f" frame_hex={active_frame.hex()}"
                               if active_frame is not None else ""))

                if options.debug and now - last_slot_debug_at >= 0.5:
                    print(
                        f"RC slot state={control_state} "
                        f"peer={owner.owner} "
                        f"session={owner.last_session} seq={owner.last_seq} "
                        f"elapsed_ms={elapsed_ms:.1f} "
                        f"accepted={rc_accepted} stale={rc_stale} "
                        f"foreign={rc_foreign}")
                    last_slot_debug_at = now

            loop_elapsed = time.monotonic() - loop_start
            if loop_elapsed < loop_period:
                time.sleep(loop_period - loop_elapsed)

    except KeyboardInterrupt:
        print("Shutdown requested.")
    finally:
        if worker is not None:
            worker.stop()
        if udp_socket is not None:
            try:
                udp_socket.close()
            except OSError as error:
                print(f"UDP socket close failed err={error}")
        if serial_port is not None:
            try:
                serial_port.close()
            except OSError as error:
                print(f"Serial close failed port={options.device} err={error}")
