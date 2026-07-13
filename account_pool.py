"""Multi-account pool: rotation, enable/disable, cooldown, failover stats.

All accounts are equal — there is no primary/preferred account.
"""

from __future__ import annotations

import os
import random
import threading
import time
import uuid
from typing import Any

from auth import AuthError, GrokCredentials, list_live_credentials, load_credentials_by_id
from settings_store import (
    get_account_mode,
    get_account_pool_state,
    save_account_pool_state,
    touch_account_stats,
)

# Modes (all accounts treated equally):
#   round_robin  — cycle all enabled live accounts
#   random       — pick randomly among enabled live accounts
#   least_used   — prefer account with fewest requests
VALID_MODES = ("round_robin", "random", "least_used")

# Default cooldown after 401 / 429 / 5xx (seconds)
DEFAULT_COOLDOWN = 60
AUTH_COOLDOWN = 300  # longer for hard auth failures

# A reset is only terminal after repeated explicit exhaustion evidence over
# multiple maintenance ticks and a real observation window.
QUOTA_RESET_GRACE_SECONDS = max(
    0.0, float(os.getenv("GROK2API_QUOTA_RESET_GRACE_SECONDS", "3600"))
)
QUOTA_CONFIRMATIONS_REQUIRED = max(
    3, int(os.getenv("GROK2API_QUOTA_CONFIRMATIONS_REQUIRED", "3"))
)
QUOTA_MAINTENANCE_CYCLES_REQUIRED = max(
    2, int(os.getenv("GROK2API_QUOTA_MAINTENANCE_CYCLES_REQUIRED", "2"))
)
QUOTA_POST_RESET_PROBE_SECONDS = max(
    60.0, float(os.getenv("GROK2API_QUOTA_POST_RESET_PROBE_SECONDS", "1800"))
)
QUOTA_INCONCLUSIVE_RETRY_SECONDS = max(
    60.0, float(os.getenv("GROK2API_QUOTA_INCONCLUSIVE_RETRY_SECONDS", "900"))
)

_lock = threading.RLock()
_rr_index = 0


def _now() -> float:
    return time.time()


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _pool_meta(account_id: str, state: dict[str, Any]) -> dict[str, Any]:
    meta = state.get(account_id) or {}
    blocked = meta.get("blocked_models") or {}
    if not isinstance(blocked, dict):
        blocked = {}
    return {
        "enabled": bool(meta.get("enabled", True)),
        "weight": max(1, _safe_int(meta.get("weight"), 1)),
        "request_count": _safe_int(meta.get("request_count")),
        "success_count": _safe_int(meta.get("success_count")),
        "fail_count": _safe_int(meta.get("fail_count")),
        "last_used_at": meta.get("last_used_at"),
        "last_error": meta.get("last_error"),
        "cooldown_until": meta.get("cooldown_until"),
        "disabled_for_quota": bool(meta.get("disabled_for_quota")),
        "quota_waiting": bool(meta.get("quota_waiting") or meta.get("disabled_for_quota")),
        "quota_status": meta.get("quota_status")
        or ("quota_waiting" if meta.get("quota_waiting") or meta.get("disabled_for_quota") else "active"),
        "quota_reset_at": meta.get("quota_reset_at"),
        "quota_wait_reason": meta.get("quota_wait_reason") or meta.get("disabled_reason"),
        "disabled_reason": meta.get("disabled_reason"),
        "manual_disabled": bool(meta.get("manual_disabled")),
        "credential_suspended": bool(meta.get("credential_suspended")),
        "suspended_at": meta.get("suspended_at"),
        "suspend_source": meta.get("suspend_source"),
        "quota_disabled_at": meta.get("quota_disabled_at"),
        "quota_source": meta.get("quota_source"),
        "quota_cycle_id": meta.get("quota_cycle_id"),
        "quota_waiting_since": meta.get("quota_waiting_since"),
        "quota_next_probe_at": meta.get("quota_next_probe_at"),
        "quota_limit_tokens": meta.get("quota_limit_tokens"),
        "quota_remaining_tokens": meta.get("quota_remaining_tokens"),
        "quota_grace_count": _safe_int(meta.get("quota_grace_count")),
        "quota_confirmation_count": _safe_int(meta.get("quota_confirmation_count")),
        "quota_first_confirm_at": meta.get("quota_first_confirm_at"),
        "quota_last_confirm_at": meta.get("quota_last_confirm_at"),
        "quota_last_evidence": meta.get("quota_last_evidence"),
        "quota_confirmation_cycle_id": meta.get("quota_confirmation_cycle_id"),
        "quota_terminal_at": meta.get("quota_terminal_at"),
        "last_quota": meta.get("last_quota"),
        "last_probe": meta.get("last_probe"),
        "blocked_models": blocked,
        "blocked_model_ids": list(blocked.keys()),
    }


def is_model_blocked(account_id: str, model: str | None, state: dict[str, Any] | None = None) -> bool:
    """True if this account must not be scheduled for `model`."""
    if not account_id or not model:
        return False
    if state is None:
        state = get_account_pool_state()
    meta = _pool_meta(account_id, state)
    blocked = meta.get("blocked_models") or {}
    return model in blocked


def is_in_cooldown(meta: dict[str, Any]) -> bool:
    until = meta.get("cooldown_until")
    if until is None:
        return False
    try:
        return _now() < float(until)
    except (TypeError, ValueError):
        return False


def is_quota_waiting(meta: dict[str, Any], *, now: float | None = None) -> bool:
    """True while account is in rolling free-usage wait (not permanently dead)."""
    if not meta.get("quota_waiting") and not meta.get("disabled_for_quota"):
        return False
    # Legacy disabled_for_quota without waiting flag still treated as waiting if reset known
    if meta.get("quota_waiting") is False and meta.get("disabled_for_quota"):
        # old permanent path — still exclude from rotation but do not delete until grace
        return True
    return bool(meta.get("quota_waiting") or meta.get("disabled_for_quota"))


def quota_probe_due(meta: dict[str, Any], *, now: float | None = None) -> bool:
    current = time.time() if now is None else now
    if not is_quota_waiting(meta, now=current):
        return False
    reset_at = meta.get("quota_reset_at")
    try:
        reset_at_f = float(reset_at) if reset_at is not None else 0.0
    except (TypeError, ValueError):
        reset_at_f = 0.0
    next_probe = meta.get("quota_next_probe_at")
    try:
        next_probe_f = float(next_probe) if next_probe is not None else 0.0
    except (TypeError, ValueError):
        next_probe_f = 0.0
    if next_probe_f and current < next_probe_f:
        return False
    # Due if past reset or no reset known but wait started long enough ago
    if reset_at_f and current >= reset_at_f:
        return True
    if not reset_at_f:
        started = float(meta.get("quota_waiting_since") or meta.get("quota_disabled_at") or 0)
        # probe every 30m if no reset_at
        return current - started >= 1800.0
    return False


def list_pool_accounts() -> list[dict[str, Any]]:
    """Live credentials merged with pool metadata (for admin UI).

    Read-only status routes must not synchronously refresh OIDC tokens: a
    stalled upstream refresh otherwise blocks this single Uvicorn worker and
    makes every endpoint appear offline.
    """
    state = get_account_pool_state()
    out: list[dict[str, Any]] = []
    for creds in list_live_credentials(include_expired=True, auto_refresh=False):
        meta = _pool_meta(creds.auth_key or "", state)
        out.append(
            {
                "id": creds.auth_key,
                "email": creds.email,
                "user_id": creds.user_id,
                "team_id": creds.team_id,
                "expires_at": creds.expires_at,
                "expired": creds.expired,
                "has_refresh_token": bool(creds.refresh_token),
                "refresh_status": getattr(creds, "refresh_status", None) or "active",
                "refresh_failure_count": _safe_int(
                    getattr(creds, "refresh_failure_count", 0)
                ),
                "refresh_first_failed_at": getattr(
                    creds, "refresh_first_failed_at", None
                ),
                "refresh_last_failed_at": getattr(
                    creds, "refresh_last_failed_at", None
                ),
                "refresh_next_retry_at": getattr(
                    creds, "refresh_next_retry_at", None
                ),
                "refresh_terminal_at": getattr(creds, "refresh_terminal_at", None),
                "refresh_confirmed_after_expiry": bool(
                    getattr(creds, "refresh_confirmed_after_expiry", False)
                ),
                "token_hint": _mask(creds.token),
                **meta,
                "in_cooldown": is_in_cooldown(meta),
            }
        )
    return out


def _mask(token: str | None) -> str:
    if not token:
        return ""
    if len(token) <= 12:
        return "****"
    return token[:6] + "..." + token[-4:]


def _eligible(
    creds: GrokCredentials,
    state: dict[str, Any],
    *,
    model: str | None = None,
) -> bool:
    if creds.expired:
        return False
    aid = creds.auth_key or ""
    meta = _pool_meta(aid, state)
    if not meta["enabled"]:
        return False
    if is_in_cooldown(meta):
        return False
    if is_quota_waiting(meta):
        return False
    if model and is_model_blocked(aid, model, state):
        return False
    return True


def _pick_round_robin(eligible: list[GrokCredentials]) -> GrokCredentials:
    global _rr_index
    with _lock:
        if not eligible:
            raise AuthError("No eligible accounts for round-robin")
        idx = _rr_index % len(eligible)
        _rr_index = (idx + 1) % len(eligible)
        return eligible[idx]


def _pick_random(eligible: list[GrokCredentials], state: dict[str, Any]) -> GrokCredentials:
    weights = []
    for c in eligible:
        meta = _pool_meta(c.auth_key or "", state)
        weights.append(meta["weight"])
    return random.choices(eligible, weights=weights, k=1)[0]


def _pick_least_used(eligible: list[GrokCredentials], state: dict[str, Any]) -> GrokCredentials:
    def score(c: GrokCredentials) -> tuple[int, float]:
        meta = _pool_meta(c.auth_key or "", state)
        return (meta["request_count"], float(meta["last_used_at"] or 0))

    return min(eligible, key=score)


_last_normalize_at = 0.0
_NORMALIZE_MIN_INTERVAL = 30.0  # avoid re-scanning auth.json every request


def _ensure_multi_account_layout() -> None:
    """Re-key CLI client_id single-slot into per-user keys (throttled)."""
    global _last_normalize_at
    now = time.time()
    if now - _last_normalize_at < _NORMALIZE_MIN_INTERVAL:
        return
    try:
        from oidc_auth import normalize_auth_file_keys

        normalize_auth_file_keys()
        _last_normalize_at = now
    except Exception:
        pass


def acquire(
    exclude: set[str] | None = None,
    *,
    model: str | None = None,
    auto_refresh: bool = True,
) -> GrokCredentials:
    """
    Select next account according to configured mode.
    `exclude` skips already-tried accounts in a failover pass.
    `model` skips accounts that blocked this model as unavailable.
    Auto-refreshes near-expiry tokens via refresh_token when available.
    """
    exclude = exclude or set()
    mode = get_account_mode()
    if mode not in VALID_MODES:
        mode = "round_robin"

    _ensure_multi_account_layout()

    # Read-only callers disable refresh so a stalled OIDC exchange cannot
    # monopolize Uvicorn's event-loop thread.
    all_live = list_live_credentials(include_expired=False, auto_refresh=auto_refresh)
    if not all_live:
        raise AuthError(
            "No live accounts in auth store. "
            "Use device-code login, import token/auth.json, "
            "or add more accounts to the pool."
        )

    state = get_account_pool_state()
    candidates = [c for c in all_live if (c.auth_key or "") not in exclude]

    eligible = [c for c in candidates if _eligible(c, state, model=model)]
    # If everything is cooling down, relax cooldown and still try
    # (but still respect model blocks + enabled)
    if not eligible:
        # Relax cooldown only — NEVER schedule quota_waiting / permanently blocked.
        eligible = [
            c
            for c in candidates
            if not c.expired
            and _pool_meta(c.auth_key or "", state)["enabled"]
            and not is_quota_waiting(_pool_meta(c.auth_key or "", state))
            and not (model and is_model_blocked(c.auth_key or "", model, state))
        ]
    if not eligible:
        msg = "No eligible accounts (all disabled, expired, excluded"
        if model:
            msg += f", or blocked for model `{model}`"
        msg += "). Enable accounts, clear model blocks, or re-login."
        raise AuthError(msg)

    if mode == "round_robin":
        return _pick_round_robin(eligible)
    if mode == "random":
        return _pick_random(eligible, state)
    if mode == "least_used":
        return _pick_least_used(eligible, state)
    return eligible[0]


def report_success(account_id: str | None) -> None:
    if not account_id:
        return
    touch_account_stats(
        account_id,
        success=True,
        clear_cooldown=True,
    )


def report_failure(
    account_id: str | None,
    *,
    error: str = "",
    status_code: int | None = None,
    cooldown: float | None = None,
    model: str | None = None,
    headers: Any = None,
) -> None:
    if not account_id:
        return
    if cooldown is None:
        if status_code == 401:
            cooldown = AUTH_COOLDOWN
        elif status_code in (429, 503, 502):
            cooldown = DEFAULT_COOLDOWN
        else:
            cooldown = DEFAULT_COOLDOWN / 2
    until = _now() + float(cooldown)
    touch_account_stats(
        account_id,
        success=False,
        error=(error or "")[:300],
        cooldown_until=until,
    )
    # Hard quota/credit errors → remove from rotation immediately
    quota_handled = False
    try:
        from quota import handle_upstream_error_for_quota

        quota_handled = handle_upstream_error_for_quota(
            account_id, error=error, status_code=status_code, headers=headers
        ) is not None
    except Exception:
        pass
    # Quota is account-wide but temporary.  Never let the same error create a
    # model block or credential suspension after quota classified it.
    if quota_handled:
        return
    # Model unavailable → stop scheduling this account for that model ONLY if
    # the error clearly names this model. Do not let errors from other models
    # (e.g. a model-not-found for model A) block the account for model B.
    try:
        from model_health import (
            handle_upstream_error_for_model,
            is_account_block_error,
            is_model_unavailable_error,
        )

        if is_account_block_error(error, status_code):
            handle_upstream_error_for_model(
                account_id, model=model, error=error, status_code=status_code
            )
        elif model and is_model_unavailable_error(error, status_code):
            # extra guard: ensure the error text references this model id
            err_lower = (error or "").lower()
            if model.lower() in err_lower or f"model `{model}`" in err_lower:
                handle_upstream_error_for_model(
                    account_id, model=model, error=error, status_code=status_code
                )
    except Exception:
        pass


def set_account_enabled(account_id: str, enabled: bool) -> dict[str, Any] | None:
    state = get_account_pool_state()
    # ensure key exists even if new
    meta = state.get(account_id) or {}
    meta["enabled"] = bool(enabled)
    if enabled:
        meta["manual_disabled"] = False
        # A manual enable is an explicit operator decision to clear a prior
        # credential suspension atomically.  Quota/model state has separate
        # ownership and is intentionally left untouched.
        meta["credential_suspended"] = False
        meta.pop("disabled_reason", None)
        meta.pop("suspended_at", None)
        meta.pop("suspend_source", None)
        if meta.get("quota_waiting") or meta.get("disabled_for_quota"):
            meta["quota_status"] = meta.get("quota_status") or "quota_waiting"
        else:
            meta["quota_status"] = "active"
    else:
        meta["manual_disabled"] = True
        meta["quota_status"] = (
            meta.get("quota_status")
            if meta.get("quota_waiting") or meta.get("disabled_for_quota")
            else "manual_disabled"
        )
    state[account_id] = meta
    save_account_pool_state(state)
    for a in list_pool_accounts():
        if a["id"] == account_id:
            return a
    return {"id": account_id, "enabled": enabled}


def block_model(
    account_id: str,
    model: str,
    *,
    reason: str = "模型不可用",
    source: str = "probe",
) -> dict[str, Any] | None:
    """Stop scheduling this account for a specific model."""
    if not account_id or not model:
        return None
    state = get_account_pool_state()
    meta = state.get(account_id) or {}
    if not isinstance(meta, dict):
        meta = {}
    blocked = meta.get("blocked_models")
    if not isinstance(blocked, dict):
        blocked = {}
    already = model in blocked
    blocked[model] = {
        "reason": (reason or "模型不可用")[:300],
        "blocked_at": _now(),
        "source": source,
    }
    meta["blocked_models"] = blocked
    meta["last_error"] = f"[{model}] {blocked[model]['reason']}"
    state[account_id] = meta
    save_account_pool_state(state)
    if not already:
        print(
            f"  [model] blocked {model} for account "
            f"{account_id}: {blocked[model]['reason']}"
        )
    for a in list_pool_accounts():
        if a["id"] == account_id:
            return a
    return {
        "id": account_id,
        "blocked_models": blocked,
        "model": model,
        "reason": blocked[model]["reason"],
    }


def unblock_model(account_id: str, model: str | None = None) -> dict[str, Any] | None:
    """Clear one model block, or all model blocks if model is None."""
    if not account_id:
        return None
    state = get_account_pool_state()
    meta = state.get(account_id) or {}
    if not isinstance(meta, dict):
        return None
    blocked = meta.get("blocked_models")
    if not isinstance(blocked, dict):
        blocked = {}
    if model is None:
        meta.pop("blocked_models", None)
    elif model in blocked:
        blocked = dict(blocked)
        blocked.pop(model, None)
        if blocked:
            meta["blocked_models"] = blocked
        else:
            meta.pop("blocked_models", None)
    state[account_id] = meta
    save_account_pool_state(state)
    for a in list_pool_accounts():
        if a["id"] == account_id:
            return a
    return {"id": account_id, "blocked_models": meta.get("blocked_models") or {}}


def mark_quota_waiting(
    account_id: str,
    *,
    reason: str = "额度已耗尽（滚动24h等待）",
    source: str = "upstream",
    reset_at: float | None = None,
    limit_tokens: int | float | None = None,
    remaining_tokens: int | float | None = None,
) -> dict[str, Any] | None:
    """Put account into quota_waiting — NOT permanently dead.

    Account stays out of rotation until reset_at / re-probe confirms recovery.
    Must never be treated as delete-eligible solely for this state.
    """
    state = get_account_pool_state()
    meta = state.get(account_id) or {}
    if not isinstance(meta, dict):
        meta = {}
    now = _now()
    already_waiting = bool(meta.get("quota_waiting") or meta.get("disabled_for_quota"))
    meta["quota_waiting"] = True
    # Do not flip manual disabled -> enabled. Rotation uses is_quota_waiting.
    if meta.get("enabled") is not False:
        meta["enabled"] = True
    meta["disabled_for_quota"] = False  # waiting is not permanent quota death
    meta["quota_wait_reason"] = (reason or "额度已耗尽")[:300]
    # Same-cycle exhaustion must NOT extend reset_at with a fresh now+24h.
    if not already_waiting:
        meta["quota_waiting_since"] = now
        meta["quota_cycle_id"] = f"qc_{int(now)}_{uuid.uuid4().hex[:8]}"
        meta["quota_grace_count"] = 0
        meta["quota_confirmation_count"] = 0
        meta.pop("quota_first_confirm_at", None)
        meta.pop("quota_last_confirm_at", None)
        meta.pop("quota_last_evidence", None)
        meta.pop("quota_confirmation_cycle_id", None)
        meta.pop("quota_terminal_at", None)
    meta["quota_source"] = source
    meta["last_error"] = meta["quota_wait_reason"]
    if reset_at is not None:
        try:
            new_reset = float(reset_at)
        except (TypeError, ValueError):
            new_reset = None
        # Authoritative header may set/shorten reset; never push later via fallback alone
        if new_reset is not None:
            old_reset = _safe_float(meta.get("quota_reset_at")) or None
            if not already_waiting or old_reset is None or new_reset < old_reset:
                meta["quota_reset_at"] = new_reset
    elif not already_waiting:
        meta["quota_reset_at"] = now + 24 * 3600
    # else: keep existing quota_reset_at
    if limit_tokens is not None or not already_waiting:
        meta["quota_limit_tokens"] = (
            limit_tokens if limit_tokens is not None else 1_000_000
        )
    if remaining_tokens is not None or not already_waiting:
        meta["quota_remaining_tokens"] = (
            remaining_tokens if remaining_tokens is not None else 0
        )
    # Next probe at reset_at (or +30m)
    meta["quota_next_probe_at"] = _safe_float(
        meta.get("quota_reset_at"), now + 1800
    )
    meta["quota_status"] = "quota_waiting"
    state[account_id] = meta
    save_account_pool_state(state)
    if not already_waiting:
        print(
            f"  [quota] account waiting for reset: "
            f"{account_id} — {meta['quota_wait_reason']} "
            f"reset_at={meta.get('quota_reset_at')}"
        )
    for a in list_pool_accounts():
        if a["id"] == account_id:
            return a
    return {
        "id": account_id,
        "enabled": bool(meta.get("enabled", True)),
        "quota_waiting": True,
        "disabled_for_quota": False,
        "quota_status": "quota_waiting",
        "quota_wait_reason": meta["quota_wait_reason"],
        "quota_reset_at": meta.get("quota_reset_at"),
    }


def mark_credential_suspended(
    account_id: str,
    *,
    reason: str = "账号不可用",
    source: str = "model_health",
) -> dict[str, Any] | None:
    """Permanent-ish credential/account block — NOT quota waiting."""
    state = get_account_pool_state()
    meta = state.get(account_id) or {}
    if not isinstance(meta, dict):
        meta = {}
    meta["enabled"] = False
    meta["manual_disabled"] = False
    meta["credential_suspended"] = True
    meta["quota_waiting"] = False
    meta["disabled_for_quota"] = False
    meta["disabled_reason"] = (reason or "账号不可用")[:300]
    meta["suspended_at"] = _now()
    meta["suspend_source"] = source
    meta["last_error"] = meta["disabled_reason"]
    meta["quota_status"] = "credential_suspended"
    state[account_id] = meta
    save_account_pool_state(state)
    print(f"  [pool] credential suspended: {account_id} — {meta['disabled_reason']}")
    for a in list_pool_accounts():
        if a["id"] == account_id:
            return a
    return {"id": account_id, "enabled": False, "credential_suspended": True}


def disable_for_quota(
    account_id: str,
    *,
    reason: str = "额度已耗尽",
    source: str = "billing",
    reset_at: float | None = None,
    limit_tokens: int | float | None = None,
    remaining_tokens: int | float | None = None,
) -> dict[str, Any] | None:
    """Backward-compatible entry: maps to mark_quota_waiting (never permanent)."""
    return mark_quota_waiting(
        account_id,
        reason=reason,
        source=source,
        reset_at=reset_at,
        limit_tokens=limit_tokens,
        remaining_tokens=remaining_tokens,
    )


def clear_quota_waiting(
    account_id: str,
    *,
    source: str = "quota_probe",
) -> dict[str, Any] | None:
    """Re-enter active pool after successful quota probe."""
    state = get_account_pool_state()
    meta = state.get(account_id) or {}
    if not isinstance(meta, dict):
        meta = {}
    meta["quota_waiting"] = False
    meta["disabled_for_quota"] = False
    # Only re-enable if not manually disabled for non-quota reasons
    if meta.get("manual_disabled"):
        meta["enabled"] = False
    else:
        meta["enabled"] = True
    meta["quota_status"] = "manual_disabled" if meta.get("manual_disabled") else "active"
    meta["quota_recovered_at"] = _now()
    meta["quota_source"] = source
    for key in (
        "quota_wait_reason",
        "quota_reset_at",
        "quota_next_probe_at",
        "quota_waiting_since",
        "quota_cycle_id",
        "quota_limit_tokens",
        "quota_remaining_tokens",
        "quota_grace_count",
        "quota_confirmation_count",
        "quota_first_confirm_at",
        "quota_last_confirm_at",
        "quota_last_evidence",
        "quota_confirmation_cycle_id",
        "quota_terminal_at",
        "quota_disabled_at",
    ):
        meta.pop(key, None)
    if not meta.get("credential_suspended"):
        meta.pop("disabled_reason", None)
    meta["last_error"] = ""
    state[account_id] = meta
    save_account_pool_state(state)
    print(f"  [quota] account re-activated after quota recovery: {account_id}")
    for a in list_pool_accounts():
        if a["id"] == account_id:
            return a
    return {
        "id": account_id,
        "enabled": bool(meta.get("enabled")),
        "quota_waiting": False,
        "quota_status": meta["quota_status"],
    }


def quota_cleanup_ready(meta: dict[str, Any], *, now: float | None = None) -> bool:
    """Return True only for a fully evidenced terminal quota-reset failure."""
    current = _now() if now is None else float(now)
    try:
        confirmations = int(meta.get("quota_confirmation_count") or 0)
        cycles = int(meta.get("quota_grace_count") or 0)
        first = float(meta.get("quota_first_confirm_at") or 0)
        last = float(meta.get("quota_last_confirm_at") or 0)
        terminal = float(meta.get("quota_terminal_at") or 0)
    except (TypeError, ValueError):
        return False
    return bool(
        meta.get("quota_status") == "quota_reset_failed"
        and meta.get("quota_cycle_id")
        and confirmations >= QUOTA_CONFIRMATIONS_REQUIRED
        and cycles >= QUOTA_MAINTENANCE_CYCLES_REQUIRED
        and first > 0
        and last >= first
        and current - first >= QUOTA_RESET_GRACE_SECONDS
        and meta.get("quota_last_evidence") == "free_usage_exhausted"
        and meta.get("quota_confirmation_cycle_id") == meta.get("quota_cycle_id")
        and terminal > 0
        and terminal <= current
    )


def save_quota_snapshot(account_id: str, quota_result: dict[str, Any]) -> None:
    """Cache last successful quota snapshot on pool meta (no secrets)."""
    if not account_id:
        return
    snap = {
        "fetched_at": quota_result.get("fetched_at") or _now(),
        "monthly_limit": quota_result.get("monthly_limit"),
        "used": quota_result.get("used"),
        "remaining": quota_result.get("remaining"),
        "usage_percent": quota_result.get("usage_percent"),
        "unlimited_or_free": quota_result.get("unlimited_or_free"),
        "exhausted": quota_result.get("exhausted"),
        "summary": (quota_result.get("display") or {}).get("summary"),
        "billing_period_end": quota_result.get("billing_period_end"),
    }
    state = get_account_pool_state()
    meta = state.get(account_id) or {}
    if not isinstance(meta, dict):
        meta = {}
    meta["last_quota"] = snap
    state[account_id] = meta
    save_account_pool_state(state)


def pool_summary(*, include_accounts: bool = True) -> dict[str, Any]:
    """Summarize pool health.

    `include_accounts=False` keeps the payload small for /health and status
    routes on large multi-account pools (hundreds of entries) and avoids
    building the full admin account dict list.
    """
    if include_accounts:
        accounts = list_pool_accounts()
        live = [a for a in accounts if not a.get("expired")]
        available = [
            a
            for a in live
            if a.get("enabled")
            and not a.get("quota_waiting")
            and a.get("quota_status") not in ("quota_grace", "quota_reset_failed")
            and not a.get("manual_disabled")
            and not a.get("credential_suspended")
            and a.get("refresh_status") != "refresh_terminal"
        ]
        cooling = [a for a in available if a.get("in_cooldown")]
        quota_disabled = [a for a in accounts if a.get("disabled_for_quota")]
        waiting = [a for a in accounts if a.get("quota_status") == "quota_waiting"]
        grace = [a for a in accounts if a.get("quota_status") in ("quota_probe_due", "quota_grace")]
        reset_failed = [a for a in accounts if a.get("quota_status") == "quota_reset_failed"]
        manual = [a for a in accounts if a.get("manual_disabled")]
        suspended = [a for a in accounts if a.get("credential_suspended")]
        refresh_pending = [
            a for a in accounts if a.get("refresh_status") == "refresh_pending_confirmation"
        ]
        refresh_terminal = [
            a for a in accounts if a.get("refresh_status") == "refresh_terminal"
        ]
        model_blocked = [
            a for a in accounts if (a.get("blocked_model_ids") or a.get("blocked_models"))
        ]
        return {
            "mode": get_account_mode(),
            "total": len(accounts),
            "live": len(live),
            "enabled": len(available),
            "available": len(available),
            "active": len(available),
            "in_cooldown": len(cooling),
            "quota_disabled": len(quota_disabled),
            "quota_waiting": len(waiting),
            "quota_grace": len(grace),
            "quota_reset_failed": len(reset_failed),
            "manual_disabled": len(manual),
            "credential_suspended": len(suspended),
            "refresh_pending": len(refresh_pending),
            "refresh_terminal": len(refresh_terminal),
            "model_blocked": len(model_blocked),
            "accounts": accounts,
        }

    # Lightweight counts-only path for /health and frequent status polls.
    state = get_account_pool_state()
    total = live = enabled = cooling = quota_disabled = model_blocked = 0
    waiting = grace = reset_failed = manual = suspended = 0
    refresh_pending = refresh_terminal = 0
    for creds in list_live_credentials(include_expired=True, auto_refresh=False):
        total += 1
        meta = _pool_meta(creds.auth_key or "", state)
        if meta.get("disabled_for_quota"):
            quota_disabled += 1
        status = meta.get("quota_status")
        if status == "quota_waiting":
            waiting += 1
        elif status in ("quota_probe_due", "quota_grace"):
            grace += 1
        elif status == "quota_reset_failed":
            reset_failed += 1
        if meta.get("manual_disabled"):
            manual += 1
        if meta.get("credential_suspended"):
            suspended += 1
        refresh_status = getattr(creds, "refresh_status", None)
        if refresh_status == "refresh_pending_confirmation":
            refresh_pending += 1
        elif refresh_status == "refresh_terminal":
            refresh_terminal += 1
        if meta.get("blocked_model_ids") or meta.get("blocked_models"):
            model_blocked += 1
        if creds.expired:
            continue
        live += 1
        if (
            not meta["enabled"]
            or is_quota_waiting(meta)
            or status in ("quota_grace", "quota_reset_failed")
            or meta.get("manual_disabled")
            or meta.get("credential_suspended")
            or refresh_status == "refresh_terminal"
        ):
            continue
        enabled += 1
        if is_in_cooldown(meta):
            cooling += 1
    return {
        "mode": get_account_mode(),
        "total": total,
        "live": live,
        "enabled": enabled,
        "available": enabled,
        "active": enabled,
        "in_cooldown": cooling,
        "quota_disabled": quota_disabled,
        "quota_waiting": waiting,
        "quota_grace": grace,
        "quota_reset_failed": reset_failed,
        "manual_disabled": manual,
        "credential_suspended": suspended,
        "refresh_pending": refresh_pending,
        "refresh_terminal": refresh_terminal,
        "model_blocked": model_blocked,
    }


def try_acquire_sequence(
    max_attempts: int | None = None,
    *,
    model: str | None = None,
    prefer_account_id: str | None = None,
) -> list[GrokCredentials]:
    """
    Build an ordered list of accounts to try for one request (failover chain).
    Covers all enabled live accounts equally; skips model-blocked accounts.

    `prefer_account_id`: conversation affinity — put this account first so
    multi-turn chats stay on the same account (memory continuity).
    """
    _ensure_multi_account_layout()
    mode = get_account_mode()
    all_live = list_live_credentials(include_expired=False, auto_refresh=True)
    state = get_account_pool_state()
    def _usable(c):
        meta = _pool_meta(c.auth_key or "", state)
        if is_quota_waiting(meta):
            return False
        if meta.get("disabled_for_quota") and not meta.get("quota_waiting"):
            # legacy permanent flag: still exclude from request rotation
            return False
        if not meta.get("enabled", True) and not meta.get("quota_waiting"):
            # manually disabled
            if meta.get("enabled") is False and not meta.get("disabled_for_quota"):
                return False
        if model and is_model_blocked(c.auth_key or "", model, state):
            return False
        return bool(meta.get("enabled", True)) and not is_quota_waiting(meta)

    enabled = [c for c in all_live if _usable(c)]
    if not enabled:
        # last resort: still never include quota_waiting accounts
        enabled = [
            c
            for c in all_live
            if not is_quota_waiting(_pool_meta(c.auth_key or "", state))
            and not (model and is_model_blocked(c.auth_key or "", model, state))
            and _pool_meta(c.auth_key or "", state).get("enabled", True)
        ]
    if not enabled:
        # Empty sequence is correct when every account is waiting/disabled.
        return []

    # De-dupe by user_id (legacy dual keys)
    seen_users: set[str] = set()
    deduped: list[GrokCredentials] = []
    for c in enabled:
        uid = c.user_id or c.auth_key or ""
        if uid in seen_users:
            continue
        seen_users.add(uid)
        deduped.append(c)
    enabled = deduped

    # sort: not cooling first, then by strategy bias
    def cool_key(c: GrokCredentials) -> tuple[int, int, float]:
        meta = _pool_meta(c.auth_key or "", state)
        cooling = 1 if is_in_cooldown(meta) else 0
        used = meta["request_count"]
        last = float(meta["last_used_at"] or 0)
        return (cooling, used if mode == "least_used" else 0, last)

    if mode == "random":
        ordered = list(enabled)
        random.shuffle(ordered)
        ordered.sort(key=lambda c: 1 if is_in_cooldown(_pool_meta(c.auth_key or "", state)) else 0)
    elif mode == "least_used":
        ordered = sorted(enabled, key=cool_key)
    else:  # round_robin — start from current RR head
        if not enabled:
            return []
        global _rr_index
        with _lock:
            start = _rr_index % len(enabled)
            _rr_index = (start + 1) % max(len(enabled), 1)
        rotated = enabled[start:] + enabled[:start]
        # non-cooling first, preserve RR order within each group
        not_cooling = [
            c
            for c in rotated
            if not is_in_cooldown(_pool_meta(c.auth_key or "", state))
        ]
        cooling = [
            c
            for c in rotated
            if is_in_cooldown(_pool_meta(c.auth_key or "", state))
        ]
        ordered = not_cooling + cooling

    # Conversation affinity: pin multi-turn chat to same account first
    if prefer_account_id and ordered:
        sticky: list[GrokCredentials] = []
        rest: list[GrokCredentials] = []
        pref = prefer_account_id
        for c in ordered:
            aid = c.auth_key or ""
            if aid == pref or c.user_id == pref or aid.endswith(f"::{pref}"):
                sticky.append(c)
            else:
                rest.append(c)
        if sticky:
            ordered = sticky + rest

    if max_attempts is not None:
        ordered = ordered[: max(1, max_attempts)]
    return ordered


def load_for_id(account_id: str) -> GrokCredentials:
    return load_credentials_by_id(account_id)


def list_quota_probe_due(*, now: float | None = None) -> list[str]:
    """Account ids in quota_waiting whose reset/probe time has arrived."""
    current = time.time() if now is None else now
    state = get_account_pool_state()
    due: list[str] = []
    for aid, meta in state.items():
        if not isinstance(meta, dict):
            continue
        if quota_probe_due(meta, now=current):
            due.append(str(aid))
    return due


def process_quota_probe_due(
    *,
    now: float | None = None,
    max_n: int = 20,
    probe_fn=None,
) -> dict[str, int]:
    """Probe due waiting accounts; recover or schedule next grace probe.

    probe_fn(account_id) -> dict with ok/exhausted/remaining_tokens keys.
    Default uses the real quota.probe_free_usage_for_creds /responses boundary.
    """
    current = time.time() if now is None else now
    due = list_quota_probe_due(now=current)[: max(1, int(max_n))]
    recovered = still_waiting = failed = 0
    if not due:
        return {"due": 0, "recovered": 0, "still_waiting": 0, "failed": 0}
    for aid in due:
        try:
            if probe_fn is not None:
                result = probe_fn(aid)
            else:
                from auth import load_credentials_by_id
                from quota import probe_free_usage_for_creds

                creds = load_credentials_by_id(aid)
                if creds is None:
                    raise AuthError("quota probe credentials unavailable")
                # Production default: real 1M free-usage probe — NOT monthly /billing.
                import config

                result = probe_free_usage_for_creds(
                    creds, proxy=(config.XAI_PROXY or None)
                )
            state = get_account_pool_state()
            meta = state.get(aid) or {}
            if result.get("error_class") == "credential":
                mark_credential_suspended(
                    aid,
                    reason=(result.get("error") or "quota probe credential rejected")[:300],
                    source="quota_probe",
                )
                failed += 1
            elif result.get("free_usage_ok") and not result.get("exhausted") and not result.get("inconclusive"):
                if meta.get("quota_waiting") or meta.get("disabled_for_quota"):
                    clear_quota_waiting(aid, source="quota_probe")
                recovered += 1
            elif (
                result.get("exhausted")
                and not result.get("inconclusive")
                and result.get("error_class") in (None, "", "free_usage_exhausted")
            ):
                meta = dict(meta)
                reset_at = _safe_float(meta.get("quota_reset_at"))
                post_reset = bool(reset_at and current >= reset_at)
                if post_reset:
                    meta["quota_confirmation_count"] = _safe_int(
                        meta.get("quota_confirmation_count")
                    ) + 1
                    meta["quota_grace_count"] = _safe_int(
                        meta.get("quota_grace_count")
                    ) + 1
                    meta["quota_last_confirm_at"] = current
                    first_confirm = _safe_float(meta.get("quota_first_confirm_at"))
                    if first_confirm <= 0:
                        first_confirm = current
                        meta["quota_first_confirm_at"] = current
                    meta["quota_last_evidence"] = "free_usage_exhausted"
                    meta["quota_confirmation_cycle_id"] = meta.get("quota_cycle_id")
                    meta["quota_status"] = "quota_grace"
                    if (
                        meta["quota_confirmation_count"] >= QUOTA_CONFIRMATIONS_REQUIRED
                        and meta["quota_grace_count"]
                        >= QUOTA_MAINTENANCE_CYCLES_REQUIRED
                        and current - first_confirm >= QUOTA_RESET_GRACE_SECONDS
                    ):
                        meta["quota_status"] = "quota_reset_failed"
                        meta["quota_terminal_at"] = current
                else:
                    # still inside original window — keep waiting, do not extend reset
                    meta["quota_status"] = "quota_waiting"
                meta["quota_next_probe_at"] = current + QUOTA_POST_RESET_PROBE_SECONDS
                state[aid] = meta
                save_account_pool_state(state)
                still_waiting += 1
            else:
                # inconclusive network/429/5xx — retry later, no grace/terminal evidence
                meta = dict(meta)
                meta["quota_next_probe_at"] = current + QUOTA_INCONCLUSIVE_RETRY_SECONDS
                if meta.get("quota_status") != "quota_reset_failed":
                    meta["quota_status"] = "quota_probe_due"
                state[aid] = meta
                save_account_pool_state(state)
                still_waiting += 1
        except Exception:
            # Credential loading and transport setup can fail before a probe
            # result exists. Treat that as inconclusive and advance backoff so
            # a broken entry is not retried on every maintenance tick.
            try:
                state = get_account_pool_state()
                meta = dict(state.get(aid) or {})
                meta["quota_next_probe_at"] = (
                    current + QUOTA_INCONCLUSIVE_RETRY_SECONDS
                )
                if meta.get("quota_status") != "quota_reset_failed":
                    meta["quota_status"] = "quota_probe_due"
                state[aid] = meta
                save_account_pool_state(state)
            except Exception:
                pass
            failed += 1
    return {
        "due": len(due),
        "recovered": recovered,
        "still_waiting": still_waiting,
        "failed": failed,
    }
