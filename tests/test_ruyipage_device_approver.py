"""Unit tests for device_approver helpers (no real browser)."""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def _load_approver_module():
    """Load device_approver with mocked ruyipage dependency."""
    path = Path(__file__).resolve().parents[1] / "integrations" / "ruyipage-runtime" / "device_approver.py"
    # Stub ruyipage before load
    fake = mock.MagicMock()
    sys.modules["ruyipage"] = fake
    # fastapi / pydantic may exist; if not, stub
    try:
        import fastapi  # noqa: F401
        import pydantic  # noqa: F401
    except ImportError:
        sys.modules.setdefault("fastapi", mock.MagicMock())
        sys.modules.setdefault("pydantic", mock.MagicMock())
        # Minimal BaseModel stand-in
        class _BM:
            def __init__(self, **kwargs):
                for k, v in kwargs.items():
                    setattr(self, k, v)

            @classmethod
            def __class_getitem__(cls, item):
                return cls

        sys.modules["pydantic"].BaseModel = _BM
        sys.modules["pydantic"].Field = lambda *a, **k: None
        sys.modules["fastapi"].FastAPI = mock.MagicMock
        sys.modules["fastapi"].HTTPException = type("HTTPException", (Exception,), {})

    spec = importlib.util.spec_from_file_location("device_approver_under_test", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # Provide Field default_factory support if real pydantic missing handled above
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:
        raise unittest.SkipTest(f"cannot load device_approver: {exc}") from exc
    return mod


class DeviceApproverHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_approver_module()

    def test_warm_flag_default_off(self) -> None:
        os.environ.pop("GROK2API_RUYIPAGE_WARM_BROWSER", None)
        os.environ.pop("RUYIPAGE_WARM_BROWSER", None)
        self.assertFalse(self.mod.warm_enabled())
        os.environ["GROK2API_RUYIPAGE_WARM_BROWSER"] = "1"
        self.assertTrue(self.mod.warm_enabled())
        os.environ["GROK2API_RUYIPAGE_WARM_BROWSER"] = "0"

    def test_success_and_denied_url(self) -> None:
        self.assertTrue(self.mod._success_url("https://auth.x.ai/oauth2/device/done"))
        self.assertTrue(self.mod._rate_limited_url("https://x/rate_limited"))
        self.assertTrue(self.mod._denied_url("https://auth.x.ai/oauth2/error?error=access_denied"))
        # Critical: done path with denied query must NOT be success
        denied_done = "https://auth.x.ai/oauth2/device/done?error=access_denied"
        self.assertTrue(self.mod._denied_url(denied_done))
        self.assertFalse(self.mod._success_url(denied_done))
        denied_done2 = "https://auth.x.ai/oauth2/device/done?denied=1"
        self.assertTrue(self.mod._denied_url(denied_done2))
        self.assertFalse(self.mod._success_url(denied_done2))

    def test_cookie_list_sso_only(self) -> None:
        # Avoid pydantic forward-ref rebuild issues: pass a simple namespace object.
        from types import SimpleNamespace

        r = SimpleNamespace(
            sso="SECRET",
            verification_url="https://auth.x.ai/device",
            cookie_mode="sso_only",
            cookie_bundle_path="",
            extra_cookies=[{"name": "cf_clearance", "value": "x"}],
        )
        cookies = self.mod._cookie_list(r)
        names = [c["name"] for c in cookies]
        self.assertIn("sso", names)
        r2 = SimpleNamespace(
            sso="SECRET",
            verification_url="https://x",
            cookie_mode="sso_only",
            cookie_bundle_path="",
            extra_cookies=[{"name": "password", "value": "p"}],
        )
        cookies2 = self.mod._cookie_list(r2)
        self.assertFalse(any(c["name"] == "password" for c in cookies2))

    def test_recycle_thresholds(self) -> None:
        os.environ["GROK2API_RUYIPAGE_RECYCLE_TASKS"] = "10"
        self.assertEqual(self.mod.recycle_tasks(), 10)
        os.environ["GROK2API_RUYIPAGE_RECYCLE_SEC"] = "5400"
        self.assertEqual(self.mod.recycle_sec(), 5400.0)


if __name__ == "__main__":
    unittest.main()
