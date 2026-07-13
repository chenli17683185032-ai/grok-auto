"""Unit tests for metrics emission and redaction."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from registration_metrics import RegistrationMetrics, protection_action, reset_metrics_for_tests


class MetricsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "m.db"
        self.m = reset_metrics_for_tests(self.db)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_emit_and_funnel(self) -> None:
        self.m.emit("signup_started", session_id="s1", ok=True)
        self.m.emit("sso_obtained", session_id="s1", ok=True)
        self.m.emit(
            "auth_imported",
            session_id="s1",
            ok=True,
            sso="SHOULD_NOT_STORE",
            access_token="nope",
        )
        funnel = self.m.funnel()
        self.assertEqual(funnel["signup_started"], 1)
        self.assertEqual(funnel["sso_obtained"], 1)
        self.assertEqual(funnel["auth_imported"], 1)
        # Ensure banned fields not in db text
        raw = self.db.read_text(errors="ignore")
        self.assertNotIn("SHOULD_NOT_STORE", raw)
        self.assertNotIn("nope", raw)

    def test_protection_action(self) -> None:
        a = protection_action({"MemAvailable": int(1.5 * 1024**3)})
        self.assertTrue(a["pause_registration"])
        self.assertTrue(a["pause_mint"])
        b = protection_action({"MemAvailable": int(2.2 * 1024**3)})
        self.assertTrue(b["mint_single_route"])
        c = protection_action({"MemAvailable": int(4 * 1024**3)})
        self.assertFalse(c["pause_registration"])


if __name__ == "__main__":
    unittest.main()
