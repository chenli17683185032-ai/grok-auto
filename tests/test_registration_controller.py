"""Unit / integration tests for mint controller (mocked device flow)."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from registration_controller import RegistrationController
from registration_jobs import JobState
from registration_queue import RegistrationQueue, dual_write_pending
from route_registry import Route, reset_registry_for_tests


class ControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "q.db"
        self.pending = Path(self.tmp.name) / "pending"
        self.q = RegistrationQueue(self.db)
        # Isolate metrics DB so emit never touches project data/ or fails open.
        from registration_metrics import reset_metrics_for_tests

        reset_metrics_for_tests(Path(self.tmp.name) / "metrics.db")
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
        self.ctrl = RegistrationController(self.q, worker_id="test-worker")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_enqueue_after_sso_dual_write(self) -> None:
        import os

        os.environ["GROK2API_DATA_DIR"] = self.tmp.name
        job = self.ctrl.enqueue_after_sso(
            session_id="gba_ctrl1",
            email="u@example.com",
            sso="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.aaa.bbb",
            route_id="route-2",
            dual_write=True,
        )
        self.assertEqual(job.route_id, "route-2")
        self.assertEqual(job.state, JobState.MINT_QUEUED.value)
        self.assertTrue(job.sso_ref)
        self.assertTrue(Path(job.sso_ref).is_file())

    def test_controller_returns_persisted_job_id(self) -> None:
        first = self.ctrl.enqueue_after_sso(
            session_id="gba_idempotent",
            email="u@example.com",
            sso="eyJfirst",
            route_id="route-1",
            dual_write=False,
        )
        second = self.ctrl.enqueue_after_sso(
            session_id="gba_idempotent",
            email="u@example.com",
            sso="eyJsecond",
            route_id="route-2",
            dual_write=False,
        )
        self.assertEqual(second.job_id, first.job_id)
        self.assertEqual(second.route_id, first.route_id)
        self.assertEqual(sum(self.q.list_states().values()), 1)

    def test_two_mint_workers_use_two_routes_and_two_approvers(self) -> None:
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"GROK2API_ROUTE_STICKY": "1"}):
            first = self.ctrl.enqueue_after_sso(
                session_id="gba_route_a",
                email="a@example.com",
                sso="eyJa",
                dual_write=False,
            )
            second = self.ctrl.enqueue_after_sso(
                session_id="gba_route_b",
                email="b@example.com",
                sso="eyJb",
                dual_write=False,
            )
        self.assertEqual({first.route_id, second.route_id}, {"route-1", "route-2"})
        registry = reset_registry_for_tests(
            (
                Route("route-1", "http://p1", "http://p1", "http://a1", "a1", "m1"),
                Route("route-2", "http://p2", "http://p2", "http://a2", "a2", "m2"),
            )
        )
        self.assertEqual(
            {registry.approver_for(first.route_id), registry.approver_for(second.route_id)},
            {"http://a1", "http://a2"},
        )

    def _run_with_heartbeat_loss(self):
        path = dual_write_pending(
            session_id="gba_lease_loss",
            email="lease@example.com",
            sso="eyJlease",
            pending_dir=self.pending,
        )
        self.q.import_pending_json(path, route_id="route-1")
        claimed = self.q.claim("test-worker")
        assert claimed is not None
        actions: list[str] = []
        imported: list[str] = []
        heartbeat_calls = 0

        def heartbeat(_job, *, lease_sec=300.0):
            nonlocal heartbeat_calls
            heartbeat_calls += 1
            return heartbeat_calls == 1

        def token_fn(_sso, *, cancel_event=None, lease_guard=None, **_kwargs):
            actions.append("upstream-1")
            assert cancel_event is not None
            self.assertTrue(cancel_event.wait(timeout=2.0))
            if not cancel_event.is_set() and (lease_guard is None or lease_guard()):
                actions.append("upstream-2")
            return {
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_in": 3600,
            }

        def importer(_token, _session_id):
            imported.append("called")
            return {"ok": True}

        with mock.patch.object(self.ctrl, "_heartbeat", side_effect=heartbeat), mock.patch.dict(
            "os.environ", {"GROK2API_LEASE_HEARTBEAT_SEC": "0.05"}
        ):
            result = self.ctrl.process_job(
                claimed,
                sso_to_token=token_fn,
                import_entry=importer,
                require_probe=False,
            )
        return result, actions, imported

    def test_heartbeat_loss_cancels_before_next_upstream_action(self) -> None:
        _result, actions, _imported = self._run_with_heartbeat_loss()
        self.assertEqual(actions, ["upstream-1"])

    def test_heartbeat_loss_never_imports(self) -> None:
        _result, _actions, imported = self._run_with_heartbeat_loss()
        self.assertEqual(imported, [])

    def test_heartbeat_thread_exits(self) -> None:
        self._run_with_heartbeat_loss()
        time.sleep(0.05)
        self.assertFalse(
            any(
                thread.is_alive() and thread.name.startswith("lease-hb-")
                for thread in threading.enumerate()
            )
        )

    def test_process_job_success_probe_import(self) -> None:
        path = dual_write_pending(
            session_id="gba_ok",
            email="ok@example.com",
            sso="eyJtest.sso.value",
            pending_dir=self.pending,
        )
        job = self.q.import_pending_json(path, route_id="route-1")
        assert job is not None
        claimed = self.q.claim("test-worker")
        assert claimed is not None

        def fake_token(sso: str, **kwargs):
            self.assertEqual(sso, "eyJtest.sso.value")
            # route sticky kwargs may be present
            return {
                "access_token": "access-xyz",
                "refresh_token": "refresh-xyz",
                "expires_in": 3600,
            }

        def fake_import(token: dict, session_id: str):
            self.assertEqual(token["access_token"], "access-xyz")
            return {"ok": True, "imported": ["acc1"]}

        def fake_probe(token: dict):
            return {"ok": True}

        out = self.ctrl.process_job(
            claimed,
            sso_to_token=fake_token,
            import_entry=fake_import,
            probe_fn=fake_probe,
            require_probe=True,
        )
        self.assertEqual(out.state, JobState.AUTH_IMPORTED.value)
        self.assertFalse(path.exists())  # pending removed

    def test_probe_failure_not_imported(self) -> None:
        path = dual_write_pending(
            session_id="gba_probe_fail",
            email="p@example.com",
            sso="eyJsso",
            pending_dir=self.pending,
        )
        self.q.import_pending_json(path, route_id="route-1")
        claimed = self.q.claim("test-worker")
        assert claimed is not None

        out = self.ctrl.process_job(
            claimed,
            sso_to_token=lambda sso, **k: {
                "access_token": "a",
                "refresh_token": "r",
                "expires_in": 1,
            },
            import_entry=lambda t, s: {"ok": True},
            probe_fn=lambda t: {"ok": False, "error": "no_grok"},
            require_probe=True,
        )
        # Probe failures are retryable: first failure requeues to mint_queued.
        self.assertIn(out.state, (JobState.MINT_QUEUED.value, JobState.FAILED.value))
        self.assertEqual(out.error_class, "probe_failed")
        # Must not import; SSO retained for retry
        self.assertTrue(path.exists())
        self.assertNotEqual(out.state, JobState.AUTH_IMPORTED.value)

    def test_worker_crash_reclaim(self) -> None:
        path = dual_write_pending(
            session_id="gba_crash",
            email="c@example.com",
            sso="eyJcrash",
            pending_dir=self.pending,
            owner="mint_queue",
        )
        self.q.import_pending_json(path, route_id="route-1")
        c1 = self.q.claim("dead-worker", lease_sec=1)
        assert c1 is not None
        import time

        # Crash while mint_running (not requeued)
        c1.lease_until = time.time() - 10
        self.q.save(c1)
        c2 = self.q.claim("alive-worker")
        self.assertIsNotNone(c2)
        assert c2 is not None
        self.assertEqual(c2.lease_owner, "alive-worker")
        self.assertEqual(c2.state, JobState.MINT_RUNNING.value)

    def test_default_probe_builds_real_credential_shape(self) -> None:
        """Default probe path constructs objects with email/user_id/token attrs.

        Avoids importing auth.py (needs httpx); exercises the shape contract
        that previously raised AttributeError on bare _Creds.
        """
        from types import SimpleNamespace
        from unittest import mock

        path = dual_write_pending(
            session_id="gba_probe_real",
            email="probe@example.com",
            sso="eyJprobe",
            pending_dir=self.pending,
            owner="mint_queue",
        )
        job = self.q.import_pending_json(path, route_id="route-1")
        assert job is not None
        job.payload["email"] = "probe@example.com"

        seen: dict = {}

        class FakeGC:
            def __init__(self, **kwargs):
                self.token = kwargs.get("token")
                self.email = kwargs.get("email")
                self.user_id = kwargs.get("user_id")
                self.expires_at = kwargs.get("expires_at")
                self.auth_key = kwargs.get("auth_key")
                self.refresh_token = kwargs.get("refresh_token")
                seen["creds"] = self

        def fake_probe(creds, model, **kwargs):
            # Would AttributeError on old _Creds
            _ = creds.email, creds.user_id, creds.token, creds.auth_key
            seen["model"] = model
            return {"ok": True, "available": True}

        # Avoid importing real auth/model_health (needs httpx). Inject stubs only.
        import sso_to_auth_json as sso_mod

        with mock.patch.dict(
            "sys.modules",
            {
                "auth": SimpleNamespace(GrokCredentials=FakeGC),
                "model_health": SimpleNamespace(probe_model_for_creds=fake_probe),
                "sso_to_auth_json": sso_mod,
            },
        ):
            result = self.ctrl._run_probe(
                {
                    "access_token": "eyJhbGciOiJub25lIn0.eyJzdWIiOiJ1MSIsImVtYWlsIjoicEBlLmNvbSJ9.",
                    "refresh_token": "r",
                },
                job=job,
                probe_fn=None,
            )
        self.assertTrue(result.get("ok"), result)
        self.assertIn("creds", seen)
        self.assertEqual(seen["creds"].email, "probe@example.com")
        self.assertIsNotNone(seen["creds"].token)
        self.assertIsNone(seen["creds"].auth_key)  # pre-import


if __name__ == "__main__":
    unittest.main()
