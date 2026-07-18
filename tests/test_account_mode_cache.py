"""The request-path account mode read must not refresh all PG settings."""

from __future__ import annotations

import importlib
import unittest
from unittest.mock import patch

import settings_store


class AccountModeCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        importlib.reload(settings_store)

    def test_pg_mode_is_cached_without_entering_full_settings_load(self) -> None:
        pg = type("Pg", (), {"get_setting": lambda self, key: "round_robin"})()
        with (
            patch.object(settings_store, "_pg_settings", return_value=pg),
            patch.object(
                settings_store,
                "_load",
                side_effect=AssertionError("request mode read refreshed all settings"),
            ),
        ):
            self.assertEqual(settings_store.get_account_mode(), "round_robin")
            self.assertEqual(settings_store.get_account_mode(), "round_robin")

    def test_set_mode_updates_cache_immediately(self) -> None:
        with (
            patch.object(settings_store, "_pg_settings", return_value=None),
            patch.object(settings_store, "_save"),
        ):
            settings_store.set_account_mode("least_used")
        self.assertEqual(settings_store.get_account_mode(), "least_used")


if __name__ == "__main__":
    unittest.main()
