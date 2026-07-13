"""Fetch per-account usage / billing quota from cli-chat-proxy.

Upstream endpoints (Grok session token):
  GET /v1/billing  — monthly limit, used, on-demand cap, period, history
  GET /v1/user     — profile, grok code access flags

When quota is exhausted, the account is auto-disabled in the rotation pool
so subsequent requests skip it.
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import httpx

from auth import GrokCredentials, list_live_credentials, load_credentials_by_id, upstream_headers
from config import CLI_VERSION, DEFAULT_MODEL, UPSTREAM_BASE

# Upstream returns amounts as {"val": number}. Unit is USD (often 0 on free/promo).
_QUOTA_TIMEOUT = 20.0

# Hard quota / credit exhaustion signals from upstream error bodies.
# (Pure rate-limit 429 alone is temporary cooldown — not permanent disable.)
_QUOTA_ERROR_RE = re.compile(
    r"("
    r"usage[_ -]?limit[_ -]?reached|"
    r"usage[_ -]?pool[_ -]?exhausted|"
    r"free[_ -]?usage[_ -]?exhausted|"
    r"subscription:free-usage-exhausted|"
    r"quota[_ -]?exceeded|"
    r"quota\s+exceeded|"
    r"run\s+out\s+of\s+credits|"
    r"out\s+of\s+credits|"
    r"spending[-_ ]?limit|"
    r"personal-team-blocked|"
    r"need\s+a\s+grok\s+subscription|"
    r"monthly\s+limit|"
    r"no\s+credits|"
    r"insufficient\s+credits|"
    r"billing\s+limit|"
    r"usage\s+limit"
    r")",
    re.IGNORECASE,
)


def _headers(token: str) -> dict[str, str]:
    # Reuse CLI client headers; model override not needed for billing/user.
    h = upstream_headers(token, DEFAULT_MODEL)
    h["Accept"] = "application/json"
    return h


def _money(obj: Any) -> float | None:
    if obj is None:
        return None
    if isinstance(obj, (int, float)):
        return float(obj)
    if isinstance(obj, dict) and "val" in obj:
        try:
            return float(obj["val"])
        except (TypeError, ValueError):
            return None
    return None


def _fmt_usd(v: float | None) -> str | None:
    if v is None:
        return None
    if abs(v) < 0.005:
        return "$0.00"
    if abs(v) >= 100:
        return f"${v:,.2f}"
    return f"${v:.2f}"


def normalize_billing(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Flatten cli-chat-proxy /v1/billing payload into a stable shape."""
    if not isinstance(raw, dict):
        return {"ok": False, "error": "empty billing response"}

    cfg = raw.get("config") if isinstance(raw.get("config"), dict) else raw
    monthly_limit = _money(cfg.get("monthlyLimit") or cfg.get("monthly_limit"))
    used = _money(cfg.get("used"))
    on_demand_cap = _money(cfg.get("onDemandCap") or cfg.get("on_demand_cap"))
    prepaid = _money(cfg.get("prepaidBalance") or cfg.get("prepaid_balance"))
    on_demand_used = _money(cfg.get("onDemandUsed") or cfg.get("on_demand_used"))

    remaining: float | None = None
    if monthly_limit is not None and used is not None:
        remaining = max(0.0, monthly_limit - used)

    usage_pct: float | None = None
    if monthly_limit and monthly_limit > 0 and used is not None:
        usage_pct = round(100.0 * used / monthly_limit, 2)

    history: list[dict[str, Any]] = []
    for item in cfg.get("history") or []:
        if not isinstance(item, dict):
            continue
        cycle = item.get("billingCycle") or item.get("billing_cycle") or {}
        history.append(
            {
                "year": (cycle or {}).get("year"),
                "month": (cycle or {}).get("month"),
                "included_used": _money(item.get("includedUsed") or item.get("included_used")),
                "on_demand_used": _money(item.get("onDemandUsed") or item.get("on_demand_used")),
                "total_used": _money(item.get("totalUsed") or item.get("total_used")),
            }
        )

    unlimited = bool(
        (monthly_limit is None or monthly_limit == 0)
        and (on_demand_cap is None or on_demand_cap == 0)
    )

    exhausted, exhaust_reason = _detect_billing_exhausted(
        monthly_limit=monthly_limit,
        used=used,
        remaining=remaining,
        on_demand_cap=on_demand_cap,
        on_demand_used=on_demand_used,
        unlimited=unlimited,
    )

    return {
        "ok": True,
        "monthly_limit": monthly_limit,
        "used": used,
        "remaining": remaining,
        "on_demand_cap": on_demand_cap,
        "on_demand_used": on_demand_used,
        "prepaid_balance": prepaid,
        "usage_percent": usage_pct,
        "unlimited_or_free": unlimited,
        "exhausted": exhausted,
        "exhaust_reason": exhaust_reason,
        "billing_period_start": cfg.get("billingPeriodStart") or cfg.get("billing_period_start"),
        "billing_period_end": cfg.get("billingPeriodEnd") or cfg.get("billing_period_end"),
        "history": history,
        "display": {
            "monthly_limit": _fmt_usd(monthly_limit),
            "used": _fmt_usd(used),
            "remaining": _fmt_usd(remaining),
            "on_demand_cap": _fmt_usd(on_demand_cap),
            "prepaid_balance": _fmt_usd(prepaid),
            "summary": _summary_text(
                monthly_limit=monthly_limit,
                used=used,
                remaining=remaining,
                unlimited=unlimited,
                exhausted=exhausted,
                usage_pct=usage_pct,
            ),
        },
        "raw": raw,
    }


def _detect_billing_exhausted(
    *,
    monthly_limit: float | None,
    used: float | None,
    remaining: float | None,
    on_demand_cap: float | None,
    on_demand_used: float | None,
    unlimited: bool,
) -> tuple[bool, str | None]:
    """Return (exhausted, reason) from billing numbers."""
    if unlimited:
        return False, None

    # Included monthly budget fully consumed
    if monthly_limit is not None and monthly_limit > 0 and used is not None:
        if used >= monthly_limit or (remaining is not None and remaining <= 0):
            # On-demand may still allow spend
            if on_demand_cap is not None and on_demand_cap > 0:
                od_used = on_demand_used or 0.0
                if od_used >= on_demand_cap:
                    return True, "月限额与按需额度均已用尽"
                # monthly included gone but on-demand remains — not fully exhausted
                return False, None
            return True, f"月限额已用尽（{_fmt_usd(used)} / {_fmt_usd(monthly_limit)}）"

    if on_demand_cap is not None and on_demand_cap > 0 and on_demand_used is not None:
        if on_demand_used >= on_demand_cap and (
            monthly_limit is None or monthly_limit <= 0 or (used is not None and used >= (monthly_limit or 0))
        ):
            return True, f"按需额度已用尽（{_fmt_usd(on_demand_used)} / {_fmt_usd(on_demand_cap)}）"

    return False, None



def parse_quota_reset_at(headers: Any = None, *, body: str = "") -> float | None:
    """Extract absolute reset time (epoch seconds) from headers or body if present."""
    import time as _time
    now = _time.time()
    if headers is not None:
        try:
            # httpx Headers or dict
            get = headers.get if hasattr(headers, "get") else lambda k, d=None: None
            for key in (
                "x-ratelimit-reset",
                "x-ratelimit-reset-tokens",
                "x-rate-limit-reset",
                "x-rate-limit-reset-tokens",
                "ratelimit-reset",
                "x-grok-usage-reset",
                "x-usage-reset-at",
                "retry-after",
            ):
                raw = get(key) or get(key.title()) or get(key.upper())
                if raw is None:
                    continue
                # HTTP-date Retry-After not fully parsed; numeric only here
                try:
                    val = float(str(raw).strip())
                except (TypeError, ValueError):
                    continue
                if val > 1e12:  # epoch ms
                    return val / 1000.0
                if key.lower() == "retry-after" or val < 1e9:
                    return now + max(0.0, val)
                return val
        except Exception:
            pass
    # body may include resetAt / reset_at ISO or epoch
    import re as _re
    m = _re.search(r'"reset[_-]?at"\s*:\s*"?(\d{10,13})"?', body or "", _re.I)
    if m:
        v = float(m.group(1))
        if v > 1e12:
            v /= 1000.0
        return v
    # default rolling 24h window
    return now + 24 * 3600


def is_free_usage_exhausted_message(error: str = "", status_code: int | None = None) -> bool:
    text = (error or "").lower()
    if "free-usage-exhausted" in text or "free_usage_exhausted" in text:
        return True
    if "subscription:free-usage-exhausted" in text:
        return True
    # Numeric remaining_tokens only if explicitly zero/negative with a limit
    import re as _re
    m_rem = _re.search(r"remaining[_-]?tokens\D+(\d+)", text)
    m_lim = _re.search(r"limit[_-]?tokens\D+(\d+)", text)
    if m_rem and m_lim:
        try:
            rem = int(m_rem.group(1))
            lim = int(m_lim.group(1))
            if lim > 0 and rem <= 0:
                return True
        except ValueError:
            pass
    # Do NOT treat bare "limit_tokens=1000000 remaining=500000" as exhausted.
    return False


def is_quota_error_message(error: str | None, status_code: int | None = None) -> bool:
    """True if upstream error indicates hard quota/credit exhaustion."""
    text = (error or "").strip()
    if not text:
        return False
    if _QUOTA_ERROR_RE.search(text):
        return True
    # 403 + spending/subscription style codes often mean no credits
    if status_code == 403 and any(
        k in text.lower()
        for k in ("credit", "subscription", "billing", "spending", "limit", "quota")
    ):
        return True
    return False


def _summary_text(
    *,
    monthly_limit: float | None,
    used: float | None,
    remaining: float | None,
    unlimited: bool,
    exhausted: bool,
    usage_pct: float | None,
) -> str:
    if exhausted:
        base = "额度已耗尽"
        if used is not None and monthly_limit is not None:
            return f"{base}（{_fmt_usd(used)} / {_fmt_usd(monthly_limit)}）"
        return base
    if unlimited:
        return "免费/促销（未设月限额）"
    parts = []
    if used is not None and monthly_limit is not None:
        parts.append(f"已用 {_fmt_usd(used)} / {_fmt_usd(monthly_limit)}")
    elif used is not None:
        parts.append(f"已用 {_fmt_usd(used)}")
    if remaining is not None and monthly_limit and monthly_limit > 0:
        parts.append(f"剩余 {_fmt_usd(remaining)}")
    if usage_pct is not None:
        parts.append(f"{usage_pct}%")
    return " · ".join(parts) if parts else "—"


def apply_exhaustion_to_pool(
    account_id: str | None,
    *,
    reason: str,
    source: str = "billing",
    reset_at: float | None = None,
    limit_tokens: int | float | None = None,
    remaining_tokens: int | float | None = None,
) -> dict[str, Any] | None:
    """Mark account quota_waiting (not permanent disable)."""
    if not account_id:
        return None
    try:
        import account_pool

        return account_pool.mark_quota_waiting(
            account_id,
            reason=reason,
            source=source,
            reset_at=reset_at,
            limit_tokens=limit_tokens,
            remaining_tokens=remaining_tokens,
        )
    except Exception as e:  # noqa: BLE001
        return {"id": account_id, "error": str(e)}


def maybe_disable_from_quota_result(result: dict[str, Any]) -> dict[str, Any]:
    """If quota result says exhausted, disable the account and annotate result."""
    if not result.get("ok"):
        return result
    account_id = result.get("account_id")
    if result.get("exhausted"):
        reason = result.get("exhaust_reason") or "额度已耗尽（等待24h重置）"
        disabled = apply_exhaustion_to_pool(
            account_id,
            reason=reason,
            source="billing",
            reset_at=result.get("reset_at"),
            limit_tokens=result.get("limit_tokens"),
            remaining_tokens=result.get("remaining_tokens"),
        )
        result["auto_disabled"] = False
        result["quota_waiting"] = True
        result["waiting_record"] = disabled
        result["display"] = dict(result.get("display") or {})
        result["display"]["summary"] = f"额度等待重置 · 暂不轮询（{reason}）"
    else:
        result["auto_disabled"] = False
        result["quota_waiting"] = False
        if account_id:
            try:
                import account_pool

                account_pool.save_quota_snapshot(account_id, result)
                # Monthly USD billing health must NOT clear free-usage 1M waiting.
                # Only clear when caller sets free_usage_ok=True (token probe path).
                meta_state = account_pool.get_account_pool_state().get(account_id) or {}
                if (
                    result.get("free_usage_ok")
                    and (meta_state.get("quota_waiting") or meta_state.get("disabled_for_quota"))
                ):
                    account_pool.clear_quota_waiting(account_id, source="free_usage_ok")
                    result["quota_recovered"] = True
            except Exception:
                pass
    return result


def handle_upstream_error_for_quota(
    account_id: str | None,
    *,
    error: str = "",
    status_code: int | None = None,
    headers: Any = None,
) -> dict[str, Any] | None:
    """
    On upstream failure: if message indicates free-usage/quota exhaustion,
    mark quota_waiting until reset (default +24h). Never permanent delete.
    """
    if not account_id or not is_free_usage_exhausted_message(error, status_code):
        return None
    reason = f"上游额度等待 (HTTP {status_code}): {(error or '')[:120]}"
    reset_at = parse_quota_reset_at(headers, body=error or "")
    return apply_exhaustion_to_pool(
        account_id,
        reason=reason,
        source="upstream_error",
        reset_at=reset_at,
        limit_tokens=1_000_000,
        remaining_tokens=0,
    )


def normalize_user(raw: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    return {
        "user_id": raw.get("userId") or raw.get("principalId") or raw.get("user_id"),
        "email": raw.get("email"),
        "first_name": raw.get("firstName") or raw.get("first_name"),
        "last_name": raw.get("lastName") or raw.get("last_name"),
        "has_grok_code_access": raw.get("hasGrokCodeAccess"),
        "user_blocked_reason": raw.get("userBlockedReason"),
        "team_id": raw.get("teamId"),
        "team_name": raw.get("teamName"),
        "organization_id": raw.get("organizationId"),
        "organization_name": raw.get("organizationName"),
        "principal_type": raw.get("principalType"),
    }


def fetch_quota_for_creds(creds: GrokCredentials) -> dict[str, Any]:
    """Synchronous quota fetch for one account."""
    base = {
        "account_id": creds.auth_key,
        "email": creds.email,
        "user_id": creds.user_id,
        "fetched_at": time.time(),
    }
    headers = _headers(creds.token)
    billing_url = f"{UPSTREAM_BASE}/billing"
    user_url = f"{UPSTREAM_BASE}/user"
    try:
        with httpx.Client(timeout=_QUOTA_TIMEOUT) as client:
            br = client.get(billing_url, headers=headers)
            ur = client.get(user_url, headers=headers)
    except httpx.HTTPError as e:
        return {**base, "ok": False, "error": f"network: {e}"}

    billing_raw = None
    user_raw = None
    try:
        if br.status_code == 200:
            billing_raw = br.json()
        else:
            return {
                **base,
                "ok": False,
                "error": f"billing HTTP {br.status_code}: {(br.text or '')[:200]}",
                "status_code": br.status_code,
            }
    except Exception as e:  # noqa: BLE001
        return {**base, "ok": False, "error": f"billing parse: {e}"}

    try:
        if ur.status_code == 200:
            user_raw = ur.json()
    except Exception:
        user_raw = None

    bill = normalize_billing(billing_raw if isinstance(billing_raw, dict) else None)
    user = normalize_user(user_raw if isinstance(user_raw, dict) else None)
    result = {
        **base,
        **bill,
        "user": user,
        "cli_version": CLI_VERSION,
        "upstream": UPSTREAM_BASE,
    }
    return maybe_disable_from_quota_result(result)


async def fetch_quota_for_creds_async(creds: GrokCredentials) -> dict[str, Any]:
    base = {
        "account_id": creds.auth_key,
        "email": creds.email,
        "user_id": creds.user_id,
        "fetched_at": time.time(),
    }
    headers = _headers(creds.token)
    billing_url = f"{UPSTREAM_BASE}/billing"
    user_url = f"{UPSTREAM_BASE}/user"
    try:
        async with httpx.AsyncClient(timeout=_QUOTA_TIMEOUT) as client:
            br = await client.get(billing_url, headers=headers)
            ur = await client.get(user_url, headers=headers)
    except httpx.HTTPError as e:
        return {**base, "ok": False, "error": f"network: {e}"}

    try:
        if br.status_code != 200:
            return {
                **base,
                "ok": False,
                "error": f"billing HTTP {br.status_code}: {(br.text or '')[:200]}",
                "status_code": br.status_code,
            }
        billing_raw = br.json()
    except Exception as e:  # noqa: BLE001
        return {**base, "ok": False, "error": f"billing parse: {e}"}

    user_raw = None
    try:
        if ur.status_code == 200:
            user_raw = ur.json()
    except Exception:
        user_raw = None

    bill = normalize_billing(billing_raw if isinstance(billing_raw, dict) else None)
    user = normalize_user(user_raw if isinstance(user_raw, dict) else None)
    result = {
        **base,
        **bill,
        "user": user,
        "cli_version": CLI_VERSION,
        "upstream": UPSTREAM_BASE,
    }
    return maybe_disable_from_quota_result(result)


def fetch_quota_by_account_id(account_id: str) -> dict[str, Any]:
    creds = load_credentials_by_id(account_id)
    return fetch_quota_for_creds(creds)


async def fetch_all_quotas(
    *,
    include_expired: bool = False,
    max_workers: int | None = None,
) -> dict[str, Any]:
    """Query quota for every live account concurrently; auto-disable exhausted ones."""
    try:
        from config import QUOTA_WORKERS
    except Exception:
        QUOTA_WORKERS = 4
    if max_workers is None:
        max_workers = QUOTA_WORKERS

    # auto_refresh=False: avoid OIDC fan-out while also hitting billing endpoints
    accounts = list_live_credentials(include_expired=include_expired, auto_refresh=False)
    # de-dupe by user_id
    seen: set[str] = set()
    unique: list[GrokCredentials] = []
    for c in accounts:
        uid = c.user_id or c.auth_key or ""
        if uid in seen:
            continue
        seen.add(uid)
        unique.append(c)

    results: list[dict[str, Any]] = []

    def _fetch_one(creds: GrokCredentials) -> dict[str, Any]:
        return fetch_quota_for_creds(creds)

    workers = min(int(max_workers), max(1, len(unique))) if unique else 1
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="quota-") as ex:
        for fut in as_completed(ex.submit(_fetch_one, c) for c in unique):
            try:
                results.append(fut.result())
            except Exception as e:  # noqa: BLE001
                results.append({
                    "ok": False,
                    "error": str(e)[:300],
                    "fetched_at": time.time(),
                })

    # Mark results that belong to disabled rotation accounts so UI/stats can
    # exclude them from available-quota aggregates.
    disabled_ids: set[str] = set()
    try:
        import account_pool

        for a in account_pool.list_pool_accounts():
            if a.get("enabled") is False or a.get("disabled_for_quota"):
                if a.get("id"):
                    disabled_ids.add(str(a["id"]))
    except Exception:
        disabled_ids = set()

    for r in results:
        aid = r.get("account_id")
        r["pool_disabled"] = bool(aid and str(aid) in disabled_ids)

    ok_count = sum(1 for r in results if r.get("ok"))
    exhausted_count = sum(1 for r in results if r.get("exhausted"))
    auto_disabled = sum(1 for r in results if r.get("auto_disabled"))
    pool_disabled_count = sum(1 for r in results if r.get("pool_disabled"))
    # Available totals exclude manually/quota-disabled accounts.
    active_ok = [
        r
        for r in results
        if r.get("ok") and not r.get("pool_disabled") and not r.get("exhausted")
    ]
    total_used = sum(
        float(r["used"]) for r in active_ok if r.get("used") is not None
    )
    total_limit = sum(
        float(r["monthly_limit"])
        for r in active_ok
        if r.get("monthly_limit") is not None
    )
    total_remaining = sum(
        float(r["remaining"])
        for r in active_ok
        if r.get("remaining") is not None
    )
    return {
        "ok": True,
        "fetched_at": time.time(),
        "count": len(results),
        "ok_count": ok_count,
        "exhausted_count": exhausted_count,
        "auto_disabled_count": auto_disabled,
        "pool_disabled_count": pool_disabled_count,
        "active_ok_count": len(active_ok),
        "total_used": total_used,
        "total_monthly_limit": total_limit,
        "total_remaining": total_remaining,
        "accounts": results,
    }


def probe_free_usage_for_creds(
    creds: GrokCredentials,
    *,
    proxy: str | None = None,
    timeout: float | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Production Grok Build 1M/24h free-usage probe via POST /responses.

    Never logs Authorization or cookies. Uses explicit proxy without env mutation.
    """
    import re as _re
    t_out = float(timeout if timeout is not None else _QUOTA_TIMEOUT)
    use_model = (model or DEFAULT_MODEL or "grok-4.5").strip()
    url = f"{UPSTREAM_BASE}/responses"
    headers = _headers(creds.token)
    # Prefer chat-completions style if responses unavailable — still same host/v1
    body = {
        "model": use_model,
        "input": [{"role": "user", "content": "ping"}],
        "max_output_tokens": 8,
    }
    # Also support chat/completions shape as fallback endpoint
    chat_body = {
        "model": use_model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 8,
        "stream": False,
    }
    base: dict[str, Any] = {
        "ok": False,
        "free_usage_ok": False,
        "exhausted": False,
        "inconclusive": True,
        "status_code": None,
        "limit_tokens": None,
        "remaining_tokens": None,
        "actual_tokens": None,
        "reset_at": None,
        "error_class": "",
        "account_id": creds.auth_key,
        "source": "free_usage_probe",
    }
    client_kwargs: dict[str, Any] = {"timeout": t_out, "trust_env": False}
    if proxy:
        client_kwargs["proxy"] = proxy
    try:
        with httpx.Client(**client_kwargs) as client:
            resp = client.post(url, headers=headers, json=body)
            # Fallback if /responses not found
            if resp.status_code in (404, 405):
                resp = client.post(
                    f"{UPSTREAM_BASE}/chat/completions",
                    headers=headers,
                    json=chat_body,
                )
    except httpx.TimeoutException:
        return {**base, "error_class": "timeout", "inconclusive": True}
    except httpx.HTTPError as e:
        return {**base, "error_class": f"network:{type(e).__name__}", "inconclusive": True}

    status = int(resp.status_code)
    base["status_code"] = status
    text = (resp.text or "")[:2000]
    # headers
    def _hi(name: str):
        try:
            v = resp.headers.get(name) or resp.headers.get(name.title())
            return int(float(v)) if v is not None else None
        except (TypeError, ValueError):
            return None

    limit_tokens = _hi("x-ratelimit-limit-tokens")
    remaining_tokens = _hi("x-ratelimit-remaining-tokens")
    base["limit_tokens"] = limit_tokens
    base["remaining_tokens"] = remaining_tokens
    base["reset_at"] = parse_quota_reset_at(resp.headers, body=text)

    # explicit exhaustion patterns
    low = text.lower()
    m_al = _re.search(r"tokens\s*\(actual/limit\)\s*:\s*(\d+)\s*/\s*(\d+)", text, _re.I)
    if m_al:
        actual, lim = int(m_al.group(1)), int(m_al.group(2))
        base["actual_tokens"] = actual
        base["limit_tokens"] = base["limit_tokens"] or lim
        base["remaining_tokens"] = max(0, lim - actual)
    exhausted = is_free_usage_exhausted_message(text, status)
    if not exhausted and m_al:
        actual, lim = int(m_al.group(1)), int(m_al.group(2))
        if lim > 0 and actual >= lim:
            exhausted = True
    if not exhausted and remaining_tokens is not None and limit_tokens is not None:
        if limit_tokens > 0 and remaining_tokens <= 0:
            exhausted = True

    if status in (401, 403) and not exhausted:
        return {
            **base,
            "ok": False,
            "inconclusive": False,
            "error_class": "credential",
            "exhausted": False,
            "free_usage_ok": False,
        }

    if exhausted:
        return {
            **base,
            "ok": True,
            "inconclusive": False,
            "exhausted": True,
            "free_usage_ok": False,
            "error_class": "free_usage_exhausted",
            "limit_tokens": base["limit_tokens"] or 1_000_000,
            "remaining_tokens": 0,
        }

    if 200 <= status < 300:
        return {
            **base,
            "ok": True,
            "inconclusive": False,
            "exhausted": False,
            "free_usage_ok": True,
            "error_class": "",
        }

    if status == 429 and not exhausted:
        return {
            **base,
            "ok": False,
            "inconclusive": True,
            "error_class": "rate_limited",
        }

    if status >= 500:
        return {
            **base,
            "ok": False,
            "inconclusive": True,
            "error_class": f"upstream_{status}",
        }

    return {
        **base,
        "ok": False,
        "inconclusive": True,
        "error_class": f"http_{status}",
    }
