"""Redis hot fields for account pool: cooldown, stats counters, RR index."""

from __future__ import annotations

import hashlib
import time
from typing import Any

from store.redis_client import (
    get_str,
    hgetall,
    hincrby,
    hset_map,
    incr,
    key,
    redis_enabled,
    set_ex,
    delete,
    get_client,
)


def rr_next() -> int | None:
    """Atomically advance global round-robin cursor. None if Redis off."""
    if not redis_enabled():
        return None
    return incr(key("rr", "index"))


def _latency_keys(model: str) -> tuple[str, str]:
    model_hash = hashlib.sha256((model or "default").encode("utf-8")).hexdigest()[:20]
    return key("latency", model_hash), key("latency_count", model_hash)


def record_latency(account_id: str, model: str, latency_ms: float | int) -> float | None:
    """Update per-model upstream TTFT EWMA and return the new score."""
    if not redis_enabled() or not account_id:
        return None
    try:
        sample = max(50.0, min(120_000.0, float(latency_ms)))
    except (TypeError, ValueError):
        return None
    client = get_client()
    if client is None:
        return None
    latency_key, count_key = _latency_keys(model)
    script = """
    local old = redis.call('zscore', KEYS[1], ARGV[1])
    local count = tonumber(redis.call('hget', KEYS[2], ARGV[1]) or '0')
    local sample = tonumber(ARGV[2])
    local score = sample
    if old then
        local alpha = 0.25
        if count < 3 then alpha = 0.50 end
        score = tonumber(old) * (1.0 - alpha) + sample * alpha
    end
    redis.call('zadd', KEYS[1], score, ARGV[1])
    redis.call('hincrby', KEYS[2], ARGV[1], 1)
    return tostring(score)
    """
    try:
        value = client.eval(
            script,
            2,
            latency_key,
            count_key,
            account_id,
            str(sample),
        )
        return float(value) if value is not None else None
    except Exception:
        return None


def fast_account_ids(model: str, *, limit: int = 32) -> list[str]:
    """Return the lowest-EWMA accounts; caller still applies health filters."""
    if not redis_enabled():
        return []
    client = get_client()
    if client is None:
        return []
    latency_key, _ = _latency_keys(model)
    size = max(1, min(256, int(limit or 32)))
    try:
        return [str(item) for item in client.zrange(latency_key, 0, size - 1)]
    except Exception:
        return []


def set_cooldown(account_id: str, until_ts: float) -> None:
    if not redis_enabled() or not account_id:
        return
    now = time.time()
    remaining = max(1, int(float(until_ts) - now))
    set_ex(key("cooldown", account_id), str(float(until_ts)), remaining)


def get_cooldown(account_id: str) -> float | None:
    if not redis_enabled() or not account_id:
        return None
    raw = get_str(key("cooldown", account_id))
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def clear_cooldown(account_id: str) -> None:
    if not redis_enabled() or not account_id:
        return
    delete(key("cooldown", account_id))


def touch_stats(
    account_id: str,
    *,
    success: bool = True,
    error: str = "",
    cooldown_until: float | None = None,
    clear_cooldown_flag: bool = False,
    consecutive_fails: int | None = None,
    last_status_code: int | None = None,
    cooldown_sec: float | None = None,
) -> dict[str, Any] | None:
    """Increment request counters in Redis. Returns snapshot or None if disabled.

    Note: live-success paths should pass clear_cooldown_flag=False and leave
    cooldown_until=None so Redis does not wipe an active durable cooldown.
    """
    if not redis_enabled() or not account_id:
        return None
    k = key("stats", account_id)
    hincrby(k, "request_count", 1)
    if success:
        hincrby(k, "success_count", 1)
    else:
        hincrby(k, "fail_count", 1)
    mapping: dict[str, Any] = {"last_used_at": time.time()}
    # Only overwrite last_error when caller provided one (failure) or explicitly
    # cleared via non-empty handling. Empty error + success leaves existing.
    if error:
        mapping["last_error"] = error[:500]
    if success:
        mapping["consecutive_fails"] = 0
    elif consecutive_fails is not None:
        mapping["consecutive_fails"] = int(consecutive_fails)
    if last_status_code is not None:
        mapping["last_status_code"] = int(last_status_code)
    if cooldown_sec is not None:
        mapping["cooldown_sec"] = float(cooldown_sec)
    hset_map(k, mapping)
    if cooldown_until is not None:
        set_cooldown(account_id, float(cooldown_until))
    if clear_cooldown_flag:
        clear_cooldown(account_id)
        # reset streak fields when clearing
        hset_map(k, {"consecutive_fails": 0, "cooldown_sec": 0})
    return get_stats(account_id)


def get_stats(account_id: str) -> dict[str, Any]:
    if not redis_enabled() or not account_id:
        return {}
    raw = hgetall(key("stats", account_id))
    out: dict[str, Any] = {}
    for field in ("request_count", "success_count", "fail_count", "consecutive_fails", "last_status_code"):
        if field in raw:
            try:
                out[field] = int(float(raw[field]))
            except ValueError:
                out[field] = 0
    if "cooldown_sec" in raw:
        try:
            out["cooldown_sec"] = float(raw["cooldown_sec"])
        except ValueError:
            pass
    if "last_used_at" in raw:
        try:
            out["last_used_at"] = float(raw["last_used_at"])
        except ValueError:
            pass
    if raw.get("last_error"):
        out["last_error"] = raw["last_error"]
    cd = get_cooldown(account_id)
    if cd is not None:
        out["cooldown_until"] = cd
    return out


def merge_pool_meta(account_id: str, base: dict[str, Any]) -> dict[str, Any]:
    """Overlay Redis hot fields onto durable pool meta dict.

    Durable PostgreSQL/file cooldown_until is the source of truth. Redis may
    only extend / fill a missing until — never wipe an active durable cooldown
    just because the Redis TTL key expired early.
    """
    if not redis_enabled() or not account_id:
        return base
    hot = get_stats(account_id)
    durable_cd = base.get("cooldown_until") if isinstance(base, dict) else None
    try:
        durable_active = durable_cd is not None and float(durable_cd) > time.time()
    except (TypeError, ValueError):
        durable_active = False

    if not hot:
        # still check cooldown key alone
        cd = get_cooldown(account_id)
        if cd is not None:
            base = dict(base)
            # Prefer the later of durable / redis so neither side under-reports.
            try:
                if durable_active and float(durable_cd) >= float(cd):
                    base["cooldown_until"] = float(durable_cd)
                else:
                    base["cooldown_until"] = float(cd)
            except (TypeError, ValueError):
                base["cooldown_until"] = cd
        return base

    merged = dict(base)
    for k, v in hot.items():
        if v is None or v == "":
            continue
        if k == "cooldown_until":
            # Never let a missing/short redis cooldown erase durable active CD.
            try:
                hot_cd = float(v)
            except (TypeError, ValueError):
                continue
            if durable_active:
                try:
                    merged["cooldown_until"] = max(float(durable_cd), hot_cd)
                except (TypeError, ValueError):
                    merged["cooldown_until"] = hot_cd
            else:
                merged["cooldown_until"] = hot_cd
            continue
        merged[k] = v

    # If redis has no cooldown key but durable still active, keep durable.
    if durable_active and not merged.get("cooldown_until"):
        merged["cooldown_until"] = float(durable_cd)  # type: ignore[arg-type]
    return merged
