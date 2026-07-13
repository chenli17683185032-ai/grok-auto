"""Round 8 production acceptance coverage for quota and refresh lifecycles."""

from __future__ import annotations

import asyncio
import copy
import time
import unittest
from contextlib import contextmanager
from email.utils import formatdate
from unittest import mock


@contextmanager
def _pool_store(store: dict):
    import account_pool as ap

    def get_state():
        return copy.deepcopy(store)

    def save_state(value):
        store.clear()
        store.update(copy.deepcopy(value))

    with mock.patch.object(
        ap, "get_account_pool_state", side_effect=get_state
    ), mock.patch.object(
        ap, "save_account_pool_state", side_effect=save_state
    ), mock.patch.object(ap, "list_pool_accounts", return_value=[]):
        yield


@contextmanager
def _auth_store(store: dict):
    import oidc_auth as oidc

    def read_map():
        return copy.deepcopy(store)

    def mutate(fn):
        value = copy.deepcopy(store)
        fn(value)
        store.clear()
        store.update(copy.deepcopy(value))
        return copy.deepcopy(value)

    with mock.patch.object(oidc, "read_auth_map", side_effect=read_map), mock.patch.object(
        oidc, "mutate_auth_map", side_effect=mutate
    ):
        yield


def _refresh_entry(*, expires_at: float = 1200.0) -> dict:
    return {
        "key": "access-old",
        "refresh_token": "refresh-old",
        "expires_at": expires_at,
        "user_id": "user-1",
    }


class QuotaLifecycleAcceptanceTests(unittest.TestCase):
    def test_first_quota_exhaustion_initializes_full_cycle(self) -> None:
        import account_pool as ap

        store: dict = {}
        with _pool_store(store), mock.patch.object(ap, "_now", return_value=1000.0):
            ap.mark_quota_waiting("a1", reason="free-usage-exhausted", reset_at=2000.0)

        row = store["a1"]
        self.assertTrue(row["quota_cycle_id"].startswith("qc_1000_"))
        self.assertEqual(row["quota_waiting_since"], 1000.0)
        self.assertEqual(row["quota_reset_at"], 2000.0)
        self.assertEqual(row["quota_next_probe_at"], 2000.0)
        self.assertEqual(row["quota_limit_tokens"], 1_000_000)
        self.assertEqual(row["quota_remaining_tokens"], 0)
        self.assertEqual(row["quota_grace_count"], 0)
        self.assertEqual(row["quota_confirmation_count"], 0)
        self.assertEqual(row["quota_status"], "quota_waiting")

    def test_first_quota_exhaustion_without_header_waits_24h(self) -> None:
        import account_pool as ap

        store: dict = {}
        with _pool_store(store), mock.patch.object(ap, "_now", return_value=1000.0):
            ap.mark_quota_waiting("a1")
        self.assertEqual(store["a1"]["quota_reset_at"], 1000.0 + 86400.0)
        self.assertEqual(store["a1"]["quota_next_probe_at"], 1000.0 + 86400.0)

    def test_quota_waiting_fallback_preserves_manual_disable(self) -> None:
        import account_pool as ap

        store = {"a1": {"enabled": False, "manual_disabled": True}}
        with _pool_store(store), mock.patch.object(ap, "_now", return_value=1000.0):
            result = ap.mark_quota_waiting("a1")

        self.assertFalse(store["a1"]["enabled"])
        self.assertFalse(result["enabled"])

    def test_quota_waiting_preserves_credential_suspend_reason(self) -> None:
        import account_pool as ap

        store = {
            "a1": {
                "enabled": False,
                "credential_suspended": True,
                "disabled_reason": "credential revoked",
            }
        }
        with _pool_store(store), mock.patch.object(ap, "_now", return_value=1000.0):
            ap.mark_quota_waiting("a1")
            ap.clear_quota_waiting("a1")

        self.assertTrue(store["a1"]["credential_suspended"])
        self.assertEqual(store["a1"]["disabled_reason"], "credential revoked")

    def test_default_quota_probe_passes_explicit_proxy(self) -> None:
        import account_pool as ap
        import config
        from auth import GrokCredentials

        store = {
            "a1": {
                "enabled": True,
                "quota_waiting": True,
                "quota_reset_at": 900.0,
                "quota_next_probe_at": 900.0,
            }
        }
        seen: list[dict] = []

        class Response:
            status_code = 200
            text = '{"id":"ok"}'
            headers = {}

        class Client:
            def __init__(self, **kwargs):
                seen.append(kwargs)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def post(self, *_args, **_kwargs):
                return Response()

        creds = GrokCredentials(token="token", auth_key="a1", expires_at=5000.0)
        with _pool_store(store), mock.patch(
            "auth.load_credentials_by_id", return_value=creds
        ), mock.patch("httpx.Client", Client), mock.patch.object(
            config, "XAI_PROXY", "http://proxy.internal:7890"
        ):
            result = ap.process_quota_probe_due(now=1000.0)

        self.assertEqual(result["recovered"], 1)
        self.assertEqual(seen[0]["proxy"], "http://proxy.internal:7890")
        self.assertFalse(seen[0]["trust_env"])

    def test_quota_probe_credential_error_suspends_account(self) -> None:
        import account_pool as ap

        store = {
            "a1": {
                "enabled": True,
                "quota_waiting": True,
                "quota_reset_at": 900.0,
                "quota_next_probe_at": 900.0,
            }
        }
        result = {
            "ok": False,
            "inconclusive": False,
            "exhausted": False,
            "error_class": "credential",
            "error": "account_suspended",
        }
        with _pool_store(store):
            stats = ap.process_quota_probe_due(
                now=1000.0, probe_fn=lambda _aid: result
            )
        self.assertEqual(stats["failed"], 1)
        self.assertTrue(store["a1"]["credential_suspended"])
        self.assertFalse(store["a1"]["enabled"])

    def test_quota_probe_setup_failure_advances_backoff(self) -> None:
        import account_pool as ap

        store = {
            "a1": {
                "enabled": True,
                "quota_waiting": True,
                "quota_reset_at": 900.0,
                "quota_next_probe_at": 900.0,
                "quota_confirmation_count": 0,
            }
        }
        with _pool_store(store):
            stats = ap.process_quota_probe_due(
                now=1000.0,
                probe_fn=lambda _aid: (_ for _ in ()).throw(TimeoutError()),
            )

        self.assertEqual(stats["failed"], 1)
        self.assertEqual(
            store["a1"]["quota_next_probe_at"],
            1000.0 + ap.QUOTA_INCONCLUSIVE_RETRY_SECONDS,
        )
        self.assertEqual(store["a1"]["quota_confirmation_count"], 0)

    def test_explicit_credit_error_enters_quota_not_model_state(self) -> None:
        import account_pool as ap

        store = {"a1": {"enabled": True}}
        with _pool_store(store), mock.patch.object(ap, "touch_account_stats"):
            ap.report_failure(
                "a1",
                error="usage_limit_reached: out of credits",
                status_code=429,
                model="grok-4.5",
            )

        self.assertTrue(store["a1"]["quota_waiting"])
        self.assertNotIn("blocked_models", store["a1"])

    def test_quota_reset_failed_not_cleanup_ready_before_all_gates(self) -> None:
        import registration_producer as producer

        row = {
            "quota_waiting": True,
            "quota_status": "quota_reset_failed",
            "quota_cycle_id": "cycle",
            "quota_confirmation_cycle_id": "cycle",
            "quota_confirmation_count": 2,
            "quota_grace_count": 2,
            "quota_first_confirm_at": 100.0,
            "quota_last_confirm_at": 200.0,
            "quota_last_evidence": "free_usage_exhausted",
            "quota_terminal_at": 200.0,
        }
        self.assertIsNone(producer._cleanup_reason(row))

    def test_quota_reset_failed_cleanup_candidate_after_all_gates(self) -> None:
        import registration_producer as producer

        row = {
            "quota_waiting": True,
            "quota_status": "quota_reset_failed",
            "quota_cycle_id": "cycle",
            "quota_confirmation_cycle_id": "cycle",
            "quota_confirmation_count": 3,
            "quota_grace_count": 3,
            "quota_first_confirm_at": 100.0,
            "quota_last_confirm_at": 300.0,
            "quota_last_evidence": "free_usage_exhausted",
            "quota_terminal_at": 300.0,
        }
        self.assertEqual(
            producer._cleanup_reason(row), ("quota_reset_failed", 300.0)
        )

    def test_quota_recovery_clears_all_cycle_evidence(self) -> None:
        import account_pool as ap

        evidence = {
            "quota_waiting": True,
            "quota_status": "quota_reset_failed",
            "quota_cycle_id": "cycle",
            "quota_waiting_since": 1.0,
            "quota_reset_at": 2.0,
            "quota_next_probe_at": 3.0,
            "quota_grace_count": 3,
            "quota_confirmation_count": 3,
            "quota_first_confirm_at": 4.0,
            "quota_last_confirm_at": 5.0,
            "quota_last_evidence": "free_usage_exhausted",
            "quota_confirmation_cycle_id": "cycle",
            "quota_terminal_at": 6.0,
        }
        store = {"a1": evidence}
        with _pool_store(store):
            ap.clear_quota_waiting("a1")
        for key in evidence:
            if key.startswith("quota_") and key not in ("quota_status", "quota_waiting"):
                self.assertNotIn(key, store["a1"])
        self.assertEqual(store["a1"]["quota_status"], "active")

    def test_second_quota_cycle_starts_from_zero(self) -> None:
        import account_pool as ap

        store: dict = {}
        with _pool_store(store), mock.patch.object(ap, "_now", return_value=1000.0):
            ap.mark_quota_waiting("a1", reset_at=1100.0)
            first_cycle = store["a1"]["quota_cycle_id"]
            store["a1"]["quota_confirmation_count"] = 3
            store["a1"]["quota_grace_count"] = 3
            ap.clear_quota_waiting("a1")
        with _pool_store(store), mock.patch.object(ap, "_now", return_value=2000.0):
            ap.mark_quota_waiting("a1", reset_at=2100.0)
        self.assertNotEqual(store["a1"]["quota_cycle_id"], first_cycle)
        self.assertEqual(store["a1"]["quota_confirmation_count"], 0)
        self.assertEqual(store["a1"]["quota_grace_count"], 0)

    def test_manual_disable_survives_quota_recovery(self) -> None:
        import account_pool as ap

        store = {"a1": {"enabled": True}}
        with _pool_store(store):
            ap.set_account_enabled("a1", False)
            ap.mark_quota_waiting("a1", reset_at=1.0)
            ap.clear_quota_waiting("a1")
        self.assertTrue(store["a1"]["manual_disabled"])
        self.assertFalse(store["a1"]["enabled"])

    def test_manual_enable_resolves_suspend_state_atomically(self) -> None:
        import account_pool as ap

        store = {
            "a1": {
                "enabled": False,
                "credential_suspended": True,
                "suspended_at": 1.0,
                "suspend_source": "probe",
            }
        }
        with _pool_store(store):
            ap.set_account_enabled("a1", True)
        self.assertTrue(store["a1"]["enabled"])
        self.assertFalse(store["a1"]["credential_suspended"])
        self.assertNotIn("suspended_at", store["a1"])
        self.assertNotIn("suspend_source", store["a1"])

    def test_quota_error_never_creates_model_block(self) -> None:
        import account_pool as ap

        with mock.patch.object(ap, "touch_account_stats"), mock.patch(
            "quota.handle_upstream_error_for_quota", return_value={"quota_waiting": True}
        ), mock.patch("model_health.handle_upstream_error_for_model") as model_handler:
            ap.report_failure(
                "a1",
                error="subscription:free-usage-exhausted",
                status_code=429,
                model="grok-4.5",
            )
        model_handler.assert_not_called()

    def test_normal_request_account_suspended_is_terminal(self) -> None:
        import account_pool as ap

        store = {"a1": {"enabled": True}}
        with _pool_store(store), mock.patch.object(ap, "touch_account_stats"):
            ap.report_failure(
                "a1", error="account_suspended", status_code=403, model="grok-4.5"
            )
        self.assertTrue(store["a1"]["credential_suspended"])
        self.assertFalse(store["a1"]["enabled"])

    def test_pool_summary_excludes_waiting_from_available(self) -> None:
        import account_pool as ap

        rows = [
            {"id": "active", "enabled": True, "expired": False},
            {
                "id": "waiting",
                "enabled": True,
                "expired": False,
                "quota_waiting": True,
                "quota_status": "quota_waiting",
            },
        ]
        with mock.patch.object(ap, "list_pool_accounts", return_value=rows):
            summary = ap.pool_summary()
        self.assertEqual(summary["enabled"], 1)
        self.assertEqual(summary["available"], 1)
        self.assertEqual(summary["quota_waiting"], 1)

    def _assert_stream_headers(self, function_name: str) -> None:
        import app
        from auth import GrokCredentials

        upstream_headers = {"retry-after": "120", "x-test-reset": "yes"}

        class Response:
            status_code = 429
            headers = upstream_headers

            async def aread(self):
                return b"rate limited"

        class StreamContext:
            async def __aenter__(self):
                return Response()

            async def __aexit__(self, *_args):
                return False

        class Client:
            def __init__(self, *_args, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def stream(self, *_args, **_kwargs):
                return StreamContext()

        async def disconnected():
            return False

        creds = GrokCredentials(token="token", auth_key="a1", expires_at=time.time() + 60)

        async def consume():
            if function_name == "openai":
                iterator = app._stream_proxy_with_failover(
                    url="https://upstream.invalid",
                    body={"model": "grok-4.5", "messages": []},
                    chain=[creds],
                    chat_id="chat-1",
                    model="grok-4.5",
                    created=1,
                    client_disconnected=disconnected,
                )
            else:
                iterator = app._stream_anthropic_with_failover(
                    url="https://upstream.invalid",
                    body={"model": "grok-4.5", "messages": []},
                    chain=[creds],
                    message_id="msg-1",
                    model="grok-4.5",
                    client_disconnected=disconnected,
                )
            return [item async for item in iterator]

        with mock.patch.object(app.httpx, "AsyncClient", Client), mock.patch.object(
            app.account_pool, "report_failure"
        ) as report:
            asyncio.run(consume())
        self.assertEqual(report.call_args.kwargs["headers"], upstream_headers)

    def test_openai_stream_passes_reset_headers(self) -> None:
        self._assert_stream_headers("openai")

    def test_anthropic_stream_passes_reset_headers(self) -> None:
        self._assert_stream_headers("anthropic")

    def test_retry_after_http_date(self) -> None:
        import quota

        reset = 2_000_000_000.0
        header = formatdate(reset, usegmt=True)
        with mock.patch.object(quota.time, "time", return_value=1_900_000_000.0):
            parsed = quota.parse_quota_reset_at({"retry-after": header})
        self.assertEqual(parsed, reset)


class RefreshLifecycleAcceptanceTests(unittest.TestCase):
    def _run_refresh_failure(self, store: dict, *, now: float) -> dict:
        import oidc_auth as oidc

        with _auth_store(store), mock.patch.object(
            oidc.time, "time", return_value=now
        ), mock.patch.object(
            oidc, "REFRESH_RETRY_BASE_SECONDS", 0.0
        ), mock.patch.object(
            oidc, "REFRESH_RETRY_MAX_SECONDS", 0.0
        ), mock.patch.object(
            oidc,
            "refresh_and_persist",
            side_effect=oidc.RefreshRevokedError("invalid_grant"),
        ):
            return oidc.refresh_all_accounts(
                only_near_expiry=False, max_workers=1, max_accounts=10
            )

    def test_first_invalid_grant_is_pending_not_terminal(self) -> None:
        store = {"a1": _refresh_entry(expires_at=1200.0)}
        result = self._run_refresh_failure(store, now=1000.0)
        self.assertEqual(result["pending_confirmation"], 1)
        self.assertEqual(store["a1"]["refresh_status"], "refresh_pending_confirmation")
        self.assertEqual(store["a1"]["refresh_failure_count"], 1)
        self.assertNotIn("refresh_terminal_at", store["a1"])

    def test_refresh_retries_next_maintenance_cycle(self) -> None:
        import oidc_auth as oidc

        store = {"a1": _refresh_entry(expires_at=1200.0)}
        self._run_refresh_failure(store, now=1000.0)
        refreshed = _refresh_entry(expires_at=5000.0)
        refreshed["key"] = "access-new"
        with _auth_store(store), mock.patch.object(
            oidc.time, "time", return_value=1100.0
        ), mock.patch.object(
            oidc,
            "refresh_and_persist",
            return_value={"account_id": "a1", "entry": refreshed},
        ) as refresh:
            result = oidc.refresh_all_accounts(
                only_near_expiry=True, max_workers=1, max_accounts=10
            )
        refresh.assert_called_once()
        self.assertEqual(result["refreshed"], 1)

    def test_inline_refresh_revocation_enters_backoff(self) -> None:
        import oidc_auth as oidc

        store = {"a1": _refresh_entry(expires_at=900.0)}
        with _auth_store(store), mock.patch.object(
            oidc.time, "time", return_value=1000.0
        ), mock.patch.object(
            oidc,
            "refresh_and_persist",
            side_effect=oidc.RefreshRevokedError("invalid_grant"),
        ) as refresh:
            first = oidc.ensure_fresh_entry("a1", store["a1"])
            second = oidc.ensure_fresh_entry("a1", first)

        self.assertEqual(first["refresh_status"], "refresh_pending_confirmation")
        self.assertEqual(first["refresh_failure_count"], 1)
        self.assertEqual(second["refresh_failure_count"], 1)
        refresh.assert_called_once()

    def test_refresh_terminal_requires_post_expiry_confirmation(self) -> None:
        store = {"a1": _refresh_entry(expires_at=1200.0)}
        self._run_refresh_failure(store, now=1000.0)
        self._run_refresh_failure(store, now=1100.0)
        self._run_refresh_failure(store, now=1150.0)
        self.assertEqual(store["a1"]["refresh_failure_count"], 3)
        self.assertEqual(store["a1"]["refresh_status"], "refresh_pending_confirmation")
        self.assertFalse(store["a1"]["refresh_confirmed_after_expiry"])
        self._run_refresh_failure(store, now=1300.0)
        self.assertEqual(store["a1"]["refresh_status"], "refresh_terminal")
        self.assertTrue(store["a1"]["refresh_confirmed_after_expiry"])

    def test_refresh_success_clears_failure_evidence(self) -> None:
        import oidc_auth as oidc

        store = {"a1": _refresh_entry(expires_at=1200.0)}
        self._run_refresh_failure(store, now=1000.0)
        refreshed = _refresh_entry(expires_at=5000.0)
        refreshed["key"] = "access-new"
        with _auth_store(store), mock.patch.object(
            oidc.time, "time", return_value=1100.0
        ), mock.patch.object(
            oidc,
            "refresh_and_persist",
            return_value={"account_id": "a1", "entry": refreshed},
        ):
            oidc.refresh_all_accounts(
                only_near_expiry=False, max_workers=1, max_accounts=10
            )
        for key in oidc._REFRESH_FAILURE_KEYS:
            self.assertNotIn(key, store["a1"])

    def test_legacy_refresh_invalid_requires_new_confirmation(self) -> None:
        store = {"a1": _refresh_entry(expires_at=1200.0)}
        store["a1"].update(
            {
                "refresh_invalid": True,
                "refresh_invalid_at": 900.0,
                "refresh_invalid_reason": "invalid_grant",
            }
        )
        self._run_refresh_failure(store, now=1000.0)
        self.assertEqual(store["a1"]["refresh_status"], "refresh_pending_confirmation")
        self.assertEqual(store["a1"]["refresh_failure_count"], 2)
        self.assertNotIn("refresh_invalid", store["a1"])

    def test_corrupt_persisted_refresh_count_is_recovered(self) -> None:
        import accounts
        import auth
        import oidc_auth as oidc

        entry = _refresh_entry(expires_at=5000.0)
        entry["refresh_failure_count"] = "not-an-integer"
        creds = auth._entry_to_creds("a1", entry)
        self.assertEqual(creds.refresh_failure_count, 0)

        with mock.patch.object(accounts, "read_auth_map", return_value={"a1": entry}):
            self.assertEqual(accounts.list_accounts()[0]["refresh_failure_count"], 0)

        failed = oidc._record_definitive_refresh_failure(
            entry, reason="invalid_grant", now=1000.0
        )
        self.assertEqual(failed["refresh_failure_count"], 1)
        self.assertEqual(failed["refresh_status"], "refresh_pending_confirmation")


if __name__ == "__main__":
    unittest.main()
