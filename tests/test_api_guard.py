"""Tests for the in-process API guard feedback."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import api_guard


class ApiGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        with api_guard._lock:
            api_guard._samples.clear()

    def test_snapshot_keeps_feedback_local_and_calculates_p95(self) -> None:
        api_guard.record_timing(local_ms=10, ttft_ms=100, ok=True)
        api_guard.record_timing(local_ms=40, ttft_ms=200, ok=True)
        api_guard.record_timing(local_ms=90, ttft_ms=300, ok=False)

        snapshot = api_guard.snapshot(now=api_guard.time.time())
        self.assertEqual(snapshot["sample_count"], 3)
        self.assertEqual(snapshot["local_p95_ms"], 90.0)
        self.assertAlmostEqual(snapshot["error_rate"], 1 / 3, places=3)

    def test_cluster_reader_ignores_stale_or_invalid_rows(self) -> None:
        rows = {
            "g2a:api_guard:a": json.dumps(
                {
                    "at": 100.0,
                    "healthy": True,
                    "sample_count": 10,
                    "local_p95_ms": 120,
                    "error_rate": 0,
                }
            ),
            "g2a:api_guard:b": "not-json",
        }

        class Client:
            def scan_iter(self, *, match: str, count: int):
                self.match = match
                self.count = count
                return rows.keys()

            def get(self, key: str):
                return rows[key]

        with (
            patch("store.redis_client.get_client", return_value=Client()),
            patch("store.redis_client.key", side_effect=lambda *parts: ":".join(parts)),
            patch.object(api_guard.time, "time", return_value=100.0),
        ):
            result = api_guard.read_cluster(max_age_sec=15)

        self.assertTrue(result["healthy"])
        self.assertEqual(result["workers"], 1)
        self.assertEqual(result["local_p95_ms"], 120.0)


if __name__ == "__main__":
    unittest.main()
