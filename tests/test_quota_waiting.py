"""Quota waiting state machine — not permanent disable/delete."""

from __future__ import annotations

import time
import unittest
from unittest import mock


class QuotaWaitingTests(unittest.TestCase):
    def test_mark_quota_waiting_keeps_enabled_for_token_refresh(self) -> None:
        import account_pool as ap

        store: dict = {}

        def get_state():
            return dict(store)

        def save_state(s):
            store.clear()
            store.update(s)

        with mock.patch.object(ap, "get_account_pool_state", side_effect=get_state), mock.patch.object(
            ap, "save_account_pool_state", side_effect=save_state
        ), mock.patch.object(ap, "list_pool_accounts", return_value=[]):
            out = ap.mark_quota_waiting(
                "acct1",
                reason="subscription:free-usage-exhausted",
                reset_at=time.time() + 86400,
                limit_tokens=1_000_000,
                remaining_tokens=0,
            )
        self.assertTrue(out["quota_waiting"])
        self.assertTrue(out["enabled"])
        self.assertFalse(out["disabled_for_quota"])
        self.assertIn("acct1", store)
        self.assertTrue(ap.is_quota_waiting(store["acct1"]))

    def test_eligible_excludes_quota_waiting(self) -> None:
        import account_pool as ap
        from auth import GrokCredentials

        creds = GrokCredentials(token="t", auth_key="a1", expires_at=time.time() + 3600)
        state = {
            "a1": {
                "enabled": True,
                "quota_waiting": True,
                "quota_reset_at": time.time() + 1000,
            }
        }
        self.assertFalse(ap._eligible(creds, state))

    def test_is_free_usage_exhausted_message(self) -> None:
        import quota as q

        self.assertTrue(
            q.is_free_usage_exhausted_message(
                "subscription:free-usage-exhausted limit_tokens=1000000", 429
            )
        )
        self.assertTrue(q.is_quota_error_message("free-usage-exhausted", 403))

    def test_producer_cleanup_skips_quota_waiting(self) -> None:
        import registration_producer as prod

        row = {
            "id": "x",
            "disabled_for_quota": True,
            "quota_waiting": True,
            "has_refresh_token": True,
            "enabled": True,
        }
        self.assertIsNone(prod._cleanup_reason(row))

    def test_clear_quota_waiting_reactivates(self) -> None:
        import account_pool as ap

        state = {
            "a1": {
                "enabled": True,
                "quota_waiting": True,
                "disabled_for_quota": True,
                "quota_reset_at": time.time() - 10,
            }
        }
        with mock.patch.object(ap, "get_account_pool_state", return_value=state), mock.patch.object(
            ap, "save_account_pool_state"
        ), mock.patch.object(ap, "list_pool_accounts", return_value=[]):
            out = ap.clear_quota_waiting("a1")
        self.assertFalse(out["quota_waiting"])
        self.assertTrue(out["enabled"])


if __name__ == "__main__":
    unittest.main()
