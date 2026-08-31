import sys
import types
import unittest


sys.modules.setdefault("pygame", types.ModuleType("pygame"))

import joystick_crsf


class JoystickMuxTests(unittest.TestCase):
    def test_target_uses_default_port(self):
        self.assertEqual(
            joystick_crsf.parse_target("radio.local", 60000),
            ("radio.local", 60000),
        )

    def test_target_can_override_port(self):
        self.assertEqual(
            joystick_crsf.parse_target("192.168.1.20:60002", 60000),
            ("192.168.1.20", 60002),
        )

    def test_invalid_target_port_is_rejected(self):
        with self.assertRaises(ValueError):
            joystick_crsf.parse_target("192.168.1.20:notaport", 60000)

    def test_each_target_gets_its_own_port_and_sys_id(self):
        targets, remaining = joystick_crsf.parse_target_specs([
            "--target", "127.0.0.1",
            "--sys_id", "1",
            "--port", "60000",
            "--target", "127.0.0.1",
            "--port", "60010",
            "--sys-id", "2",
            "--rate", "75",
        ])
        self.assertEqual(targets, [
            ("127.0.0.1", 60000, 1),
            ("127.0.0.1", 60010, 2),
        ])
        self.assertEqual(remaining, ["--rate", "75"])

    def test_target_defaults_are_per_target(self):
        targets, remaining = joystick_crsf.parse_target_specs([
            "--target", "radio-a.local",
            "--target", "radio-b.local:60010",
            "--sys-id", "7",
        ])
        self.assertEqual(targets, [
            ("radio-a.local", 60000, 1),
            ("radio-b.local", 60010, 7),
        ])
        self.assertEqual(remaining, [])

    def test_port_before_target_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "--port must follow a --target"):
            joystick_crsf.parse_target_specs(["--port", "60010", "--target", "localhost"])


if __name__ == "__main__":
    unittest.main()
