"""Round 8 fail-closed, permission, API, OIDC, and log-redaction tests."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _request():
    from starlette.requests import Request

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 3000),
        }
    )


class SecurityGateTests(unittest.TestCase):
    def test_startup_migrates_all_secret_permissions(self) -> None:
        from secure_storage import migrate_secret_permissions

        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir(mode=0o755)
            auth = data / "auth.json"
            settings = data / "settings.json"
            keys = data / "keys.json"
            backup = data / "auth.json.bak.1"
            queue_db = data / "registration_queue.db"
            metrics_db = data / "registration_metrics.db"
            secret_dirs = [
                data / "pending_sso",
                data / "cookie_bundles",
                data / "register_sso",
            ]
            for path in (auth, settings, keys, backup, queue_db, metrics_db):
                path.write_text("{}", encoding="utf-8")
                os.chmod(path, 0o644)
            nested_files: list[Path] = []
            for directory in secret_dirs:
                directory.mkdir(mode=0o755)
                nested = directory / "secret.json"
                nested.write_text("{}", encoding="utf-8")
                os.chmod(nested, 0o644)
                nested_files.append(nested)

            result = migrate_secret_permissions(
                data_dir=data,
                auth_file=auth,
                settings_file=settings,
                keys_file=keys,
                secret_dirs=secret_dirs,
                database_paths=(queue_db, metrics_db),
                strict=True,
            )

            self.assertEqual(result["failed"], 0)
            self.assertEqual(_mode(data), 0o700)
            for directory in secret_dirs:
                self.assertEqual(_mode(directory), 0o700)
            for path in (auth, settings, keys, backup, queue_db, metrics_db, *nested_files):
                self.assertEqual(_mode(path), 0o600)

    def test_new_settings_and_keys_are_0600(self) -> None:
        import apikeys
        import settings_store

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings_file = root / "state" / "settings.json"
            keys_file = root / "keys" / "keys.json"
            old_mem = settings_store._mem
            old_dirty = settings_store._mem_dirty
            settings_store._mem = None
            settings_store._mem_dirty = False
            try:
                with mock.patch.object(settings_store, "DATA_DIR", settings_file.parent), mock.patch.object(
                    settings_store, "SETTINGS_FILE", settings_file
                ), mock.patch.object(apikeys, "KEYS_FILE", keys_file):
                    settings_store.set_admin_password("test-password")
                    apikeys.create_key("test")
                self.assertEqual(_mode(settings_file), 0o600)
                self.assertEqual(_mode(keys_file), 0o600)
            finally:
                settings_store._mem = old_mem
                settings_store._mem_dirty = old_dirty

    def test_secret_directories_are_0700(self) -> None:
        from secure_storage import ensure_private_dir

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "private" / "nested"
            ensure_private_dir(target)
            self.assertEqual(_mode(target), 0o700)

    def test_health_has_no_identity(self) -> None:
        import app
        import model_health
        from auth import GrokCredentials

        creds = GrokCredentials(
            token="secret-token",
            email="identity@example.com",
            auth_key="account-secret",
            expires_at=time.time() + 3600,
        )
        with mock.patch.object(
            app.account_pool,
            "pool_summary",
            return_value={"mode": "round_robin", "live": 1, "enabled": 1, "total": 1},
        ), mock.patch.object(app.account_pool, "acquire", return_value=creds), mock.patch.object(
            app.token_maintainer, "status", return_value={"ok": True}
        ), mock.patch.object(model_health, "status", return_value={"ok": True}), mock.patch.object(
            app.conversation_affinity, "status", return_value={"size": 0}
        ), mock.patch("grok_build_adapter.registration_available", return_value={"available": True}):
            result = asyncio.run(app.health())
        payload = json.dumps(result, ensure_ascii=False, default=str)
        self.assertNotIn("identity@example.com", payload)
        self.assertNotIn("account-secret", payload)
        self.assertNotIn("secret-token", payload)
        for key in ("email", "expires_at", "auth_key"):
            self.assertNotIn(key, result)

    def test_public_status_has_no_credentials_email(self) -> None:
        import admin_routes as routes
        from auth import GrokCredentials

        creds = GrokCredentials(token="token", email="identity@example.com")
        with mock.patch.object(routes, "is_setup_needed", return_value=False), mock.patch.object(
            routes.accounts,
            "account_status",
            return_value={"account_count": 1, "active_count": 1},
        ), mock.patch.object(routes.apikeys, "stats", return_value={"total": 1}), mock.patch.object(
            routes.account_pool,
            "pool_summary",
            return_value={
                "mode": "round_robin",
                "total": 1,
                "live": 1,
                "enabled": 1,
                "in_cooldown": 0,
            },
        ), mock.patch.object(routes.account_pool, "acquire", return_value=creds), mock.patch.object(
            routes, "load_models_from_cache", return_value=[]
        ), mock.patch.object(routes, "get_public_settings", return_value={}), mock.patch.object(
            routes.token_maintainer, "status", return_value={}
        ), mock.patch.object(routes.model_health, "status", return_value={}), mock.patch.object(
            routes.conversation_affinity, "status", return_value={}
        ), mock.patch.object(
            routes.reg_adapter, "registration_available", return_value={"available": True}
        ):
            result = asyncio.run(routes.admin_status(_request()))
        payload = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("credentials_email", result)
        self.assertNotIn("identity@example.com", payload)

    def test_client_responses_have_no_internal_account(self) -> None:
        import anthropic_compat as anth
        import app
        from auth import GrokCredentials

        creds = GrokCredentials(
            token="token", email="identity@example.com", auth_key="internal-account"
        )
        request = _request()
        openai_req = app.ChatCompletionRequest(
            model="grok-4.5", messages=[{"role": "user", "content": "hello"}]
        )
        anthropic_req = anth.AnthropicMessagesRequest(
            model="grok-4.5",
            messages=[{"role": "user", "content": "hello"}],
            max_tokens=32,
        )
        collected = ("hello", None, "stop", {"total_tokens": 2}, None)
        with mock.patch.object(
            app, "_resolve_conversation_affinity", return_value=(None, None)
        ), mock.patch.object(
            app, "_resolve_anthropic_affinity", return_value=(None, None)
        ), mock.patch.object(
            app.account_pool, "try_acquire_sequence", return_value=[creds]
        ), mock.patch.object(
            app, "_collect_completion", new=mock.AsyncMock(return_value=collected)
        ), mock.patch.object(app.account_pool, "report_success"):
            openai_result = asyncio.run(app.chat_completions(openai_req, request))
            anthropic_result = asyncio.run(
                app.anthropic_messages(anthropic_req, request, None)
            )
        for result in (openai_result, anthropic_result):
            payload = json.dumps(result, ensure_ascii=False, default=str)
            self.assertNotIn("x_grok2api_account", payload)
            self.assertNotIn("identity@example.com", payload)
            self.assertNotIn("internal-account", payload)

    def test_oidc_session_redacts_all_secrets(self) -> None:
        import oidc_auth

        session_id = "session-safe-view"
        secret_values = (
            "device-secret",
            "USER-CODE",
            "access-secret",
            "identity@example.com",
            "https://auth.invalid/verify?code=device-secret",
        )
        oidc_auth._device_sessions[session_id] = {
            "id": session_id,
            "mode": "oidc",
            "status": "success",
            "device_code": secret_values[0],
            "user_code": secret_values[1],
            "access_token": secret_values[2],
            "email": secret_values[3],
            "verification_url": secret_values[4],
            "output": "token=" + secret_values[2],
            "started_at": 1.0,
            "finished_at": 2.0,
        }
        try:
            result = oidc_auth.get_device_session(session_id)
        finally:
            oidc_auth._device_sessions.pop(session_id, None)
        payload = json.dumps(result, ensure_ascii=False)
        for secret in secret_values:
            self.assertNotIn(secret, payload)
        for key in ("device_code", "user_code", "output_tail", "email", "account_id"):
            self.assertNotIn(key, result)

    def test_batch_redacts_device_user_code_and_query_secrets(self) -> None:
        import grok_build_adapter as adapter

        raw = {
            "device_code": "device-secret",
            "user_code": "USER-CODE",
            "nested": {
                "url": "https://example.invalid/cb?token=secret-token&state=secret-state",
                "password": "secret-password",
            },
        }
        result = adapter._compact_value(raw)
        payload = json.dumps(result, ensure_ascii=False)
        for secret in (
            "device-secret",
            "USER-CODE",
            "secret-token",
            "secret-state",
            "secret-password",
        ):
            self.assertNotIn(secret, payload)

    def test_debug_never_prints_set_cookie_jwt_or_sso(self) -> None:
        import grok_build_adapter as adapter

        adapter.ensure_xconsole()
        from xconsole_client.oauth_protocol import ProtocolOAuthClient

        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzZWNyZXQifQ.signaturesecret"
        instance = ProtocolOAuthClient.__new__(ProtocolOAuthClient)
        instance.debug = True
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            instance._log(
                "set-cookie Location=https://auth.invalid/set-cookie?q="
                + jwt
                + " device_code=device-secret user_code=USER-CODE"
            )
        output = stream.getvalue()
        for secret in (jwt, "device-secret", "USER-CODE", "signaturesecret"):
            self.assertNotIn(secret, output)

    def test_corrupt_settings_fails_closed(self) -> None:
        import settings_store

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text("{not-json", encoding="utf-8")
            old_mem = settings_store._mem
            settings_store._mem = None
            try:
                with mock.patch.object(settings_store, "DATA_DIR", path.parent), mock.patch.object(
                    settings_store, "SETTINGS_FILE", path
                ), self.assertRaises(settings_store.SettingsStoreCorrupt):
                    settings_store.is_setup_needed()
            finally:
                settings_store._mem = old_mem

    def test_corrupt_key_store_still_requires_auth(self) -> None:
        import apikeys

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "keys.json"
            path.write_text("{not-json", encoding="utf-8")
            with mock.patch.object(apikeys, "KEYS_FILE", path), mock.patch.object(
                apikeys, "REQUIRE_API_KEY", "0"
            ):
                self.assertTrue(apikeys.auth_required())
                self.assertIsNone(apikeys.verify_key("anything"))

    def test_api_auth_default_is_fail_closed(self) -> None:
        env = dict(os.environ)
        env.pop("GROK2API_REQUIRE_API_KEY", None)
        result = subprocess.run(
            [sys.executable, "-c", "import config; print(config.REQUIRE_API_KEY)"],
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.stdout.strip(), "1")


if __name__ == "__main__":
    unittest.main()
