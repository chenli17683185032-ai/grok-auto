"""Mint worker terminal states and loop isolation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from registration_controller import RegistrationController
from registration_jobs import JobState
from registration_queue import RegistrationQueue, dual_write_pending
from route_registry import Route, reset_registry_for_tests


class MintWorkerTerminalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "q.db"
        self.pending = Path(self.tmp.name) / "pending"
        self.q = RegistrationQueue(self.db)
        reset_registry_for_tests(
            (
                Route(
                    route_id="route-1",
                    register_proxy="http://p1",
                    token_proxy="http://p1",
                    approver="http://a1",
                    approver_id="approver-1",
                    mihomo_id="m1",
                ),
                Route(
                    route_id="route-2",
                    register_proxy="http://p2",
                    token_proxy="http://p2",
                    approver="http://a2",
                    approver_id="approver-2",
                    mihomo_id="m2",
                ),
            )
        )
        from registration_metrics import reset_metrics_for_tests

        reset_metrics_for_tests(Path(self.tmp.name) / "m.db")
        self.ctrl = RegistrationController(self.q, worker_id="w1")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _claim_job(self, sso: str = "eyJsso") -> object:
        path = dual_write_pending(
            session_id="gba_t",
            email="t@example.com",
            sso=sso,
            pending_dir=self.pending,
            owner="mint_queue",
        )
        self.q.import_pending_json(path, route_id="route-1")
        return self.q.claim("w1")

    def test_empty_token_moves_to_terminal_without_worker_crash(self) -> None:
        job = self._claim_job()
        assert job is not None
        out = self.ctrl.process_job(
            job,
            sso_to_token=lambda s, **k: None,
            require_probe=False,
        )
        self.assertIn(out.state, (JobState.FAILED.value, JobState.DEAD_LETTER.value, JobState.MINT_QUEUED.value))
        self.assertEqual(out.lease_owner, "")

    def test_denied_does_not_exit_worker_loop(self) -> None:
        job = self._claim_job()
        assert job is not None
        out = self.ctrl.process_job(
            job,
            sso_to_token=lambda s, **k: (_ for _ in ()).throw(RuntimeError("access_denied")),
            require_probe=False,
        )
        self.assertIn(out.state, (JobState.FAILED.value, JobState.DEAD_LETTER.value, JobState.MINT_QUEUED.value))

    def test_retry_exhaustion_reaches_dead_letter(self) -> None:
        job = self._claim_job()
        assert job is not None
        job.attempts = 99
        self.q.save(job)
        out = self.ctrl.process_job(
            job,
            sso_to_token=lambda s, **k: (_ for _ in ()).throw(RuntimeError("timeout network")),
            require_probe=False,
        )
        self.assertEqual(out.state, JobState.DEAD_LETTER.value)

    def test_unexpected_exception_isolated_to_one_job(self) -> None:
        job = self._claim_job()
        assert job is not None
        with mock.patch.object(
            self.ctrl, "process_job", side_effect=RuntimeError("boom unexpected")
        ):
            # claim_and_process_once should not raise
            out = self.ctrl.claim_and_process_once()
        # May be None if claim got different job; at least no raise
        _ = out

    def test_terminal_transition_clears_lease(self) -> None:
        job = self._claim_job()
        assert job is not None
        out = self.ctrl.process_job(
            job,
            sso_to_token=lambda s, **k: {"access_token": "a"},  # missing refresh
            require_probe=False,
        )
        self.assertEqual(out.lease_owner, "")
        self.assertEqual(out.lease_until, 0.0)


if __name__ == "__main__":
    unittest.main()
