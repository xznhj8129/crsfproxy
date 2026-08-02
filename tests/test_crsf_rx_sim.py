import json
import struct
import time
import unittest
import zlib

from crsf_protocol import FrameParser, FrameType
from crsf_rx_sim import (
    DEFAULT_FAILSAFE_CHANNELS_US,
    LinkStatistics,
    ReceiverState,
    apply_control_request,
    decode_udp_rc_packet,
    parse_control_request,
    parse_endpoint,
)


class ReceiverSimulatorTests(unittest.TestCase):
    def test_parse_tcp_endpoint(self) -> None:
        endpoint = parse_endpoint("tcp://127.0.0.1:5763")
        self.assertEqual(endpoint.mode, "tcp")
        self.assertEqual(endpoint.host, "127.0.0.1")
        self.assertEqual(endpoint.port, 5763)

    def test_decode_udp_rc_packet(self) -> None:
        channels = [1000 + index * 50 for index in range(16)]
        payload = struct.pack("<I16H", 123456, *channels)
        packet = payload + struct.pack("<I", zlib.crc32(payload) & 0xFFFFFFFF)
        timestamp_ms, decoded = decode_udp_rc_packet(packet)
        self.assertEqual(timestamp_ms, 123456)
        self.assertEqual(decoded, channels)

    def test_decode_udp_rc_packet_rejects_bad_crc(self) -> None:
        payload = struct.pack("<I16H", 1, *([1500] * 16))
        with self.assertRaisesRegex(ValueError, "packet_crc"):
            decode_udp_rc_packet(payload + b"\x00\x00\x00\x00")

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
        apply_control_request(state, {"action": "drop_rf"}, time.monotonic(), True)
        self.assertFalse(state.rf_link)
        apply_control_request(state, {"action": "restore-rf"}, time.monotonic(), True)
        self.assertTrue(state.rf_link)

    def test_control_updates_link_values(self) -> None:
        state = ReceiverState()
        result = apply_control_request(
            state,
            {"action": "set_link", "uplink_lq": 42, "uplink_snr": -11},
            time.monotonic(),
            True,
        )
        self.assertEqual(state.link.uplink_lq, 42)
        self.assertEqual(state.link.uplink_snr, -11)
        self.assertEqual(result["link"]["uplink_lq"], 42)

    def test_shell_control_request(self) -> None:
        self.assertEqual(parse_control_request(b"drop-rf"), {"action": "drop_rf"})
        self.assertEqual(
            parse_control_request(b"set uplink_lq 25"),
            {"action": "set_link", "uplink_lq": 25},
        )

    def test_json_control_request(self) -> None:
        request = parse_control_request(
            json.dumps({"action": "set_link", "uplink_lq": 12}).encode()
        )
        self.assertEqual(request["uplink_lq"], 12)


if __name__ == "__main__":
    unittest.main()
