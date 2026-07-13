"""pending-recovery must not process mint-queue-owned pending files."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import registration_producer as producer
from registration_queue import dual_write_pending, pending_owned_by_mint_queue


class PendingOwnershipTests(unittest.TestCase):
    def test_owner_flag(self) -> None:
        self.assertTrue(pending_owned_by_mint_queue({"owner": "mint_queue"}))
        self.assertTrue(pending_owned_by_mint_queue({"pipeline_v2": True}))
        self.assertFalse(pending_owned_by_mint_queue({"owner": "legacy_inline"}))
        self.assertFalse(pending_owned_by_mint_queue({}))

    def test_recovery_skips_mint_owned(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = dual_write_pending(
                session_id="gba_owned",
                email="o@example.com",
                sso="eyJowned",
                pending_dir=Path(td),
                owner="mint_queue",
            )
            payload = json.loads(path.read_text())
            with mock.patch.object(producer, "sso_to_auth_json", create=True):
                ok = producer._recover_pending_file(path, payload, now=1e12)
            self.assertFalse(ok)
            self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
