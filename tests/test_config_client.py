import curses
import json
import socket
import threading
import time
import unittest
from contextlib import contextmanager
from unittest import mock

from lib import envelope
from config_client import (
    ControlClient, ControlError, cli_control_request, command_request, fetch_all_params,
    other_devices, run_tui)
from lib.config import ControlCommand, ControlErrorCode, ControlRequest
from lib.crsf_protocol import CommandStatus

INFO = ControlRequest(ControlCommand.INFO)


class FakeControlServer(threading.Thread):
    """One loopback UDP socket that answers CONTROL envelopes."""

    def __init__(self, handler) -> None:
        super().__init__(daemon=True)
        self.handler = handler
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.settimeout(0.05)
        self.port = self.socket.getsockname()[1]
        self.requests: list[tuple[envelope.Envelope, dict]] = []
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                data, peer = self.socket.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                message = envelope.decode(data)
                request = json.loads(message.payload)
            except ValueError:
                continue
            self.requests.append((message, request))
            reply = self.handler(message, request, len(self.requests))
            if reply is None:
                continue
            payload = json.dumps(reply, separators=(",", ":")).encode("utf-8")
            self.socket.sendto(
                envelope.encode(
                    envelope.Kind.CONTROL_REPLY,
                    message.session,
                    message.seq,
                    payload,
                ),
                peer,
            )

    def close(self) -> None:
        self._stop_event.set()
        self.socket.close()
        self.join(timeout=1.0)


class FakeScreen:
    """Just enough curses window for run_tui."""

    def __init__(self, keys) -> None:
        self.keys = list(keys)
        self.lines: list[str] = []

    def erase(self) -> None:
        self.lines = []

    def getmaxyx(self):
        return (24, 100)

    def addnstr(self, row, col, text, width, attr=0) -> None:
        self.lines.append(text)

    def getch(self) -> int:
        if not self.keys:
            raise AssertionError("run_tui asked for a key after the script ended")
        return self.keys.pop(0)


def tui_parameter(identifier, name, parent=0, parameter_type="UINT8", value=0):
    return {
        "id": identifier, "parent": parent, "type": parameter_type,
        "hidden": False, "name": name, "value": value,
    }


class FakeTuiClient:
    """A CONTROL server's replies for the TUI, without a socket."""

    def __init__(self, fail=()) -> None:
        self.requests: list[ControlRequest] = []
        self.fail = set(fail)

    def request(self, request: ControlRequest, timeout=None) -> dict:
        self.requests.append(request)
        if request.command == ControlCommand.DEVICES:
            return {"protocol": "elrs", "devices": [
                {"address": 0xEE, "name": "TX", "version": "1.0.0"},
                {"address": 0xEC, "name": "RX", "version": "1.0.0"},
            ]}
        if request.address in self.fail:
            raise ControlError(ControlErrorCode.BAD_REQUEST, "no device answered")
        return {
            "protocol": "elrs",
            "device": {"name": "TX" if request.address is None else "RX"},
            "parameters": [tui_parameter(0, "Rate"),
                           tui_parameter(1, "Folder", parameter_type="FOLDER")],
            "offset": 0,
            "total": 2,
            "more": False,
            "radio_rate_hz": 500,
            "binding": None,
        }


class ControlClientTests(unittest.TestCase):
    @contextmanager
    def control_pair(self, handler, timeout: float = 2.0):
        server = FakeControlServer(handler)
        server.start()
        client = ControlClient(
            "127.0.0.1", server.port, timeout=timeout, retry_interval_s=0.05)
        try:
            yield server, client
        finally:
            client.close()
            server.close()

    def test_success_reply(self) -> None:
        def handler(message, request, count):
            return {"ok": True, "result": {"echo": request}}

        with self.control_pair(handler) as (server, client):
            result = client.request(INFO)

        self.assertEqual(result, {"echo": {"command": "info"}})
        self.assertEqual(client.seq, 1)
        message, request = server.requests[0]
        self.assertEqual(message.kind, envelope.Kind.CONTROL)
        self.assertEqual(message.session, client.session)
        self.assertNotEqual(message.session, 0)
        self.assertEqual(message.seq, 0)
        self.assertEqual(request, {"command": "info"})

    def test_busy_then_success_reuses_the_same_key(self) -> None:
        def handler(message, request, count):
            if count == 1:
                return {"ok": False, "error": {"code": "busy", "message": "working"}}
            return {"ok": True, "result": {"count": count}}

        started = time.monotonic()
        with self.control_pair(handler) as (server, client):
            result = client.request(INFO)
        elapsed = time.monotonic() - started

        self.assertEqual(result, {"count": 2})
        self.assertLess(elapsed, 1.0)
        self.assertEqual(len(server.requests), 2)
        first, second = server.requests[0][0], server.requests[1][0]
        self.assertEqual(first.session, second.session)
        self.assertEqual(first.seq, second.seq)
        self.assertEqual(client.seq, 1)

    def test_timeout_retries_and_raises_timeout(self) -> None:
        def handler(message, request, count):
            return None

        with self.control_pair(handler, timeout=0.2) as (server, client):
            with self.assertRaises(ControlError) as caught:
                client.request(INFO)
        self.assertEqual(caught.exception.code, "timeout")
        self.assertGreaterEqual(len(server.requests), 2)
        self.assertEqual(server.requests[0][0].seq, server.requests[-1][0].seq)
        self.assertEqual(client.seq, 1)

    def test_error_replies_raise_with_the_server_code(self) -> None:
        for code in ("stale", "bad_request", "failed"):
            with self.subTest(code=code):
                def handler(message, request, count, code=code):
                    return {
                        "ok": False,
                        "error": {"code": code, "message": f"{code} message"},
                    }

                with self.control_pair(handler) as (server, client):
                    with self.assertRaises(ControlError) as caught:
                        client.request(INFO)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.message, f"{code} message")
                self.assertEqual(client.seq, 1)

    def test_fetch_all_params_combines_pages(self) -> None:
        def handler(message, request, count):
            offset = request["offset"]
            if offset == 0:
                return {"ok": True, "result": {
                    "protocol": "elrs",
                    "device": {"name": "TX"},
                    "parameters": [{"id": 0}, {"id": 1}],
                    "offset": 0,
                    "total": 3,
                    "more": True,
                    "radio_rate_hz": 500,
                    "binding": None,
                }}
            if offset == 2:
                return {"ok": True, "result": {
                    "protocol": "elrs",
                    "device": {"name": "TX"},
                    "parameters": [{"id": 2}],
                    "offset": 2,
                    "total": 3,
                    "more": False,
                    "radio_rate_hz": 500,
                    "binding": None,
                }}
            return {"ok": False, "error": {
                "code": "bad_request", "message": f"unexpected offset {offset}"}}

        with self.control_pair(handler) as (server, client):
            result = fetch_all_params(client)

        self.assertEqual([parameter["id"] for parameter in result["parameters"]], [0, 1, 2])
        self.assertEqual(result["protocol"], "elrs")
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["offset"], 0)
        self.assertFalse(result["more"])
        self.assertEqual(result["radio_rate_hz"], 500)
        self.assertEqual([request[1]["offset"] for request in server.requests], [0, 2])

    def test_fetch_all_params_rejects_an_empty_more_page(self) -> None:
        def handler(message, request, count):
            return {"ok": True, "result": {
                "parameters": [],
                "offset": 0,
                "total": 3,
                "more": True,
            }}

        with self.control_pair(handler) as (server, client):
            with self.assertRaises(ControlError) as caught:
                fetch_all_params(client)
        self.assertEqual(caught.exception.code, "bad_reply")
        self.assertEqual(len(server.requests), 1)

    def test_fetch_all_params_sends_the_address_on_every_page(self) -> None:
        def handler(message, request, count):
            offset = request["offset"]
            if offset == 0:
                return {"ok": True, "result": {
                    "protocol": "elrs",
                    "device": {"name": "RX"},
                    "parameters": [{"id": 0}],
                    "offset": 0,
                    "total": 2,
                    "more": True,
                    "radio_rate_hz": 500,
                    "binding": None,
                }}
            if offset == 1:
                return {"ok": True, "result": {
                    "protocol": "elrs",
                    "device": {"name": "RX"},
                    "parameters": [{"id": 1}],
                    "offset": 1,
                    "total": 2,
                    "more": False,
                    "radio_rate_hz": 500,
                    "binding": None,
                }}
            return {"ok": False, "error": {
                "code": "bad_request", "message": f"unexpected offset {offset}"}}

        with self.control_pair(handler) as (server, client):
            result = fetch_all_params(client, 0xEC)

        self.assertEqual([parameter["id"] for parameter in result["parameters"]], [0, 1])
        self.assertEqual(result["device"]["name"], "RX")
        self.assertEqual(
            [(request[1]["offset"], request[1]["address"]) for request in server.requests],
            [(0, 0xEC), (1, 0xEC)])

    def test_command_request_sends_the_address(self) -> None:
        def handler(message, request, count):
            return {"ok": True, "result": {"parameter": {"id": 4, "command_status": 1}}}

        with self.control_pair(handler) as (server, client):
            state = command_request(client, "4", CommandStatus.START, address=0xEC)

        self.assertEqual(state, {"id": 4, "command_status": 1})
        self.assertEqual(server.requests[0][1], {
            "command": "command", "parameter": "4", "step": "start", "address": 0xEC})

    def test_request_without_an_address_sends_no_address_key(self) -> None:
        def handler(message, request, count):
            return {"ok": True, "result": {
                "parameters": [], "offset": 0, "total": 0, "more": False}}

        with self.control_pair(handler) as (server, client):
            fetch_all_params(client)

        self.assertEqual(server.requests[0][1], {"command": "params", "offset": 0})

    def test_only_addressed_commands_carry_the_address(self) -> None:
        self.assertIsNone(cli_control_request(ControlCommand.INFO, 0xEC).address)
        self.assertIsNone(cli_control_request(ControlCommand.DEVICES, 0xEC).address)
        for command in (ControlCommand.PARAMS, ControlCommand.GET,
                        ControlCommand.SET, ControlCommand.COMMAND):
            self.assertEqual(
                cli_control_request(command, 0xEC, "1", "value").address, 0xEC)
        self.assertIsNone(cli_control_request(ControlCommand.GET, None, "1").address)

    def test_other_devices_excludes_the_current_device(self) -> None:
        devices = [
            {"address": 0xEE, "name": "TX"},
            {"address": 0xEC, "name": "RX"},
            {"address": 0xEA, "name": "Handset"},
        ]

        self.assertEqual(
            [device["address"] for device in other_devices(devices, None)],
            [0xEC, 0xEA])
        self.assertEqual(
            [device["address"] for device in other_devices(devices, 0xEE)],
            [0xEC, 0xEA])
        self.assertEqual(
            [device["address"] for device in other_devices(devices, 0xEC)],
            [0xEE, 0xEA])


class TuiDeviceTests(unittest.TestCase):
    SWITCH_AND_BACK = (curses.KEY_DOWN, curses.KEY_DOWN, curses.KEY_ENTER,
                       curses.KEY_RIGHT, curses.KEY_LEFT, curses.KEY_LEFT)

    def run_keys(self, keys, client: FakeTuiClient, address=None) -> FakeScreen:
        screen = FakeScreen(keys)
        with mock.patch.object(curses, "curs_set"), \
                mock.patch.object(curses, "set_escdelay"):
            run_tui(screen, client, 1.0, address)
        return screen

    def params_addresses(self, client: FakeTuiClient) -> list[int | None]:
        return [request.address for request in client.requests
                if request.command == ControlCommand.PARAMS]

    def test_other_devices_switches_and_returns_to_the_transmitter(self) -> None:
        client = FakeTuiClient()
        screen = self.run_keys(self.SWITCH_AND_BACK, client)

        self.assertEqual(self.params_addresses(client), [None, 0xEC, None])
        self.assertEqual(
            [request.command for request in client.requests].count(ControlCommand.DEVICES),
            1)
        self.assertTrue(any("Other Devices" in line for line in screen.lines))

    def test_picker_back_keeps_the_transmitter(self) -> None:
        client = FakeTuiClient()
        self.run_keys((curses.KEY_DOWN, curses.KEY_DOWN, curses.KEY_ENTER,
                       curses.KEY_LEFT, curses.KEY_LEFT), client)

        self.assertEqual(self.params_addresses(client), [None])

    def test_failed_device_load_falls_back_to_the_transmitter(self) -> None:
        client = FakeTuiClient(fail={0xEC})
        screen = self.run_keys((curses.KEY_LEFT,), client, address=0xEC)

        self.assertEqual(self.params_addresses(client), [0xEC, None])
        self.assertTrue(any("no device answered" in line for line in screen.lines))


if __name__ == "__main__":
    unittest.main()
