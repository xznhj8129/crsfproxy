"""Configuration transport between crsfproxy's serial loop and a service.

The serial loop feeds every valid inbound frame to ``observe``; the service
queues CRSF frames with ``queue`` and collects device replies with ``poll``.
The server drains queued frames into its scheduled serial write.
"""

from __future__ import annotations

import queue
import threading
import time

from .crsf_protocol import (
    CRSF_ADDRESS_ELRS_LUA,
    CRSF_ADDRESS_RADIO_TRANSMITTER,
    CRSF_ADDRESS_TRANSMITTER,
    Frame as CrsfFrame,
    FrameType,
)


class ProxyConfigTransport:
    """Thread-safe ELRS Lua transport driven by crsfproxy's serial loop."""

    INBOUND_QUEUE_SIZE = 256

    def __init__(self) -> None:
        self.inbound: queue.Queue[CrsfFrame] = queue.Queue(
            maxsize=self.INBOUND_QUEUE_SIZE)
        self.outbound: queue.Queue[bytes] = queue.Queue()
        self.state_lock = threading.Lock()
        self.radio_rate_hz = None
        self.status = None
        # Device replies are only queued while a CONTROL operation runs, so an
        # operation never reads replies meant for an earlier operation or for
        # another configurator (e.g. a radio's elrs.lua through the RC owner).
        self._accepting = False
        self.dropped = 0

    def begin_operation(self) -> None:
        self.poll()
        self._accepting = True

    def end_operation(self) -> None:
        self._accepting = False
        self.poll()

    def deliver(self, frame: CrsfFrame) -> None:
        """Queue a device reply for the running operation."""
        if not self._accepting:
            return
        try:
            self.inbound.put_nowait(frame)
        except queue.Full:
            self.dropped += 1
            print(f"Config reply dropped type=0x{frame.type:02X} len={len(frame.raw)} "
                  f"queue_size={self.INBOUND_QUEUE_SIZE} dropped_total={self.dropped}")

    def origin(self, device_address: int) -> int:
        if device_address == CRSF_ADDRESS_TRANSMITTER:
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
        if frame.type in (
                FrameType.DEVICE_INFO,
                FrameType.PARAMETER_SETTINGS_ENTRY,
                FrameType.PARAMETER_WRITE):
            self.deliver(frame)

    def snapshot(self) -> dict:
        with self.state_lock:
            return {
                "radio_rate_hz": self.radio_rate_hz,
                "binding": self.status,
            }
