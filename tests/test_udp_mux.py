import unittest

from lib import (
    SYS_ID_BROADCAST,
    accepts_sys_id,
    unwrap,
    validate_local_sys_id,
    validate_target_sys_id,
    wrap,
)


class UdpMuxTests(unittest.TestCase):
    def test_wrap_and_unwrap(self):
        packet = wrap(17, b"payload")
        self.assertEqual(packet, b"\x11payload")
        self.assertEqual(unwrap(packet), (17, b"payload"))

    def test_broadcast_is_valid_target(self):
        self.assertEqual(validate_target_sys_id(SYS_ID_BROADCAST), 0)
        self.assertTrue(accepts_sys_id(42, 0))

    def test_specific_id_only_matches_itself(self):
        self.assertTrue(accepts_sys_id(42, 42))
        self.assertFalse(accepts_sys_id(42, 41))

    def test_local_id_cannot_be_broadcast(self):
        with self.assertRaises(ValueError):
            validate_local_sys_id(0)

    def test_255_is_reserved(self):
        with self.assertRaises(ValueError):
            validate_target_sys_id(255)
        with self.assertRaises(ValueError):
            validate_local_sys_id(255)

    def test_empty_datagram_is_invalid(self):
        with self.assertRaises(ValueError):
            unwrap(b"")


if __name__ == "__main__":
    unittest.main()
