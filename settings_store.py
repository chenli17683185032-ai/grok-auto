"""Admin settings (password hash, flags, account pool)."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from typing import Any

from config import ACCOUNT_MODE, ADMIN_PASSWORD, DATA_DIR, SETTINGS_FILE
from secure_storage import atomic_write_private_json, ensure_private_dir

_lock = threading.RLock()

# All modes treat accounts equally — no "primary" account concept.
VALID_ACCOUNT_MODES = ("round_robin", "random", "least_used")
DEFAULT_ACCOUNT_MODE = "round_robin"
# Legacy mode name migrated to round_robin
_LEGACY_MODES = {"primary": "round_robin"}

# In-memory settings cache + deferred dirty flush so probe/refresh of hundreds
# of accounts doesn't rewrite settings.json on every single account touch.
_mem: dict[str, Any] | None = None
_mem_dirty = False
_flush_timer: threading.Timer | None = None
_FLUSH_DELAY_SEC = 1.0


class SettingsStoreCorrupt(RuntimeError):
    """An existing settings file cannot be trusted as application state."""


def _ensure() -> None:
    ensure_private_dir(DATA_DIR)


def _load_disk() -> dict[str, Any]:
    _ensure()
    if not SETTINGS_FILE.is_file():
        return {}
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SettingsStoreCorrupt("existing settings store is unreadable") from exc
    if not isinstance(data, dict):
        raise SettingsStoreCorrupt("existing settings store has an invalid root")
    return data


def _load() -> dict[str, Any]:
    """Return the live in-memory settings map (lazy-loaded once)."""
    global _mem
    with _lock:
        if _mem is None:
            _mem = _load_disk()
        return _mem


def _write_disk(data: dict[str, Any]) -> None:
    _ensure()
    atomic_write_private_json(SETTINGS_FILE, data, pretty=True)


def _schedule_flush_locked() -> None:
    global _flush_timer, _mem_dirty
    _mem_dirty = True
    if _flush_timer is not None:
        return

    def _flush() -> None:
        global _flush_timer, _mem_dirty
        with _lock:
            _flush_timer = None
            if not _mem_dirty or _mem is None:
                _mem_dirty = False
                return
            snapshot = json.loads(json.dumps(_mem))  # deep-ish copy via json
            _mem_dirty = False
        try:
            _write_disk(snapshot)
        except Exception:
            # re-mark dirty so a later touch retries
            with _lock:
                _mem_dirty = True
                if _flush_timer is None:
                    _schedule_flush_locked()

    t = threading.Timer(_FLUSH_DELAY_SEC, _flush)
    t.daemon = True
    _flush_timer = t
    t.start()


def _save(data: dict[str, Any], *, immediate: bool = False) -> None:
    """
    Persist settings. Default is coalesced (1s) to avoid thrashing disk when
    model probes / quota checks touch hundreds of pool entries.
    """
    global _mem
    with _lock:
        _mem = data
        if immediate:
            # cancel pending timer
            global _flush_timer, _mem_dirty
            if _flush_timer is not None:
                try:
                    _flush_timer.cancel()
                except Exception:
                    pass
                _flush_timer = None
            _mem_dirty = False
            snapshot = json.loads(json.dumps(data))
            _write_disk(snapshot)
        else:
            _schedule_flush_locked()


def flush_settings() -> None:
    """Force any deferred settings writes to disk (call on shutdown if needed)."""
    global _flush_timer, _mem_dirty
    with _lock:
        if _flush_timer is not None:
            try:
                _flush_timer.cancel()
            except Exception:
                pass
            _flush_timer = None
        if _mem is None:
            _mem_dirty = False
            return
        snapshot = json.loads(json.dumps(_mem))
        _mem_dirty = False
    _write_disk(snapshot)


def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000
    ).hex()


def is_setup_needed() -> bool:
    if ADMIN_PASSWORD:
        return False
    data = _load()
    return not data.get("admin_password_hash")


def has_admin_password() -> bool:
    if ADMIN_PASSWORD:
        return True
    data = _load()
    return bool(data.get("admin_password_hash"))


def set_admin_password(password: str) -> None:
    if len(password) < 4:
        raise ValueError("密码至少 4 位")
    salt = secrets.token_hex(16)
    with _lock:
        data = _load()
        data["admin_password_hash"] = _hash_password(password, salt)
        data["admin_password_salt"] = salt
        data["updated_at"] = time.time()
        _save(data, immediate=True)


def verify_admin_password(password: str) -> bool:
    if not password:
        return False
    # Env password always works if set
    if ADMIN_PASSWORD and secrets.compare_digest(password, ADMIN_PASSWORD):
        return True
    data = _load()
    salt = data.get("admin_password_salt")
    expected = data.get("admin_password_hash")
    if not salt or not expected:
        return False
    got = _hash_password(password, salt)
    return hmac.compare_digest(got, expected)


def create_session_token() -> str:
    token = secrets.token_urlsafe(32)
    with _lock:
        data = _load()
        sessions = data.setdefault("sessions", {})
        now = time.time()
        sessions = {
            k: v
            for k, v in sessions.items()
            if isinstance(v, (int, float)) and now - float(v) < 7 * 86400
        }
        sessions[token] = now
        data["sessions"] = sessions
        _save(data, immediate=True)
    return token


def verify_session_token(token: str | None) -> bool:
    if not token:
        return False
    with _lock:
        data = _load()
        sessions = data.get("sessions") or {}
        ts = sessions.get(token)
        if ts is None:
            return False
        if time.time() - float(ts) > 7 * 86400:
            sessions.pop(token, None)
            data["sessions"] = sessions
            _save(data, immediate=True)
            return False
        # sliding refresh — coalesce to avoid rewrite-per-request
        sessions[token] = time.time()
        data["sessions"] = sessions
        _save(data, immediate=False)
        return True


def revoke_session(token: str | None) -> None:
    if not token:
        return
    with _lock:
        data = _load()
        sessions = data.get("sessions") or {}
        if token in sessions:
            sessions.pop(token, None)
            data["sessions"] = sessions
            _save(data, immediate=True)


def _normalize_mode(mode: str | None) -> str:
    mode = (mode or "").strip().lower()
    mode = _LEGACY_MODES.get(mode, mode)
    if mode not in VALID_ACCOUNT_MODES:
        return DEFAULT_ACCOUNT_MODE
    return mode


def get_account_mode() -> str:
    # Env override wins when set
    if ACCOUNT_MODE:
        return _normalize_mode(ACCOUNT_MODE)
    data = _load()
    return _normalize_mode(str(data.get("account_mode") or DEFAULT_ACCOUNT_MODE))


def set_account_mode(mode: str) -> str:
    raw = (mode or "").strip().lower()
    raw = _LEGACY_MODES.get(raw, raw)
    if raw not in VALID_ACCOUNT_MODES:
        raise ValueError(
            f"Invalid account_mode. Use one of: {', '.join(VALID_ACCOUNT_MODES)}"
        )
    mode = raw
    with _lock:
        data = _load()
        data["account_mode"] = mode
        # Drop legacy preferred-account setting if present
        data.pop("preferred_account_id", None)
        data["updated_at"] = time.time()
        _save(data, immediate=True)
    return mode


def get_account_pool_state() -> dict[str, Any]:
    data = _load()
    pool = data.get("account_pool") or {}
    # Return a shallow copy so callers can mutate without racing the cache
    return dict(pool) if isinstance(pool, dict) else {}


def save_account_pool_state(state: dict[str, Any]) -> None:
    with _lock:
        data = _load()
        data["account_pool"] = state
        data["updated_at"] = time.time()
        # Coalesce: model probe / quota may update hundreds of entries
        _save(data, immediate=False)


def touch_account_stats(
    account_id: str,
    *,
    success: bool = True,
    error: str = "",
    cooldown_until: float | None = None,
    clear_cooldown: bool = False,
) -> None:
    with _lock:
        data = _load()
        pool = data.setdefault("account_pool", {})
        if not isinstance(pool, dict):
            pool = {}
            data["account_pool"] = pool
        meta = pool.get(account_id) or {}
        if not isinstance(meta, dict):
            meta = {}
        else:
            meta = dict(meta)
        meta.setdefault("enabled", True)
        meta.setdefault("weight", 1)
        meta["request_count"] = int(meta.get("request_count") or 0) + 1
        meta["last_used_at"] = time.time()
        if success:
            meta["success_count"] = int(meta.get("success_count") or 0) + 1
            meta.pop("last_error", None)
            if clear_cooldown:
                meta.pop("cooldown_until", None)
        else:
            meta["fail_count"] = int(meta.get("fail_count") or 0) + 1
            if error:
                meta["last_error"] = error
            if cooldown_until is not None:
                meta["cooldown_until"] = float(cooldown_until)
        pool[account_id] = meta
        data["updated_at"] = time.time()
        _save(data, immediate=False)


def get_public_settings() -> dict[str, Any]:
    data = _load()
    return {
        "account_mode": get_account_mode(),
        "has_admin_password": has_admin_password(),
        "setup_needed": is_setup_needed(),
        "admin_password_from_env": bool(ADMIN_PASSWORD),
        "updated_at": data.get("updated_at"),
    }
