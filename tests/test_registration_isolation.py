"""Contracts for keeping account registration outside the API failure domain."""

from __future__ import annotations

import importlib
import os
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def _service_block(compose: str, name: str) -> str:
    marker = f"  {name}:\n"
    tail = compose.split(marker, 1)[1]
    next_service = re.search(r"\n  [a-zA-Z0-9][a-zA-Z0-9_-]*:\n", tail)
    return tail[: next_service.start()] if next_service else tail


class RegistrationIsolationComposeTests(unittest.TestCase):
    def test_api_service_cannot_start_registration_or_browser(self) -> None:
        compose = (ROOT / "docker-compose.server.yml").read_text(encoding="utf-8")
        api = _service_block(compose, "grokcli-2api")

        self.assertIn('GROK2API_REG_AUTO_MAINTAIN: "0"', api)
        self.assertIn('GROK2API_INLINE_SOLVER: "0"', api)
        self.assertIn('mem_limit: 2g', api)
        self.assertIn('cpus: "2.00"', api)
        self.assertIn('pids_limit: 256', api)

    def test_registration_service_is_small_private_and_slow(self) -> None:
        compose = (ROOT / "docker-compose.server.yml").read_text(encoding="utf-8")
        worker = _service_block(compose, "grok-registration")

        self.assertIn('command: ["python", "registration_worker.py"]', worker)
        self.assertIn('GROK2API_REG_AUTO_MAINTAIN: "1"', worker)
        self.assertIn('GROK2API_REG_AUTO_BATCH_SIZE: "1"', worker)
        self.assertIn('GROK2API_REG_CONCURRENCY: "1"', worker)
        self.assertIn('GROK2API_REG_PREFETCH_SLOTS: "0"', worker)
        self.assertIn('GROK2API_REG_AUTO_REST_SEC: "600"', worker)
        self.assertIn('GROK2API_REG_ADAPTIVE_CONCURRENCY: "1"', worker)
        self.assertIn('GROK2API_REG_MAX_CONCURRENCY: "2"', worker)
        self.assertIn('GROK2API_REG_MAX_MEMORY_BYTES: "1900000000"', worker)
        self.assertIn('GROK2API_REG_MAX_PIDS: "300"', worker)
        self.assertIn('TURNSTILE_THREAD: "1"', worker)
        self.assertIn('TURNSTILE_NICE: "10"', worker)
        self.assertIn('restart: "no"', worker)
        self.assertIn('mem_limit: 2g', worker)
        self.assertIn('cpus: "0.75"', worker)
        self.assertIn('pids_limit: 320', worker)
        self.assertNotIn("\n    ports:", worker)
        self.assertNotIn("\n      new-api:", worker)


class RegistrationWorkerTests(unittest.TestCase):
    @staticmethod
    def _module():
        return importlib.import_module("registration_worker")

    def test_resource_guard_detects_memory_pids_and_oom(self) -> None:
        worker = self._module()

        self.assertIn(
            "memory",
            worker._resource_guard_reason(
                memory_bytes=101,
                pids=1,
                oom_kill=0,
                baseline_oom_kill=0,
                max_memory_bytes=100,
                max_pids=10,
            ),
        )
        self.assertIn(
            "pids",
            worker._resource_guard_reason(
                memory_bytes=1,
                pids=11,
                oom_kill=0,
                baseline_oom_kill=0,
                max_memory_bytes=100,
                max_pids=10,
            ),
        )
        self.assertIn(
            "oom_kill",
            worker._resource_guard_reason(
                memory_bytes=1,
                pids=1,
                oom_kill=1,
                baseline_oom_kill=0,
                max_memory_bytes=100,
                max_pids=10,
            ),
        )

    def test_runtime_validation_rejects_unsafe_batch_size(self) -> None:
        worker = self._module()

        with (
            patch.object(worker.redis_client, "ping", return_value=True),
            patch.object(worker.pg, "ping", return_value=True),
            patch.object(worker.registration_maintainer, "is_enabled", return_value=True),
            patch.object(worker.registration_maintainer, "_batch_size", return_value=3),
            patch.object(worker.registration_maintainer, "_concurrency", return_value=1),
            patch.object(worker.registration_maintainer, "_rest_sec", return_value=600),
            patch.object(worker.adapter, "REG_PREFETCH_SLOTS", 0),
        ):
            with self.assertRaisesRegex(RuntimeError, "batch size"):
                worker._validate_runtime()

    def test_runtime_validation_fails_closed_without_redis(self) -> None:
        worker = self._module()

        with patch.object(worker.redis_client, "ping", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "healthy Redis"):
                worker._validate_runtime()

    def test_heartbeat_is_local_and_cluster_visible(self) -> None:
        worker = self._module()
        with tempfile.TemporaryDirectory() as tmp:
            heartbeat = Path(tmp) / "heartbeat"
            with patch.object(worker.redis_client, "set_ex", return_value=True) as publish:
                worker._write_heartbeat(heartbeat)

        self.assertTrue(publish.called)
        self.assertEqual(publish.call_args.args[0], worker.HEARTBEAT_KEY)
        self.assertEqual(publish.call_args.args[1], worker.WORKER_ID)
        self.assertEqual(publish.call_args.args[2], 30)

    def test_api_status_recognizes_remote_registration_worker(self) -> None:
        import registration_maintainer
        from store import leader, redis_client

        maintainer = importlib.reload(registration_maintainer)

        def get_str(name: str):
            if name.endswith(":registration_worker:heartbeat"):
                return "registration@worker"
            return None

        with (
            patch.object(maintainer, "is_enabled", return_value=False),
            patch.object(leader, "status", return_value={"leader_id": None}),
            patch.object(redis_client, "redis_enabled", return_value=True),
            patch.object(redis_client, "get_str", side_effect=get_str),
        ):
            status = maintainer.status()

        self.assertTrue(status["running"])
        self.assertEqual(status["worker_id"], "registration@worker")

    def test_clean_exit_stops_maintainer_and_active_sessions(self) -> None:
        worker = self._module()
        stop = threading.Event()
        stop.set()

        with (
            patch.object(worker, "_validate_runtime"),
            patch.object(worker, "_write_heartbeat"),
            patch.object(worker, "_cgroup_snapshot", return_value=(1, 1, 0)),
            patch.object(
                worker,
                "read_snapshot",
                return_value=worker.ResourceSnapshot(
                    at=1.0,
                    cpu_idle_pct=None,
                    mem_available_bytes=4 * 1024 * 1024 * 1024,
                    registration_memory_bytes=1,
                    registration_pids=1,
                    registration_oom_kill=0,
                    api_healthy=True,
                    api_age_sec=1.0,
                    api_local_p95_ms=100.0,
                    api_sample_count=1,
                    api_error_rate=0.0,
                ),
            ),
            patch.object(worker.registration_maintainer, "start_background") as start,
            patch.object(worker.registration_maintainer, "stop_background") as stop_bg,
            patch.object(worker.adapter, "stop_all_active_registrations") as stop_all,
        ):
            self.assertEqual(worker.run(stop_event=stop, poll_sec=0.01), 0)

        start.assert_called_once_with()
        stop_all.assert_called_once_with()
        stop_bg.assert_called_once_with()


class SingleAttemptBatchContractTests(unittest.TestCase):
    def test_adapter_can_wrap_one_attempt_in_batch_mode(self) -> None:
        import grok_build_adapter as adapter

        with (
            patch.dict(os.environ, {}, clear=False),
            patch.object(adapter, "CAPTCHA_PROVIDER", adapter.CAPTCHA_PROVIDER),
            patch.object(adapter, "LOCAL_SOLVER_URL", adapter.LOCAL_SOLVER_URL),
            patch.object(adapter, "YESCAPTCHA_KEY", adapter.YESCAPTCHA_KEY),
            patch.object(adapter, "ensure_xconsole"),
            patch.object(adapter, "_clean_old_sessions"),
            patch.object(adapter, "_mirror_reg_batch"),
            patch.object(
                adapter,
                "_spawn_batch_runner",
                return_value={"ok": True, "batch_id": "unused"},
            ),
            patch.object(adapter.time, "sleep"),
        ):
            result = adapter.start_registration(
                captcha_provider="local",
                count=1,
                concurrency=1,
                force_batch=True,
                mail_provider="yyds",
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["batch"])
        self.assertEqual(result["count"], 1)
        self.assertTrue(result["batch_id"].startswith("batch_"))
        adapter._batches.pop(result["batch_id"], None)

    def test_maintainer_requests_batch_envelope_for_one_attempt(self) -> None:
        import grok_build_adapter as adapter
        import registration_maintainer as maintainer
        import settings_store

        with (
            patch.object(settings_store, "resolve_registration_inputs", return_value={}),
            patch.object(
                adapter,
                "start_registration",
                return_value={"ok": True, "batch_id": "batch_one"},
            ) as start,
        ):
            result = maintainer._start_batch(1)

        self.assertEqual(result["batch_id"], "batch_one")
        self.assertIs(start.call_args.kwargs["force_batch"], True)

    def test_maintainer_stops_if_batch_id_is_missing(self) -> None:
        import grok_build_adapter as adapter
        import registration_maintainer as maintainer
        import settings_store

        with (
            patch.object(settings_store, "resolve_registration_inputs", return_value={}),
            patch.object(
                adapter,
                "start_registration",
                return_value={"ok": True, "id": "single_session"},
            ),
            patch.object(adapter, "stop_all_active_registrations") as stop_all,
        ):
            result = maintainer._start_batch(1)

        self.assertFalse(result["ok"])
        self.assertIn("batch_id", result["error"])
        stop_all.assert_called_once_with()

    def test_start_failure_publishes_start_error_without_keyword_collision(self) -> None:
        import registration_maintainer as maintainer

        published: list[dict] = []
        waits = iter([False, True])

        with (
            patch.object(maintainer, "_load_remote_state", return_value={}),
            patch.object(maintainer, "_publish", side_effect=lambda **kw: published.append(kw)),
            patch.object(maintainer, "_wait", side_effect=lambda _seconds: next(waits)),
            patch.object(maintainer, "_find_active_batch", return_value=None),
            patch.object(
                maintainer,
                "_pool_counts",
                return_value={"total": 1, "live": 1, "enabled": 1, "available": 0},
            ),
            patch.object(maintainer, "_target", return_value=1),
            patch.object(maintainer, "_start_batch", return_value={"ok": False, "error": "nope"}),
        ):
            maintainer._stop.clear()
            maintainer._worker()

        self.assertTrue(any(item.get("phase") == "start_error" for item in published))
        self.assertFalse(
            any("multiple values" in str(item.get("last_error")) for item in published)
        )

    def test_stop_all_skips_terminal_session_writes(self) -> None:
        import grok_build_adapter as adapter

        listed = {
            "sessions": [
                {"id": "done", "status": "completed"},
                {"id": "live", "status": "solving_turnstile"},
            ],
            "batches": [],
        }
        with (
            patch.object(adapter, "list_registration_sessions", return_value=listed),
            patch.object(
                adapter,
                "stop_registration_session",
                return_value={"ok": True, "id": "live"},
            ) as stop_one,
        ):
            result = adapter.stop_all_active_registrations()

        stop_one.assert_called_once_with("live")
        self.assertEqual(result["stopped_count"], 1)
        self.assertEqual(result["already_count"], 1)


if __name__ == "__main__":
    unittest.main()
