import json
import unittest

from lib.config import ControlCommand, ControlErrorCode, ControlReply, ControlRequest
from lib.server import (
    CACHEABLE_ERROR_CODES,
    ControlJob,
    ControlSessions,
    ControlWorker,
    RcOwner,
    RcResult,
    _reply_payload,
)
from lib.transport import ProxyConfigTransport


PEER_A = ("127.0.0.1", 50000)
PEER_B = ("127.0.0.1", 50001)


class ControlSessionsTests(unittest.TestCase):
    def test_cached_reply(self) -> None:
        sessions = ControlSessions()
        reply = {"ok": True, "result": {"protocol": "elrs"}}
        sessions.complete(PEER_A, 7, 3, reply, now=100.0)
        self.assertIs(sessions.cached(PEER_A, 7, 3, 100.0), reply)
        self.assertIsNone(sessions.cached(PEER_A, 7, 4, 100.0))
        self.assertIsNone(sessions.cached(PEER_B, 7, 3, 100.0))

    def test_reply_is_retained_for_ttl_then_expires(self) -> None:
        sessions = ControlSessions(ttl_s=60.0)
        reply = ControlReply(result={})
        sessions.complete(PEER_A, 7, 3, reply, now=100.0)
        self.assertIs(sessions.cached(PEER_A, 7, 3, 160.0), reply)
        self.assertIsNone(sessions.cached(PEER_A, 7, 3, 160.001))

    def test_prune_expires_cache_but_keeps_high_water(self) -> None:
        sessions = ControlSessions(ttl_s=60.0)
        sessions.complete(
            PEER_A, 7, 3, ControlReply(result={}), now=100.0)
        sessions.prune(161.0)
        self.assertIsNone(sessions.cached(PEER_A, 7, 3, 161.0))
        self.assertFalse(sessions.is_new(PEER_A, 7, 3))
        self.assertTrue(sessions.is_new(PEER_A, 7, 4))

    def test_aba_replay_uses_cache_without_executing(self) -> None:
        sessions = ControlSessions()
        executions = []

        def execute(seq: int) -> None:
            executions.append(seq)
            sessions.complete(
                PEER_A, 7, seq, {"ok": True, "result": {"seq": seq}}, now=1.0)

        def dispatch(seq: int) -> dict:
            cached = sessions.cached(PEER_A, 7, seq, 1.0)
            if cached is not None:
                return cached
            self.assertTrue(sessions.is_new(PEER_A, 7, seq))
            execute(seq)
            return sessions.cached(PEER_A, 7, seq, 1.0)

        first = dispatch(0)
        dispatch(1)
        replay = dispatch(0)
        self.assertEqual(executions, [0, 1])
        self.assertIs(replay, first)
        self.assertTrue(sessions.is_new(PEER_A, 7, 2))

    def test_old_evicted_seq_is_stale(self) -> None:
        sessions = ControlSessions(capacity=2)
        for seq in range(3):
            sessions.complete(
                PEER_A, 7, seq, {"ok": True, "result": {"seq": seq}}, now=1.0)
        self.assertIsNone(sessions.cached(PEER_A, 7, 0, 1.0))
        self.assertFalse(sessions.is_new(PEER_A, 7, 0))
        self.assertTrue(sessions.is_new(PEER_A, 7, 3))

    def test_newer_seq_is_new(self) -> None:
        sessions = ControlSessions()
        self.assertTrue(sessions.is_new(PEER_A, 7, 5))
        sessions.complete(PEER_A, 7, 5, ControlReply(result={}), now=1.0)
        self.assertFalse(sessions.is_new(PEER_A, 7, 5))
        self.assertFalse(sessions.is_new(PEER_A, 7, 4))
        self.assertTrue(sessions.is_new(PEER_A, 7, 6))

    def test_wrap(self) -> None:
        sessions = ControlSessions()
        sessions.complete(
            PEER_A, 7, 0xFFFFFFFF, ControlReply(result={}), now=1.0)
        self.assertTrue(sessions.is_new(PEER_A, 7, 0))
        self.assertFalse(sessions.is_new(PEER_A, 7, 0xFFFFFFFE))
        sessions.complete(PEER_A, 7, 0, ControlReply(result={}), now=2.0)
        self.assertTrue(sessions.is_new(PEER_A, 7, 1))


class ControlReplyCacheRuleTests(unittest.TestCase):
    def test_only_success_and_failed_are_cached(self) -> None:
        self.assertIn(ControlReply(result={}).error_code, CACHEABLE_ERROR_CODES)
        self.assertIn(ControlErrorCode.FAILED, CACHEABLE_ERROR_CODES)
        for code in (ControlErrorCode.BAD_REQUEST, ControlErrorCode.BUSY, ControlErrorCode.STALE):
            self.assertNotIn(code, CACHEABLE_ERROR_CODES)


class RcOwnerTests(unittest.TestCase):
    def test_accept_stale_and_foreign(self) -> None:
        owner = RcOwner(1000)
        self.assertEqual(owner.accept(PEER_A, 7, 0, b"a0", 0.0), RcResult.ACCEPTED)
        self.assertTrue(owner.is_owner(PEER_A, 7))
        self.assertEqual(owner.last_frame, b"a0")
        self.assertEqual(owner.last_seq, 0)
        self.assertEqual(owner.last_session, 7)
        self.assertEqual(owner.last_at, 0.0)

        self.assertEqual(owner.accept(PEER_A, 7, 0, b"a0b", 0.1), RcResult.STALE)
        self.assertEqual(owner.last_frame, b"a0")
        self.assertEqual(owner.accept(PEER_A, 7, 5, b"a5", 0.2), RcResult.ACCEPTED)
        self.assertEqual(owner.last_frame, b"a5")
        self.assertEqual(owner.accept(PEER_B, 7, 6, b"b6", 0.3), RcResult.FOREIGN)
        self.assertEqual(owner.accept(PEER_A, 8, 6, b"a8", 0.3), RcResult.FOREIGN)
        self.assertTrue(owner.is_owner(PEER_A, 7))
        self.assertEqual(owner.last_frame, b"a5")

    def test_release_reacquire_and_released_owner_stale(self) -> None:
        owner = RcOwner(1000)
        owner.accept(PEER_A, 7, 10, b"a10", 0.0)
        self.assertFalse(owner.release_if_stale(0.999))
        self.assertTrue(owner.release_if_stale(1.0))
        self.assertIsNone(owner.owner)
        self.assertEqual(owner.last_seq, 10)
        self.assertFalse(owner.is_owner(PEER_A, 7))

        self.assertEqual(owner.accept(PEER_A, 7, 10, b"a10b", 1.1), RcResult.STALE)
        self.assertEqual(owner.accept(PEER_A, 7, 9, b"a9", 1.1), RcResult.STALE)
        self.assertIsNone(owner.owner)
        self.assertEqual(owner.accept(PEER_A, 7, 11, b"a11", 1.2), RcResult.ACCEPTED)
        self.assertTrue(owner.is_owner(PEER_A, 7))
        self.assertEqual(owner.last_frame, b"a11")

    def test_different_session_becomes_owner_after_release(self) -> None:
        owner = RcOwner(1000)
        owner.accept(PEER_A, 7, 10, b"a10", 0.0)
        self.assertTrue(owner.release_if_stale(1.0))
        self.assertEqual(owner.accept(PEER_A, 8, 0, b"b0", 1.1), RcResult.ACCEPTED)
        self.assertTrue(owner.is_owner(PEER_A, 8))
        self.assertFalse(owner.is_owner(PEER_A, 7))
        self.assertEqual(owner.last_frame, b"b0")

    def test_another_peer_becomes_owner_when_no_owner(self) -> None:
        owner = RcOwner(1000)
        self.assertEqual(owner.accept(PEER_B, 9, 3, b"b3", 0.0), RcResult.ACCEPTED)
        self.assertTrue(owner.is_owner(PEER_B, 9))


class FakeService:
    def __init__(self, outcome) -> None:
        self.outcome = outcome
        self.requests = []

    def execute(self, request: ControlRequest) -> dict:
        self.requests.append(request)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


INFO = ControlRequest(ControlCommand.INFO)


class ControlWorkerTests(unittest.TestCase):
    def run_job(self, outcome, request: ControlRequest = INFO) -> tuple[ControlReply, FakeService]:
        service = FakeService(outcome)
        worker = ControlWorker(service, ProxyConfigTransport())
        try:
            worker.submit(ControlJob(PEER_A, 7, 0, request))
            return worker.done.get(timeout=2.0).reply, service
        finally:
            worker.stop()

    def test_success(self) -> None:
        reply, service = self.run_job({"protocol": "elrs"})
        self.assertEqual(reply, ControlReply(result={"protocol": "elrs"}))
        self.assertEqual(service.requests, [INFO])

    def test_key_error_is_bad_request(self) -> None:
        reply, _ = self.run_job(KeyError("missing parameter"),
                                ControlRequest(ControlCommand.GET, parameter="missing"))
        self.assertEqual(reply.error_code, ControlErrorCode.BAD_REQUEST)
        self.assertIn("missing parameter", reply.message)

    def test_timeout_error_is_failed(self) -> None:
        reply, _ = self.run_job(TimeoutError("device timeout"))
        self.assertEqual(reply.error_code, ControlErrorCode.FAILED)
        self.assertIn("device timeout", reply.message)

    def test_oversized_reply_becomes_failed(self) -> None:
        reply, _ = self.run_job({"data": "x" * 70000})
        decoded = json.loads(_reply_payload(reply))
        self.assertFalse(decoded["ok"])
        self.assertEqual(decoded["error"]["code"], ControlErrorCode.FAILED.value)


class ControlRequestTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        request = ControlRequest.from_json(
            {"command": "command", "parameter": "Bind", "step": "start"})
        self.assertEqual(ControlRequest.from_json(request.to_json()), request)

    def test_invalid_requests(self) -> None:
        for data in ({"command": "nope"}, {"command": "get"}, {"command": "set", "parameter": "x"},
                     {"command": "command", "parameter": "Bind", "step": "go"},
                     {"command": "params", "offset": -1}):
            with self.assertRaises(ValueError):
                ControlRequest.from_json(data)


if __name__ == "__main__":
    unittest.main()
