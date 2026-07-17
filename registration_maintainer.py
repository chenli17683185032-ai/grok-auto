"""Continuously refill the usable registration pool to a configured target."""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

_stop = threading.Event()
_thread: threading.Thread | None = None
_lock = threading.Lock()
_state: dict[str, Any] = {}

_TERMINAL = {"done", "partial", "error", "cancelled", "stopped", "completed"}
_STATE_TTL_SEC = 30 * 86400


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"", "0", "false", "no", "off"}


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def is_enabled() -> bool:
    return _env_bool("GROK2API_REG_AUTO_MAINTAIN", False)


def _target() -> int:
    return _env_int("GROK2API_REG_TARGET_AVAILABLE", 5000, 1, 100_000)


def _batch_size() -> int:
    return _env_int("GROK2API_REG_AUTO_BATCH_SIZE", 500, 1, 5000)


def _concurrency() -> int:
    return _env_int("GROK2API_REG_CONCURRENCY", 1, 1, 10)


def _rest_sec() -> int:
    return _env_int("GROK2API_REG_AUTO_REST_SEC", 3600, 60, 86400)


def _monitor_sec() -> int:
    return _env_int("GROK2API_REG_AUTO_MONITOR_SEC", 3600, 30, 86400)


def _startup_delay_sec() -> int:
    return _env_int("GROK2API_REG_AUTO_STARTUP_DELAY_SEC", 10, 0, 600)


def _state_key() -> str:
    try:
        from store.redis_client import key

        return key("registration_maintainer", "state")
    except Exception:
        return "g2a:registration_maintainer:state"


def _load_remote_state() -> dict[str, Any]:
    try:
        from store.redis_client import get_str, redis_enabled

        if redis_enabled():
            raw = get_str(_state_key())
            if raw:
                data = json.loads(raw)
                if isinstance(data, dict):
                    return data
    except Exception:
        pass
    return {}


def _publish(**patch: Any) -> dict[str, Any]:
    now = time.time()
    with _lock:
        _state.update(patch)
        _state["updated_at"] = now
        snapshot = dict(_state)
    try:
        from store.redis_client import redis_enabled, set_ex

        if redis_enabled():
            set_ex(
                _state_key(),
                json.dumps(snapshot, ensure_ascii=False, default=str),
                _STATE_TTL_SEC,
            )
    except Exception:
        pass
    return snapshot


def _available_from_accounts(rows: list[dict[str, Any]]) -> int:
    available = 0
    for row in rows:
        if row.get("expired") or not row.get("enabled", True):
            continue
        if row.get("in_cooldown") or row.get("disabled_for_quota"):
            continue
        if row.get("blocked_model_ids") or row.get("blocked_models"):
            continue
        available += 1
    return available


def _pool_counts() -> dict[str, int]:
    import account_pool

    pool = account_pool.pool_summary(include_accounts=True)
    rows = list(pool.get("accounts") or [])
    return {
        "total": int(pool.get("total") or len(rows)),
        "live": int(pool.get("live") or 0),
        "enabled": int(pool.get("enabled") or 0),
        "available": _available_from_accounts(rows),
    }


def _batch_view(batch: dict[str, Any]) -> dict[str, Any]:
    def _int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    return {
        "batch_id": str(batch.get("id") or batch.get("batch_id") or ""),
        "batch_status": str(
            batch.get("batch_status") or batch.get("status") or "running"
        ).lower(),
        "batch_count": _int(batch.get("count") or batch.get("total")),
        "batch_finished": _int(batch.get("finished") or batch.get("done")),
        "batch_ok": _int(batch.get("ok_count") or batch.get("imported")),
        "batch_fail": _int(batch.get("fail_count") or batch.get("error")),
        "batch_inflight": _int(batch.get("inflight") or batch.get("running")),
    }


def _find_active_batch() -> dict[str, Any] | None:
    import grok_build_adapter as adapter

    listed = adapter.list_registration_sessions()
    batches = list(listed.get("batches") or [])
    batches.sort(
        key=lambda b: float(b.get("updated_at") or b.get("created_at") or 0),
        reverse=True,
    )
    for batch in batches:
        view = _batch_view(batch)
        if view["batch_status"] in _TERMINAL:
            continue
        batch_id = view["batch_id"]
        if not batch_id:
            continue
        runner_live = False
        try:
            from store.redis_client import get_str

            runner_live = bool(get_str(adapter._batch_runner_lock_key(batch_id)))
        except Exception:
            runner_live = bool(batch.get("runner_alive"))
        if runner_live:
            return batch

        # A killed worker can leave a six-hour "running" batch snapshot. Once
        # its 90-second runner lock is gone, close it so refill can continue.
        try:
            adapter.stop_registration_batch(batch_id)
            print(
                f"  [registration-maintainer] closed stale batch {batch_id[-8:]}",
                flush=True,
            )
        except Exception:
            pass
    return None


def _start_batch(attempts: int) -> dict[str, Any]:
    import grok_build_adapter as adapter
    from settings_store import resolve_registration_inputs

    resolved = resolve_registration_inputs(
        {
            "count": attempts,
            "concurrency": _concurrency(),
            "stagger_ms": 0,
            "probe_delay_sec": 0,
        }
    )
    return adapter.start_registration(
        proxy=resolved.get("proxy") or None,
        moemail_api_key=resolved.get("api_key") or None,
        moemail_base_url=resolved.get("base_url") or None,
        prefix=resolved.get("prefix") or None,
        domain=resolved.get("domain") or None,
        expiry_ms=resolved.get("expiry_ms"),
        mail_provider=resolved.get("mail_provider") or None,
        captcha_provider=resolved.get("captcha_provider") or None,
        local_solver_url=resolved.get("local_solver_url") or None,
        yescaptcha_key=resolved.get("yescaptcha_key") or None,
        count=attempts,
        concurrency=_concurrency(),
        stagger_ms=0,
        probe_delay_sec=0,
    )


def _wait(seconds: float) -> bool:
    return _stop.wait(max(0.1, seconds))


def _worker() -> None:
    remote = _load_remote_state()
    if remote:
        with _lock:
            _state.update(remote)
    _publish(
        enabled=True,
        target=_target(),
        batch_size=_batch_size(),
        concurrency=_concurrency(),
        rest_sec=_rest_sec(),
        monitor_sec=_monitor_sec(),
        phase="starting",
        last_error=None,
    )
    if _wait(_startup_delay_sec()):
        return

    while not _stop.is_set():
        try:
            active = _find_active_batch()
            if active is not None:
                view = _batch_view(active)
                _publish(phase="running", current_batch_id=view["batch_id"], **view)
                if _wait(5.0):
                    break
                continue

            with _lock:
                current_id = str(_state.get("current_batch_id") or "")
                rest_until = float(_state.get("rest_until") or 0)

            if current_id:
                import grok_build_adapter as adapter

                finished = adapter.get_registration_batch(current_id)
                view = _batch_view(finished or {"id": current_id, "status": "stopped"})
                rest_until = time.time() + _rest_sec()
                _publish(
                    phase="resting",
                    current_batch_id=None,
                    last_batch=view,
                    rest_until=rest_until,
                    **view,
                )
                print(
                    "  [registration-maintainer] batch finished "
                    f"ok={view['batch_ok']} fail={view['batch_fail']}; "
                    f"resting {_rest_sec()}s",
                    flush=True,
                )

            counts = _pool_counts()
            target = _target()
            deficit = max(0, target - counts["available"])
            base = {
                **counts,
                "target": target,
                "deficit": deficit,
                "last_error": None,
            }
            if deficit <= 0:
                _publish(phase="target_reached", **base)
                if _wait(_monitor_sec()):
                    break
                continue

            now = time.time()
            if rest_until > now:
                _publish(phase="resting", rest_until=rest_until, **base)
                if _wait(min(30.0, rest_until - now)):
                    break
                continue

            attempts = min(_batch_size(), deficit)
            result = _start_batch(attempts)
            if not result.get("ok"):
                error = str(result.get("error") or "registration batch start failed")[:300]
                retry_at = time.time() + 60
                _publish(
                    phase="start_error",
                    last_error=error,
                    retry_at=retry_at,
                    rest_until=retry_at,
                    **base,
                )
                print(f"  [registration-maintainer] start failed: {error}", flush=True)
                if _wait(60.0):
                    break
                continue

            batch_id = str(result.get("batch_id") or "")
            _publish(
                phase="running",
                current_batch_id=batch_id,
                batch_id=batch_id,
                batch_count=attempts,
                batch_status="running",
                rest_until=0,
                **base,
            )
            print(
                "  [registration-maintainer] started batch "
                f"{batch_id[-8:]} attempts={attempts} "
                f"available={counts['available']}/{target} concurrency={_concurrency()}",
                flush=True,
            )
            if _wait(2.0):
                break
        except Exception as e:  # noqa: BLE001
            error = str(e)[:300]
            _publish(phase="error", last_error=error, retry_at=time.time() + 60)
            print(f"  [registration-maintainer] cycle error: {error}", flush=True)
            if _wait(60.0):
                break


def start_background() -> None:
    global _thread
    if not is_enabled():
        return
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(
        target=_worker,
        name="g2a-registration-maintainer",
        daemon=True,
    )
    _thread.start()


def stop_background() -> None:
    global _thread
    _stop.set()
    thread = _thread
    if thread and thread.is_alive():
        thread.join(timeout=2.0)
    _thread = None


def status(*, light: bool = False) -> dict[str, Any]:
    local_running = bool(_thread and _thread.is_alive())
    snapshot: dict[str, Any]
    with _lock:
        snapshot = dict(_state)
    if not snapshot:
        snapshot = _load_remote_state()

    cluster_running = local_running
    leader_id = None
    try:
        from store.leader import status as leader_status
        from store.redis_client import get_str, key, redis_enabled

        leader = leader_status()
        leader_id = leader.get("leader_id")
        if not local_running and is_enabled() and redis_enabled():
            cluster_running = bool(get_str(key("lock", "maintainer_leader")))
    except Exception:
        pass

    out = {
        "enabled": is_enabled(),
        "running": bool(cluster_running),
        "local_running": local_running,
        "leader_id": leader_id,
        "target": _target(),
        "batch_size": _batch_size(),
        "concurrency": _concurrency(),
        "rest_sec": _rest_sec(),
        "monitor_sec": _monitor_sec(),
        **snapshot,
    }
    if light:
        if not out.get("last_error"):
            out.pop("last_error", None)
    return out
