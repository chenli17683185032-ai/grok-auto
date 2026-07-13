"""Unit tests for SQLite registration queue leases and pending import."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from registration_jobs import JobState, RegistrationJob, new_job_id
from registration_queue import RegistrationQueue, dual_write_pending, read_sso_from_ref


class QueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "q.db"
        self.q = RegistrationQueue(self.db)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _job(self, state: str = JobState.MINT_QUEUED.value) -> RegistrationJob:
        return RegistrationJob(
            job_id=new_job_id(),
            session_id=f"sess-{new_job_id()[-6:]}",
            route_id="route-1",
            state=state,
            sso_ref="",
        )

    def test_enqueue_and_claim(self) -> None:
        job = self._job()
        self.q.enqueue(job)
        claimed = self.q.claim("worker-a", lease_sec=60)
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual(claimed.job_id, job.job_id)
        self.assertEqual(claimed.state, JobState.MINT_RUNNING.value)
        self.assertEqual(claimed.lease_owner, "worker-a")
        # Second claim should get nothing while leased
        self.assertIsNone(self.q.claim("worker-b", lease_sec=60))

    def test_lease_expiry_reclaim(self) -> None:
        job = self._job()
        self.q.enqueue(job)
        c1 = self.q.claim("worker-a", lease_sec=1)
        self.assertIsNotNone(c1)
        # Force expiry
        assert c1 is not None
        c1.lease_until = time.time() - 1
        self.q.save(c1)
        # Put back to claimable state
        c1.state = JobState.MINT_QUEUED.value
        self.q.save(c1)
        c2 = self.q.claim("worker-b", lease_sec=30)
        self.assertIsNotNone(c2)
        assert c2 is not None
        self.assertEqual(c2.lease_owner, "worker-b")

    def test_hard_limit(self) -> None:
        import os

        os.environ["GROK2API_REGISTRATION_QUEUE_HARD_LIMIT"] = "2"
        os.environ["GROK2API_REGISTRATION_QUEUE_SOFT_LIMIT"] = "1"
        try:
            q = RegistrationQueue(Path(self.tmp.name) / "q2.db")
            q.enqueue(self._job())
            q.enqueue(self._job())
            with self.assertRaises(RuntimeError):
                q.enqueue(self._job())
        finally:
            os.environ.pop("GROK2API_REGISTRATION_QUEUE_HARD_LIMIT", None)
            os.environ.pop("GROK2API_REGISTRATION_QUEUE_SOFT_LIMIT", None)

    def test_pending_import_and_sso_ref(self) -> None:
        pending_dir = Path(self.tmp.name) / "pending"
        path = dual_write_pending(
            session_id="gba_test1",
            email="t@example.com",
            sso="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.test.sig",
            pending_dir=pending_dir,
        )
        self.assertEqual(oct(path.stat().st_mode)[-3:], "600")
        job = self.q.import_pending_json(path, route_id="route-2")
        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(job.route_id, "route-2")
        self.assertEqual(job.state, JobState.MINT_QUEUED.value)
        sso = read_sso_from_ref(job.sso_ref)
        self.assertTrue(sso.startswith("eyJ"))
        # SSO must not appear in SQLite payload
        raw = Path(self.db).read_bytes()
        # db path is self.db for first queue; imported into self.q
        stored = self.q.get(job.job_id)
        assert stored is not None
        self.assertNotIn("eyJ", json.dumps(stored.payload))

    def test_requeue(self) -> None:
        job = self._job()
        self.q.enqueue(job)
        claimed = self.q.claim("w1")
        assert claimed is not None
        self.q.requeue(claimed, delay_sec=0.0, error_class="browser_timeout")
        again = self.q.claim("w2")
        self.assertIsNotNone(again)

    def test_atomic_claim_no_double(self) -> None:
        """Two queue instances must not both claim the same job."""
        job = self._job()
        self.q.enqueue(job)
        q2 = RegistrationQueue(self.db)
        results: list[str | None] = []
        import threading

        barrier = threading.Barrier(2)

        def worker(name: str) -> None:
            barrier.wait()
            claimed = RegistrationQueue(self.db).claim(name, lease_sec=60)
            results.append(claimed.job_id if claimed else None)

        t1 = threading.Thread(target=worker, args=("wa",))
        t2 = threading.Thread(target=worker, args=("wb",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        non_null = [r for r in results if r is not None]
        self.assertEqual(len(non_null), 1, f"double claim results={results}")

    def test_mint_running_reclaim_after_lease_expiry(self) -> None:
        job = self._job()
        self.q.enqueue(job)
        c1 = self.q.claim("dead", lease_sec=1)
        assert c1 is not None
        self.assertEqual(c1.state, JobState.MINT_RUNNING.value)
        # Expire lease without requeue
        c1.lease_until = time.time() - 5
        self.q.save(c1)
        c2 = self.q.claim("alive", lease_sec=30)
        self.assertIsNotNone(c2)
        assert c2 is not None
        self.assertEqual(c2.lease_owner, "alive")
        self.assertEqual(c2.state, JobState.MINT_RUNNING.value)

    def test_failed_does_not_consume_capacity(self) -> None:
        import os

        os.environ["GROK2API_REGISTRATION_QUEUE_HARD_LIMIT"] = "2"
        os.environ["GROK2API_REGISTRATION_QUEUE_SOFT_LIMIT"] = "2"
        try:
            q = RegistrationQueue(Path(self.tmp.name) / "cap.db")
            j1 = self._job()
            j1.state = JobState.FAILED.value
            q.enqueue(j1)
            j2 = self._job()
            j2.state = JobState.DEAD_LETTER.value
            q.enqueue(j2)
            # FAILED/DEAD_LETTER not open → can still accept mint_queued
            self.assertTrue(q.can_accept(hard=True))
            q.enqueue(self._job())
        finally:
            os.environ.pop("GROK2API_REGISTRATION_QUEUE_HARD_LIMIT", None)
            os.environ.pop("GROK2API_REGISTRATION_QUEUE_SOFT_LIMIT", None)


if __name__ == "__main__":
    unittest.main()
