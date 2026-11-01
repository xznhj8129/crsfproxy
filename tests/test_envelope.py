import struct
import unittest

from lib.envelope import (
    STATUS_NO_VALUE,
    Kind,
    Status,
    VERSION,
    decode,
    encode,
    is_newer,
    new_session,
)


class EnvelopeTests(unittest.TestCase):
    def test_encode_decode_round_trip(self) -> None:
        payload = b'{"command":"info"}'
        data = encode(Kind.CONTROL, 0x12345678, 7, payload)
        self.assertEqual(data[0], VERSION)
        self.assertEqual(data[1], int(Kind.CONTROL))
        envelope = decode(data)
        self.assertEqual(envelope.version, VERSION)
        self.assertEqual(envelope.kind, Kind.CONTROL)
        self.assertEqual(envelope.session, 0x12345678)
        self.assertEqual(envelope.seq, 7)
        self.assertEqual(envelope.payload, payload)

    def test_header_is_little_endian(self) -> None:
        data = encode(Kind.STATUS, 0x01020304, 0x05060708, b"")
        self.assertEqual(
            data, struct.pack("<BBII", 1, int(Kind.STATUS), 0x01020304, 0x05060708))

    def test_decode_rejects_short_datagram(self) -> None:
        with self.assertRaises(ValueError):
            decode(b"\x01" * 9)

    def test_decode_rejects_wrong_version(self) -> None:
        data = struct.pack("<BBII", 2, int(Kind.CRSF), 1, 0)
        with self.assertRaises(ValueError):
            decode(data)

    def test_decode_rejects_zero_session(self) -> None:
        data = struct.pack("<BBII", VERSION, int(Kind.CRSF), 0, 0)
        with self.assertRaises(ValueError):
            decode(data)

    def test_decode_rejects_unknown_kind(self) -> None:
        data = struct.pack("<BBII", VERSION, 9, 1, 0)
        with self.assertRaises(ValueError):
            decode(data)

    def test_encode_rejects_zero_session(self) -> None:
        with self.assertRaises(ValueError):
            encode(Kind.CRSF, 0, 0)

    def test_new_session_is_nonzero_u32(self) -> None:
        for _ in range(100):
            session = new_session()
            self.assertGreaterEqual(session, 1)
            self.assertLessEqual(session, 0xFFFFFFFF)

    def test_is_newer_handles_wrap(self) -> None:
        self.assertTrue(is_newer(1, 0))
        self.assertFalse(is_newer(0, 1))
        self.assertFalse(is_newer(0, 0))
        self.assertTrue(is_newer(0, 0xFFFFFFFF))
        self.assertFalse(is_newer(0xFFFFFFFF, 0))
        self.assertTrue(is_newer(5, 0xFFFFFFFE))
        self.assertTrue(is_newer(0x7FFFFFFF, 0))
        self.assertFalse(is_newer(0x80000000, 0))
        self.assertFalse(is_newer(0, 0x80000000))


class StatusTests(unittest.TestCase):
    def test_status_round_trip(self) -> None:
        status = Status(
            rc_age_ms=12,
            rc_session=34,
            rc_seq=56,
            rc_accepted=78,
            rc_stale=90,
            rc_foreign=100,
            device_rx_age_ms=STATUS_NO_VALUE,
        )
        data = status.pack()
        self.assertEqual(len(data), 28)
        self.assertEqual(Status.unpack(data), status)

    def test_status_defaults_use_sentinels(self) -> None:
        status = Status()
        self.assertEqual(status.rc_age_ms, STATUS_NO_VALUE)
        self.assertEqual(status.rc_session, 0)
        self.assertEqual(status.rc_seq, 0)
        self.assertEqual(status.device_rx_age_ms, STATUS_NO_VALUE)

    def test_status_unpack_rejects_wrong_length(self) -> None:
        with self.assertRaises(ValueError):
            Status.unpack(b"\x00" * 27)

    def test_status_pack_rejects_out_of_range(self) -> None:
        with self.assertRaises(ValueError):
            Status(rc_age_ms=-1).pack()
        with self.assertRaises(ValueError):
            Status(rc_accepted=0x100000000).pack()


if __name__ == "__main__":
    unittest.main()
