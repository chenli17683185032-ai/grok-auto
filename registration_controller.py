"""Feedback controller for bounded registration concurrency.

The controller is deliberately small: it can only hold the current slot or
promote one step after a stable window. Any missing or unsafe measurement is a
stop signal, never permission to use more resources.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


def _env_bool(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"", "0", "false", "no", "off"}


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        value = float(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


@dataclass(frozen=True)
class ResourceSnapshot:
    at: float
    cpu_idle_pct: float | None
    mem_available_bytes: int | None
    registration_memory_bytes: int | None
    registration_pids: int | None
    registration_oom_kill: int | None
    api_healthy: bool
    api_age_sec: float | None
    api_local_p95_ms: float | None
    api_sample_count: int
    api_error_rate: float


@dataclass(frozen=True)
class ControllerDecision:
    allowed_concurrency: int
    safe: bool
    stop: bool
    reason: str
    stable_samples: int
    promoted: bool = False


class _CpuSampler:
    def __init__(self, proc_stat: Path = Path("/proc/stat")) -> None:
        self._path = proc_stat
        self._previous: tuple[int, int] | None = None

    def sample(self) -> float | None:
        try:
            line = self._path.read_text(encoding="utf-8").splitlines()[0]
            fields = line.split()
            if not fields or fields[0] != "cpu":
                return None
            values = [int(item) for item in fields[1:]]
            if len(values) < 4:
                return None
            idle = values[3] + (values[4] if len(values) > 4 else 0)
            total = sum(values)
        except (OSError, ValueError, IndexError):
            return None
        previous = self._previous
        self._previous = (total, idle)
        if previous is None:
            return None
        total_delta = total - previous[0]
        idle_delta = idle - previous[1]
        if total_delta <= 0:
            return None
        return max(0.0, min(100.0, idle_delta * 100.0 / total_delta))


def _mem_available(path: Path = Path("/proc/meminfo")) -> int | None:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _read_oom_kill(root: Path) -> int | None:
    try:
        for line in (root / "memory.events").read_text(encoding="utf-8").splitlines():
            name, value = line.split(maxsplit=1)
            if name == "oom_kill":
                return int(value)
    except (OSError, ValueError):
        pass
    return None


def read_snapshot(
    *,
    cgroup_root: Path = Path("/sys/fs/cgroup"),
    cpu_sampler: _CpuSampler | None = None,
    api_reader: Callable[[], dict[str, Any]] | None = None,
    now: float | None = None,
) -> ResourceSnapshot:
    """Collect only bounded local reads plus the API guard heartbeat."""
    now = time.time() if now is None else float(now)
    sampler = cpu_sampler or _default_cpu_sampler()
    api = api_reader() if api_reader is not None else _read_api_guard()
    try:
        age = float(api.get("age_sec")) if api.get("age_sec") is not None else None
    except (TypeError, ValueError):
        age = None
    try:
        p95 = float(api.get("local_p95_ms")) if api.get("local_p95_ms") is not None else None
    except (TypeError, ValueError):
        p95 = None
    return ResourceSnapshot(
        at=now,
        cpu_idle_pct=sampler.sample(),
        mem_available_bytes=_mem_available(),
        registration_memory_bytes=_read_int(cgroup_root / "memory.current"),
        registration_pids=_read_int(cgroup_root / "pids.current"),
        registration_oom_kill=_read_oom_kill(cgroup_root),
        api_healthy=bool(api.get("healthy")),
        api_age_sec=age,
        api_local_p95_ms=p95,
        api_sample_count=max(0, int(api.get("sample_count") or 0)),
        api_error_rate=max(0.0, float(api.get("error_rate") or 0.0)),
    )


def _default_cpu_sampler() -> _CpuSampler:
    # A process-local sampler is shared by consecutive read_snapshot calls.
    global _cpu_sampler
    with _state_lock:
        if _cpu_sampler is None:
            _cpu_sampler = _CpuSampler()
        return _cpu_sampler


def _read_api_guard() -> dict[str, Any]:
    try:
        import api_guard

        return api_guard.read_cluster(
            max_age_sec=_env_float("GROK2API_REG_API_MAX_AGE_SEC", 15.0, 2.0)
        )
    except Exception:
        return {"healthy": False, "reason": "api_guard_unavailable"}


_state_lock = threading.Lock()
_cpu_sampler: _CpuSampler | None = None
_current_allowed = 1
_current_reason = "initial"


def current_concurrency() -> int:
    with _state_lock:
        return int(_current_allowed)


def current_state() -> dict[str, Any]:
    with _state_lock:
        return {
            "allowed_concurrency": int(_current_allowed),
            "reason": _current_reason,
        }


def set_current(decision: ControllerDecision) -> None:
    global _current_allowed, _current_reason
    with _state_lock:
        _current_allowed = max(1, int(decision.allowed_concurrency))
        _current_reason = str(decision.reason or "unknown")[:200]


class AdaptiveRegistrationController:
    def __init__(
        self,
        *,
        min_concurrency: int = 1,
        max_concurrency: int = 2,
        min_mem_available_bytes: int = 3 * 1024 * 1024 * 1024,
        min_cpu_idle_pct: float = 25.0,
        max_registration_memory_bytes: int = 1_900_000_000,
        max_registration_pids: int = 300,
        max_api_local_p95_ms: float = 500.0,
        min_api_samples: int = 5,
        max_api_error_rate: float = 0.20,
        promote_after_samples: int = 6,
        cooldown_sec: float = 300.0,
        startup_oom_kill: int = 0,
    ) -> None:
        self.min_concurrency = max(1, int(min_concurrency))
        self.max_concurrency = max(self.min_concurrency, min(2, int(max_concurrency)))
        self.min_mem_available_bytes = max(0, int(min_mem_available_bytes))
        self.min_cpu_idle_pct = max(0.0, float(min_cpu_idle_pct))
        self.max_registration_memory_bytes = max(1, int(max_registration_memory_bytes))
        self.max_registration_pids = max(1, int(max_registration_pids))
        self.max_api_local_p95_ms = max(1.0, float(max_api_local_p95_ms))
        self.min_api_samples = max(0, int(min_api_samples))
        self.max_api_error_rate = max(0.0, min(1.0, float(max_api_error_rate)))
        self.promote_after_samples = max(1, int(promote_after_samples))
        self.cooldown_sec = max(0.0, float(cooldown_sec))
        self.allowed_concurrency = self.min_concurrency
        self.stable_samples = 0
        self.cooldown_until = 0.0
        self.startup_oom_kill = int(startup_oom_kill)
        self._evaluations = 0

    @classmethod
    def from_env(cls, *, startup_oom_kill: int = 0) -> "AdaptiveRegistrationController":
        return cls(
            min_concurrency=_env_int("GROK2API_REG_MIN_CONCURRENCY", 1, 1, 1),
            max_concurrency=_env_int("GROK2API_REG_MAX_CONCURRENCY", 2, 1, 2),
            min_mem_available_bytes=_env_int(
                "GROK2API_REG_MIN_MEM_AVAILABLE_BYTES",
                3 * 1024 * 1024 * 1024,
                256 * 1024 * 1024,
                32 * 1024 * 1024 * 1024,
            ),
            min_cpu_idle_pct=_env_float("GROK2API_REG_MIN_CPU_IDLE_PCT", 25.0, 1.0),
            max_registration_memory_bytes=_env_int(
                "GROK2API_REG_MAX_MEMORY_BYTES",
                1_900_000_000,
                256 * 1024 * 1024,
                8 * 1024 * 1024 * 1024,
            ),
            max_registration_pids=_env_int("GROK2API_REG_MAX_PIDS", 300, 32, 4096),
            max_api_local_p95_ms=_env_float("GROK2API_REG_MAX_API_LOCAL_P95_MS", 500.0, 50.0),
            min_api_samples=_env_int("GROK2API_REG_MIN_API_SAMPLES", 5, 0, 1000),
            max_api_error_rate=_env_float("GROK2API_REG_MAX_API_ERROR_RATE", 0.20, 0.0),
            promote_after_samples=_env_int("GROK2API_REG_PROMOTE_SAMPLES", 6, 1, 100),
            cooldown_sec=_env_float("GROK2API_REG_COOLDOWN_SEC", 300.0, 0.0),
            startup_oom_kill=startup_oom_kill,
        )

    def _unsafe_reason(self, snapshot: ResourceSnapshot) -> str | None:
        if snapshot.cpu_idle_pct is None:
            return "cpu_sample_unready"
        if snapshot.cpu_idle_pct < self.min_cpu_idle_pct:
            return f"host_cpu_idle={snapshot.cpu_idle_pct:.1f}%<{self.min_cpu_idle_pct:.1f}%"
        if snapshot.mem_available_bytes is None:
            return "host_memory_unreadable"
        if snapshot.mem_available_bytes < self.min_mem_available_bytes:
            return f"mem_available={snapshot.mem_available_bytes}<{self.min_mem_available_bytes}"
        if snapshot.registration_memory_bytes is None:
            return "registration_memory_unreadable"
        if snapshot.registration_memory_bytes > self.max_registration_memory_bytes:
            return (
                f"registration_memory={snapshot.registration_memory_bytes}"
                f">{self.max_registration_memory_bytes}"
            )
        if snapshot.registration_pids is None:
            return "registration_pids_unreadable"
        if snapshot.registration_pids > self.max_registration_pids:
            return f"registration_pids={snapshot.registration_pids}>{self.max_registration_pids}"
        if snapshot.registration_oom_kill is None:
            return "registration_oom_unreadable"
        if snapshot.registration_oom_kill > self.startup_oom_kill:
            return f"registration_oom_kill={snapshot.registration_oom_kill}>{self.startup_oom_kill}"
        if not snapshot.api_healthy:
            return "api_guard_unhealthy"
        if snapshot.api_age_sec is None or snapshot.api_age_sec > _env_float(
            "GROK2API_REG_API_MAX_AGE_SEC", 15.0, 2.0
        ):
            return "api_guard_stale"
        if snapshot.api_sample_count >= self.min_api_samples:
            if snapshot.api_local_p95_ms is None:
                return "api_latency_unreadable"
            if snapshot.api_local_p95_ms > self.max_api_local_p95_ms:
                return (
                    f"api_local_p95={snapshot.api_local_p95_ms:.1f}"
                    f">{self.max_api_local_p95_ms:.1f}ms"
                )
            if snapshot.api_error_rate > self.max_api_error_rate:
                return f"api_error_rate={snapshot.api_error_rate:.3f}>{self.max_api_error_rate:.3f}"
        return None

    def evaluate(
        self,
        snapshot: ResourceSnapshot,
        *,
        now: float | None = None,
        promotion_ready: bool = True,
    ) -> ControllerDecision:
        now = time.time() if now is None else float(now)
        self._evaluations += 1
        unsafe = self._unsafe_reason(snapshot)
        if unsafe:
            # /proc/stat needs two samples. Hold the initial slot for that
            # first observation instead of treating the missing delta as a
            # resource failure.
            if unsafe == "cpu_sample_unready" and self._evaluations == 1:
                return ControllerDecision(
                    allowed_concurrency=self.min_concurrency,
                    safe=False,
                    stop=False,
                    reason=unsafe,
                    stable_samples=0,
                )
            self.allowed_concurrency = self.min_concurrency
            self.stable_samples = 0
            self.cooldown_until = now + self.cooldown_sec
            return ControllerDecision(
                allowed_concurrency=self.allowed_concurrency,
                safe=False,
                stop=True,
                reason=unsafe,
                stable_samples=0,
            )
        if now < self.cooldown_until:
            return ControllerDecision(
                allowed_concurrency=self.min_concurrency,
                safe=True,
                stop=False,
                reason=f"cooldown_until={self.cooldown_until:.0f}",
                stable_samples=self.stable_samples,
            )
        if not promotion_ready and self.allowed_concurrency < self.max_concurrency:
            self.stable_samples = 0
            return ControllerDecision(
                allowed_concurrency=self.allowed_concurrency,
                safe=True,
                stop=False,
                reason="waiting_single_slot_success",
                stable_samples=0,
            )
        self.stable_samples += 1
        promoted = False
        if (
            self.allowed_concurrency < self.max_concurrency
            and self.stable_samples >= self.promote_after_samples
        ):
            self.allowed_concurrency += 1
            self.stable_samples = 0
            promoted = True
        return ControllerDecision(
            allowed_concurrency=self.allowed_concurrency,
            safe=True,
            stop=False,
            reason="promoted" if promoted else "stable",
            stable_samples=self.stable_samples,
            promoted=promoted,
        )
