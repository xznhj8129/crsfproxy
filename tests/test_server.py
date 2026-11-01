import unittest

from lib.server import (
    ERROR_BAD_REQUEST,
    ERROR_BUSY,
    ERROR_FAILED,
    ERROR_STALE,
    ControlJob,
    ControlSessions,
    ControlWorker,
    RcOwner,
    _cacheable_reply,
)


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
        reply = {"ok": True, "result": {}}
        sessions.complete(PEER_A, 7, 3, reply, now=100.0)
        self.assertIs(sessions.cached(PEER_A, 7, 3, 160.0), reply)
        self.assertIsNone(sessions.cached(PEER_A, 7, 3, 160.001))

    def test_prune_expires_cache_but_keeps_high_water(self) -> None:
        sessions = ControlSessions(ttl_s=60.0)
        sessions.complete(
            PEER_A, 7, 3, {"ok": True, "result": {}}, now=100.0)
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
        sessions.complete(PEER_A, 7, 5, {"ok": True, "result": {}}, now=1.0)
        self.assertFalse(sessions.is_new(PEER_A, 7, 5))
        self.assertFalse(sessions.is_new(PEER_A, 7, 4))
        self.assertTrue(sessions.is_new(PEER_A, 7, 6))

    def test_wrap(self) -> None:
        sessions = ControlSessions()
        sessions.complete(
            PEER_A, 7, 0xFFFFFFFF, {"ok": True, "result": {}}, now=1.0)
        self.assertTrue(sessions.is_new(PEER_A, 7, 0))
        self.assertFalse(sessions.is_new(PEER_A, 7, 0xFFFFFFFE))
        sessions.complete(PEER_A, 7, 0, {"ok": True, "result": {}}, now=2.0)
        self.assertTrue(sessions.is_new(PEER_A, 7, 1))


class ControlReplyCacheRuleTests(unittest.TestCase):
    def test_only_success_and_failed_are_cached(self) -> None:
        self.assertTrue(_cacheable_reply({"ok": True, "result": {}}))
        self.assertTrue(
            _cacheable_reply(
                {"ok": False, "error": {"code": ERROR_FAILED, "message": ""}}))
        for code in (ERROR_BAD_REQUEST, ERROR_BUSY, ERROR_STALE):
            self.assertFalse(
                _cacheable_reply(
                    {"ok": False, "error": {"code": code, "message": ""}}))


class RcOwnerTests(unittest.TestCase):
    def test_accept_stale_and_foreign(self) -> None:
        owner = RcOwner(1000)
        self.assertEqual(owner.accept(PEER_A, 7, 0, b"a0", 0.0), "accepted")
        self.assertTrue(owner.is_owner(PEER_A, 7))
        self.assertEqual(owner.last_frame, b"a0")
        self.assertEqual(owner.last_seq, 0)
        self.assertEqual(owner.last_session, 7)
        self.assertEqual(owner.last_at, 0.0)

        self.assertEqual(owner.accept(PEER_A, 7, 0, b"a0b", 0.1), "stale")
        self.assertEqual(owner.last_frame, b"a0")
        self.assertEqual(owner.accept(PEER_A, 7, 5, b"a5", 0.2), "accepted")
        self.assertEqual(owner.last_frame, b"a5")
        self.assertEqual(owner.accept(PEER_B, 7, 6, b"b6", 0.3), "foreign")
        self.assertEqual(owner.accept(PEER_A, 8, 6, b"a8", 0.3), "foreign")
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

        self.assertEqual(owner.accept(PEER_A, 7, 10, b"a10b", 1.1), "stale")
        self.assertEqual(owner.accept(PEER_A, 7, 9, b"a9", 1.1), "stale")
        self.assertIsNone(owner.owner)
        self.assertEqual(owner.accept(PEER_A, 7, 11, b"a11", 1.2), "accepted")
        self.assertTrue(owner.is_owner(PEER_A, 7))
        self.assertEqual(owner.last_frame, b"a11")

    def test_different_session_becomes_owner_after_release(self) -> None:
        owner = RcOwner(1000)
        owner.accept(PEER_A, 7, 10, b"a10", 0.0)
        self.assertTrue(owner.release_if_stale(1.0))
        self.assertEqual(owner.accept(PEER_A, 8, 0, b"b0", 1.1), "accepted")
        self.assertTrue(owner.is_owner(PEER_A, 8))
        self.assertFalse(owner.is_owner(PEER_A, 7))
        self.assertEqual(owner.last_frame, b"b0")

    def test_another_peer_becomes_owner_when_no_owner(self) -> None:
        owner = RcOwner(1000)
        self.assertEqual(owner.accept(PEER_B, 9, 3, b"b3", 0.0), "accepted")
        self.assertTrue(owner.is_owner(PEER_B, 9))


class FakeService:
    def __init__(self, outcome) -> None:
        self.outcome = outcome
        self.requests = []

    def execute(self, request: dict) -> dict:
        self.requests.append(request)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class ControlWorkerTests(unittest.TestCase):
    def run_job(self, worker: ControlWorker, request: dict) -> dict:
        job = ControlJob(PEER_A, 7, 0, request)
        worker.submit(job)
        return worker.done.get(timeout=2.0).reply

    def test_success(self) -> None:
        service = FakeService({"protocol": "elrs"})
        worker = ControlWorker(service)
        try:
            reply = self.run_job(worker, {"command": "info"})
        finally:
            worker.stop()
        self.assertEqual(reply, {"ok": True, "result": {"protocol": "elrs"}})
        self.assertEqual(service.requests, [{"command": "info"}])

    def test_key_error_is_bad_request(self) -> None:
        service = FakeService(KeyError("missing parameter"))
        worker = ControlWorker(service)
        try:
            reply = self.run_job(worker, {"command": "get", "parameter": "missing"})
        finally:
            worker.stop()
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"]["code"], ERROR_BAD_REQUEST)
        self.assertIn("missing parameter", reply["error"]["message"])

    def test_timeout_error_is_failed(self) -> None:
        service = FakeService(TimeoutError("device timeout"))
        worker = ControlWorker(service)
        try:
            reply = self.run_job(worker, {"command": "info"})
        finally:
            worker.stop()
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"]["code"], ERROR_FAILED)
        self.assertIn("device timeout", reply["error"]["message"])

    def test_oversized_reply_becomes_failed(self) -> None:
        service = FakeService({"data": "x" * 70000})
        worker = ControlWorker(service)
        try:
            reply = self.run_job(worker, {"command": "info"})
        finally:
            worker.stop()
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"]["code"], ERROR_FAILED)

    def test_oversized_error_reply_becomes_failed(self) -> None:
        service = FakeService(ValueError("x" * 70000))
        worker = ControlWorker(service)
        try:
            reply = self.run_job(worker, {"command": "info"})
        finally:
            worker.stop()
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"]["code"], ERROR_FAILED)


if __name__ == "__main__":
    unittest.main()
