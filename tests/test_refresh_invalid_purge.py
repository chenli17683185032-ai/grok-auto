"""Permanent refresh failures must settle instead of rewriting every sweep."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import oidc_auth


class RefreshInvalidPurgeTests(unittest.TestCase):
    def test_existing_invalid_account_is_skipped_on_soft_sweep(self) -> None:
        entry = {
            "refresh_invalid": True,
            "refresh_invalid_reason": "invalid_grant",
            "refresh_token": "revoked",
        }
        with (
            patch.object(oidc_auth, "read_auth_map", return_value={"account-1": entry}),
            patch.object(oidc_auth, "mark_refresh_invalid") as mark,
            patch.object(oidc_auth, "_hard_delete_invalid_refresh_enabled", return_value=False),
        ):
            result = oidc_auth.purge_refresh_invalid_accounts()

        mark.assert_not_called()
        self.assertEqual(result["disabled"], 0)
        self.assertEqual(result["deleted"], 0)
        self.assertEqual(result["skipped"], 1)

    def test_newly_doomed_account_is_marked_once(self) -> None:
        with (
            patch.object(oidc_auth, "read_auth_map", return_value={"account-1": {}}),
            patch.object(
                oidc_auth,
                "mark_refresh_invalid",
                return_value={"ok": True, "action": "disabled"},
            ) as mark,
            patch.object(oidc_auth, "_hard_delete_invalid_refresh_enabled", return_value=False),
        ):
            result = oidc_auth.purge_refresh_invalid_accounts()

        mark.assert_called_once_with(
            "account-1", reason="no_refresh_token_and_no_access_token", hard_delete=False
        )
        self.assertEqual(result["disabled"], 1)
        self.assertEqual(result["skipped"], 0)


if __name__ == "__main__":
    unittest.main()
