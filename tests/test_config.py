import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crsfproxy import (
    ConfigService,
    CrsfFrame,
    DeviceInfo,
    FrameType,
    Parameter,
    ParameterType,
    ProxyConfigTransport,
    crc8_command,
    negotiate_serial_speed,
    parse_config_request,
)


class FakeSerial:
    def __init__(self, response: bytes) -> None:
        self.baudrate = 115200
        self.response = bytearray(response)
        self.writes = []

    @property
    def in_waiting(self) -> int:
        return len(self.response)

    def read(self, size: int) -> bytes:
        data = bytes(self.response[:size])
        del self.response[:size]
        return data

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        return len(data)

    def flush(self) -> None:
        pass


class FakeClient:
    def __init__(self, device, parameters) -> None:
        self.device = device
        self.parameters = parameters

    def discover(self, seconds):
        return {self.device.address: self.device}

    def read_all(self, device):
        return self.parameters

    def find(self, device, name_or_id):
        for parameter in self.parameters:
            if str(parameter.id) == name_or_id or parameter.name == name_or_id:
                return parameter
        raise KeyError(name_or_id)


class ConfigTests(unittest.TestCase):
    def test_command_crc_matches_crsf_bind_vector(self) -> None:
        self.assertEqual(crc8_command(bytes.fromhex("32ecc81001")), 0x9E)

    def test_speed_negotiation_switches_after_accepted_response(self) -> None:
        serial_port = FakeSerial(bytes.fromhex("ea0932eaee0a7100014cde"))
        negotiate_serial_speed(serial_port, 460800)
        self.assertEqual(
            serial_port.writes,
            [bytes.fromhex("c80c32eeea0a700000070800344b")],
        )
        self.assertEqual(serial_port.baudrate, 460800)

    def test_speed_negotiation_does_not_switch_after_rejection(self) -> None:
        serial_port = FakeSerial(bytes.fromhex("ea0932eaee0a710000f69a"))
        with self.assertRaisesRegex(RuntimeError, "response=0"):
            negotiate_serial_speed(serial_port, 460800)
        self.assertEqual(serial_port.baudrate, 115200)

    def test_speed_negotiation_rejects_bad_command_crc(self) -> None:
        serial_port = FakeSerial(bytes.fromhex("ea0932eaee0a7100010094"))
        with self.assertRaisesRegex(ValueError, "command_crc=0x00"):
            negotiate_serial_speed(serial_port, 460800)
        self.assertEqual(serial_port.baudrate, 115200)

    def test_shell_style_set_request_preserves_quoted_names(self) -> None:
        request = parse_config_request(
            b'set "Packet Rate" "333Hz Full(-105dBm)"')
        self.assertEqual(request, {
            "command": "set",
            "parameter": "Packet Rate",
            "value": "333Hz Full(-105dBm)",
        })

    def test_json_request(self) -> None:
        request = parse_config_request(
            b'{"command":"command","parameter":"Bind","confirm":true}')
        self.assertEqual(request["parameter"], "Bind")
        self.assertTrue(request["confirm"])

    def test_info_contains_device_configuration_and_link_state(self) -> None:
        transport = ProxyConfigTransport()
        transport.radio_rate_hz = 333.0
        transport.status = {
            "connected": True,
            "packets_bad": 0,
            "packets_good": 42,
            "flags": "0x01",
            "message": "",
        }
        device = DeviceInfo(0xEE, "RM Nomad", "ELRS", 0, 0x00040102, 3, 0)
        parameters = [
            Parameter(1, 0, ParameterType.SELECTION, False, "RF Band",
                      value=1, options=("915MHz", "2.4GHz")),
            Parameter(2, 0, ParameterType.SELECTION, False, "Packet Rate",
                      value=0, options=("333Hz",)),
            Parameter(3, 0, ParameterType.INFO, False, "Git Hash", value="abc123"),
        ]
        service = ConfigService(transport)
        service.client = FakeClient(device, parameters)
        result = service.execute({"command": "info"})
        self.assertEqual(result["device"]["version"], "4.1.2")
        self.assertEqual(result["configuration"]["RF Band"], "2.4GHz")
        self.assertEqual(result["lua_info"]["Git Hash"], "abc123")
        self.assertTrue(result["binding"]["connected"])
        self.assertEqual(result["radio_rate_hz"], 333.0)

    def test_params_contains_link_state_for_tui_banner(self) -> None:
        transport = ProxyConfigTransport()
        transport.status = {
            "connected": False,
            "packets_bad": 0,
            "packets_good": 82,
            "flags": "0x40",
            "message": "Baud rate too low",
        }
        device = DeviceInfo(0xEE, "RM Nomad", "ELRS", 0, 0x00040001, 1, 0)
        parameter = Parameter(
            1, 0, ParameterType.SELECTION, False, "Packet Rate",
            value=0, options=("250Hz",))
        service = ConfigService(transport)
        service.client = FakeClient(device, [parameter])
        result = service.execute({"command": "params"})
        self.assertEqual(result["binding"]["message"], "Baud rate too low")
        self.assertEqual(result["binding"]["flags"], "0x40")

    def test_transport_decodes_radio_rate_and_binding_status(self) -> None:
        transport = ProxyConfigTransport()
        self.assertEqual(transport.origin(0x00), 0xEA)
        self.assertEqual(transport.origin(0xEE), 0xEF)
        interval = 30030
        radio_payload = (
            bytes([0xEA, 0xEE, 0x10])
            + interval.to_bytes(4, "big", signed=True)
            + (0).to_bytes(4, "big", signed=True)
        )
        transport.observe(CrsfFrame(0xC8, FrameType.RADIO_ID, radio_payload, b""))
        status_payload = bytes([0xEA, 0xEE, 2, 0, 9, 1]) + b"Connected\x00"
        transport.observe(CrsfFrame(0xC8, FrameType.ELRS_STATUS, status_payload, b""))
        snapshot = transport.snapshot()
        self.assertAlmostEqual(snapshot["radio_rate_hz"], 333.0003, places=3)
        self.assertTrue(snapshot["binding"]["connected"])
        self.assertEqual(snapshot["binding"]["packets_good"], 9)


if __name__ == "__main__":
    unittest.main()
