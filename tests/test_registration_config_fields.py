"""Regression tests for provider-specific registration configuration fields."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from admin_routes import EmailRegistrationBody, _registration_cfg_from_body
import settings_store


class RegistrationConfigFieldTests(unittest.TestCase):
    def test_moemail_host_survives_registration_request_parsing(self) -> None:
        body = EmailRegistrationBody(
            mail_provider="moemail",
            moemail_base_url="https://mail.example.test",
            moemail_api_key="mail-key",
            moemail_domain="example.test",
            count=1,
            concurrency=1,
        )

        config = _registration_cfg_from_body(body)

        self.assertEqual(config["moemail_base_url"], "https://mail.example.test")
        self.assertEqual(config["moemail_api_key"], "mail-key")
        self.assertEqual(config["moemail_domain"], "example.test")

    def test_cfmail_host_is_preserved_for_provider_switching(self) -> None:
        body = EmailRegistrationBody(
            mail_provider="cfmail",
            cfmail_base_url="https://cf.example.test",
            count=1,
            concurrency=1,
        )

        self.assertEqual(
            _registration_cfg_from_body(body)["cfmail_base_url"],
            "https://cf.example.test",
        )

    def test_provider_switch_does_not_reuse_previous_moemail_host(self) -> None:
        body = EmailRegistrationBody(
            mail_provider="moemail",
            moemail_base_url="https://mail.example.test",
            moemail_api_key="mail-key",
            moemail_domain="example.test",
            count=1,
            concurrency=1,
        )

        with (
            patch.object(
                settings_store,
                "_get_setting_value",
                return_value={
                    "registration_config": {
                        "mail_provider": "yyds",
                        "api_key": "old-key",
                        "yyds_api_key": "old-key",
                        "base_url": "https://maliapi.215.im",
                        "moemail_base_url": "https://old-mail.example.test",
                    }
                },
            ),
            patch.object(settings_store, "_env_registration_defaults", return_value={}),
        ):
            resolved = settings_store.resolve_registration_inputs(
                _registration_cfg_from_body(body)
            )

        self.assertEqual(resolved["mail_provider"], "moemail")
        self.assertEqual(resolved["base_url"], "https://mail.example.test")
        self.assertEqual(resolved["moemail_base_url"], "https://mail.example.test")


if __name__ == "__main__":
    unittest.main()
