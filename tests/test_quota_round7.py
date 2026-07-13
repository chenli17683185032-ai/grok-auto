"""Round 7: acquire never returns waiting; probe-due; narrow free-usage; permanent vs waiting."""

from __future__ import annotations

import time
import unittest
from unittest import mock


class AcquireNeverWaitingTests(unittest.TestCase):
    def test_acquire_fallback_excludes_waiting(self) -> None:
        import account_pool as ap
        from auth import AuthError, GrokCredentials

        waiting = GrokCredentials(
            token="t", auth_key="wait", expires_at=time.time() + 3600
        )
        state = {
            "wait": {
                "enabled": True,
                "quota_waiting": True,
                "quota_reset_at": time.time() + 1000,
            }
        }
        with mock.patch.object(ap, "list_live_credentials", return_value=[waiting]), mock.patch.object(
            ap, "get_account_pool_state", return_value=state
        ), mock.patch.object(ap, "_ensure_multi_account_layout"):
            with self.assertRaises(AuthError):
                ap.acquire()

    def test_try_acquire_sequence_empty_when_all_waiting(self) -> None:
        import account_pool as ap
        from auth import GrokCredentials

        waiting = GrokCredentials(
            token="t", auth_key="wait", expires_at=time.time() + 3600
        )
        state = {
            "wait": {
                "enabled": True,
                "quota_waiting": True,
                "quota_reset_at": time.time() + 1000,
            }
        }
        with mock.patch.object(ap, "list_live_credentials", return_value=[waiting]), mock.patch.object(
            ap, "get_account_pool_state", return_value=state
        ), mock.patch.object(ap, "_ensure_multi_account_layout"):
            seq = ap.try_acquire_sequence()
        self.assertEqual(seq, [])


class FreeUsageDetectionTests(unittest.TestCase):
    def test_remaining_positive_not_exhausted(self) -> None:
        import quota as q

        self.assertFalse(
            q.is_free_usage_exhausted_message(
                "limit_tokens=1000000 remaining_tokens=500000", 200
            )
        )

    def test_free_usage_exhausted_detected(self) -> None:
        import quota as q

        self.assertTrue(
            q.is_free_usage_exhausted_message(
                "subscription:free-usage-exhausted", 429
            )
        )


class BillingDoesNotClearWaitTests(unittest.TestCase):
    def test_healthy_billing_keeps_waiting(self) -> None:
        import account_pool as ap
        import quota as q

        state = {
            "a1": {
                "enabled": True,
                "quota_waiting": True,
                "quota_reset_at": time.time() + 5000,
            }
        }
        with mock.patch.object(ap, "get_account_pool_state", return_value=state), mock.patch.object(
            ap, "save_account_pool_state"
        ), mock.patch.object(ap, "save_quota_snapshot"), mock.patch.object(
            ap, "clear_quota_waiting"
        ) as clear:
            result = {
                "ok": True,
                "exhausted": False,
                "account_id": "a1",
                "display": {},
            }
            out = q.maybe_disable_from_quota_result(result)
        clear.assert_not_called()
        self.assertFalse(out.get("quota_recovered"))


class CredentialSuspendedTests(unittest.TestCase):
    def test_account_suspended_not_quota_waiting(self) -> None:
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
            out = ap.mark_credential_suspended("bad", reason="account_suspended")
        self.assertTrue(out["credential_suspended"])
        self.assertFalse(out.get("quota_waiting", False))
        self.assertFalse(out["enabled"])


class ProbeDueTests(unittest.TestCase):
    def test_process_probe_due_recovers(self) -> None:
        import account_pool as ap

        store = {
            "a1": {
                "enabled": True,
                "quota_waiting": True,
                "quota_reset_at": time.time() - 10,
                "quota_next_probe_at": time.time() - 5,
            }
        }

        def get_state():
            return dict(store)

        def save_state(s):
            store.clear()
            store.update(s)

        def probe_fn(aid):
            return {"ok": True, "exhausted": False, "free_usage_ok": True, "quota_recovered": True}

        with mock.patch.object(ap, "get_account_pool_state", side_effect=get_state), mock.patch.object(
            ap, "save_account_pool_state", side_effect=save_state
        ), mock.patch.object(ap, "list_pool_accounts", return_value=[]), mock.patch.object(
            ap, "clear_quota_waiting", side_effect=lambda aid, source="x": store[aid].update(
                {"quota_waiting": False, "enabled": True}
            ) or {"id": aid, "quota_waiting": False, "enabled": True}
        ):
            stats = ap.process_quota_probe_due(probe_fn=probe_fn, max_n=5)
        self.assertEqual(stats["due"], 1)
        self.assertEqual(stats["recovered"], 1)


class ProducerEffectiveTests(unittest.TestCase):
    def test_waiting_not_effective(self) -> None:
        import registration_producer as prod

        row = {
            "expired": False,
            "enabled": True,
            "disabled_for_quota": False,
            "quota_waiting": True,
            "refresh_invalid": False,
            "has_refresh_token": True,
            "blocked_model_ids": [],
        }
        self.assertFalse(prod._is_effective_account(row))


if __name__ == "__main__":
    unittest.main()
