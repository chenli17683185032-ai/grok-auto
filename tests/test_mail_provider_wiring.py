from __future__ import annotations

import unittest
from unittest.mock import patch

from pydantic import ValidationError

import admin_routes
import grok_build_adapter as adapter


class MailProviderWiringTests(unittest.TestCase):
    def test_admin_provider_defaults_to_environment_and_accepts_yyds_alias(self) -> None:
        self.assertIsNone(admin_routes.EmailRegistrationBody().provider)
        self.assertEqual(
            admin_routes.EmailRegistrationBody(provider="yyds").provider,
            "yyds",
        )
        self.assertEqual(
            admin_routes.EmailRegistrationBody(provider="yydsmail").provider,
            "yydsmail",
        )
        with self.assertRaises(ValidationError):
            admin_routes.EmailRegistrationBody(provider="unknown")

    def test_adapter_selects_yyds_receiver_without_moemail_domain_fallback(self) -> None:
        sentinel = ("generated@example.test", object())
        with patch(
            "yydsmail.create_yydsmail_receiver",
            return_value=sentinel,
        ) as create_receiver:
            result = adapter._make_email_receiver(
                mail_provider="yyds",
                api_key="fake-api-key",
                base_url="https://vip.215.im/docs",
                prefix="test-prefix",
                domain=None,
            )

        self.assertIs(result, sentinel)
        create_receiver.assert_called_once_with(
            prefix="test-prefix",
            api_key="fake-api-key",
            base_url="https://vip.215.im/docs",
            domain="",
        )

    def test_mail_provider_alias_normalization_is_strict(self) -> None:
        self.assertEqual(adapter._normalize_mail_provider("yydsmail"), "yyds")
        self.assertEqual(adapter._normalize_mail_provider("moemail"), "moemail")
        with self.assertRaises(ValueError):
            adapter._normalize_mail_provider("unknown")

    def test_legacy_adapter_fallback_only_accepts_signature_errors(self) -> None:
        self.assertTrue(
            admin_routes._is_legacy_register_signature_error(
                TypeError("start_registration() got an unexpected keyword argument 'mail_provider'")
            )
        )
        self.assertFalse(
            admin_routes._is_legacy_register_signature_error(
                TypeError("unsupported operand type(s) for +")
            )
        )


if __name__ == "__main__":
    unittest.main()
