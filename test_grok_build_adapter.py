"""Regression tests for grok-build-auth batch orchestration (no network)."""

from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

import grok_build_adapter as adapter


class BatchConcurrencyTests(unittest.TestCase):
    def setUp(self):
        adapter._sessions.clear()
        adapter._batches.clear()

    def test_batch_concurrency_covers_full_registration(self):
        """The executor slot must be held until the registration worker exits."""
        email_counter = 0

        def fake_receiver(**_kwargs):
            nonlocal email_counter
            email_counter += 1
            return f"batch-{email_counter}@example.test", object()

        counter_lock = threading.Lock()
        active = 0
        max_active = 0

        def fake_run(sid, _key, _proxy, _receiver):
            nonlocal active, max_active
            with counter_lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.06)
            adapter._sessions[sid]["status"] = "imported"
            with counter_lock:
                active -= 1

        with (
            patch.object(adapter, "ensure_xconsole", lambda: None),
            patch.object(adapter, "_make_email_receiver", fake_receiver),
            patch.object(adapter, "_run_registration", fake_run),
        ):
            result = adapter.start_registration(
                yescaptcha_key="test-key",
                proxy="http://proxy.invalid",
                count=6,
                concurrency=1,
                stagger_ms=0,
            )
            self.assertTrue(result["ok"])
            batch_id = result["batch_id"]

            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                batch = adapter._batches[batch_id]
                if batch["status"] == "spawned":
                    break
                time.sleep(0.01)
            else:
                self.fail("batch worker did not finish")

        session_ids = adapter._batches[batch_id]["session_ids"]
        self.assertEqual(len(session_ids), 6)
        self.assertEqual(max_active, 1)
        self.assertTrue(
            all(adapter._sessions[sid]["status"] == "imported" for sid in session_ids)
        )

    def test_mail_provider_reaches_receiver_and_session(self):
        received: list[dict[str, object]] = []

        class FakeReceiver:
            provider = "yyds"

        def fake_receiver(**kwargs):
            received.append(kwargs)
            return "mailbox@example.test", FakeReceiver()

        def fake_run(sid, _key, _proxy, _receiver):
            adapter._sessions[sid]["status"] = "imported"

        with (
            patch.object(adapter, "ensure_xconsole", lambda: None),
            patch.object(adapter, "_make_email_receiver", fake_receiver),
            patch.object(adapter, "_run_registration", fake_run),
        ):
            result = adapter.start_registration(
                yescaptcha_key="test-key",
                proxy="http://proxy.invalid",
                mail_provider="yyds",
                count=1,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(received[0]["mail_provider"], "yyds")
        self.assertEqual(result["mail_provider"], "yyds")

    def test_registration_failure_closes_mail_receiver(self):
        closed = threading.Event()

        class FakeReceiver:
            def close(self):
                closed.set()

        sid = "gba_close_receiver"
        adapter._sessions[sid] = {
            "id": sid,
            "email": "mailbox@example.test",
            "password": "test-password",
            "status": "started",
        }

        with patch.object(
            adapter,
            "ensure_xconsole",
            side_effect=RuntimeError("synthetic registration failure"),
        ):
            adapter._run_registration(
                sid,
                "test-key",
                "http://proxy.invalid",
                FakeReceiver(),
            )

        self.assertTrue(closed.is_set())
        self.assertEqual(adapter._sessions[sid]["status"], "error")

    def test_batch_prefixes_are_unique_and_bounded(self):
        prefixes: list[str] = []

        class FakeReceiver:
            provider = "yyds"

        def fake_receiver(**kwargs):
            prefixes.append(str(kwargs.get("prefix") or ""))
            return f"mailbox-{len(prefixes)}@example.test", FakeReceiver()

        def fake_run(sid, _key, _proxy, _receiver):
            adapter._sessions[sid]["status"] = "imported"

        with (
            patch.object(adapter, "ensure_xconsole", lambda: None),
            patch.object(adapter, "_make_email_receiver", fake_receiver),
            patch.object(adapter, "_run_registration", fake_run),
        ):
            result = adapter.start_registration(
                yescaptcha_key="test-key",
                proxy="http://proxy.invalid",
                mail_provider="yyds",
                prefix="p" * 64,
                count=3,
                concurrency=3,
                stagger_ms=0,
            )
            batch_id = result["batch_id"]
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if adapter._batches[batch_id]["status"] == "spawned":
                    break
                time.sleep(0.01)

        self.assertEqual(len(prefixes), 3)
        self.assertEqual(len(set(prefixes)), 3)
        self.assertTrue(all(len(value) <= 64 for value in prefixes))


if __name__ == "__main__":
    unittest.main()
