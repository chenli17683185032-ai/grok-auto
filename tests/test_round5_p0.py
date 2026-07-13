"""Round-5 P0: SSOExtractor save, fenced requeue, no material delete on fence fail, no dead_letter resurrect."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


class SsoExtractorSaveTests(unittest.TestCase):
    def test_sso_py_save_explicit_only(self) -> None:
        src = (
            Path(__file__).resolve().parents[1]
            / "grok-build-auth/xconsole_client/sso.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("save or email", src)
        self.assertIn("if token and save:", src)

    def test_adapter_does_not_log_set_cookie_jwt_urls(self) -> None:
        src = (
            Path(__file__).resolve().parents[1] / "grok_build_adapter.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("parse_all_set_cookie_urls(rsc_body)[:3]", src)
        self.assertNotIn("parse_sso_jwt_url(rsc_body)}", src)
        self.assertIn("set-cookie hop_count=", src)


class FencedRequeueTests(unittest.TestCase):
    def test_stale_requeue_cannot_overwrite(self) -> None:
        from registration_jobs import JobState, RegistrationJob, new_job_id
        from registration_queue import RegistrationQueue

        with tempfile.TemporaryDirectory() as td:
            q = RegistrationQueue(Path(td) / "q.db")
            job = RegistrationJob(
                job_id=new_job_id(),
                session_id="s",
                route_id="route-1",
                state=JobState.MINT_QUEUED.value,
            )
            q.enqueue(job)
            c1 = q.claim("old", lease_sec=1)
            assert c1 is not None
            c1.lease_until = time.time() - 10
            q.save(c1)
            c2 = q.claim("new", lease_sec=60)
            assert c2 is not None
            gen_new = c2.lease_generation
            ok = q.requeue(c1, delay_sec=5, error_class="x", error_code="y")
            self.assertFalse(ok)
            fresh = q.get(c2.job_id)
            assert fresh is not None
            self.assertEqual(fresh.lease_owner, "new")
            self.assertEqual(fresh.lease_generation, gen_new)
            self.assertEqual(fresh.state, JobState.MINT_RUNNING.value)


class NoCleanupOnFenceFailTests(unittest.TestCase):
    def test_auth_imported_fence_fail_keeps_materials(self) -> None:
        from registration_controller import RegistrationController
        from registration_jobs import JobState, RegistrationJob, new_job_id
        from registration_queue import RegistrationQueue, dual_write_pending
        from route_registry import Route, reset_registry_for_tests

        with tempfile.TemporaryDirectory() as td:
            reset_registry_for_tests(
                (
                    Route(
                        route_id="route-1",
                        register_proxy="http://p1",
                        token_proxy="http://p1",
                        approver="http://a1",
                        approver_id="a1",
                        mihomo_id="m1",
                    ),
                )
            )
            q = RegistrationQueue(Path(td) / "q.db")
            pending = Path(td) / "pending"
            sso_path = dual_write_pending(
                session_id="s1",
                email="e@x.com",
                sso="eyJsso",
                pending_dir=pending,
                owner="mint_queue",
            )
            job = RegistrationJob(
                job_id=new_job_id(),
                session_id="s1",
                route_id="route-1",
                state=JobState.MINT_QUEUED.value,
                sso_ref=str(sso_path),
                cookie_bundle_path=str(Path(td) / "b.json"),
            )
            Path(job.cookie_bundle_path).write_text("{}", encoding="utf-8")
            q.enqueue(job)
            c = q.claim("w1", lease_sec=60)
            assert c is not None
            ctrl = RegistrationController(q, worker_id="w1")
            # Force save_terminal to fail
            with mock.patch.object(q, "save_terminal", return_value=False):
                out = ctrl.process_job(
                    c,
                    sso_to_token=lambda s, **k: {
                        "access_token": "a",
                        "refresh_token": "r",
                    },
                    import_entry=lambda token, sid: {"ok": True},
                    require_probe=False,
                )
            self.assertTrue(Path(c.sso_ref).exists())
            self.assertTrue(Path(c.cookie_bundle_path).exists())


class DeadLetterNoResurrectTests(unittest.TestCase):
    def test_repair_does_not_requeue_dead_letter_session(self) -> None:
        from registration_jobs import JobState, RegistrationJob, new_job_id
        from registration_queue import RegistrationQueue, repair_orphan_mint_pending

        with tempfile.TemporaryDirectory() as td:
            q = RegistrationQueue(Path(td) / "q.db")
            pending = Path(td) / "pending"
            pending.mkdir()
            path = pending / "sdead.json"
            path.write_text(
                json.dumps(
                    {
                        "session_id": "sdead",
                        "sso": "eyJ",
                        "owner": "mint_queue",
                        "pipeline_v2": True,
                        "created_at": time.time(),
                    }
                ),
                encoding="utf-8",
            )
            job = RegistrationJob(
                job_id=new_job_id(),
                session_id="sdead",
                route_id="route-1",
                state=JobState.DEAD_LETTER.value,
            )
            # enqueue dead letter via raw save (bypass capacity)
            q.save(job)
            stats = repair_orphan_mint_pending(pending_dir=pending, queue=q)
            self.assertEqual(stats.get("repaired"), 0)
            # still only one job
            again = q.get_by_session("sdead")
            assert again is not None
            self.assertEqual(again.state, JobState.DEAD_LETTER.value)
            imported = q.import_pending_json(path, route_id="route-1")
            assert imported is not None
            self.assertEqual(imported.job_id, again.job_id)
            self.assertEqual(imported.state, JobState.DEAD_LETTER.value)
            # still single row
            self.assertEqual(sum(q.list_states().values()), 1)


if __name__ == "__main__":
    unittest.main()
