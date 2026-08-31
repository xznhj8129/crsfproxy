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


if __name__ == "__main__":
    unittest.main()
