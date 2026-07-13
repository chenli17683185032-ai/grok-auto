"""Round 8: production free-usage probe default path (mock HTTP boundary)."""

from __future__ import annotations

import time
import unittest
from unittest import mock


class FreeUsageProbeDefaultPathTests(unittest.TestCase):
    def test_real_default_quota_reprobe_uses_responses_not_billing(self) -> None:
        import account_pool as ap
        from auth import GrokCredentials

        store = {
            "a1": {
                "enabled": True,
                "quota_waiting": True,
                "quota_reset_at": time.time() - 10,
                "quota_next_probe_at": time.time() - 5,
                "quota_waiting_since": time.time() - 90000,
            }
        }
        creds = GrokCredentials(
            token="tok", auth_key="a1", email="e@x.com", expires_at=time.time() + 3600
        )
        seen_urls: list[str] = []

        class FakeResp:
            status_code = 200
            text = '{"id":"1"}'
            headers = {
                "x-ratelimit-limit-tokens": "1000000",
                "x-ratelimit-remaining-tokens": "999000",
            }

            def json(self):
                return {"id": "1"}

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, headers=None, json=None):
                seen_urls.append(url)
                # Never require Authorization log; just ensure header present without printing
                assert headers and "Authorization" in headers
                return FakeResp()

            def get(self, url, headers=None):
                raise AssertionError(f"billing GET must not be used: {url}")

        def get_state():
            return dict(store)

        def save_state(s):
            store.clear()
            store.update(s)

        with mock.patch.object(ap, "get_account_pool_state", side_effect=get_state), mock.patch.object(
            ap, "save_account_pool_state", side_effect=save_state
        ), mock.patch.object(ap, "list_pool_accounts", return_value=[]), mock.patch(
            "auth.load_credentials_by_id", return_value=creds
        ), mock.patch("httpx.Client", FakeClient), mock.patch.object(
            ap, "clear_quota_waiting", side_effect=lambda aid, source="x": store[aid].update(
                {"quota_waiting": False, "enabled": True, "quota_status": "active"}
            )
            or {"id": aid, "quota_waiting": False}
        ):
            # Production default path — no custom probe_fn
            stats = ap.process_quota_probe_due(max_n=5)

        self.assertTrue(any("/responses" in u or "/chat/completions" in u for u in seen_urls))
        self.assertFalse(any("/billing" in u for u in seen_urls))
        self.assertEqual(stats["due"], 1)
        self.assertEqual(stats["recovered"], 1)

    def test_explicit_free_usage_exhaustion_from_probe(self) -> None:
        import quota as q
        from auth import GrokCredentials

        creds = GrokCredentials(token="t", auth_key="a", expires_at=time.time() + 1000)

        class FakeResp:
            status_code = 429
            text = "subscription:free-usage-exhausted tokens (actual/limit): 1000001/1000000"
            headers = {"x-ratelimit-limit-tokens": "1000000", "x-ratelimit-remaining-tokens": "0"}

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, *a, **k):
                return FakeResp()

        with mock.patch("httpx.Client", FakeClient):
            result = q.probe_free_usage_for_creds(creds)
        self.assertTrue(result["exhausted"])
        self.assertFalse(result["free_usage_ok"])
        self.assertFalse(result["inconclusive"])

    def test_generic_429_inconclusive(self) -> None:
        import quota as q
        from auth import GrokCredentials

        creds = GrokCredentials(token="t", auth_key="a", expires_at=time.time() + 1000)

        class FakeResp:
            status_code = 429
            text = "rate limit please retry"
            headers = {}

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, *a, **k):
                return FakeResp()

        with mock.patch("httpx.Client", FakeClient):
            result = q.probe_free_usage_for_creds(creds)
        self.assertTrue(result["inconclusive"])
        self.assertFalse(result["exhausted"])

    def test_repeated_exhaustion_does_not_extend_existing_reset(self) -> None:
        import account_pool as ap

        store: dict = {}
        fixed_reset = time.time() + 10000

        def get_state():
            return dict(store)

        def save_state(s):
            store.clear()
            store.update(s)

        with mock.patch.object(ap, "get_account_pool_state", side_effect=get_state), mock.patch.object(
            ap, "save_account_pool_state", side_effect=save_state
        ), mock.patch.object(ap, "list_pool_accounts", return_value=[]):
            ap.mark_quota_waiting("a1", reason="exhausted", reset_at=fixed_reset)
            first = store["a1"]["quota_reset_at"]
            ap.mark_quota_waiting("a1", reason="exhausted again")  # fallback must not extend
            second = store["a1"]["quota_reset_at"]
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
