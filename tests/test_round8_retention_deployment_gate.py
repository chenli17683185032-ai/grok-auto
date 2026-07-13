"""Round 8 retention, Compose, deployment script, and manual gates."""

from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from registration_jobs import JobState, RegistrationJob, new_job_id
from registration_metrics import RegistrationMetrics
from registration_queue import RegistrationQueue


ROOT = Path(__file__).resolve().parents[1]


def _job(session: str, *, state: str = JobState.MINT_QUEUED.value, **kwargs):
    return RegistrationJob(
        job_id=new_job_id(),
        session_id=session,
        route_id="route-1",
        state=state,
        **kwargs,
    )


class RetentionGateTests(unittest.TestCase):
    def test_terminal_queue_retention_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "queue.db"
            queue = RegistrationQueue(db)
            active = queue.enqueue(_job("active"))
            terminal_ids = []
            for index in range(5):
                job = _job(f"terminal-{index}", state=JobState.FAILED.value)
                queue.save(job)
                terminal_ids.append(job.job_id)
            with closing(sqlite3.connect(db)) as conn:
                conn.execute("UPDATE jobs SET updated_at=100")
                conn.commit()
            self.assertEqual(queue.purge_terminal(before=200.0, limit=2), 2)
            states = queue.list_states()
            self.assertEqual(states.get(JobState.FAILED.value), 3)
            self.assertIsNotNone(queue.get(active.job_id))

    def test_metrics_retention_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            metrics = RegistrationMetrics(Path(tmp) / "metrics.db")
            with mock.patch("registration_metrics.time.time", return_value=100.0):
                for index in range(5):
                    metrics.emit("test", sequence=index)
            result = metrics.purge(before=200.0, max_rows=1000, limit=2)
            self.assertEqual(result, {"deleted": 2, "remaining": 3})

    def test_metrics_sqlite_files_are_private(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "metrics.db"
            metrics = RegistrationMetrics(db)
            paths = (db, Path(f"{db}-wal"), Path(f"{db}-shm"))
            for path in paths:
                path.touch(exist_ok=True)
                os.chmod(path, 0o644)

            metrics._chmod_storage_files()

            for path in paths:
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_retention_preserves_open_jobs(self) -> None:
        import maintenance_retention as retention

        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            pending = data / "pending_sso"
            cookies = data / "cookie_bundles"
            pending.mkdir()
            cookies.mkdir()
            active_pending = pending / "active.json"
            active_cookie = cookies / "active.json"
            stale_pending = pending / "stale.json"
            stale_cookie = cookies / "stale.json"
            for path in (active_pending, active_cookie, stale_pending, stale_cookie):
                path.write_text("{}", encoding="utf-8")
                os.utime(path, (100.0, 100.0))

            queue = RegistrationQueue(data / "queue.db")
            queue.enqueue(
                _job(
                    "active",
                    sso_ref=str(active_pending),
                    cookie_bundle_path=str(active_cookie),
                )
            )
            metrics = RegistrationMetrics(data / "metrics.db")
            with mock.patch.dict(
                os.environ,
                {
                    "GROK2API_PENDING_SSO_DIR": str(pending),
                    "GROK2API_COOKIE_BUNDLE_DIR": str(cookies),
                    "GROK2API_TEMP_FILE_TTL_SEC": "100",
                    "GROK2API_RETENTION_BATCH_SIZE": "20",
                },
            ):
                result = retention.run_if_due(
                    force=True,
                    now=1000.0,
                    data_dir=data,
                    queue=queue,
                    metrics=metrics,
                )
            self.assertTrue(result["ran"])
            self.assertTrue(active_pending.exists())
            self.assertTrue(active_cookie.exists())
            self.assertFalse(stale_pending.exists())
            self.assertFalse(stale_cookie.exists())
            self.assertTrue((data / "retention_status.json").is_file())

    def test_cookie_sweeper_runs_in_production_maintenance(self) -> None:
        import cookie_bundle
        import maintenance_retention as retention

        queue = mock.Mock()
        queue.active_material_paths.return_value = set()
        queue.purge_terminal.return_value = 0
        metrics = mock.Mock()
        metrics.purge.return_value = {"deleted": 0, "remaining": 0}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            cookie_bundle, "sweep_expired", return_value=1
        ) as sweep:
            result = retention.run_if_due(
                force=True,
                now=2000.0,
                data_dir=Path(tmp),
                queue=queue,
                metrics=metrics,
            )
        self.assertEqual(result["cookie_deleted"], 1)
        sweep.assert_called_once()


class DeploymentGateTests(unittest.TestCase):
    def test_all_compose_services_have_log_rotation(self) -> None:
        text = (ROOT / "docker-compose.server.yml").read_text(encoding="utf-8")
        expected_services = (
            "grok-mihomo",
            "grok-mihomo-2",
            "grokcli-2api",
            "ruyipage-approver",
            "ruyipage-approver-2",
            "registration-producer",
            "pending-recovery",
            "registration-mint-worker",
            "registration-mint-worker-2",
        )
        for service in expected_services:
            self.assertIn(f"  {service}:", text)
        self.assertEqual(text.count("logging: *json-logging"), len(expected_services))
        self.assertIn('max-size: "10m"', text)
        self.assertIn('max-file: "3"', text)

    def test_pipeline_profile_has_two_mint_workers(self) -> None:
        text = (ROOT / "docker-compose.server.yml").read_text(encoding="utf-8")
        self.assertIn("  registration-mint-worker:", text)
        self.assertIn("  registration-mint-worker-2:", text)
        self.assertGreaterEqual(text.count('profiles: ["pipeline-v2"]'), 2)
        self.assertIn('GROK2API_MINT_WORKER_ID: mint-1', text)
        self.assertIn('GROK2API_MINT_WORKER_ID: mint-2', text)

    def test_deployment_manual_starts_and_stops_both_workers(self) -> None:
        text = (ROOT / "SERVER_DEPLOYMENT.md").read_text(encoding="utf-8")
        self.assertIn("registration-mint-worker registration-mint-worker-2", text)
        self.assertIn("--profile pipeline-v2 up -d", text)
        self.assertIn("--profile pipeline-v2 stop", text)
        self.assertIn("9 services", text)
        self.assertIn("2.55 CPU", text)
        self.assertIn("4336 MiB", text)

    def test_preflight_has_bounded_waits_and_secret_redaction(self) -> None:
        text = (ROOT / "scripts/server_preflight.sh").read_text(encoding="utf-8")
        self.assertIn("set -euo pipefail", text)
        self.assertIn("set +x", text)
        self.assertIn("timeout --foreground", text)
        self.assertIn("required variable %s is present", text)
        self.assertNotIn('printf \'%s\\n\' "$value"', text)
        self.assertIn("source.backup(target)", text)
        self.assertIn('docker network create "$network_name"', text)
        self.assertIn("database/config backup created", text)

    def test_smoke_has_bounded_waits(self) -> None:
        text = (ROOT / "scripts/smoke_server.sh").read_text(encoding="utf-8")
        self.assertIn("set -euo pipefail", text)
        self.assertIn("timeout --foreground", text)
        self.assertIn("SMOKE_TIMEOUT_SEC", text)
        self.assertIn("deadline=", text)
        self.assertIn("pending-recovery", text)
        self.assertIn("registration-mint-worker-2", text)

    def test_rollback_force_recreates_affected_services(self) -> None:
        text = (ROOT / "scripts/rollback_server.sh").read_text(encoding="utf-8")
        self.assertIn("set -euo pipefail", text)
        self.assertIn("--force-recreate", text)
        self.assertIn('rm -f "$DATA_DIR/$name"', text)
        self.assertIn("file_app_image", text)
        for service in (
            "grokcli-2api",
            "registration-producer",
            "pending-recovery",
            "ruyipage-approver",
            "ruyipage-approver-2",
            "registration-mint-worker",
            "registration-mint-worker-2",
        ):
            self.assertIn(service, text)
        self.assertIn("smoke_server.sh", text)


if __name__ == "__main__":
    unittest.main()
