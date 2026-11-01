import json
import socket
import time
import unittest

from lib import envelope
from lib.crsf_protocol import FrameParser, FrameType, make_frame, make_rc_frame
from crsf_rx_sim import (
    DEFAULT_FAILSAFE_CHANNELS_US,
    LinkStatistics,
    ReceiverState,
    apply_control_request,
    decode_rc_frame,
    open_udp_listener,
    parse_control_request,
    parse_endpoint,
    receive_packets,
)


CHANNELS = [1000, 1050, 1100, 1150, 1200, 1250, 1300, 1350,
            1400, 1450, 1500, 1550, 1600, 1650, 1700, 1750]


class ReceiverSimulatorTests(unittest.TestCase):
    def test_parse_tcp_endpoint(self) -> None:
        endpoint = parse_endpoint("tcp://127.0.0.1:5763")
        self.assertEqual(endpoint.mode, "tcp")
        self.assertEqual(endpoint.host, "127.0.0.1")
        self.assertEqual(endpoint.port, 5763)

    def test_decode_rc_frame(self) -> None:
        frame = make_rc_frame(CHANNELS)
        self.assertEqual(decode_rc_frame(frame), CHANNELS)

    def test_decode_rc_frame_rejects_bad_crc(self) -> None:
        frame = bytearray(make_rc_frame(CHANNELS))
        frame[-1] ^= 0xFF
        with self.assertRaisesRegex(ValueError, "invalid CRSF frame"):
            decode_rc_frame(bytes(frame))

    def test_decode_rc_frame_rejects_other_frame_types(self) -> None:
        frame = make_frame(FrameType.LINK_STATISTICS, bytes(10))
        with self.assertRaisesRegex(ValueError, "frame_type"):
            decode_rc_frame(frame)

    def test_link_statistics_frame_layout(self) -> None:
        link = LinkStatistics(
            uplink_rssi_ant1=55,
            uplink_rssi_ant2=58,
            uplink_lq=99,
            uplink_snr=-7,
            active_antenna=1,
            rf_mode=7,
            uplink_tx_power=3,
            downlink_rssi=62,
            downlink_lq=88,
            downlink_snr=6,
        )
        frame = FrameParser().feed(link.frame(True))[0]
        self.assertEqual(frame.type, FrameType.LINK_STATISTICS)
        self.assertEqual(frame.payload, bytes([55, 58, 99, 0xF9, 1, 7, 3, 62, 88, 6]))

    def test_dropped_rf_zeros_link_quality(self) -> None:
        frame = FrameParser().feed(LinkStatistics().frame(False))[0]
        self.assertEqual(frame.payload[2], 0)
        self.assertEqual(frame.payload[8], 0)

    def test_rf_stop_policy_stops_rc_frames(self) -> None:
        state = ReceiverState(rf_link=False, rf_loss_policy="stop")
        self.assertIsNone(state.output_channels(time.monotonic()))

    def test_rf_failsafe_policy_emits_failsafe_channels(self) -> None:
        state = ReceiverState(rf_link=False, rf_loss_policy="failsafe")
        self.assertEqual(state.output_channels(time.monotonic()), DEFAULT_FAILSAFE_CHANNELS_US)

    def test_source_timeout_stop_is_distinct_from_rf_loss(self) -> None:
        now = time.monotonic()
        state = ReceiverState(source_timeout_s=0.1, source_timeout_action="stop")
        state.accept_rc(10, [1500] * 16, now - 1.0)
        self.assertTrue(state.rf_link)
        self.assertIsNone(state.output_channels(now))

    def test_control_drop_and_restore_rf(self) -> None:
        state = ReceiverState()
        apply_control_request(state, {"command": "drop_rf"}, time.monotonic(), True)
        self.assertFalse(state.rf_link)
        apply_control_request(state, {"command": "restore-rf"}, time.monotonic(), True)
        self.assertTrue(state.rf_link)

    def test_control_updates_link_values(self) -> None:
        state = ReceiverState()
        result = apply_control_request(
            state,
            {"command": "set_link", "uplink_lq": 42, "uplink_snr": -11},
            time.monotonic(),
            True,
        )
        self.assertEqual(state.link.uplink_lq, 42)
        self.assertEqual(state.link.uplink_snr, -11)
        self.assertEqual(result["link"]["uplink_lq"], 42)

    def test_control_requires_json(self) -> None:
        with self.assertRaises(ValueError):
            parse_control_request(b"drop-rf")
        with self.assertRaises(ValueError):
            parse_control_request(b"{}")
        request = parse_control_request(
            json.dumps({"command": "set_link", "uplink_lq": 12}).encode()
        )
        self.assertEqual(request["uplink_lq"], 12)

    def test_udp_envelope_round_trip(self) -> None:
        listener = open_udp_listener("127.0.0.1", 0)
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.bind(("127.0.0.1", 0))
        sender.settimeout(0.05)
        state = ReceiverState()
        session = 0x11223344
        try:
            target = ("127.0.0.1", listener.getsockname()[1])
            sender.sendto(
                envelope.encode(
                    envelope.Kind.CRSF, session, 0, make_rc_frame(CHANNELS)),
                target,
            )
            deadline = time.monotonic() + 1.0
            while state.accepted_rc_packets == 0 and time.monotonic() < deadline:
                receive_packets(listener, state, time.monotonic(), False, False)
                if state.accepted_rc_packets == 0:
                    time.sleep(0.005)
            self.assertEqual(state.accepted_rc_packets, 1)
            self.assertEqual(state.channels_us, CHANNELS)
            self.assertIsNotNone(state.last_rc_source_timestamp_ms)

            request = {"command": "set_link", "uplink_lq": 42, "uplink_rssi_ant1": 99}
            sender.sendto(
                envelope.encode(
                    envelope.Kind.CONTROL, session, 9,
                    json.dumps(request).encode("utf-8")),
                target,
            )
            reply = None
            deadline = time.monotonic() + 1.0
            while reply is None and time.monotonic() < deadline:
                receive_packets(listener, state, time.monotonic(), False, False)
                try:
                    data, _ = sender.recvfrom(65535)
                except socket.timeout:
                    continue
                reply = envelope.decode(data)
            self.assertIsNotNone(reply)
            self.assertEqual(reply.kind, envelope.Kind.CONTROL_REPLY)
            self.assertEqual((reply.session, reply.seq), (session, 9))
            decoded = json.loads(reply.payload)
            self.assertTrue(decoded["ok"])
            self.assertEqual(decoded["result"]["link"]["uplink_lq"], 42)
            self.assertEqual(state.link.uplink_lq, 42)
        finally:
            sender.close()
            listener.close()


if __name__ == "__main__":
    unittest.main()
