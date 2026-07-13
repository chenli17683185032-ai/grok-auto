"""Auth file writes must be 0600 from temp creation."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class AuthPermissionTests(unittest.TestCase):
    def test_write_auth_map_mode_600(self) -> None:
        import auth_store

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "auth.json"
            with mock.patch.object(auth_store, "AUTH_FILE", path):
                auth_store.write_auth_map({"k": {"key": "x"}}, path=path)
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o600)

    def test_mutate_auth_map_mode_600(self) -> None:
        import auth_store

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "auth.json"
            path.write_text("{}", encoding="utf-8")
            os.chmod(path, 0o644)
            with mock.patch.object(auth_store, "AUTH_FILE", path):
                def mut(d):
                    d["a"] = {"key": "v"}

                auth_store.mutate_auth_map(mut)
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o600)

    def test_cli_auth_writers_mode_600(self) -> None:
        import sso_to_auth_json as m

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "out.json"
            m.write_auth_json(path, "key", {"key": "tok", "user_id": "u1"})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            m.merge_auth_json(path, "key", {"key": "tok2", "user_id": "u2"}, unique=True)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
