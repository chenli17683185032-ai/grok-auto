"""Ensure model probes warm the same latency scheduler as live traffic."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from auth import GrokCredentials
import model_health


class _Response:
    status_code = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def iter_lines(self):
        yield "data: {\"choices\":[{\"delta\":{\"content\":\"ok\"}}]}"


class ProbeLatencyFeedbackTests(unittest.TestCase):
    def test_successful_probe_reports_first_data_latency(self) -> None:
        creds = GrokCredentials(
            token="token",
            auth_key="account-1",
            email="a@example.test",
        )
        client = MagicMock()
        client.__enter__.return_value = client
        client.__exit__.return_value = False
        client.stream.return_value = _Response()

        with (
            patch.object(model_health.httpx, "Client", return_value=client),
            patch.object(model_health, "_save_last_probe"),
            patch("account_pool.report_latency") as report_latency,
            patch("account_pool.record_model_probe_outcome", return_value={}),
            patch("settings_store.get_account_pool_state", return_value={}),
        ):
            result = model_health.probe_model_for_creds(
                creds,
                "grok-4.5",
                auto_disable=False,
                source="background",
            )

        self.assertTrue(result["available"])
        self.assertTrue(result["stream_ok"])
        self.assertIn("ttft_ms", result)
        report_latency.assert_called_once()
        self.assertEqual(report_latency.call_args.kwargs["model"], "grok-4.5")
        self.assertEqual(report_latency.call_args.args[0], "account-1")


if __name__ == "__main__":
    unittest.main()
