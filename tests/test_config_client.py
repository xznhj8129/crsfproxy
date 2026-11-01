import json
import socket
import threading
import time
import unittest
from contextlib import contextmanager

from lib import envelope
from config_client import ControlClient, ControlError, fetch_all_params
from lib.config import ControlCommand, ControlRequest

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


if __name__ == "__main__":
    unittest.main()
