"""Queue fencing, reclaim of in-flight states, multiprocess claim."""

from __future__ import annotations

import multiprocessing as mp
import tempfile
import time
import unittest
from pathlib import Path

from registration_jobs import JobState, RegistrationJob, new_job_id
from registration_queue import RegistrationQueue


def _mp_claim_worker(db: str, name: str, out_q: mp.Queue) -> None:
    q = RegistrationQueue(db)
    ids = []
    while True:
        j = q.claim(name, lease_sec=60)
        if not j:
            break
        ids.append(j.job_id)
    out_q.put(ids)


def _mp_enqueue_worker(db: str, worker_no: int, count: int, out_q: mp.Queue) -> None:
    q = RegistrationQueue(db)
    inserted: list[str] = []
    for index in range(count):
        session_id = f"w{worker_no}-job{index}"
        try:
            job = q.enqueue(
                RegistrationJob(
                    job_id=new_job_id(),
                    session_id=session_id,
                    route_id="route-1",
                    state=JobState.MINT_QUEUED.value,
                )
            )
            inserted.append(job.job_id)
        except RuntimeError as exc:
            if "hard limit" not in str(exc):
                raise
    out_q.put(inserted)


class QueueFencingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "q.db"
        self.q = RegistrationQueue(self.db)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_probe_running_reclaimed_after_expired_lease(self) -> None:
        job = RegistrationJob(
            job_id=new_job_id(),
            session_id="s1",
            route_id="route-1",
            state=JobState.MINT_QUEUED.value,
        )
        self.q.enqueue(job)
        c = self.q.claim("dead", lease_sec=1)
        assert c is not None
        c.state = JobState.PROBE_RUNNING.value
        c.lease_until = time.time() - 10
        self.q.save(c)
        c2 = self.q.claim("alive", lease_sec=30)
        self.assertIsNotNone(c2)
        assert c2 is not None
        self.assertEqual(c2.lease_owner, "alive")
        self.assertEqual(c2.state, JobState.MINT_RUNNING.value)
        self.assertGreater(c2.lease_generation, c.lease_generation)

    def test_stale_worker_cannot_overwrite_new_owner(self) -> None:
        job = RegistrationJob(
            job_id=new_job_id(),
            session_id="s2",
            route_id="route-1",
            state=JobState.MINT_QUEUED.value,
        )
        self.q.enqueue(job)
        c1 = self.q.claim("w1", lease_sec=1)
        assert c1 is not None
        # expire and reclaim
        c1.lease_until = time.time() - 5
        self.q.save(c1)
        c2 = self.q.claim("w2", lease_sec=60)
        assert c2 is not None
        # stale w1 tries fenced save
        c1.state = JobState.AUTH_IMPORTED.value
        c1.lease_owner = "w1"
        ok = self.q.save(c1, require_fence=True)
        self.assertFalse(ok)
        fresh = self.q.get(c2.job_id)
        assert fresh is not None
        self.assertEqual(fresh.lease_owner, "w2")
        self.assertNotEqual(fresh.state, JobState.AUTH_IMPORTED.value)

    def test_lease_heartbeat_prevents_early_reclaim(self) -> None:
        job = RegistrationJob(
            job_id=new_job_id(),
            session_id="s3",
            route_id="route-1",
            state=JobState.MINT_QUEUED.value,
        )
        self.q.enqueue(job)
        c = self.q.claim("w", lease_sec=2)
        assert c is not None
        time.sleep(0.5)
        self.assertTrue(self.q.heartbeat(c, lease_sec=30))
        # another worker should not reclaim
        self.assertIsNone(self.q.claim("other", lease_sec=10))

    def test_concurrent_enqueue_respects_hard_limit(self) -> None:
        import os

        os.environ["GROK2API_REGISTRATION_QUEUE_HARD_LIMIT"] = "3"
        os.environ["GROK2API_REGISTRATION_QUEUE_SOFT_LIMIT"] = "3"
        try:
            q = RegistrationQueue(Path(self.tmp.name) / "hl.db")
            for i in range(3):
                q.enqueue(
                    RegistrationJob(
                        job_id=new_job_id(),
                        session_id=f"h{i}",
                        route_id="route-1",
                        state=JobState.MINT_QUEUED.value,
                    )
                )
            with self.assertRaises(RuntimeError):
                q.enqueue(
                    RegistrationJob(
                        job_id=new_job_id(),
                        session_id="overflow",
                        route_id="route-1",
                        state=JobState.MINT_QUEUED.value,
                    )
                )
        finally:
            os.environ.pop("GROK2API_REGISTRATION_QUEUE_HARD_LIMIT", None)
            os.environ.pop("GROK2API_REGISTRATION_QUEUE_SOFT_LIMIT", None)

    def test_multiprocess_claim_exactly_once(self) -> None:
        """Concurrent claims via threads (cross-process SQLite covered by atomic claim).

        Full ProcessPool is environment-sensitive under unittest spawn; we still
        stress concurrent claim with 8 threads and assert exact-once semantics.
        """
        import os
        import threading

        n = 100
        os.environ["GROK2API_REGISTRATION_QUEUE_HARD_LIMIT"] = str(n + 10)
        os.environ["GROK2API_REGISTRATION_QUEUE_SOFT_LIMIT"] = str(n + 10)
        q = RegistrationQueue(Path(self.tmp.name) / "mp.db")
        for i in range(n):
            q.enqueue(
                RegistrationJob(
                    job_id=new_job_id(),
                    session_id=f"mp{i}",
                    route_id="route-1",
                    state=JobState.MINT_QUEUED.value,
                )
            )
        claimed: list[str] = []
        lock = threading.Lock()

        def worker(name: str) -> None:
            local = RegistrationQueue(Path(self.tmp.name) / "mp.db")
            while True:
                j = local.claim(name, lease_sec=60)
                if not j:
                    break
                with lock:
                    claimed.append(j.job_id)

        threads = [threading.Thread(target=worker, args=(f"t{i}",)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        os.environ.pop("GROK2API_REGISTRATION_QUEUE_HARD_LIMIT", None)
        os.environ.pop("GROK2API_REGISTRATION_QUEUE_SOFT_LIMIT", None)
        self.assertEqual(len(claimed), n)
        self.assertEqual(len(set(claimed)), n)

    def test_multiprocess_claim_exactly_once_8_processes_1000_jobs(self) -> None:
        import os

        count = 1000
        old_hard = os.environ.get("GROK2API_REGISTRATION_QUEUE_HARD_LIMIT")
        old_soft = os.environ.get("GROK2API_REGISTRATION_QUEUE_SOFT_LIMIT")
        os.environ["GROK2API_REGISTRATION_QUEUE_HARD_LIMIT"] = str(count + 10)
        os.environ["GROK2API_REGISTRATION_QUEUE_SOFT_LIMIT"] = str(count + 10)
        db = Path(self.tmp.name) / "mp-1000.db"
        try:
            queue = RegistrationQueue(db)
            for index in range(count):
                queue.enqueue(
                    RegistrationJob(
                        job_id=new_job_id(),
                        session_id=f"claim-{index}",
                        route_id="route-1",
                        state=JobState.MINT_QUEUED.value,
                    )
                )
            context = mp.get_context("spawn")
            output = context.Queue()
            processes = [
                context.Process(
                    target=_mp_claim_worker,
                    args=(str(db), f"process-{index}", output),
                )
                for index in range(8)
            ]
            for process in processes:
                process.start()
            claimed: list[str] = []
            for _ in processes:
                claimed.extend(output.get(timeout=90))
            for process in processes:
                process.join(timeout=90)
                self.assertFalse(process.is_alive())
                self.assertEqual(process.exitcode, 0)
            self.assertEqual(len(claimed), count)
            self.assertEqual(len(set(claimed)), count)
        finally:
            if old_hard is None:
                os.environ.pop("GROK2API_REGISTRATION_QUEUE_HARD_LIMIT", None)
            else:
                os.environ["GROK2API_REGISTRATION_QUEUE_HARD_LIMIT"] = old_hard
            if old_soft is None:
                os.environ.pop("GROK2API_REGISTRATION_QUEUE_SOFT_LIMIT", None)
            else:
                os.environ["GROK2API_REGISTRATION_QUEUE_SOFT_LIMIT"] = old_soft

    def test_concurrent_enqueue_hard_limit_8_processes(self) -> None:
        import os

        hard_limit = 37
        old_hard = os.environ.get("GROK2API_REGISTRATION_QUEUE_HARD_LIMIT")
        old_soft = os.environ.get("GROK2API_REGISTRATION_QUEUE_SOFT_LIMIT")
        os.environ["GROK2API_REGISTRATION_QUEUE_HARD_LIMIT"] = str(hard_limit)
        os.environ["GROK2API_REGISTRATION_QUEUE_SOFT_LIMIT"] = str(hard_limit)
        db = Path(self.tmp.name) / "mp-hard-limit.db"
        try:
            RegistrationQueue(db)
            context = mp.get_context("spawn")
            output = context.Queue()
            processes = [
                context.Process(
                    target=_mp_enqueue_worker,
                    args=(str(db), index, 25, output),
                )
                for index in range(8)
            ]
            for process in processes:
                process.start()
            inserted: list[str] = []
            for _ in processes:
                inserted.extend(output.get(timeout=90))
            for process in processes:
                process.join(timeout=90)
                self.assertFalse(process.is_alive())
                self.assertEqual(process.exitcode, 0)
            self.assertEqual(len(inserted), hard_limit)
            self.assertEqual(len(set(inserted)), hard_limit)
            self.assertEqual(RegistrationQueue(db).count_open(), hard_limit)
        finally:
            if old_hard is None:
                os.environ.pop("GROK2API_REGISTRATION_QUEUE_HARD_LIMIT", None)
            else:
                os.environ["GROK2API_REGISTRATION_QUEUE_HARD_LIMIT"] = old_hard
            if old_soft is None:
                os.environ.pop("GROK2API_REGISTRATION_QUEUE_SOFT_LIMIT", None)
            else:
                os.environ["GROK2API_REGISTRATION_QUEUE_SOFT_LIMIT"] = old_soft


if __name__ == "__main__":
    unittest.main()
