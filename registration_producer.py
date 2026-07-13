"""Continuously feed the account-registration pipeline through the admin API.

This process deliberately talks to the running API instead of importing the
adapter directly.  Registration session state therefore has a single owner and
the API's real concurrency cap covers the complete registration lifecycle.
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

from secure_storage import atomic_write_private_json


BASE_URL = os.getenv("GROK2API_PRODUCER_BASE_URL", "http://grokcli-2api:3000").rstrip("/")
PASSWORD = os.getenv("GROK2API_ADMIN_PASSWORD", "")
_MAIL_PROVIDER_RAW = os.getenv("GROK2API_MAIL_PROVIDER", "moemail").strip().lower()
MAIL_PROVIDER = "yyds" if _MAIL_PROVIDER_RAW == "yydsmail" else _MAIL_PROVIDER_RAW
if MAIL_PROVIDER not in {"moemail", "yyds"}:
    raise ValueError("GROK2API_MAIL_PROVIDER must be moemail or yyds")
YYDSMAIL_DOMAIN = (
    os.getenv("GROK2API_YYDSMAIL_DOMAIN", "").strip().lower().lstrip("@").strip(".")
)
BATCH_SIZE = max(1, int(os.getenv("GROK2API_PRODUCER_BATCH_SIZE", "1")))
CONCURRENCY = max(1, int(os.getenv("GROK2API_PRODUCER_CONCURRENCY", "1")))
STAGGER_MS = max(0, int(os.getenv("GROK2API_PRODUCER_STAGGER_MS", "1500")))
POLL_SEC = max(5.0, float(os.getenv("GROK2API_PRODUCER_POLL_SEC", "15")))
COOLDOWN_SEC = max(0.0, float(os.getenv("GROK2API_PRODUCER_COOLDOWN_SEC", "45")))
BATCH_TIMEOUT_SEC = max(300.0, float(os.getenv("GROK2API_PRODUCER_BATCH_TIMEOUT_SEC", "1800")))
MAX_BACKOFF_SEC = max(60.0, float(os.getenv("GROK2API_PRODUCER_MAX_BACKOFF_SEC", "900")))
HEARTBEAT = Path(os.getenv("GROK2API_PRODUCER_HEARTBEAT", "/tmp/producer-heartbeat"))
PRODUCER_STATE = Path(os.getenv("GROK2API_PRODUCER_STATE_FILE", "/app/data/producer_state.json"))
REGISTRATION_ACTIVE = Path(os.getenv("GROK2API_REGISTRATION_ACTIVE_FILE", "/app/data/registration_active"))
PRODUCER_DOMAINS = tuple(
    dict.fromkeys(
        value.strip().lower()
        for value in os.getenv("GROK2API_PRODUCER_DOMAINS", "").replace(";", ",").split(",")
        if value.strip()
    )
)
DOMAIN_ROTATE_EVERY = max(1, int(os.getenv("GROK2API_PRODUCER_DOMAIN_ROTATE_EVERY", "500")))
TARGET_ACCOUNTS = max(1, int(os.getenv("GROK2API_PRODUCER_TARGET_ACCOUNTS", "5000")))
TARGET_MODEL = os.getenv("GROK2API_PRODUCER_TARGET_MODEL", "grok-4.5").strip()
POOL_CHECK_SEC = max(5.0, float(os.getenv("GROK2API_PRODUCER_POOL_CHECK_SEC", "60")))
CLEANUP_ENABLED = os.getenv("GROK2API_PRODUCER_CLEANUP_ENABLED", "1").lower() in (
    "1", "true", "yes", "on",
)
# Default to observation-only. Production must opt in to destructive cleanup
# after reviewing candidate logs by setting GROK2API_PRODUCER_CLEANUP_DRY_RUN=0.
CLEANUP_DRY_RUN = os.getenv("GROK2API_PRODUCER_CLEANUP_DRY_RUN", "1").lower() in (
    "1", "true", "yes", "on",
)
CLEANUP_MIN_AGE_SEC = max(
    300.0, float(os.getenv("GROK2API_PRODUCER_CLEANUP_MIN_AGE_SEC", "21600"))
)
CLEANUP_CONFIRMATIONS = max(
    2, int(os.getenv("GROK2API_PRODUCER_CLEANUP_CONFIRMATIONS", "3"))
)
CLEANUP_CONFIRM_WINDOW_SEC = max(
    60.0,
    float(os.getenv("GROK2API_PRODUCER_CLEANUP_CONFIRM_WINDOW_SEC", "900")),
)
CLEANUP_BATCH_SIZE = max(
    1, min(2000, int(os.getenv("GROK2API_PRODUCER_CLEANUP_BATCH_SIZE", "100")))
)
# Default ON for pending-recovery container; main registration-producer
# compose should set GROK2API_PENDING_RECOVERY=0 so only one consumer runs.
PENDING_RECOVERY_ENABLED = os.getenv("GROK2API_PENDING_RECOVERY", "1").lower() in (
    "1",
    "true",
    "yes",
    "on",
)
PENDING_DIR = Path(os.getenv("GROK2API_PENDING_SSO_DIR", "/app/data/pending_sso"))
PENDING_MIN_AGE_SEC = max(0.0, float(os.getenv("GROK2API_PENDING_MIN_AGE_SEC", "120")))
PENDING_MAX_PER_CYCLE = max(1, int(os.getenv("GROK2API_PENDING_MAX_PER_CYCLE", "2")))
PENDING_RETRY_BASE_SEC = max(30.0, float(os.getenv("GROK2API_PENDING_RETRY_BASE_SEC", "120")))
PENDING_RETRY_MAX_SEC = max(
    PENDING_RETRY_BASE_SEC,
    float(os.getenv("GROK2API_PENDING_RETRY_MAX_SEC", "3600")),
)
PENDING_SUCCESS_COOLDOWN_SEC = max(
    0.0, float(os.getenv("GROK2API_PENDING_SUCCESS_COOLDOWN_SEC", "5"))
)

# Intentionally process-local: a restart resets the confirmation window rather
# than allowing stale observations to delete an account.
_cleanup_observations: dict[str, dict[str, Any]] = {}


class _SecretRedactingWriter:
    """Forward converter diagnostics without ever emitting the pending SSO."""

    _jwt_pattern = re.compile(r"eyJ[A-Za-z0-9_-]{20,}(?:\.[A-Za-z0-9_-]+){0,2}")

    def __init__(self, target: Any, secret: str) -> None:
        self.target = target
        self.secret = secret

    def write(self, value: str) -> int:
        clean = value.replace(self.secret, "<redacted-sso>") if self.secret else value
        clean = self._jwt_pattern.sub("<redacted-jwt>", clean)
        self.target.write(clean)
        return len(value)

    def flush(self) -> None:
        self.target.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.target, name)


def _atomic_write_pending(path: Path, payload: dict[str, Any]) -> None:
    """Persist retry metadata without weakening the SSO file permissions."""
    atomic_write_private_json(path, payload)



def _claim_pending_file(path: Path) -> Path | None:
    """Atomically rename pending -> .processing.<pid> so only one consumer owns it."""
    if ".processing." in path.name:
        return None
    claimed = path.with_name(f"{path.stem}.processing.{os.getpid()}{path.suffix}")
    try:
        os.rename(path, claimed)
        return claimed
    except OSError:
        return None


def _release_claimed_pending(claimed: Path, original: Path, payload: dict[str, Any]) -> None:
    """Write backoff payload back to original name after failed recovery."""
    try:
        _atomic_write_pending(claimed, payload)
        os.rename(claimed, original)
    except OSError:
        try:
            if claimed.exists() and not original.exists():
                os.rename(claimed, original)
        except OSError:
            pass


def _reclaim_stale_processing(pending_dir: Path, *, max_age_sec: float = 3600.0) -> int:
    """Recover processing files left by crashed workers."""
    now = time.time()
    n = 0
    for path in pending_dir.glob("*.processing.*.json"):
        try:
            age = now - path.stat().st_mtime
            if age < max_age_sec:
                continue
            # strip .processing.<pid>
            name = path.name
            # e.g. gba.processing.123.json -> gba.json
            parts = name.split(".processing.")
            if len(parts) != 2:
                continue
            stem = parts[0]
            restored = path.with_name(f"{stem}.json")
            if restored.exists():
                path.unlink(missing_ok=True)
            else:
                os.rename(path, restored)
            n += 1
        except OSError:
            continue
    return n

def _pending_created_at(path: Path, payload: dict[str, Any]) -> float:
    try:
        return float(payload.get("created_at") or path.stat().st_mtime)
    except (OSError, TypeError, ValueError):
        return 0.0


def _pending_is_eligible(path: Path, payload: dict[str, Any], now: float) -> bool:
    """Skip live adapter files and entries still inside their retry backoff."""
    if now - _pending_created_at(path, payload) < PENDING_MIN_AGE_SEC:
        return False
    try:
        return float(payload.get("next_recovery_at") or 0) <= now
    except (TypeError, ValueError):
        return True


def _safe_log_label(value: Any, fallback: str = "unknown") -> str:
    """Keep untrusted persisted fields from injecting secrets/newlines into logs."""
    clean = re.sub(r"[^A-Za-z0-9_.-]", "_", str(value or ""))[:96]
    return clean or fallback


def _recover_pending_file(path: Path, payload: dict[str, Any], *, now: float) -> bool:
    """Convert one persisted SSO, deleting it only after a successful import."""
    # mint_queue ownership: skip only when an active mint job still exists.
    try:
        from registration_queue import RegistrationQueue, pending_owned_by_mint_queue, pipeline_v2_enabled

        if pending_owned_by_mint_queue(payload) and pipeline_v2_enabled():
            sid = str(payload.get("session_id") or path.stem)
            # strip .processing.<pid> from stem for session lookup
            sid = sid.split(".processing.")[0]
            if RegistrationQueue().has_active_job_for_session(sid):
                print(
                    f"[pending-recovery] skip mint-queue-owned name={_safe_log_label(path.name)}",
                    flush=True,
                )
                return False
            # No active job: treat as legacy recovery (pipeline rollback / orphan).
            payload = dict(payload)
            payload["owner"] = "legacy_recovery"
            payload["pipeline_v2"] = False
    except Exception:
        pass

    raw_sso = payload.get("sso")
    sso = raw_sso if isinstance(raw_sso, str) else ""
    writer = _SecretRedactingWriter(sys.stdout, sso)
    try:
        if not sso.strip():
            raise ValueError("pending entry has no SSO")
        # Imported lazily so ordinary producer startup remains lightweight and
        # unit tests can patch the conversion boundary without network clients.
        import sso_to_auth_json as sso_import

        with redirect_stdout(writer), redirect_stderr(writer):
            token = sso_import.sso_to_token(sso)
            if not token or not token.get("access_token"):
                raise RuntimeError("device flow did not return an access token")
            _key, entry = sso_import.token_to_auth_entry(
                token, email=str(payload.get("email") or "")
            )
            sso_import.import_into_project_auth(entry)
        path.unlink(missing_ok=True)
        return True
    except Exception as exc:
        try:
            previous_attempts = max(0, int(payload.get("recovery_attempts") or 0))
        except (TypeError, ValueError):
            previous_attempts = 0
        attempts = previous_attempts + 1
        delay = min(PENDING_RETRY_MAX_SEC, PENDING_RETRY_BASE_SEC * (2 ** min(attempts - 1, 8)))
        retry_payload = dict(payload)
        retry_payload.update(
            {
                "recovery_attempts": attempts,
                "last_recovery_at": now,
                "next_recovery_at": now + delay,
                # Store only a coarse class, never exception text: third-party
                # libraries can embed cookies/tokens in exception messages.
                "last_recovery_error": type(exc).__name__,
            }
        )
        if path.exists():
            _atomic_write_pending(path, retry_payload)
        return False


def _recover_pending_sso(*, now: float | None = None) -> tuple[int, int]:
    """Recover a bounded number of old pending SSO files, strictly serially.

    Uses atomic rename claim so two recovery processes cannot convert the same
    file. mint_queue-owned files are skipped only when an active queue job
    exists; otherwise they are eligible (pipeline rollback / orphan repair).
    """
    if not PENDING_RECOVERY_ENABLED or not PENDING_DIR.is_dir():
        return 0, 0
    current = time.time() if now is None else now
    try:
        _reclaim_stale_processing(PENDING_DIR)
    except Exception:
        pass

    candidates: list[tuple[float, Path, dict[str, Any]]] = []
    for path in PENDING_DIR.glob("*.json"):
        if ".processing." in path.name:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or not _pending_is_eligible(path, payload, current):
                continue
            owner = str(payload.get("owner") or "")
            if owner == "mint_queue" or payload.get("pipeline_v2") is True:
                # Skip only if mint queue still has an active job for session
                try:
                    from registration_queue import RegistrationQueue, pipeline_v2_enabled

                    if pipeline_v2_enabled():
                        sid = str(payload.get("session_id") or path.stem)
                        if RegistrationQueue().has_active_job_for_session(sid):
                            print(
                                f"[pending-recovery] skip mint-queue-owned name={_safe_log_label(path.name)}",
                                flush=True,
                            )
                            continue
                except Exception:
                    # If queue unavailable and pipeline v2 unknown, skip mint-owned to be safe
                    if owner == "mint_queue":
                        print(
                            f"[pending-recovery] skip mint-queue-owned name={_safe_log_label(path.name)}",
                            flush=True,
                        )
                        continue
            candidates.append((_pending_created_at(path, payload), path, payload))
        except (OSError, json.JSONDecodeError):
            print(
                f"[pending-recovery] skipped unreadable file name={_safe_log_label(path.name)}",
                flush=True,
            )

    recovered = failed = 0
    for _created, path, payload in sorted(candidates, key=lambda item: (item[0], item[1].name))[
        :PENDING_MAX_PER_CYCLE
    ]:
        session_id = _safe_log_label(payload.get("session_id") or path.stem)
        claimed = _claim_pending_file(path)
        if claimed is None:
            continue
        print(f"[pending-recovery] attempting session={session_id}", flush=True)
        _touch()
        # Recover from claimed path
        try:
            if _recover_pending_file(claimed, payload, now=current):
                recovered += 1
                print(f"[pending-recovery] imported session={session_id}", flush=True)
                try:
                    claimed.unlink(missing_ok=True)
                except OSError:
                    pass
                if PENDING_SUCCESS_COOLDOWN_SEC:
                    time.sleep(PENDING_SUCCESS_COOLDOWN_SEC)
            else:
                failed += 1
                # payload may have been updated on claimed path
                try:
                    payload = json.loads(claimed.read_text(encoding="utf-8"))
                except Exception:
                    pass
                _release_claimed_pending(claimed, path, payload if isinstance(payload, dict) else {})
                print(f"[pending-recovery] retained session={session_id} for backoff", flush=True)
        except Exception:
            failed += 1
            _release_claimed_pending(claimed, path, payload if isinstance(payload, dict) else {})
        _touch()
    return recovered, failed



def _request(method: str, path: str, *, token: str = "", body: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["X-Admin-Token"] = token
    req = urllib.request.Request(BASE_URL + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=60) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise RuntimeError(f"unexpected response from {path}")
    return payload


def _login() -> str:
    if not PASSWORD:
        raise RuntimeError("GROK2API_ADMIN_PASSWORD is required by registration producer")
    result = _request("POST", "/admin/api/login", body={"password": PASSWORD})
    token = str(result.get("token") or "")
    if not token:
        raise RuntimeError("admin login returned no session token")
    return token


def _touch() -> None:
    try:
        HEARTBEAT.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT.touch()
    except OSError:
        # Sandbox / unit tests may not allow /tmp heartbeats; never crash recovery.
        pass


def _set_registration_active(active: bool) -> None:
    if active:
        REGISTRATION_ACTIVE.parent.mkdir(parents=True, exist_ok=True)
        REGISTRATION_ACTIVE.touch()
    else:
        REGISTRATION_ACTIVE.unlink(missing_ok=True)


def _producer_state() -> dict[str, Any]:
    try:
        value = json.loads(PRODUCER_STATE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _save_producer_state(state: dict[str, Any]) -> None:
    PRODUCER_STATE.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_pending(PRODUCER_STATE, state)


def _registration_domain() -> str | None:
    if MAIL_PROVIDER == "yyds":
        # Empty means YYDS chooses a healthy domain server-side. Never inherit
        # MoeMail's domain list when the active provider is YYDS.
        return YYDSMAIL_DOMAIN or None
    if not PRODUCER_DOMAINS:
        return None
    imported_lifetime = max(0, _safe_int(_producer_state().get("imported_lifetime")))
    return PRODUCER_DOMAINS[(imported_lifetime // DOMAIN_ROTATE_EVERY) % len(PRODUCER_DOMAINS)]


def _record_imported(count: int) -> None:
    if count <= 0:
        return
    state = _producer_state()
    state["imported_lifetime"] = max(
        0, _safe_int(state.get("imported_lifetime"))
    ) + count
    state["updated_at"] = time.time()
    _save_producer_state(state)


def _batch_result(
    *,
    imported: int = 0,
    errors: int = 0,
    mint_queued: int = 0,
    running: int = 0,
    total: int = 0,
) -> dict[str, int]:
    """Structured registration-stage outcome (not pool import)."""
    return {
        "imported": int(imported),
        "errors": int(errors),
        "mint_queued": int(mint_queued),
        "running": int(running),
        "total": int(total),
        "signup_done": int(imported) + int(errors) + int(mint_queued),
    }


def _wait_batch(
    token: str, batch_id: str, *, expected_count: int | None = None
) -> dict[str, int]:
    """Wait until signup stage finishes (imported | error | mint_queued).

    Pipeline v2: mint_queued means SSO was handed to the mint queue — free the
    producer slot immediately without waiting for device-flow import.
    """
    deadline = time.monotonic() + BATCH_TIMEOUT_SEC
    while time.monotonic() < deadline:
        result = _request(
            "GET",
            f"/admin/api/accounts/register-email/batches/{batch_id}",
            token=token,
        )
        imported = int(result.get("imported") or 0)
        errors = int(result.get("error") or 0)
        mint_queued = int(result.get("mint_queued") or 0)
        running = int(result.get("running") or 0)
        total = int(result.get("total") or result.get("count") or expected_count or BATCH_SIZE)
        # If adapter reports fewer sessions than requested (spawn errors),
        # still complete when running==0 and done covers reported total.
        done_field = int(result.get("done") or 0)
        batch_status = str(result.get("batch_status") or "")
        _touch()
        signup_done = max(imported + errors + mint_queued, done_field)
        if running == 0 and (signup_done >= total or batch_status in ("done", "partial", "error")):
            return _batch_result(
                imported=imported,
                errors=errors,
                mint_queued=mint_queued,
                running=running,
                total=total,
            )
        time.sleep(POLL_SEC)
    raise TimeoutError(f"registration batch {batch_id} exceeded {BATCH_TIMEOUT_SEC:.0f}s")


def _wait_session(token: str, sid: str) -> dict[str, int]:
    """Wait one registration session; mint_queued counts as signup handoff."""
    deadline = time.monotonic() + BATCH_TIMEOUT_SEC
    while time.monotonic() < deadline:
        status = _request(
            "GET",
            f"/admin/api/accounts/register-email/sessions/{sid}",
            token=token,
        )
        _touch()
        state = str(status.get("status") or "")
        if state in ("imported", "success", "completed"):
            return _batch_result(imported=1, total=1)
        if state == "mint_queued":
            return _batch_result(mint_queued=1, total=1)
        if state in ("error", "failed", "expired"):
            return _batch_result(errors=1, total=1)
        time.sleep(POLL_SEC)
    raise TimeoutError(f"registration session {sid} timed out")


def _queue_open_count() -> int:
    """Best-effort open mint jobs for backpressure (0 if queue unavailable)."""
    try:
        from registration_queue import RegistrationQueue, pipeline_v2_enabled

        if not pipeline_v2_enabled():
            return 0
        return RegistrationQueue().count_open()
    except Exception:
        return 0


def _queue_remaining() -> int:
    try:
        from registration_queue import RegistrationQueue, hard_limit, pipeline_v2_enabled

        if not pipeline_v2_enabled():
            return 10**9
        q = RegistrationQueue()
        return max(0, hard_limit() - q.count_open())
    except Exception:
        return 10**9


def _as_timestamp(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _is_effective_account(row: dict[str, Any]) -> bool:
    """Count only enabled, live, renewable, non-quota-disabled accounts."""
    blocked = row.get("blocked_model_ids") or []
    if not isinstance(blocked, list):
        blocked = []
    return bool(
        not row.get("expired")
        and row.get("enabled", True) is not False
        and not row.get("disabled_for_quota")
        and not row.get("quota_waiting")
        and not row.get("credential_suspended")
        and row.get("refresh_status") != "refresh_terminal"
        and row.get("has_refresh_token")
        and (not TARGET_MODEL or TARGET_MODEL not in blocked)
    )


def _pool_snapshot(token: str) -> dict[str, Any]:
    """Fetch `/accounts` and merge auth lifecycle and pool metadata by id."""
    result = _request("GET", "/admin/api/accounts", token=token)
    account_rows = result.get("accounts")
    pool = result.get("pool")
    pool_rows = pool.get("accounts") if isinstance(pool, dict) else None
    if not isinstance(account_rows, list) or not isinstance(pool_rows, list):
        raise RuntimeError("admin accounts response is missing account pool rows")

    merged: dict[str, dict[str, Any]] = {}
    for raw in account_rows:
        if not isinstance(raw, dict):
            continue
        account_id = str(raw.get("id") or "").strip()
        if account_id:
            merged[account_id] = dict(raw)
    for raw in pool_rows:
        if not isinstance(raw, dict):
            continue
        account_id = str(raw.get("id") or "").strip()
        if account_id:
            merged.setdefault(account_id, {}).update(raw)

    rows = list(merged.values())
    return {
        "total": len(rows),
        "effective": sum(1 for row in rows if _is_effective_account(row)),
        "rows": rows,
    }


def _cleanup_reason(row: dict[str, Any]) -> tuple[str, float | None] | None:
    """Return only durable account-wide failures safe to consider deleting."""
    if (
        row.get("refresh_status") == "refresh_terminal"
        and _safe_int(row.get("refresh_failure_count")) >= 3
        and row.get("refresh_confirmed_after_expiry") is True
        and _as_timestamp(row.get("refresh_terminal_at")) is not None
    ):
        return "refresh_terminal", _as_timestamp(row.get("refresh_terminal_at"))
    # Quota is eligible only after the quota state machine persisted every
    # evidence gate.  A plain waiting flag is never sufficient.
    if (
        row.get("quota_status") == "quota_reset_failed"
        and row.get("quota_cycle_id")
        and row.get("quota_confirmation_cycle_id") == row.get("quota_cycle_id")
        and _safe_int(row.get("quota_confirmation_count")) >= 3
        and _safe_int(row.get("quota_grace_count")) >= 2
        and _as_timestamp(row.get("quota_first_confirm_at")) is not None
        and _as_timestamp(row.get("quota_last_confirm_at")) is not None
        and row.get("quota_last_evidence") == "free_usage_exhausted"
        and _as_timestamp(row.get("quota_terminal_at")) is not None
    ):
        return "quota_reset_failed", _as_timestamp(row.get("quota_terminal_at"))
    if row.get("quota_waiting") or row.get("disabled_for_quota"):
        return None
    if row.get("expired") and not row.get("has_refresh_token"):
        return "expired_without_refresh_token", _as_timestamp(row.get("expires_at"))
    # Manual disable, cooldown, transient probes, and model-only blocks are not
    # permanent account failures and must never be cleaned here.
    return None


def _cleanup_accounts(token: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Delete confirmed permanent failures behind age and observation gates."""
    now = time.time()
    present: set[str] = set()
    ready: list[tuple[str, str]] = []

    for row in rows:
        account_id = str(row.get("id") or "").strip()
        reason_info = _cleanup_reason(row)
        if not account_id or reason_info is None:
            continue
        reason, marked_at = reason_info
        present.add(account_id)
        observation = _cleanup_observations.get(account_id)
        if observation is None or observation.get("reason") != reason:
            observation = {
                "reason": reason,
                "first_seen": now,
                "last_seen": now,
                "confirmations": 1,
            }
        else:
            observation["last_seen"] = now
            observation["confirmations"] = _safe_int(
                observation.get("confirmations")
            ) + 1
        _cleanup_observations[account_id] = observation

        durable_since = marked_at or float(observation["first_seen"])
        old_enough = now - durable_since >= CLEANUP_MIN_AGE_SEC
        observed_long_enough = (
            now - float(observation["first_seen"]) >= CLEANUP_CONFIRM_WINDOW_SEC
        )
        confirmed = _safe_int(observation.get("confirmations")) >= CLEANUP_CONFIRMATIONS
        if old_enough and observed_long_enough and confirmed:
            ready.append((account_id, reason))

    # Recovery resets the evidence. If it later fails again, require a fresh
    # full confirmation window before deletion.
    for account_id in list(_cleanup_observations):
        if account_id not in present:
            _cleanup_observations.pop(account_id, None)

    ready = ready[:CLEANUP_BATCH_SIZE]
    if not CLEANUP_ENABLED or not ready:
        return {"candidates": len(present), "ready": len(ready), "removed": 0}

    ids = [account_id for account_id, _ in ready]
    reasons = {account_id: reason for account_id, reason in ready}
    if CLEANUP_DRY_RUN:
        print(
            "[registration-producer] cleanup dry-run ready="
            f"{len(ids)} reasons={json.dumps(reasons, ensure_ascii=False)}",
            flush=True,
        )
        return {
            "candidates": len(present),
            "ready": len(ids),
            "removed": 0,
            "dry_run": True,
        }

    result = _request(
        "POST", "/admin/api/accounts/delete-batch", token=token, body={"ids": ids}
    )
    removed_ids = [str(x) for x in (result.get("removed") or [])]
    for account_id in removed_ids:
        _cleanup_observations.pop(account_id, None)
    removed = int(result.get("removed_count") or len(removed_ids))
    print(
        f"[registration-producer] cleanup removed={removed} requested={len(ids)}",
        flush=True,
    )
    return {
        "candidates": len(present),
        "ready": len(ids),
        "removed": removed,
        "dry_run": False,
    }


def run_forever() -> None:
    token = ""
    failures = 0
    try:
        adaptive_concurrency = max(
            1, min(CONCURRENCY, int(_producer_state().get("adaptive_concurrency") or CONCURRENCY))
        )
    except (TypeError, ValueError):
        adaptive_concurrency = CONCURRENCY
    clean_batch_streak = 0
    while True:
        try:
            try:
                import maintenance_retention

                retention = maintenance_retention.run_if_due()
                if retention.get("ran"):
                    print(
                        "[registration-producer] retention "
                        f"queue={retention.get('queue_terminal_deleted', 0)} "
                        f"metrics={retention.get('metrics_deleted', 0)} "
                        f"cookie={retention.get('cookie_deleted', 0)} "
                        f"pending={retention.get('pending_deleted', 0)} "
                        f"ok={retention.get('ok', False)}",
                        flush=True,
                    )
            except Exception as exc:  # noqa: BLE001
                print(
                    "[registration-producer] retention error="
                    f"{type(exc).__name__}",
                    flush=True,
                )
            recovered, recovery_failed = _recover_pending_sso()
            _record_imported(recovered)
            if recovered or recovery_failed:
                print(
                    f"[registration-producer] pending recovered={recovered} failed={recovery_failed}",
                    flush=True,
                )
            token = token or _login()
            snapshot = _pool_snapshot(token)
            cleanup = _cleanup_accounts(token, snapshot["rows"])
            effective = int(snapshot["effective"])
            open_jobs = _queue_open_count()
            remaining_q = _queue_remaining()
            # Subtract open mint jobs not yet in pool so we don't over-register
            gap = max(0, TARGET_ACCOUNTS - effective - open_jobs)
            print(
                "[registration-producer] pool "
                f"effective={effective}/{TARGET_ACCOUNTS} total={snapshot['total']} "
                f"gap={gap} queue_open={open_jobs} queue_remaining={remaining_q} "
                f"cleanup_candidates={cleanup['candidates']} "
                f"cleanup_removed={cleanup['removed']} concurrency={adaptive_concurrency}",
                flush=True,
            )
            _touch()
            if gap <= 0:
                failures = 0
                time.sleep(POOL_CHECK_SEC)
                continue

            # Backpressure BEFORE signup (pipeline v2)
            try:
                from registration_queue import hard_limit, pipeline_v2_enabled, soft_limit

                if pipeline_v2_enabled():
                    if open_jobs >= hard_limit() or remaining_q <= 0:
                        print(
                            "[registration-producer] queue hard full; pause new signup",
                            flush=True,
                        )
                        time.sleep(POOL_CHECK_SEC)
                        continue
                    if open_jobs >= soft_limit():
                        adaptive_concurrency = 1
            except Exception:
                remaining_q = 10**9

            requested_count = min(BATCH_SIZE, gap, max(1, remaining_q))
            run_concurrency = min(adaptive_concurrency, requested_count)
            domain = _registration_domain()
            print(
                f"[registration-producer] mail_provider={MAIL_PROVIDER} "
                f"mailbox_domain={domain or 'auto'}",
                flush=True,
            )
            _set_registration_active(True)
            started = _request(
                "POST",
                "/admin/api/accounts/register-email",
                token=token,
                body={
                    "provider": MAIL_PROVIDER,
                    "protocol": "grpc",
                    "count": requested_count,
                    "concurrency": run_concurrency,
                    "stagger_ms": STAGGER_MS,
                    **({"domain": domain} if domain else {}),
                },
            )
            batch_id = str(started.get("batch_id") or "")
            if batch_id:
                outcome = _wait_batch(
                    token, batch_id, expected_count=requested_count
                )
            else:
                sid = str(started.get("id") or "")
                if not sid:
                    raise RuntimeError("registration start returned no batch/session id")
                outcome = _wait_session(token, sid)
            imported = int(outcome.get("imported") or 0)
            errors = int(outcome.get("errors") or 0)
            mint_queued = int(outcome.get("mint_queued") or 0)
            print(
                "[registration-producer] finished "
                f"imported={imported} mint_queued={mint_queued} errors={errors} "
                f"signup_handoff={imported + mint_queued}",
                flush=True,
            )
            _set_registration_active(False)
            # Only true pool imports count toward lifetime / adaptive success.
            _record_imported(imported)
            if errors and not imported and not mint_queued:
                adaptive_concurrency = 1
                state = _producer_state()
                state["adaptive_concurrency"] = adaptive_concurrency
                _save_producer_state(state)
                clean_batch_streak = 0
                print("[registration-producer] adaptive concurrency reduced to 1", flush=True)
            elif errors == 0 and (imported + mint_queued) >= requested_count:
                # Pipeline v2 success is mint_queued handoff; allow concurrency recovery.
                clean_batch_streak += 1
                if adaptive_concurrency < CONCURRENCY and clean_batch_streak >= 3:
                    adaptive_concurrency += 1
                    state = _producer_state()
                    state["adaptive_concurrency"] = adaptive_concurrency
                    _save_producer_state(state)
                    clean_batch_streak = 0
                    print(
                        f"[registration-producer] adaptive concurrency increased to {adaptive_concurrency}",
                        flush=True,
                    )
            else:
                # mint_queued handoff is success for signup but not import streak
                clean_batch_streak = 0
            failures = failures + 1 if errors and not imported and not mint_queued else 0
            _touch()
            batch_cooldown = COOLDOWN_SEC
            if failures:
                batch_cooldown = min(
                    MAX_BACKOFF_SEC,
                    max(60.0, COOLDOWN_SEC * (2 ** min(failures, 5))),
                )
                print(
                    f"[registration-producer] failed-batch cooldown={batch_cooldown:.0f}s",
                    flush=True,
                )
            time.sleep(batch_cooldown + random.uniform(0, min(15.0, batch_cooldown * 0.25)))
        except urllib.error.HTTPError as exc:
            _set_registration_active(False)
            # Admin sessions can expire; force a fresh login on the next pass.
            if exc.code in (401, 403):
                token = ""
            failures += 1
            delay = min(MAX_BACKOFF_SEC, 30.0 * (2 ** min(failures - 1, 5)))
            print(f"[registration-producer] HTTP {exc.code}; retry in {delay:.0f}s", flush=True)
            _touch()
            time.sleep(delay)
        except Exception as exc:  # noqa: BLE001
            _set_registration_active(False)
            failures += 1
            delay = min(MAX_BACKOFF_SEC, 30.0 * (2 ** min(failures - 1, 5)))
            print(f"[registration-producer] {type(exc).__name__}: {exc}; retry in {delay:.0f}s", flush=True)
            _touch()
            time.sleep(delay)


def run_pending_recovery_forever() -> None:
    """Drain pending SSO while sharing free per-approver slots with registration."""
    time.sleep(max(0.0, float(os.getenv("GROK2API_PENDING_STARTUP_DELAY_SEC", "20"))))
    while True:
        recovered, failed = _recover_pending_sso()
        if recovered or failed:
            print(
                f"[pending-recovery] cycle recovered={recovered} failed={failed}",
                flush=True,
            )
        _touch()
        # A short idle poll lets recovery claim a sidecar as soon as one of the
        # two registration flows releases it. Per-sidecar file leases prevent
        # contention, so the old registration_active/60s gate is unnecessary.
        time.sleep(2 if not recovered else 5)


def run_mint_worker_forever() -> None:
    """Pipeline v2 mint worker: claim jobs from SQLite queue and mint tokens."""
    from registration_controller import RegistrationController

    worker = RegistrationController(
        worker_id=os.getenv("GROK2API_MINT_WORKER_ID", f"mint-{os.getpid()}")
    )
    print(f"[mint-worker] starting worker_id={worker.worker_id}", flush=True)
    worker.run_forever(idle_sec=max(0.5, float(os.getenv("GROK2API_MINT_IDLE_SEC", "2"))))


if __name__ == "__main__":
    mode = os.getenv("GROK2API_PRODUCER_MODE", "producer").strip().lower()
    if mode == "pending":
        run_pending_recovery_forever()
    elif mode in ("mint", "mint_worker", "pipeline_v2"):
        run_mint_worker_forever()
    else:
        run_forever()
