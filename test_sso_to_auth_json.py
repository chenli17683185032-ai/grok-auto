"""Unit tests for ruyiPage sidecar selection and failover (no network)."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import sso_to_auth_json as sso_import


class _Response:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self._body


class ApproverPoolTests(unittest.TestCase):
    def setUp(self) -> None:
        sso_import._approver_rotation_index = 0
        self._lock_tmp = tempfile.TemporaryDirectory()
        self.dc = {
            "verification_uri_complete": "https://verify.example/device?code=test",
            "user_code": "TEST-CODE",
        }

    def tearDown(self) -> None:
        self._lock_tmp.cleanup()

    def _env(self, plural: str = "", singular: str = ""):
        return patch.dict(
            os.environ,
            {
                "GROK2API_RUYIPAGE_APPROVERS": plural,
                "GROK2API_RUYIPAGE_APPROVER": singular,
                "GROK2API_RUYIPAGE_TIMEOUT": "10",
                "GROK2API_APPROVER_LOCK_DIR": self._lock_tmp.name,
                "GROK2API_APPROVER_LOCK_WAIT_SEC": "1",
                "GROK2API_APPROVER_LOCK_POLL_SEC": "0.01",
            },
        )

    def test_plural_setting_rotates_start_sidecar(self) -> None:
        called: list[str] = []

        def fake_urlopen(req, timeout):
            self.assertEqual(timeout, 40)
            called.append(req.full_url)
            return _Response({"ok": True, "url": "https://verify.example/done"})

        with self._env("http://a:8765, http://b:8765/,http://c:8765"), patch.object(
            sso_import.urllib.request, "urlopen", side_effect=fake_urlopen
        ):
            for _ in range(4):
                self.assertTrue(
                    sso_import.browser_approve_device("test-sso", self.dc)["ok"]
                )

        self.assertEqual(
            called,
            [
                "http://a:8765/approve",
                "http://b:8765/approve",
                "http://c:8765/approve",
                "http://a:8765/approve",
            ],
        )

    def test_transport_failure_switches_within_same_flow(self) -> None:
        called: list[str] = []

        def fake_urlopen(req, timeout):
            called.append(req.full_url)
            if req.full_url.startswith("http://a:"):
                raise urllib.error.URLError("sidecar down")
            return _Response({"ok": True, "url": "https://verify.example/done"})

        with self._env("http://a:8765,http://b:8765"), patch.object(
            sso_import.urllib.request, "urlopen", side_effect=fake_urlopen
        ):
            result = sso_import.browser_approve_device("test-sso", self.dc)

        self.assertTrue(result["ok"])
        self.assertEqual(
            called, ["http://a:8765/approve", "http://b:8765/approve"]
        )

    def test_structured_browser_failure_is_not_replayed(self) -> None:
        called: list[str] = []

        def fake_urlopen(req, timeout):
            called.append(req.full_url)
            return _Response(
                {
                    "ok": False,
                    "denied": True,
                    "url": "https://accounts.x.ai/oauth2/device/done?denied=1",
                }
            )

        with self._env("http://a:8765,http://b:8765"), patch.object(
            sso_import.urllib.request, "urlopen", side_effect=fake_urlopen
        ):
            result = sso_import.browser_approve_device("test-sso", self.dc)

        self.assertTrue(result["denied"])
        self.assertEqual(called, ["http://a:8765/approve"])

    def test_rate_limit_is_not_replayed(self) -> None:
        called: list[str] = []
        sso_import._approver_rotation_index = 0
        with self._env("http://a:8765,http://b:8765"), patch.object(
            sso_import.urllib.request,
            "urlopen",
            side_effect=lambda req, timeout: (
                called.append(req.full_url)
                or _Response({"ok": False, "rate_limited": True})
            ),
        ):
            result = sso_import.browser_approve_device("test-sso", self.dc)

        self.assertTrue(result["rate_limited"])
        self.assertEqual(called, ["http://a:8765/approve"])

    def test_busy_sidecar_is_skipped_without_global_serialisation(self) -> None:
        called: list[str] = []
        with self._env("http://a:8765,http://b:8765"):
            lease = sso_import._try_lock_approver("http://a:8765")
            self.assertIsNotNone(lease)
            try:
                with patch.object(
                    sso_import.urllib.request,
                    "urlopen",
                    side_effect=lambda req, timeout: (
                        called.append(req.full_url)
                        or _Response({"ok": True, "url": "https://verify.example/done"})
                    ),
                ):
                    result = sso_import.browser_approve_device("test-sso", self.dc)
            finally:
                sso_import._unlock_approver(lease)

        self.assertTrue(result["ok"])
        self.assertEqual(called, ["http://b:8765/approve"])

    def test_concurrent_calls_are_evenly_distributed(self) -> None:
        called: list[str] = []
        called_lock = threading.Lock()
        active: set[str] = set()
        overlap: list[str] = []

        def fake_urlopen(req, timeout):
            endpoint = req.full_url.rsplit("/approve", 1)[0]
            with called_lock:
                called.append(req.full_url)
                if endpoint in active:
                    overlap.append(endpoint)
                active.add(endpoint)
            time.sleep(0.005)
            with called_lock:
                active.remove(endpoint)
            return _Response({"ok": True, "url": "https://verify.example/done"})

        with self._env("http://a:8765,http://b:8765,http://c:8765"), patch.object(
            sso_import.urllib.request, "urlopen", side_effect=fake_urlopen
        ):
            with ThreadPoolExecutor(max_workers=12) as executor:
                results = list(
                    executor.map(
                        lambda _: sso_import.browser_approve_device(
                            "test-sso", self.dc
                        ),
                        range(60),
                    )
                )

        self.assertTrue(all(result["ok"] for result in results))
        self.assertEqual(len(called), 60)
        self.assertFalse(overlap)
        self.assertTrue(all(called.count(f"http://{name}:8765/approve") > 0 for name in "abc"))

    def test_singular_setting_remains_supported(self) -> None:
        with self._env(singular="http://legacy:8765/"), patch.object(
            sso_import.urllib.request,
            "urlopen",
            return_value=_Response({"ok": True}),
        ) as urlopen:
            result = sso_import.browser_approve_device("test-sso", self.dc)

        self.assertTrue(result["ok"])
        self.assertEqual(urlopen.call_args.args[0].full_url, "http://legacy:8765/approve")


if __name__ == "__main__":
    unittest.main()
