"""Cookie A/B must enter production enqueue path."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from registration_controller import RegistrationController
from registration_queue import RegistrationQueue
from route_registry import Route, reset_registry_for_tests


class CookieExperimentPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "q.db"
        self.pending = Path(self.tmp.name) / "pending"
        self.bundles = Path(self.tmp.name) / "bundles"
        self.bundles.mkdir()
        os.environ["GROK2API_COOKIE_BUNDLE_DIR"] = str(self.bundles)
        os.environ["GROK2API_DATA_DIR"] = self.tmp.name
        os.environ["GROK2API_COOKIE_MODE"] = "sso_only"
        os.environ["GROK2API_COOKIE_EXPERIMENT_PERCENT"] = "100"
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
        self.q = RegistrationQueue(self.db)
        self.ctrl = RegistrationController(self.q, worker_id="cw")

    def tearDown(self) -> None:
        for k in (
            "GROK2API_COOKIE_BUNDLE_DIR",
            "GROK2API_COOKIE_MODE",
            "GROK2API_COOKIE_EXPERIMENT_PERCENT",
            "GROK2API_DATA_DIR",
        ):
            os.environ.pop(k, None)
        self.tmp.cleanup()

    def test_pipeline_enqueue_honors_cookie_experiment(self) -> None:
        job = self.ctrl.enqueue_after_sso(
            session_id="sess_exp_1",
            email="e@example.com",
            sso="eyJtest",
            dual_write=True,
            session_cookies={"sso": "eyJtest", "sso-rw": "eyJtest"},
            cookie_mode=None,  # auto resolve
        )
        self.assertEqual(job.cookie_mode, "auth_bundle")

    def test_ttl_sweeper_deletes_unread_expired_bundle(self) -> None:
        import cookie_bundle as cb
        import time

        meta = cb.write_bundle(
            {"sso": "x", "sso-rw": "x"},
            session_id="old",
            mode="auth_bundle",
        )
        path = Path(meta["path"])
        # backdate mtime
        old = time.time() - 999999
        os.utime(path, (old, old))
        deleted = cb.sweep_expired(root=self.bundles, max_age_sec=60)
        self.assertGreaterEqual(deleted, 1)
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
