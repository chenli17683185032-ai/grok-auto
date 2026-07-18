"""Regressions for x.ai email-code response compatibility."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]
GROK_BUILD_AUTH = ROOT / "grok-build-auth"
sys.path.insert(0, str(GROK_BUILD_AUTH))

from xconsole_client.client import XConsoleAuthClient  # noqa: E402
from xconsole_client.models import GrpcResult  # noqa: E402


def _client(result: GrpcResult) -> XConsoleAuthClient:
    client = XConsoleAuthClient.__new__(XConsoleAuthClient)
    client.signup_url = "https://accounts.x.ai/sign-up?redirect=grok-com"
    client._grpc_call = Mock(return_value=result)
    return client


class CreateEmailValidationCodeTests(unittest.TestCase):
    def test_accepts_http_200_empty_body(self):
        client = _client(
            GrpcResult(
                ok=False,
                http_status=200,
                grpc_status=None,
                raw=b"",
            )
        )

        result = client.create_email_validation_code("user@example.com")

        self.assertTrue(result.ok)
        self.assertEqual(result.http_status, 200)
        self.assertIsNone(result.grpc_status)
        self.assertEqual(result.raw, b"")

    def test_does_not_accept_non_200_empty_body(self):
        original = GrpcResult(
            ok=False,
            http_status=403,
            grpc_status=None,
            raw=b"",
        )
        client = _client(original)

        self.assertIs(client.create_email_validation_code("user@example.com"), original)

    def test_preserves_explicit_grpc_error(self):
        original = GrpcResult(
            ok=False,
            http_status=200,
            grpc_status=7,
            trailers={"grpc-message": "permission denied"},
            raw=b"grpc-error-frame",
        )
        client = _client(original)

        self.assertIs(client.create_email_validation_code("user@example.com"), original)

    def test_verify_code_keeps_strict_empty_body_handling(self):
        original = GrpcResult(
            ok=False,
            http_status=200,
            grpc_status=None,
            raw=b"",
        )
        client = _client(original)

        self.assertIs(
            client.verify_email_validation_code("user@example.com", "ABC123"),
            original,
        )


if __name__ == "__main__":
    unittest.main()
