from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

import httpx

from yydsmail import (
    YYDSMailAuthError,
    YYDSMailProtocolError,
    YYDSMailTransportError,
    create_yydsmail_receiver,
    extract_yydsmail_code,
    normalize_yydsmail_base_url,
)


FAKE_KEY = "AC-fake-unit-test-key"
FAKE_TOKEN = "fake-temp-token-for-tests"


def _created(
    *,
    address: str = "grok-test@example.test",
    token: str = FAKE_TOKEN,
) -> dict[str, object]:
    return {
        "success": True,
        "data": {
            "id": "account-1",
            "address": address,
            "token": token,
            "expiresAt": "2026-07-14T12:00:00Z",
        },
    }


class BaseURLTests(unittest.TestCase):
    def test_production_documentation_and_api_paths_are_normalized(self) -> None:
        expected = "https://maliapi.215.im/v1"
        cases = (
            "https://vip.215.im/docs",
            "http://vip.215.im/docs",
            "https://vip.215.im/docs/anything",
            "https://maliapi.215.im",
            "http://maliapi.215.im/v1/accounts",
            "https://maliapi.215.im/",
            "https://maliapi.215.im/v1",
            "https://maliapi.215.im/v1/accounts",
        )
        for value in cases:
            with self.subTest(value=value):
                self.assertEqual(normalize_yydsmail_base_url(value), expected)

    def test_custom_origin_is_normalized_without_losing_prefix(self) -> None:
        self.assertEqual(
            normalize_yydsmail_base_url("https://mail.example.test/gateway"),
            "https://mail.example.test/gateway/v1",
        )
        self.assertEqual(
            normalize_yydsmail_base_url(
                "https://mail.example.test/gateway/v1/accounts?debug=1"
            ),
            "https://mail.example.test/gateway/v1",
        )


class CreationTests(unittest.TestCase):
    def test_create_uses_201_envelope_and_only_local_part_by_default(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(201, json=_created())

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            address, receiver = create_yydsmail_receiver(
                prefix="grok-speed",
                api_key=FAKE_KEY,
                base_url="https://vip.215.im/docs",
                client=client,
            )

            self.assertEqual(address, "grok-test@example.test")
            self.assertEqual(receiver.account_id, "account-1")
            self.assertEqual(receiver.expires_at, "2026-07-14T12:00:00Z")
            self.assertEqual(len(requests), 1)
            request = requests[0]
            self.assertEqual(request.method, "POST")
            self.assertEqual(
                str(request.url), "https://maliapi.215.im/v1/accounts"
            )
            self.assertEqual(request.headers["X-API-Key"], FAKE_KEY)
            self.assertEqual(json.loads(request.content), {"localPart": "grok-speed"})

    def test_domain_is_only_sent_when_configured(self) -> None:
        payloads: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            payloads.append(json.loads(request.content))
            return httpx.Response(201, json=_created())

        with (
            patch.dict(
                os.environ,
                {
                    "GROK2API_YYDSMAIL_API_KEY": FAKE_KEY,
                    "GROK2API_YYDSMAIL_DOMAIN": "mail.example.test",
                },
            ),
            httpx.Client(transport=httpx.MockTransport(handler)) as client,
        ):
            create_yydsmail_receiver(prefix="grok-domain", client=client)

        self.assertEqual(
            payloads,
            [{"localPart": "grok-domain", "domain": "mail.example.test"}],
        )

    def test_invalid_success_envelope_is_rejected(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(201, json={"success": True, "data": {}})

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(YYDSMailProtocolError):
                create_yydsmail_receiver(
                    prefix="grok-invalid",
                    api_key=FAKE_KEY,
                    client=client,
                )

    def test_create_does_not_retry_ambiguous_transport_or_server_failures(self) -> None:
        for mode in ("transport", "server"):
            calls = 0

            def handler(request: httpx.Request) -> httpx.Response:
                nonlocal calls
                calls += 1
                if mode == "transport":
                    raise httpx.ReadError("response lost", request=request)
                return httpx.Response(503)

            with self.subTest(mode=mode), httpx.Client(
                transport=httpx.MockTransport(handler)
            ) as client:
                with self.assertRaises(YYDSMailTransportError):
                    create_yydsmail_receiver(
                        prefix=f"grok-{mode}",
                        api_key=FAKE_KEY,
                        client=client,
                    )
                self.assertEqual(calls, 1)


class ExtractionTests(unittest.TestCase):
    def test_direct_verification_code_has_priority(self) -> None:
        self.assertEqual(
            extract_yydsmail_code(
                {
                    "verificationCode": "Ab1-Cd2",
                    "text": "A stale numeric code is 999999",
                }
            ),
            "AB1CD2",
        )

    def test_dashed_and_numeric_codes_are_found_in_all_documented_fields(self) -> None:
        values: dict[str, object] = {
            "subject": "xAI code ABC-123",
            "text": "xAI code ABC-123",
            "html": ["<b>xAI code ABC-123</b>"],
            "intro": "xAI code ABC-123",
            "from": {"name": "xAI code ABC-123", "address": "noreply@x.ai"},
        }
        for field, value in values.items():
            with self.subTest(field=field):
                self.assertEqual(extract_yydsmail_code({field: value}), "ABC123")
        self.assertEqual(
            extract_yydsmail_code({"html": ["Your verification code is 654321"]}),
            "654321",
        )
        self.assertEqual(
            extract_yydsmail_code({"text": "Your verification code is XAI0X1"}),
            "XAI0X1",
        )


class PollingTests(unittest.TestCase):
    def _receiver_for_responses(
        self,
        message_responses: list[httpx.Response],
        *,
        sleeper=lambda _delay: None,
    ) -> tuple[httpx.Client, object, list[httpx.Request]]:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.method == "POST":
                return httpx.Response(201, json=_created())
            if request.method == "GET":
                return message_responses.pop(0)
            return httpx.Response(204)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        _address, receiver = create_yydsmail_receiver(
            prefix="grok-poll",
            api_key=FAKE_KEY,
            client=client,
            sleeper=sleeper,
        )
        return client, receiver, requests

    def test_long_poll_continues_after_204_then_uses_direct_code(self) -> None:
        sleeps: list[float] = []
        client, receiver, requests = self._receiver_for_responses(
            [
                httpx.Response(204),
                httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "message": {
                                "id": "message-1",
                                "verificationCode": "AAA-BBB",
                                "text": "stale 111111",
                            },
                            "inboxAddress": "grok-test@example.test",
                        },
                    },
                ),
            ],
            sleeper=sleeps.append,
        )
        self.addCleanup(client.close)

        self.assertEqual(receiver.wait_for_code(timeout=65), "AAABBB")
        get_requests = [request for request in requests if request.method == "GET"]
        self.assertEqual(len(get_requests), 2)
        for request in get_requests:
            self.assertEqual(request.url.path, "/v1/messages/next")
            self.assertEqual(request.url.params["wait"], "30")
            self.assertNotIn("address", request.url.params)
            self.assertEqual(
                request.headers["Authorization"], f"Bearer {FAKE_TOKEN}"
            )
        self.assertEqual(sleeps, [0.25])

    def test_body_fallback_and_seen_state_reject_old_message_and_code(self) -> None:
        def detail(message_id: str, text: str) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {"message": {"id": message_id, "text": text}},
                },
            )

        client, receiver, _requests = self._receiver_for_responses(
            [
                detail("message-1", "Your xAI code is ABC-123"),
                detail("message-1", "Your xAI code is ABC-123"),
                detail("message-2", "Repeated code ABC-123"),
                detail("message-3", "Your verification code is 456789"),
            ]
        )
        self.addCleanup(client.close)

        self.assertEqual(receiver.wait_for_code(timeout=10), "ABC123")
        self.assertEqual(receiver.wait_for_code(timeout=10), "456789")

    def test_429_retry_after_is_honored(self) -> None:
        sleeps: list[float] = []
        client, receiver, _requests = self._receiver_for_responses(
            [
                httpx.Response(
                    429,
                    headers={"Retry-After": "2"},
                    json={"success": False, "errorCode": "rate_limit_slow_down"},
                ),
                httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "message": {
                                "id": "message-rate",
                                "verificationCode": "123456",
                            }
                        },
                    },
                ),
            ],
            sleeper=sleeps.append,
        )
        self.addCleanup(client.close)

        self.assertEqual(receiver.wait_for_code(timeout=10), "123456")
        self.assertEqual(sleeps, [2.0])

    def test_network_and_5xx_errors_use_bounded_retries(self) -> None:
        sleeps: list[float] = []
        get_attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal get_attempts
            if request.method == "POST":
                return httpx.Response(201, json=_created())
            get_attempts += 1
            if get_attempts == 1:
                raise httpx.ConnectError("synthetic failure", request=request)
            if get_attempts == 2:
                return httpx.Response(503)
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "message": {
                            "id": "message-retry",
                            "verificationCode": "ABC-789",
                        }
                    },
                },
            )

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            _address, receiver = create_yydsmail_receiver(
                prefix="grok-retry",
                api_key=FAKE_KEY,
                client=client,
                sleeper=sleeps.append,
            )
            self.assertEqual(receiver.wait_for_code(timeout=10), "ABC789")

        self.assertEqual(get_attempts, 3)
        self.assertEqual(sleeps, [0.25, 0.5])


class FailureAndCleanupTests(unittest.TestCase):
    def test_401_create_fails_immediately_without_key_leak(self) -> None:
        calls = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(
                401,
                json={"success": False, "error": f"bad key {FAKE_KEY}"},
            )

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(YYDSMailAuthError) as captured:
                create_yydsmail_receiver(
                    prefix="grok-auth",
                    api_key=FAKE_KEY,
                    client=client,
                )
        self.assertEqual(calls, 1)
        self.assertNotIn(FAKE_KEY, str(captured.exception))
        self.assertNotIn(FAKE_KEY, repr(captured.exception))

    def test_403_poll_fails_immediately_without_token_leak(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if request.method == "POST":
                return httpx.Response(201, json=_created())
            return httpx.Response(
                403,
                json={"success": False, "error": f"bad token {FAKE_TOKEN}"},
            )

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            _address, receiver = create_yydsmail_receiver(
                prefix="grok-forbidden",
                api_key=FAKE_KEY,
                client=client,
            )
            with self.assertRaises(YYDSMailAuthError) as captured:
                receiver.wait_for_code(timeout=10)
            self.assertNotIn(FAKE_TOKEN, str(captured.exception))
            self.assertNotIn(FAKE_TOKEN, repr(captured.exception))
            self.assertNotIn(FAKE_TOKEN, repr(receiver))
        self.assertEqual(calls, 2)

    def test_close_deletes_with_temp_token_and_is_idempotent(self) -> None:
        deletes: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(201, json=_created())
            if request.method == "DELETE":
                deletes.append(request)
                return httpx.Response(204)
            raise AssertionError(f"unexpected request: {request.method}")

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            _address, receiver = create_yydsmail_receiver(
                prefix="grok-close",
                api_key=FAKE_KEY,
                client=client,
            )
            receiver.close()
            receiver.close()

        self.assertTrue(receiver.released)
        self.assertEqual(len(deletes), 1)
        self.assertEqual(deletes[0].url.path, "/v1/accounts/account-1")
        self.assertEqual(deletes[0].headers["Authorization"], f"Bearer {FAKE_TOKEN}")
        self.assertNotIn(FAKE_TOKEN, repr(receiver))
        self.assertNotIn(FAKE_KEY, repr(receiver))


if __name__ == "__main__":
    unittest.main()
