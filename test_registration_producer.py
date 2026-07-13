from __future__ import annotations

import io
import json
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import registration_producer as producer


class RegistrationProducerTests(unittest.TestCase):
    def setUp(self) -> None:
        producer._cleanup_observations.clear()

    def test_wait_batch_uses_adapter_error_counter(self) -> None:
        response = {
            "count": 3,
            "total": 3,
            "imported": 2,
            "error": 1,
            "running": 0,
        }
        with patch.object(producer, "_request", return_value=response), patch.object(
            producer, "_touch"
        ):
            result = producer._wait_batch("token", "batch_1")
            # Structured outcome: imported/errors (mint_queued optional)
            if isinstance(result, tuple):
                self.assertEqual(result, (2, 1))
            else:
                self.assertEqual(result.get("imported"), 2)
                self.assertEqual(result.get("errors"), 1)

    def test_pending_recovery_imports_then_deletes_secret_file(self) -> None:
        secret = "eyJ" + "s" * 40 + ".payload.signature"
        with tempfile.TemporaryDirectory() as td:
            pending_dir = Path(td)
            path = pending_dir / "gba_ok.json"
            path.write_text(
                json.dumps(
                    {
                        "session_id": "gba_ok",
                        "email": "test@example.invalid",
                        "sso": secret,
                        "created_at": 1,
                    }
                ),
                encoding="utf-8",
            )
            fake_import = SimpleNamespace(
                sso_to_token=lambda value: (
                    print(f"converter saw {value}"),
                    {"access_token": "access", "refresh_token": "refresh"},
                )[1],
                token_to_auth_entry=lambda token, email="": ("key", {"key": token["access_token"], "email": email}),
                import_into_project_auth=lambda entry: "account-id",
            )
            output = io.StringIO()
            with patch.object(producer, "PENDING_DIR", pending_dir), patch.object(
                producer, "PENDING_MIN_AGE_SEC", 0
            ), patch.object(producer, "PENDING_SUCCESS_COOLDOWN_SEC", 0), patch.dict(
                "sys.modules", {"sso_to_auth_json": fake_import}
            ), patch("sys.stdout", output):
                self.assertEqual(producer._recover_pending_sso(now=100), (1, 0))

            self.assertFalse(path.exists())
            self.assertNotIn(secret, output.getvalue())
            self.assertIn("<redacted-sso>", output.getvalue())

    def test_pending_failure_is_retained_with_backoff_and_mode_600(self) -> None:
        secret = "eyJ" + "x" * 40 + ".payload.signature"
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "gba_retry.json"
            original = {"session_id": "gba_retry", "sso": secret, "created_at": 1}
            path.write_text(json.dumps(original), encoding="utf-8")
            fake_import = SimpleNamespace(sso_to_token=lambda _value: None)
            with patch.object(producer, "PENDING_RETRY_BASE_SEC", 120), patch.object(
                producer, "PENDING_RETRY_MAX_SEC", 3600
            ), patch.dict("sys.modules", {"sso_to_auth_json": fake_import}):
                self.assertFalse(producer._recover_pending_file(path, original, now=1000))

            retained = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(retained["sso"], secret)
            self.assertEqual(retained["recovery_attempts"], 1)
            self.assertEqual(retained["next_recovery_at"], 1120)
            self.assertEqual(retained["last_recovery_error"], "RuntimeError")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_pending_without_sso_is_retained_instead_of_crashing_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "broken.json"
            original = {"session_id": "broken", "created_at": 1}
            path.write_text(json.dumps(original), encoding="utf-8")
            with patch.object(producer, "PENDING_RETRY_BASE_SEC", 120), patch.object(
                producer, "PENDING_RETRY_MAX_SEC", 3600
            ):
                self.assertFalse(producer._recover_pending_file(path, original, now=1000))
            retained = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(retained["last_recovery_error"], "ValueError")
            self.assertEqual(retained["recovery_attempts"], 1)

    def test_pending_recovery_skips_live_and_backing_off_entries(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pending_dir = Path(td)
            for name, payload in {
                "live.json": {"session_id": "live", "sso": "secret", "created_at": 950},
                "backoff.json": {
                    "session_id": "backoff",
                    "sso": "secret",
                    "created_at": 1,
                    "next_recovery_at": 1100,
                },
            }.items():
                (pending_dir / name).write_text(json.dumps(payload), encoding="utf-8")
            with patch.object(producer, "PENDING_DIR", pending_dir), patch.object(
                producer, "PENDING_MIN_AGE_SEC", 120
            ), patch.object(producer, "_recover_pending_file") as recover:
                self.assertEqual(producer._recover_pending_sso(now=1000), (0, 0))
            recover.assert_not_called()

    def test_pending_recovery_is_bounded_oldest_first(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pending_dir = Path(td)
            for index, created in enumerate((30, 10, 20), 1):
                (pending_dir / f"item-{index}.json").write_text(
                    json.dumps({"session_id": f"item-{index}", "sso": "secret", "created_at": created}),
                    encoding="utf-8",
                )
            seen: list[str] = []

            def recover(path: Path, payload: dict, *, now: float) -> bool:
                # Claim renames to .processing.<pid>.json; assert order via session_id
                seen.append(f"{payload.get('session_id')}.json")
                path.unlink()
                return True

            with patch.object(producer, "PENDING_DIR", pending_dir), patch.object(
                producer, "PENDING_MIN_AGE_SEC", 0
            ), patch.object(producer, "PENDING_MAX_PER_CYCLE", 2), patch.object(
                producer, "PENDING_SUCCESS_COOLDOWN_SEC", 0
            ), patch.object(producer, "_recover_pending_file", side_effect=recover), patch.object(
                producer, "_touch"
            ):
                self.assertEqual(producer._recover_pending_sso(now=100), (2, 0))

            self.assertEqual(seen, ["item-2.json", "item-3.json"])  # oldest-first by created_at

    def test_pending_recovery_does_not_starve_behind_registration_marker(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pending_dir = root / "pending"
            pending_dir.mkdir()
            marker = root / "registration_active"
            marker.touch()
            path = pending_dir / "item.json"
            path.write_text(
                json.dumps({"session_id": "item", "sso": "secret", "created_at": 1}),
                encoding="utf-8",
            )

            def recover(target: Path, _payload: dict, *, now: float) -> bool:
                target.unlink()
                return True

            with patch.object(producer, "PENDING_DIR", pending_dir), patch.object(
                producer, "REGISTRATION_ACTIVE", marker
            ), patch.object(producer, "PENDING_MIN_AGE_SEC", 0), patch.object(
                producer, "PENDING_SUCCESS_COOLDOWN_SEC", 0
            ), patch.object(producer, "_recover_pending_file", side_effect=recover), patch.object(
                producer, "_touch"
            ):
                self.assertEqual(producer._recover_pending_sso(now=100), (1, 0))

            self.assertFalse(path.exists())

    def test_pool_snapshot_counts_only_effective_renewable_accounts(self) -> None:
        response = {
            "accounts": [
                {"id": "ok", "expired": False, "has_refresh_token": True},
                {"id": "expired", "expired": True, "has_refresh_token": True},
                {
                    "id": "invalid",
                    "expired": False,
                    "has_refresh_token": True,
                    "refresh_status": "refresh_terminal",
                    "refresh_failure_count": 3,
                    "refresh_confirmed_after_expiry": True,
                    "refresh_terminal_at": 100.0,
                },
                {"id": "no-refresh", "expired": False, "has_refresh_token": False},
                {"id": "quota", "expired": False, "has_refresh_token": True},
                {"id": "model-blocked", "expired": False, "has_refresh_token": True},
            ],
            "pool": {
                "accounts": [
                    {"id": "ok", "enabled": True},
                    {"id": "expired", "enabled": True},
                    {"id": "invalid", "enabled": True},
                    {"id": "no-refresh", "enabled": True},
                    {
                        "id": "quota",
                        "enabled": False,
                        "disabled_for_quota": True,
                    },
                    {
                        "id": "model-blocked",
                        "enabled": True,
                        "blocked_model_ids": ["grok-4.5"],
                    },
                ]
            },
        }
        with patch.object(producer, "_request", return_value=response):
            snapshot = producer._pool_snapshot("token")
        self.assertEqual(snapshot["total"], 6)
        self.assertEqual(snapshot["effective"], 1)

    def test_cleanup_dry_run_requires_age_confirmations_and_window(self) -> None:
        rows = [
            {
                "id": "dead",
                "refresh_status": "refresh_terminal",
                "refresh_failure_count": 3,
                "refresh_confirmed_after_expiry": True,
                "refresh_terminal_at": 100.0,
            }
        ]
        with patch.object(producer, "CLEANUP_ENABLED", True), patch.object(
            producer, "CLEANUP_DRY_RUN", True
        ), patch.object(producer, "CLEANUP_MIN_AGE_SEC", 100.0), patch.object(
            producer, "CLEANUP_CONFIRM_WINDOW_SEC", 50.0
        ), patch.object(producer, "CLEANUP_CONFIRMATIONS", 3), patch.object(
            producer.time, "time", side_effect=[200.0, 220.0, 260.0]
        ), patch.object(producer, "_request") as request:
            first = producer._cleanup_accounts("token", rows)
            second = producer._cleanup_accounts("token", rows)
            third = producer._cleanup_accounts("token", rows)
        self.assertEqual(first["ready"], 0)
        self.assertEqual(second["ready"], 0)
        self.assertEqual(third["ready"], 1)
        self.assertTrue(third["dry_run"])
        request.assert_not_called()

    def test_cleanup_deletes_only_permanent_failures(self) -> None:
        rows = [
            {"id": "manual", "enabled": False},
            {"id": "cooldown", "in_cooldown": True},
            {"id": "model", "blocked_model_ids": ["grok-4.5"]},
            {
                "id": "quota",
                "enabled": True,
                "disabled_for_quota": True,
                "quota_waiting": True,
                "quota_disabled_at": 1.0,
            },
            {
                "id": "dead",
                "refresh_status": "refresh_terminal",
                "refresh_failure_count": 3,
                "refresh_confirmed_after_expiry": True,
                "refresh_terminal_at": 1.0,
            },
        ]
        delete_result = {"removed": ["dead"], "removed_count": 1}
        with patch.object(producer, "CLEANUP_ENABLED", True), patch.object(
            producer, "CLEANUP_DRY_RUN", False
        ), patch.object(producer, "CLEANUP_MIN_AGE_SEC", 1.0), patch.object(
            producer, "CLEANUP_CONFIRM_WINDOW_SEC", 1.0
        ), patch.object(producer, "CLEANUP_CONFIRMATIONS", 2), patch.object(
            producer.time, "time", side_effect=[100.0, 102.0]
        ), patch.object(producer, "_request", return_value=delete_result) as request:
            first = producer._cleanup_accounts("token", rows)
            second = producer._cleanup_accounts("token", rows)
        # Quota waiting must never be deleted; only fully confirmed refresh terminal.
        self.assertEqual(first["ready"], 0)
        self.assertEqual(second["removed"], 1)
        request.assert_called_once_with(
            "POST",
            "/admin/api/accounts/delete-batch",
            token="token",
            body={"ids": ["dead"]},
        )

    def test_domain_rotates_after_configured_import_count(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch.object(
            producer, "PRODUCER_STATE", Path(td) / "state.json"
        ), patch.object(producer, "PRODUCER_DOMAINS", ("a.example", "b.example")), patch.object(
            producer, "DOMAIN_ROTATE_EVERY", 500
        ):
            self.assertEqual(producer._registration_domain(), "a.example")
            producer._record_imported(499)
            self.assertEqual(producer._registration_domain(), "a.example")
            producer._record_imported(1)
            self.assertEqual(producer._registration_domain(), "b.example")

    def test_corrupt_persisted_counts_fail_safe(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch.object(
            producer, "PRODUCER_STATE", Path(td) / "state.json"
        ), patch.object(producer, "PRODUCER_DOMAINS", ("a.example", "b.example")), patch.object(
            producer, "DOMAIN_ROTATE_EVERY", 500
        ):
            producer.PRODUCER_STATE.write_text(
                json.dumps({"imported_lifetime": "broken"}), encoding="utf-8"
            )
            self.assertEqual(producer._registration_domain(), "a.example")
            producer._record_imported(1)
            state = json.loads(producer.PRODUCER_STATE.read_text(encoding="utf-8"))
            self.assertEqual(state["imported_lifetime"], 1)

        self.assertIsNone(
            producer._cleanup_reason(
                {
                    "refresh_status": "refresh_terminal",
                    "refresh_failure_count": "broken",
                    "refresh_confirmed_after_expiry": True,
                    "refresh_terminal_at": 1.0,
                }
            )
        )


if __name__ == "__main__":
    unittest.main()
