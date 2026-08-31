import json
import unittest

import config_client
from lib import unwrap, wrap


class ConfigClientMuxTests(unittest.TestCase):
    def test_request_has_target_sys_id(self):
        packet = config_client._request_payload(9, {"command": "info"})
        sys_id, payload = unwrap(packet)
        self.assertEqual(sys_id, 9)
        self.assertEqual(json.loads(payload), {"command": "info"})

    def test_response_has_source_sys_id(self):
        response = wrap(12, json.dumps({"ok": True, "result": {"x": 1}}).encode())
        sys_id, decoded = config_client._decode_response(response)
        self.assertEqual(sys_id, 12)
        self.assertTrue(decoded["ok"])
        self.assertEqual(decoded["result"], {"x": 1})

    def test_response_source_zero_is_rejected(self):
        response = wrap(0, json.dumps({"ok": True, "result": {}}).encode())
        with self.assertRaises(ValueError):
            config_client._decode_response(response)


if __name__ == "__main__":
    unittest.main()
