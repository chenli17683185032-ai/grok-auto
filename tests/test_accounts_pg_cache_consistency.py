"""PostgreSQL account snapshots must not survive a newer invalidation."""

from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

import oidc_auth
from store import accounts_pg


class _Cursor:
    def __init__(
        self,
        rows: list[tuple[str, dict]],
        rows_lock: threading.Lock,
        query_started: threading.Event,
        allow_first_query: threading.Event,
        query_number: int,
    ) -> None:
        self._rows = rows
        self._rows_lock = rows_lock
        self._query_started = query_started
        self._allow_first_query = allow_first_query
        self._query_number = query_number
        self._snapshot: list[tuple[str, dict]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def execute(self, _query: str) -> None:
        with self._rows_lock:
            self._snapshot = [(key, dict(value)) for key, value in self._rows]
        if self._query_number == 1:
            self._query_started.set()
            if not self._allow_first_query.wait(timeout=2):
                raise TimeoutError("test query was not released")

    def fetchall(self) -> list[tuple[str, dict]]:
        return self._snapshot


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def cursor(self) -> _Cursor:
        return self._cursor


class AccountsPgCacheConsistencyTests(unittest.TestCase):
    def setUp(self) -> None:
        accounts_pg.invalidate_auth_map_cache()

    def tearDown(self) -> None:
        accounts_pg.invalidate_auth_map_cache()

    def test_invalidation_cannot_be_overwritten_by_inflight_read(self) -> None:
        rows = [("account-1", {"refresh_invalid": False})]
        rows_lock = threading.Lock()
        query_started = threading.Event()
        allow_first_query = threading.Event()
        invalidate_started = threading.Event()
        invalidated = threading.Event()
        query_count = 0
        query_count_lock = threading.Lock()

        def fake_connection() -> _Connection:
            nonlocal query_count
            with query_count_lock:
                query_count += 1
                number = query_count
            return _Connection(
                _Cursor(
                    rows,
                    rows_lock,
                    query_started,
                    allow_first_query,
                    number,
                )
            )

        first: dict[str, dict] = {}

        def read_first() -> None:
            first.update(accounts_pg.read_auth_map())

        def invalidate() -> None:
            invalidate_started.set()
            accounts_pg.invalidate_auth_map_cache()
            invalidated.set()

        with (
            patch.object(accounts_pg, "enabled", return_value=True),
            patch.object(accounts_pg, "connection", side_effect=fake_connection),
        ):
            reader = threading.Thread(target=read_first)
            reader.start()
            self.assertTrue(query_started.wait(timeout=1))

            with rows_lock:
                rows[:] = [("account-1", {"refresh_invalid": True})]

            invalidator = threading.Thread(target=invalidate)
            invalidator.start()
            self.assertTrue(invalidate_started.wait(timeout=1))
            self.assertFalse(invalidated.wait(timeout=0.05))

            allow_first_query.set()
            reader.join(timeout=2)
            invalidator.join(timeout=2)
            self.assertFalse(reader.is_alive())
            self.assertFalse(invalidator.is_alive())
            self.assertTrue(invalidated.is_set())

            current = accounts_pg.read_auth_map()

        self.assertFalse(first["account-1"]["refresh_invalid"])
        self.assertTrue(current["account-1"]["refresh_invalid"])
        self.assertEqual(query_count, 2)

    def test_refresh_invalid_account_never_enters_refresh_candidates(self) -> None:
        account = {
            "key": "access-token",
            "refresh_token": "revoked-refresh-token",
            "expires_at": 0,
            "refresh_invalid": True,
            "refresh_invalid_reason": "invalid_grant",
        }
        with (
            patch.object(oidc_auth, "read_auth_map", return_value={"account-1": account}),
            patch.object(oidc_auth, "refresh_and_persist") as refresh,
        ):
            result = oidc_auth.refresh_all_accounts(
                max_workers=1,
                max_accounts=1,
                strict_sweep=False,
            )

        refresh.assert_not_called()
        self.assertEqual(result["attempted"], 0)
        self.assertEqual(result["results"][0]["reason"], "refresh_invalid")
        self.assertTrue(result["results"][0]["skipped"])


if __name__ == "__main__":
    unittest.main()
