import time
import unittest
from dataclasses import replace

from lib.crsf_protocol import (
    DeviceInfo,
    Frame as CrsfFrame,
    FrameType,
    Parameter,
    ParameterType,
)
from lib.config import ControlRequest
from lib.elrs_backend import PARAMETER_CACHE_TTL_S, ElrsConfigService
from lib.elrs_config import WriteResult
from lib.transport import ProxyConfigTransport


def run(service, data: dict) -> dict:
    return service.execute(ControlRequest.from_json(data))


class FakeClient:
    def __init__(self, device, parameters) -> None:
        self.device = device
        self.parameters = list(parameters)
        self.read_all_calls = 0
        self.write_calls = 0
        self.command_calls = 0

    def read(self, device_address, parameter_id):
        self.live_reads = getattr(self, "live_reads", 0) + 1
        for parameter in self.parameters:
            if parameter.id == parameter_id:
                return parameter
        raise TimeoutError(f"parameter_id={parameter_id}")

    def discover(self, seconds):
        return {self.device.address: self.device}

    def read_all(self, device):
        self.read_all_calls += 1
        return list(self.parameters)

    def write(self, device_address, parameter, value):
        self.write_calls += 1
        if parameter.type == ParameterType.SELECTION:
            if value in parameter.options:
                new_value = parameter.options.index(value)
            else:
                new_value = int(value)
        elif parameter.type.value <= ParameterType.INT64:
            new_value = int(value)
        else:
            new_value = value
        updated = replace(parameter, value=new_value)
        return WriteResult(updated, parameter.value, True)

    def command_step(self, device_address, parameter, step):
        self.command_calls += 1
        return replace(parameter, command_status=0, command_timeout=0)


def selection_parameters(count: int) -> list[Parameter]:
    return [
        Parameter(
            id=index + 1,
            parent=0,
            type=ParameterType.SELECTION,
            hidden=False,
            name=f"Parameter {index + 1:02d}",
            value=0,
            minimum=0,
            maximum=2,
            default=0,
            unit="",
            options=("one", "two", "three"),
        )
        for index in range(count)
    ]


def info_parameters() -> list[Parameter]:
    return [
        Parameter(1, 0, ParameterType.SELECTION, False, "RF Band",
                  value=1, options=("915MHz", "2.4GHz")),
        Parameter(2, 0, ParameterType.SELECTION, False, "Packet Rate",
                  value=0, options=("333Hz",)),
        Parameter(3, 0, ParameterType.SELECTION, False, "Link Mode",
                  value=0, options=("Normal",)),
        Parameter(4, 0, ParameterType.UINT8, False, "Model Id", value=7),
        Parameter(5, 0, ParameterType.SELECTION, False, "Telem Ratio",
                  value=0, options=("1:2",)),
        Parameter(6, 0, ParameterType.INFO, False, "Git Hash", value="abc123"),
    ]


def make_service(parameters=None, device=None):
    parameters = list(parameters or [])
    if device is None:
        device = DeviceInfo(0xEE, "RM Nomad", "ELRS", 0, 0x00040102,
                            len(parameters), 0)
    transport = ProxyConfigTransport()
    client = FakeClient(device, parameters)
    service = ElrsConfigService(transport)
    service.client = client
    return service, client, transport


class ElrsConfigServiceTests(unittest.TestCase):
    def test_devices_has_protocol(self) -> None:
        service, _, _ = make_service(info_parameters())
        result = run(service, {"command": "devices"})
        self.assertEqual(result["protocol"], "elrs")
        self.assertEqual(result["devices"][0]["address"], 0xEE)

    def test_info_is_a_summary_with_protocol(self) -> None:
        service, _, transport = make_service(info_parameters())
        transport.radio_rate_hz = 333.0
        transport.status = {
            "connected": True,
            "packets_bad": 0,
            "packets_good": 42,
            "flags": "0x01",
            "message": "",
        }
        result = run(service, {"command": "info"})
        self.assertEqual(result["protocol"], "elrs")
        self.assertEqual(result["device"]["version"], "4.1.2")
        self.assertEqual(result["current_band"], "2.4GHz")
        self.assertEqual(result["packet_rate"], "333Hz")
        self.assertEqual(result["mode"], "Normal")
        self.assertEqual(result["model_id"], 7)
        self.assertEqual(result["telemetry_ratio"], "1:2")
        self.assertEqual(result["firmware_hash"], "abc123")
        self.assertTrue(result["binding"]["connected"])
        self.assertEqual(result["radio_rate_hz"], 333.0)
        self.assertNotIn("configuration", result)
        self.assertNotIn("lua_info", result)

    def test_params_is_paginated_compact_and_reads_once(self) -> None:
        service, client, _ = make_service(selection_parameters(30))
        offset = 0
        ids = []
        pages = 0
        while True:
            result = run(service, {"command": "params", "offset": offset})
            pages += 1
            self.assertEqual(result["protocol"], "elrs")
            self.assertIn("device", result)
            self.assertIn("radio_rate_hz", result)
            self.assertIn("binding", result)
            self.assertEqual(result["offset"], offset)
            self.assertEqual(result["total"], 30)
            self.assertGreater(len(result["parameters"]), 0)
            for parameter in result["parameters"]:
                self.assertNotIn("options", parameter)
            ids.extend(parameter["id"] for parameter in result["parameters"])
            if not result["more"]:
                break
            offset += len(result["parameters"])
        self.assertGreater(pages, 1)
        self.assertEqual(ids, list(range(1, 31)))
        self.assertEqual(client.read_all_calls, 1)

    def test_params_offset_zero_refreshes_after_ttl(self) -> None:
        service, client, _ = make_service(selection_parameters(30))
        run(service, {"command": "params", "offset": 0})
        run(service, {"command": "params", "offset": 1})
        self.assertEqual(client.read_all_calls, 1)
        service._parameters_at = time.monotonic() - PARAMETER_CACHE_TTL_S - 1
        run(service, {"command": "params", "offset": 0})
        self.assertEqual(client.read_all_calls, 2)

    def test_params_offset_bounds(self) -> None:
        service, _, _ = make_service(selection_parameters(30))
        with self.assertRaises(ValueError):
            run(service, {"command": "params", "offset": -1})
        result = run(service, {"command": "params", "offset": 1000})
        self.assertEqual(result["parameters"], [])
        self.assertFalse(result["more"])
        self.assertEqual(result["offset"], 1000)
        self.assertEqual(result["total"], 30)

    def test_params_omit_options_while_get_includes_them(self) -> None:
        service, _, _ = make_service(selection_parameters(10))
        params = run(service, {"command": "params", "offset": 0})
        self.assertNotIn("options", params["parameters"][0])
        parameter = run(service, {"command": "get", "parameter": "Parameter 01"})
        self.assertIn("options", parameter["parameter"])
        self.assertEqual(parameter["parameter"]["options"], ["one", "two", "three"])

    def test_get_accepts_name_or_decimal_id(self) -> None:
        service, _, _ = make_service(selection_parameters(10))
        by_name = run(service, {"command": "get", "parameter": "Parameter 02"})
        by_id = run(service, {"command": "get", "parameter": "3"})
        self.assertEqual(by_name["parameter"]["id"], 2)
        self.assertEqual(by_id["parameter"]["id"], 3)
        with self.assertRaises((KeyError, ValueError)):
            run(service, {"command": "get", "parameter": "missing"})
        with self.assertRaises((KeyError, ValueError)):
            run(service, {"command": "get", "parameter": "999"})

    def test_set_invalidates_parameter_cache(self) -> None:
        service, client, _ = make_service(selection_parameters(30))
        run(service, {"command": "params", "offset": 0})
        before = client.read_all_calls
        result = run(service, 
            {"command": "set", "parameter": "Parameter 01", "value": "1"})
        self.assertEqual(result["protocol"], "elrs")
        self.assertTrue(result["verified"])
        self.assertEqual(result["old_value"], 0)
        self.assertEqual(result["parameter"]["value"], "two")
        self.assertIn("options", result["parameter"])
        run(service, {"command": "params", "offset": 0})
        self.assertEqual(client.read_all_calls, before + 1)

    def test_command_invalidates_parameter_cache(self) -> None:
        parameters = selection_parameters(3) + [
            Parameter(99, 0, ParameterType.COMMAND, False, "Save",
                      command_status=0, command_timeout=0, command_info="Save it"),
        ]
        service, client, _ = make_service(parameters)
        run(service, {"command": "params", "offset": 0})
        result = run(service, 
            {"command": "command", "parameter": "Save", "step": "start"})
        self.assertEqual(result["protocol"], "elrs")
        self.assertIn("options", result["parameter"])
        self.assertEqual(result["parameter"]["command_status"], 0)
        run(service, {"command": "params", "offset": 0})
        self.assertEqual(client.read_all_calls, 2)


class ConfigTransportTests(unittest.TestCase):
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
