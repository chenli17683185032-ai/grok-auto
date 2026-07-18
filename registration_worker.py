"""Low-duty registration worker kept outside the API container."""

from __future__ import annotations

import os
import signal
import threading
from pathlib import Path

import grok_build_adapter as adapter
import registration_maintainer
from registration_controller import (
    AdaptiveRegistrationController,
    ResourceSnapshot,
    current_state as controller_state,
    read_snapshot,
    set_current,
)
from store import pg, redis_client


HEARTBEAT_PATH = Path(
    os.getenv(
        "GROK2API_REG_WORKER_HEARTBEAT",
        "/tmp/grok-registration-worker.heartbeat",
    )
)
CGROUP_ROOT = Path(os.getenv("GROK2API_REG_CGROUP_ROOT", "/sys/fs/cgroup"))
WORKER_ID = redis_client.worker_id()
HEARTBEAT_KEY = redis_client.key("registration_worker", "heartbeat")


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _memory_events(root: Path) -> dict[str, int]:
    events: dict[str, int] = {}
    for line in (root / "memory.events").read_text(encoding="utf-8").splitlines():
        name, value = line.split(maxsplit=1)
        events[name] = int(value)
    return events


def _cgroup_snapshot(root: Path = CGROUP_ROOT) -> tuple[int, int, int]:
    memory_bytes = int((root / "memory.current").read_text(encoding="utf-8"))
    pids = int((root / "pids.current").read_text(encoding="utf-8"))
    oom_kill = _memory_events(root).get("oom_kill", 0)
    return memory_bytes, pids, oom_kill


def _resource_guard_reason(
    *,
    memory_bytes: int,
    pids: int,
    oom_kill: int,
    baseline_oom_kill: int,
    max_memory_bytes: int,
    max_pids: int,
) -> str | None:
    if oom_kill > baseline_oom_kill:
        return f"oom_kill increased from {baseline_oom_kill} to {oom_kill}"
    if memory_bytes > max_memory_bytes:
        return f"memory {memory_bytes} exceeds {max_memory_bytes} bytes"
    if pids > max_pids:
        return f"pids {pids} exceeds {max_pids}"
    return None


def _validate_runtime() -> None:
    if not redis_client.ping(force=True):
        raise RuntimeError("registration worker requires healthy Redis")
    if not pg.ping(force=True):
        raise RuntimeError("registration worker requires healthy PostgreSQL")
    if not registration_maintainer.is_enabled():
        raise RuntimeError("registration maintainer must be enabled")
    batch_size = registration_maintainer._batch_size()
    concurrency = registration_maintainer._concurrency()
    if batch_size < 1 or batch_size > 2:
        raise RuntimeError("registration batch size must stay between 1 and 2")
    if concurrency < 1 or concurrency > 2:
        raise RuntimeError("registration concurrency must stay between 1 and 2")
    if registration_maintainer._rest_sec() < 600:
        raise RuntimeError("registration rest interval must be at least 600 seconds")
    if int(adapter.REG_PREFETCH_SLOTS) != 0:
        raise RuntimeError("registration prefetch slots must be 0")


def _write_heartbeat(path: Path = HEARTBEAT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    if not redis_client.set_ex(HEARTBEAT_KEY, WORKER_ID, 30):
        raise RuntimeError("failed to publish registration worker heartbeat")


def _remove_heartbeat(path: Path = HEARTBEAT_PATH) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    try:
        redis_client.compare_and_delete(HEARTBEAT_KEY, WORKER_ID)
    except Exception:
        pass


def run(
    *,
    stop_event: threading.Event | None = None,
    poll_sec: float = 5.0,
) -> int:
    stop = stop_event or threading.Event()
    _validate_runtime()
    _, _, baseline_oom_kill = _cgroup_snapshot()
    max_memory_bytes = _env_int(
        "GROK2API_REG_MAX_MEMORY_BYTES",
        1_900_000_000,
        256 * 1024 * 1024,
        8 * 1024 * 1024 * 1024,
    )
    max_pids = _env_int("GROK2API_REG_MAX_PIDS", 300, 32, 4096)
    exit_code = 0
    controller = AdaptiveRegistrationController.from_env(
        startup_oom_kill=baseline_oom_kill
    )
    baseline_status = registration_maintainer.status(light=True)
    baseline_last_batch = baseline_status.get("last_batch") or {}
    baseline_last_batch_id = (
        str(baseline_last_batch.get("batch_id") or "")
        if isinstance(baseline_last_batch, dict)
        else ""
    )
    initial = controller.evaluate(
        read_snapshot(cgroup_root=CGROUP_ROOT),
        promotion_ready=False,
    )
    set_current(initial)
    single_slot_succeeded = False

    registration_maintainer.start_background()
    _write_heartbeat()
    print(
        "[registration-worker] started "
        f"batch<=2 concurrency=1..{controller.max_concurrency} "
        f"rest={registration_maintainer._rest_sec()}s "
        f"max_memory={max_memory_bytes} max_pids={max_pids}",
        flush=True,
    )
    try:
        while not stop.wait(max(0.1, poll_sec)):
            status = registration_maintainer.status(light=True)
            if not status.get("local_running"):
                raise RuntimeError("registration maintainer thread stopped")
            last_batch = status.get("last_batch") or {}
            if (
                isinstance(last_batch, dict)
                and str(last_batch.get("batch_id") or "")
                and str(last_batch.get("batch_id") or "") != baseline_last_batch_id
                and int(last_batch.get("batch_ok") or 0) > 0
                and int(status.get("concurrency") or 1) == 1
            ):
                single_slot_succeeded = True
            snapshot = read_snapshot(cgroup_root=CGROUP_ROOT)
            decision = controller.evaluate(
                snapshot,
                promotion_ready=single_slot_succeeded,
            )
            set_current(decision)
            registration_maintainer._publish(
                controller={
                    **controller_state(),
                    "safe": decision.safe,
                    "stable_samples": decision.stable_samples,
                    "promoted": decision.promoted,
                    "cpu_idle_pct": snapshot.cpu_idle_pct,
                    "mem_available_bytes": snapshot.mem_available_bytes,
                    "api_local_p95_ms": snapshot.api_local_p95_ms,
                    "api_sample_count": snapshot.api_sample_count,
                },
                concurrency=decision.allowed_concurrency,
            )
            if decision.promoted:
                print(
                    "[registration-worker] promoted registration concurrency "
                    f"to {decision.allowed_concurrency}",
                    flush=True,
                )
            if decision.stop:
                exit_code = 75
                print(
                    f"[registration-worker] adaptive guard tripped: {decision.reason}",
                    flush=True,
                )
                break
            _write_heartbeat()
    finally:
        try:
            adapter.stop_all_active_registrations()
        finally:
            registration_maintainer.stop_background()
            _remove_heartbeat()
    return exit_code


def main() -> None:
    stop = threading.Event()

    def request_stop(_signum, _frame) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    raise SystemExit(run(stop_event=stop))


if __name__ == "__main__":
    main()
