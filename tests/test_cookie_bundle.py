"""Unit tests for cookie bundle whitelist, permissions, TTL, path safety."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

import cookie_bundle as cb


class CookieBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "bundles"
        os.environ["GROK2API_COOKIE_MODE"] = "sso_only"
        os.environ["GROK2API_COOKIE_EXPERIMENT_PERCENT"] = "0"
        os.environ["GROK2API_COOKIE_ALLOW_CF"] = "0"

    def tearDown(self) -> None:
        self.tmp.cleanup()
        for k in (
            "GROK2API_COOKIE_MODE",
            "GROK2API_COOKIE_EXPERIMENT_PERCENT",
            "GROK2API_COOKIE_ALLOW_CF",
        ):
            os.environ.pop(k, None)

    def test_sso_only_filters_cf(self) -> None:
        items = cb.normalize_cookie_items(
            {
                "sso": "AAA",
                "sso-rw": "AAA",
                "cf_clearance": "nope",
                "other": "x",
            },
            mode="sso_only",
        )
        names = {i["name"] for i in items}
        self.assertIn("sso", names)
        self.assertNotIn("cf_clearance", names)
        self.assertNotIn("other", names)

    def test_auth_bundle_cf_requires_flag(self) -> None:
        items = cb.normalize_cookie_items(
            {"sso": "A", "cf_clearance": "C"},
            mode="auth_bundle",
        )
        self.assertNotIn("cf_clearance", {i["name"] for i in items})
        os.environ["GROK2API_COOKIE_ALLOW_CF"] = "1"
        items2 = cb.normalize_cookie_items(
            {"sso": "A", "cf_clearance": "C"},
            mode="auth_bundle",
        )
        self.assertIn("cf_clearance", {i["name"] for i in items2})

    def test_write_permissions_and_read(self) -> None:
        meta = cb.write_bundle(
            {"sso": "TOKEN", "sso-rw": "TOKEN"},
            session_id="sess_1",
            mode="sso_only",
            bundle_dir=self.root,
            ttl_sec=3600,
        )
        self.assertTrue(meta["ok"])
        path = Path(meta["path"])
        self.assertEqual(oct(path.stat().st_mode)[-3:], "600")
        self.assertEqual(oct(self.root.stat().st_mode)[-3:], "700")
        data = cb.read_bundle(path)
        self.assertIsNotNone(data)
        assert data is not None
        self.assertEqual(data["mode"], "sso_only")
        # public meta has no values
        pub = cb.public_meta(path)
        self.assertTrue(pub["present"])
        self.assertNotIn("TOKEN", str(pub))

    def test_ttl_expiry(self) -> None:
        meta = cb.write_bundle(
            {"sso": "T"},
            session_id="sess_ttl",
            mode="sso_only",
            bundle_dir=self.root,
            ttl_sec=60,
        )
        path = Path(meta["path"])
        # Force expiry
        raw = path.read_text(encoding="utf-8")
        import json

        data = json.loads(raw)
        data["expires_at"] = time.time() - 1
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNone(cb.read_bundle(path))

    def test_path_traversal_rejected(self) -> None:
        cb.ensure_bundle_dir(self.root)
        with self.assertRaises(ValueError):
            cb._safe_resolve(self.root, "../etc/passwd")

    def test_experiment_mode_resolution(self) -> None:
        os.environ["GROK2API_COOKIE_MODE"] = "sso_only"
        os.environ["GROK2API_COOKIE_EXPERIMENT_PERCENT"] = "0"
        self.assertEqual(cb.resolve_mode_for_session("s1"), "sso_only")
        os.environ["GROK2API_COOKIE_EXPERIMENT_PERCENT"] = "100"
        self.assertEqual(cb.resolve_mode_for_session("s1"), "auth_bundle")

    def test_inject_list_defaults_sso(self) -> None:
        cookies = cb.inject_list_for_approver(None, sso="SSOVAL", mode="sso_only")
        names = [c["name"] for c in cookies]
        self.assertEqual(names.count("sso"), 1)
        self.assertTrue(any(c["value"] == "SSOVAL" for c in cookies))


if __name__ == "__main__":
    unittest.main()
