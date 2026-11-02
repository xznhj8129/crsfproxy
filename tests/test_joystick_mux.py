import sys
import types
import unittest


sys.modules.setdefault("pygame", types.ModuleType("pygame"))

import joystick_crsf


class JoystickMuxTests(unittest.TestCase):
    def test_target_uses_default_port(self):
        self.assertEqual(
            joystick_crsf.parse_target("radio.local"),
            ("radio.local", 60000),
        )

    def test_target_can_include_port(self):
        self.assertEqual(
            joystick_crsf.parse_target("192.168.1.20:60010"),
            ("192.168.1.20", 60010),
        )

    def test_repeated_targets_keep_independent_ports(self):
        targets, remaining = joystick_crsf.parse_target_specs([
            "--target", "127.0.0.1", "--port", "60000",
            "--target", "127.0.0.1", "--port", "60010",
            "--rate", "75",
        ])
        self.assertEqual(targets, [
            ("127.0.0.1", 60000),
            ("127.0.0.1", 60010),
        ])
        self.assertEqual(remaining, ["--rate", "75"])

    def test_no_target_sends_to_localhost(self):
        targets, remaining = joystick_crsf.parse_target_specs([])
        self.assertEqual(targets, [("127.0.0.1", 60000)])
        self.assertEqual(remaining, [])

    def test_port_alone_moves_the_default_target(self):
        targets, remaining = joystick_crsf.parse_target_specs(["--port", "60010"])
        self.assertEqual(targets, [("127.0.0.1", 60010)])
        self.assertEqual(remaining, [])

    def test_explicit_target_replaces_the_default(self):
        targets, remaining = joystick_crsf.parse_target_specs(
            ["--target", "radio.local", "--rate", "75"])
        self.assertEqual(targets, [("radio.local", 60000)])
        self.assertEqual(remaining, ["--rate", "75"])

    def test_invalid_target_port_is_rejected(self):
        with self.assertRaises(ValueError):
            joystick_crsf.parse_target("192.168.1.20:notaport")


if __name__ == "__main__":
    unittest.main()
