"""Low-overhead API health/latency feedback shared with the registration worker."""

from __future__ import annotations

import json
import math
import os
import threading
import time
from collections import deque
from typing import Any

_SAMPLE_WINDOW_SEC = 120.0
_PUBLISH_SEC = 2.0
_KEY_TTL_SEC = 15
_MAX_SAMPLES = 128

_lock = threading.Lock()
_samples: deque[tuple[float, float | None, bool]] = deque(maxlen=_MAX_SAMPLES)
_stop = threading.Event()
_thread: threading.Thread | None = None


def _finite_ms(value: float | int | None) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return max(0.0, min(120_000.0, number))


def record_timing(
    *,
    local_ms: float | int | None,
    ttft_ms: float | int | None = None,
    ok: bool = True,
) -> None:
    """Record one request without doing network or database work."""
    local = _finite_ms(local_ms)
    if local is None:
        return
    ttft = _finite_ms(ttft_ms)
    now = time.time()
    with _lock:
        _samples.append((now, local, bool(ok)))


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(math.ceil(len(ordered) * fraction)) - 1))
    return round(ordered[index], 1)


def snapshot(*, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else float(now)
    cutoff = now - _SAMPLE_WINDOW_SEC
    with _lock:
        recent = [item for item in _samples if item[0] >= cutoff]
    local = [item[1] for item in recent if item[1] is not None]
    failures = sum(1 for _, _, ok in recent if not ok)
    return {
        "at": now,
        "healthy": True,
        "sample_count": len(local),
        "local_p95_ms": _percentile(local, 0.95),
        "error_rate": round(failures / len(recent), 4) if recent else 0.0,
        "worker_id": _worker_id(),
    }


def _worker_id() -> str:
    try:
        from store.redis_client import worker_id

        return worker_id()
    except Exception:
        return f"{os.getpid()}@local"


def _redis_key() -> str:
    try:
        from store.redis_client import key

        return key("api_guard", _worker_id())
    except Exception:
        return f"g2a:api_guard:{_worker_id()}"


def publish_once() -> bool:
    try:
        from store.redis_client import redis_enabled, set_ex

        if not redis_enabled():
            return False
        payload = json.dumps(snapshot(), separators=(",", ":"), default=str)
        return bool(set_ex(_redis_key(), payload, _KEY_TTL_SEC))
    except Exception:
        return False


def _run() -> None:
    while not _stop.is_set():
        publish_once()
        _stop.wait(_PUBLISH_SEC)


def start_background() -> None:
    """Start one tiny publisher per API worker; safe to call repeatedly."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    publish_once()
    _thread = threading.Thread(target=_run, name="g2a-api-guard", daemon=True)
    _thread.start()


def stop_background() -> None:
    global _thread
    _stop.set()
    thread = _thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=1.0)
    _thread = None


def read_cluster(*, max_age_sec: float = 15.0) -> dict[str, Any]:
    """Read fresh worker heartbeats and aggregate conservative guard values."""
    try:
        from store.redis_client import get_client, key

        client = get_client()
        if client is None:
            return {"healthy": False, "reason": "redis_unavailable"}
        prefix = key("api_guard", "") + ":*"
        rows: list[dict[str, Any]] = []
        now = time.time()
        for redis_key in client.scan_iter(match=prefix, count=32):
            raw = client.get(redis_key)
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(row, dict):
                continue
            try:
                age = max(0.0, now - float(row.get("at") or 0.0))
            except (TypeError, ValueError):
                continue
            if age <= max(0.0, float(max_age_sec)):
                row["age_sec"] = round(age, 2)
                rows.append(row)
        if not rows:
            return {"healthy": False, "reason": "heartbeat_stale", "workers": 0}
        samples = sum(max(0, int(row.get("sample_count") or 0)) for row in rows)
        weighted_failures = sum(
            float(row.get("error_rate") or 0.0) * max(0, int(row.get("sample_count") or 0))
            for row in rows
        )
        p95_values = [
            float(row["local_p95_ms"])
            for row in rows
            if row.get("local_p95_ms") is not None
        ]
        return {
            "healthy": all(bool(row.get("healthy", True)) for row in rows),
            "workers": len(rows),
            "age_sec": max(float(row.get("age_sec") or 0.0) for row in rows),
            "sample_count": samples,
            "local_p95_ms": max(p95_values) if p95_values else None,
            "error_rate": round(weighted_failures / samples, 4) if samples else 0.0,
        }
    except Exception as exc:  # pragma: no cover - defensive Redis boundary
        return {"healthy": False, "reason": f"guard_read_failed:{type(exc).__name__}"}
