"""Unit tests for SQLite registration queue leases and pending import."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
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

    def _install_legacy_duplicate_rows(self) -> tuple[RegistrationJob, RegistrationJob]:
        with closing(sqlite3.connect(self.db)) as conn:
            with conn:
                conn.execute("DROP INDEX idx_jobs_session_one_active")
        survivor = RegistrationJob(
            job_id="job-active-owner",
            session_id="legacy-duplicate",
            route_id="route-1",
            state=JobState.MINT_RUNNING.value,
            lease_owner="live-owner",
            lease_until=time.time() + 300,
            payload={"owner_value": "keep"},
            created_at=10,
            updated_at=10,
        )
        recovery = RegistrationJob(
            job_id="job-recovery-material",
            session_id="legacy-duplicate",
            route_id="route-2",
            state=JobState.MINT_QUEUED.value,
            sso_ref="/secure/pending/legacy.json",
            cookie_bundle_path="/secure/cookies/legacy.json",
            payload={"email": "hash-only", "recovery_value": "merge"},
            created_at=20,
            updated_at=20,
        )
        self.q.save(survivor)
        self.q.save(recovery)
        return survivor, recovery

    def test_existing_duplicate_active_sessions_migrate_before_unique_index(self) -> None:
        survivor, recovery = self._install_legacy_duplicate_rows()
        migrated = RegistrationQueue(self.db)
        rows = [migrated.get(survivor.job_id), migrated.get(recovery.job_id)]
        active = [j for j in rows if j and j.state not in ("auth_imported", "dead_letter", "failed")]
        self.assertEqual([j.job_id for j in active], [survivor.job_id])
        loser = migrated.get(recovery.job_id)
        assert loser is not None
        self.assertEqual(loser.state, JobState.FAILED.value)
        self.assertEqual(loser.error_code, "duplicate_session_migrated")
        with closing(sqlite3.connect(self.db)) as conn:
            indexes = {row[1] for row in conn.execute("PRAGMA index_list(jobs)")}
        self.assertIn("idx_jobs_session_one_active", indexes)

    def test_duplicate_migration_is_idempotent(self) -> None:
        self._install_legacy_duplicate_rows()
        RegistrationQueue(self.db)
        with closing(sqlite3.connect(self.db)) as conn:
            before = conn.execute(
                "SELECT job_id,state,error_code,sso_ref,cookie_bundle_path,payload_json "
                "FROM jobs ORDER BY job_id"
            ).fetchall()
        RegistrationQueue(self.db)
        with closing(sqlite3.connect(self.db)) as conn:
            after = conn.execute(
                "SELECT job_id,state,error_code,sso_ref,cookie_bundle_path,payload_json "
                "FROM jobs ORDER BY job_id"
            ).fetchall()
        self.assertEqual(before, after)

    def test_duplicate_migration_preserves_recovery_material(self) -> None:
        survivor, recovery = self._install_legacy_duplicate_rows()
        migrated = RegistrationQueue(self.db)
        kept = migrated.get(survivor.job_id)
        loser = migrated.get(recovery.job_id)
        assert kept is not None and loser is not None
        self.assertEqual(kept.sso_ref, recovery.sso_ref)
        self.assertEqual(kept.cookie_bundle_path, recovery.cookie_bundle_path)
        self.assertEqual(kept.payload["owner_value"], "keep")
        self.assertEqual(kept.payload["recovery_value"], "merge")
        self.assertEqual(loser.sso_ref, recovery.sso_ref)
        self.assertEqual(loser.cookie_bundle_path, recovery.cookie_bundle_path)

    def test_duplicate_enqueue_returns_persisted_active_job(self) -> None:
        original = self._job()
        persisted = self.q.enqueue(original)
        duplicate = RegistrationJob(
            job_id=new_job_id(),
            session_id=original.session_id,
            route_id="route-2",
            state=JobState.MINT_QUEUED.value,
        )
        returned = self.q.enqueue(duplicate)
        self.assertEqual(returned.job_id, persisted.job_id)
        self.assertIsNone(self.q.get(duplicate.job_id))

    def test_duplicate_enqueue_does_not_return_terminal_history(self) -> None:
        original = self._job()
        persisted = self.q.enqueue(original)
        terminal = RegistrationJob(
            job_id=new_job_id(),
            session_id=original.session_id,
            route_id="route-2",
            state=JobState.FAILED.value,
            created_at=time.time() + 100,
            updated_at=time.time() + 100,
        )
        self.q.save(terminal)
        duplicate = RegistrationJob(
            job_id=new_job_id(),
            session_id=original.session_id,
            route_id="route-2",
            state=JobState.MINT_QUEUED.value,
        )
        returned = self.q.enqueue(duplicate)
        self.assertEqual(returned.job_id, persisted.job_id)
        self.assertNotEqual(returned.job_id, terminal.job_id)

    def test_duplicate_enqueue_at_hard_limit_is_idempotent(self) -> None:
        old_hard = os.environ.get("GROK2API_REGISTRATION_QUEUE_HARD_LIMIT")
        old_soft = os.environ.get("GROK2API_REGISTRATION_QUEUE_SOFT_LIMIT")
        os.environ["GROK2API_REGISTRATION_QUEUE_HARD_LIMIT"] = "1"
        os.environ["GROK2API_REGISTRATION_QUEUE_SOFT_LIMIT"] = "1"
        try:
            original = self._job()
            persisted = self.q.enqueue(original)
            duplicate = RegistrationJob(
                job_id=new_job_id(),
                session_id=original.session_id,
                route_id="route-2",
                state=JobState.MINT_QUEUED.value,
            )
            returned = self.q.enqueue(duplicate)
            self.assertEqual(returned.job_id, persisted.job_id)
            with self.assertRaises(RuntimeError):
                self.q.enqueue(self._job())
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
