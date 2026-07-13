"""Round-4 P0 regressions: secrets, fencing, pending claim."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


class SaveSsoExplicitOnlyTests(unittest.TestCase):
    def test_client_save_requires_explicit_true(self) -> None:
        cpath = Path(__file__).resolve().parents[1] / "grok-build-auth/xconsole_client/client.py"
        src = cpath.read_text(encoding="utf-8")
        self.assertIn("if token and save:", src)
        self.assertNotIn("save or email", src)


class CompactSessionRedactionTests(unittest.TestCase):
    def test_recursive_redaction(self) -> None:
        import grok_build_adapter as ad

        sess = {
            "password": "secret-pass",
            "sso": "eyJhbGciOiJub25lIn0.aaa.bbb",
            "error": "failed password=secret-pass token=eyJhbGciOiJub25lIn0.xxx.yyy",
            "nested": {"access_token": "eyJabc", "msg": "ok"},
            "proxy": "http://user:pass@host:7890",
        }
        out = ad._compact_session(sess)
        blob = json.dumps(out)
        self.assertNotIn("secret-pass", blob)
        self.assertNotIn("eyJ", blob)
        self.assertNotIn("user:pass", blob)
        self.assertNotIn("sso_prefix", out)
        self.assertTrue(out.get("sso_present") or "sso" not in out)


class FencedTerminalTests(unittest.TestCase):
    def test_stale_worker_cannot_terminal_overwrite(self) -> None:
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
            c1 = q.claim("w1", lease_sec=1)
            assert c1 is not None
            c1.lease_until = time.time() - 5
            q.save(c1)
            c2 = q.claim("w2", lease_sec=60)
            assert c2 is not None
            # stale tries terminal import
            c1.state = JobState.AUTH_IMPORTED.value
            ok = q.save_terminal(c1)
            self.assertFalse(ok)
            fresh = q.get(c2.job_id)
            assert fresh is not None
            self.assertEqual(fresh.lease_owner, "w2")
            self.assertNotEqual(fresh.state, JobState.AUTH_IMPORTED.value)


class PendingClaimTests(unittest.TestCase):
    def test_two_recovery_processes_claim_once(self) -> None:
        import registration_producer as prod

        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            path = d / "gba_x.json"
            path.write_text(
                json.dumps(
                    {
                        "session_id": "gba_x",
                        "email": "a@b.c",
                        "sso": "eyJsso",
                        "created_at": 1,
                    }
                ),
                encoding="utf-8",
            )
            c1 = prod._claim_pending_file(path)
            c2 = prod._claim_pending_file(path)
            self.assertIsNotNone(c1)
            self.assertIsNone(c2)
            self.assertFalse(path.exists())
            self.assertTrue(c1.exists())


if __name__ == "__main__":
    unittest.main()
