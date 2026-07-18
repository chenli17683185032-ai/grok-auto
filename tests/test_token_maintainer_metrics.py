"""Token maintainer metrics and backoff must distinguish terminal skips."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import token_maintainer


class TokenMaintainerMetricsTests(unittest.TestCase):
    def tearDown(self) -> None:
        token_maintainer._last_run.clear()
        token_maintainer._min_remaining_cache.update({"at": 0.0, "value": None})

    def test_terminal_refresh_skips_are_not_new_invalidations(self) -> None:
        summary = token_maintainer._summarize_refresh(
            {
                "attempted": 2,
                "deleted": 0,
                "results": [
                    {"id": "dead-1", "ok": False, "skipped": True, "reason": "refresh_invalid"},
                    {"id": "dead-2", "ok": False, "permanent": True, "reason": "refresh_invalid"},
                    {"id": "ok", "ok": True},
                ],
            }
        )

        self.assertEqual(summary["terminal_skipped"], 1)
        self.assertEqual(summary["invalidated"], 1)
        self.assertEqual(summary["deleted"], 0)
        self.assertEqual(summary["failed"], 1)

    def test_empty_sweep_returns_to_base_interval(self) -> None:
        token_maintainer._last_run["refresh"] = {
            "attempted": 0,
            "failed": 0,
            "skipped": 3739,
        }
        with (
            patch.dict("os.environ", {"GROK2API_TOKEN_MAINTAIN_INTERVAL": "90"}),
            patch.object(token_maintainer, "_min_remaining_seconds", return_value=-1.0),
        ):
            self.assertEqual(token_maintainer._next_wait_seconds(), 90.0)


if __name__ == "__main__":
    unittest.main()
