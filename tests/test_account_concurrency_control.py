from __future__ import annotations

import time
import unittest
from unittest.mock import patch

import account_pool
import auth
from auth import GrokCredentials


def _creds(index: int) -> GrokCredentials:
    return GrokCredentials(
        token=f"token-{index}",
        auth_key=f"https://auth.x.ai::account-{index:03d}",
        user_id=f"user-{index:03d}",
    )


class LiveCredentialsSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_cache = dict(auth._live_creds_cache)

    def tearDown(self) -> None:
        auth._live_creds_cache.clear()
        auth._live_creds_cache.update(self.original_cache)

    def test_invalidation_preserves_stale_snapshot_and_schedules_refresh(self) -> None:
        credentials = [_creds(1), _creds(2)]
        auth._live_creds_cache.update(
            {
                "at": time.time(),
                "path": str(auth.AUTH_FILE),
                "include_expired": True,
                "creds": list(credentials),
            }
        )
        with patch.object(auth, "_schedule_live_credentials_refresh") as schedule:
            auth.invalidate_live_credentials_cache()

        stale = auth.get_cached_live_credentials(
            include_expired=True,
            allow_stale=True,
        )
        self.assertEqual([c.auth_key for c in stale or []], [c.auth_key for c in credentials])
        self.assertEqual(auth._live_creds_cache["at"], 0.0)
        schedule.assert_called_once()

    def test_request_path_returns_stale_snapshot_while_refresh_runs(self) -> None:
        credentials = [_creds(3), _creds(4)]
        auth._live_creds_cache.update(
            {
                "at": 0.0,
                "path": str(auth.AUTH_FILE),
                "include_expired": True,
                "creds": list(credentials),
            }
        )
        with (
            patch.object(auth, "_schedule_live_credentials_refresh") as schedule,
            patch.object(auth, "_read_auth", side_effect=AssertionError("request did DB IO")),
        ):
            result = auth.list_live_credentials(
                include_expired=True,
                auto_refresh=False,
            )

        self.assertEqual([c.auth_key for c in result], [c.auth_key for c in credentials])
        schedule.assert_called_once()

    def test_live_only_view_filters_full_snapshot_without_rebuilding(self) -> None:
        live = _creds(5)
        expired = _creds(6)
        expired.expires_at = time.time() - 120
        auth._live_creds_cache.update(
            {
                "at": time.time(),
                "loaded_at": time.time(),
                "path": str(auth.AUTH_FILE),
                "include_expired": True,
                "creds": [live, expired],
            }
        )
        with patch.object(
            auth,
            "_read_auth",
            side_effect=AssertionError("live-only view rebuilt the full store"),
        ):
            result = auth.list_live_credentials(
                include_expired=False,
                auto_refresh=False,
            )

        self.assertEqual([credential.auth_key for credential in result], [live.auth_key])
        self.assertTrue(auth._live_creds_cache["include_expired"])


class StickyBackupRotationTests(unittest.TestCase):
    def test_sticky_chain_rotates_warm_backups_with_global_cursor(self) -> None:
        from store import pool_redis

        credentials = [_creds(index) for index in range(10)]
        preferred = credentials[0]
        replacements = {
            "_ensure_multi_account_layout": lambda: None,
            "peek_credentials_by_id": lambda _account_id: preferred,
            "get_account_pool_meta": lambda _account_id: {"enabled": True},
            "get_cached_live_credentials": lambda **_kwargs: list(credentials),
            "get_cached_account_pool_state": lambda: {},
            "get_account_mode": lambda: "round_robin",
        }
        originals = {name: getattr(account_pool, name) for name in replacements}
        original_rr_next = pool_redis.rr_next
        try:
            for name, replacement in replacements.items():
                setattr(account_pool, name, replacement)
            pool_redis.rr_next = lambda: 4
            chain = account_pool.try_acquire_sequence(
                max_attempts=4,
                model="grok-4.5",
                prefer_account_id=preferred.auth_key,
            )
        finally:
            for name, original in originals.items():
                setattr(account_pool, name, original)
            pool_redis.rr_next = original_rr_next

        self.assertEqual(
            [credential.auth_key for credential in chain],
            [
                preferred.auth_key,
                credentials[4].auth_key,
                credentials[5].auth_key,
                credentials[6].auth_key,
            ],
        )


class AccountLeaseTests(unittest.TestCase):
    def test_busy_sticky_account_spills_to_reserved_backups_and_releases(self) -> None:
        from store import account_leases

        credentials = [_creds(index) for index in range(3)]
        busy_key = account_leases.lease_key(credentials[0].auth_key or "")
        acquired: list[tuple[str, str, int]] = []
        released: list[tuple[str, str]] = []

        def set_nx(key: str, token: str, ttl: int) -> bool:
            acquired.append((key, token, ttl))
            return key != busy_key

        with (
            patch.object(account_leases, "redis_enabled", return_value=True),
            patch.object(account_leases, "set_nx_ex", side_effect=set_nx),
            patch.object(account_leases, "compare_and_delete", side_effect=lambda key, token: released.append((key, token)) or True),
            patch.object(account_leases, "_ensure_renew_thread"),
        ):
            chain = account_leases.reserve_chain(
                credentials,
                preferred_account_id=credentials[0].auth_key,
            )
            account_leases.release_chain(chain)
            account_leases.release_chain(chain)

        self.assertEqual([item.auth_key for item in chain], [credentials[1].auth_key, credentials[2].auth_key])
        self.assertTrue(chain.affinity_spillover)
        self.assertFalse(chain.degraded)
        self.assertEqual(len(acquired), 3)
        self.assertTrue(all(ttl > 0 for _, _, ttl in acquired))
        self.assertEqual(len(released), 2)

    def test_redis_error_degrades_to_original_chain(self) -> None:
        from store import account_leases

        credentials = [_creds(1), _creds(2)]
        with (
            patch.object(account_leases, "redis_enabled", return_value=True),
            patch.object(account_leases, "set_nx_ex", side_effect=RuntimeError("redis down")),
        ):
            chain = account_leases.reserve_chain(credentials)

        self.assertEqual([item.auth_key for item in chain], [item.auth_key for item in credentials])
        self.assertTrue(chain.degraded)


class LatencyAwareSelectionTests(unittest.TestCase):
    def test_non_sticky_round_robin_prefers_fast_feedback_window(self) -> None:
        from store import pool_redis

        credentials = [_creds(index) for index in range(100)]
        fast = credentials[20:28]
        replacements = {
            "_ensure_multi_account_layout": lambda: None,
            "get_cached_account_pool_state": lambda: {},
            "list_live_credentials": lambda **_kwargs: list(credentials),
            "get_account_pool_meta_many": lambda ids: {
                account_id: {"enabled": True} for account_id in ids
            },
            "get_account_mode": lambda: "round_robin",
        }
        originals = {name: getattr(account_pool, name) for name in replacements}
        original_rr_next = pool_redis.rr_next
        original_fast_ids = getattr(pool_redis, "fast_account_ids", None)
        try:
            for name, replacement in replacements.items():
                setattr(account_pool, name, replacement)
            pool_redis.rr_next = lambda: 1
            pool_redis.fast_account_ids = lambda _model, limit=32: [
                item.auth_key for item in fast
            ][:limit]
            chain = account_pool.try_acquire_sequence(
                max_attempts=4,
                model="grok-4.5",
            )
        finally:
            for name, original in originals.items():
                setattr(account_pool, name, original)
            pool_redis.rr_next = original_rr_next
            if original_fast_ids is None:
                delattr(pool_redis, "fast_account_ids")
            else:
                pool_redis.fast_account_ids = original_fast_ids

        self.assertEqual(
            [credential.auth_key for credential in chain],
            [
                fast[1].auth_key,
                fast[2].auth_key,
                fast[3].auth_key,
                fast[4].auth_key,
            ],
        )

    def test_latency_window_waits_for_eight_distinct_samples(self) -> None:
        from store import pool_redis

        credentials = [_creds(index) for index in range(100)]
        replacements = {
            "_ensure_multi_account_layout": lambda: None,
            "get_cached_account_pool_state": lambda: {},
            "list_live_credentials": lambda **_kwargs: list(credentials),
            "get_account_pool_meta_many": lambda ids: {
                account_id: {"enabled": True} for account_id in ids
            },
            "get_account_mode": lambda: "round_robin",
        }
        originals = {name: getattr(account_pool, name) for name in replacements}
        original_rr_next = pool_redis.rr_next
        original_fast_ids = pool_redis.fast_account_ids
        try:
            for name, replacement in replacements.items():
                setattr(account_pool, name, replacement)
            pool_redis.rr_next = lambda: 1
            pool_redis.fast_account_ids = lambda _model, limit=32: [
                credential.auth_key for credential in credentials[20:27]
            ]
            chain = account_pool.try_acquire_sequence(
                max_attempts=4,
                model="grok-4.5",
            )
        finally:
            for name, original in originals.items():
                setattr(account_pool, name, original)
            pool_redis.rr_next = original_rr_next
            pool_redis.fast_account_ids = original_fast_ids

        self.assertEqual(
            [credential.auth_key for credential in chain],
            [credential.auth_key for credential in credentials[1:5]],
        )


if __name__ == "__main__":
    unittest.main()
