"""Producer mint_queued handoff — must not wait 1800s for import."""

from __future__ import annotations

import unittest
from unittest import mock

import registration_producer as producer


class ProducerHandoffTests(unittest.TestCase):
    def test_pipeline_v2_batch_handoff_returns_without_timeout(self) -> None:
        """batch with mint_queued + running=0 completes immediately."""
        responses = [
            {
                "imported": 0,
                "error": 0,
                "mint_queued": 1,
                "running": 0,
                "count": 1,
                "batch_status": "done",
            }
        ]

        def fake_request(method, path, **kwargs):
            return responses[0]

        with mock.patch.object(producer, "_request", side_effect=fake_request):
            with mock.patch.object(producer, "_touch"):
                with mock.patch.object(producer, "POLL_SEC", 0.01):
                    with mock.patch.object(producer, "BATCH_TIMEOUT_SEC", 2.0):
                        out = producer._wait_batch("tok", "batch_x", expected_count=1)
        self.assertEqual(out["mint_queued"], 1)
        self.assertEqual(out["imported"], 0)
        self.assertEqual(out["signup_done"], 1)

    def test_pipeline_v2_single_session_accepts_mint_queued(self) -> None:
        def fake_request(method, path, **kwargs):
            return {"status": "mint_queued", "id": "gba_1"}

        with mock.patch.object(producer, "_request", side_effect=fake_request):
            with mock.patch.object(producer, "_touch"):
                with mock.patch.object(producer, "POLL_SEC", 0.01):
                    out = producer._wait_session("tok", "gba_1")
        self.assertEqual(out["mint_queued"], 1)
        self.assertEqual(out["imported"], 0)

    def test_mint_queued_not_counted_as_imported_lifetime(self) -> None:
        state = {"imported_lifetime": 5}
        with mock.patch.object(producer, "_producer_state", return_value=dict(state)):
            saved = {}

            def save(s):
                saved.update(s)

            with mock.patch.object(producer, "_save_producer_state", side_effect=save):
                producer._record_imported(0)  # mint_queued path must not call with mint count
                producer._record_imported(2)
        self.assertEqual(saved.get("imported_lifetime"), 7)

    def test_legacy_inline_wait_semantics_unchanged(self) -> None:
        """imported still works as before when no mint_queued field."""
        def fake_request(method, path, **kwargs):
            return {
                "imported": 1,
                "error": 0,
                "running": 0,
                "count": 1,
                "batch_status": "done",
            }

        with mock.patch.object(producer, "_request", side_effect=fake_request):
            with mock.patch.object(producer, "_touch"):
                out = producer._wait_batch("tok", "b", expected_count=1)
        self.assertEqual(out["imported"], 1)
        self.assertEqual(out["mint_queued"], 0)


if __name__ == "__main__":
    unittest.main()
