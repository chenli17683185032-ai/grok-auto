"""Managed API key store for client distribution."""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from config import API_KEY, KEYS_FILE, REQUIRE_API_KEY
from secure_storage import atomic_write_private_json, ensure_private_dir


_lock = threading.RLock()


class KeyStoreCorrupt(RuntimeError):
    """An existing key store is unreadable and must not be overwritten."""


@dataclass
class ApiKeyRecord:
    id: str
    name: str
    prefix: str
    key_hash: str
    created_at: float
    enabled: bool = True
    note: str = ""
    last_used_at: float | None = None
    request_count: int = 0
    # Full plaintext for admin re-copy (local self-host). Older keys may lack it.
    secret: str | None = None

    def public_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "prefix": self.prefix,
            "created_at": self.created_at,
            "enabled": self.enabled,
            "note": self.note,
            "last_used_at": self.last_used_at,
            "request_count": self.request_count,
            "key_hint": f"{self.prefix}…****",
            "has_secret": bool(self.secret),
        }
        # Admin list / create only — never exposed on public client routes
        if self.secret:
            d["secret"] = self.secret
        return d


def _ensure_parent(path: Path) -> None:
    ensure_private_dir(path.parent)


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _new_key_secret() -> str:
    return "sk-g2a-" + secrets.token_urlsafe(32)


def _load_raw() -> dict[str, Any]:
    if not KEYS_FILE.is_file():
        return {"keys": []}
    try:
        data = json.loads(KEYS_FILE.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise KeyStoreCorrupt("existing API key store is unreadable") from exc
    if not isinstance(data, dict) or not isinstance(data.get("keys", []), list):
        raise KeyStoreCorrupt("existing API key store has an invalid root")
    data.setdefault("keys", [])
    return data


def _save_raw(data: dict[str, Any]) -> None:
    _ensure_parent(KEYS_FILE)
    atomic_write_private_json(KEYS_FILE, data, pretty=True)


def _from_dict(d: dict[str, Any]) -> ApiKeyRecord:
    secret = d.get("secret") or d.get("key") or None
    if isinstance(secret, str):
        secret = secret.strip() or None
    else:
        secret = None
    return ApiKeyRecord(
        id=d["id"],
        name=d.get("name") or "unnamed",
        prefix=d.get("prefix") or "",
        key_hash=d["key_hash"],
        created_at=float(d.get("created_at") or time.time()),
        enabled=bool(d.get("enabled", True)),
        note=d.get("note") or "",
        last_used_at=d.get("last_used_at"),
        request_count=int(d.get("request_count") or 0),
        secret=secret,
    )


def list_keys() -> list[dict[str, Any]]:
    with _lock:
        data = _load_raw()
        return [_from_dict(k).public_dict() for k in data["keys"]]


def create_key(name: str, note: str = "") -> dict[str, Any]:
    """Create a new key. Stores secret for admin re-copy; also returns `key` once."""
    name = (name or "default").strip() or "default"
    raw = _new_key_secret()
    prefix = raw[:12]
    rec = ApiKeyRecord(
        id=str(uuid.uuid4()),
        name=name,
        prefix=prefix,
        key_hash=_hash_key(raw),
        created_at=time.time(),
        enabled=True,
        note=(note or "").strip(),
        secret=raw,
    )
    with _lock:
        data = _load_raw()
        data["keys"].append(asdict(rec))
        _save_raw(data)
    out = rec.public_dict()
    out["key"] = raw  # alias for older admin UI
    return out


def regenerate_key(key_id: str) -> dict[str, Any] | None:
    """Rotate an existing key and store the new plaintext for admin copying."""
    with _lock:
        data = _load_raw()
        for k in data["keys"]:
            if k.get("id") == key_id:
                raw = _new_key_secret()
                k["prefix"] = raw[:12]
                k["key_hash"] = _hash_key(raw)
                k["secret"] = raw
                _save_raw(data)
                out = _from_dict(k).public_dict()
                out["key"] = raw
                return out
    return None


def set_enabled(key_id: str, enabled: bool) -> dict[str, Any] | None:
    with _lock:
        data = _load_raw()
        for k in data["keys"]:
            if k.get("id") == key_id:
                k["enabled"] = bool(enabled)
                _save_raw(data)
                return _from_dict(k).public_dict()
    return None


def delete_key(key_id: str) -> bool:
    with _lock:
        data = _load_raw()
        before = len(data["keys"])
        data["keys"] = [k for k in data["keys"] if k.get("id") != key_id]
        if len(data["keys"]) == before:
            return False
        _save_raw(data)
        return True


def update_key(key_id: str, *, name: str | None = None, note: str | None = None) -> dict[str, Any] | None:
    with _lock:
        data = _load_raw()
        for k in data["keys"]:
            if k.get("id") == key_id:
                if name is not None:
                    k["name"] = name.strip() or k["name"]
                if note is not None:
                    k["note"] = note
                _save_raw(data)
                return _from_dict(k).public_dict()
    return None


def verify_key(raw: str | None) -> ApiKeyRecord | None:
    """Validate client API key. Accepts managed keys and legacy env API_KEY."""
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None

    # Legacy env single key
    if API_KEY and secrets.compare_digest(raw, API_KEY):
        return ApiKeyRecord(
            id="env",
            name="env:GROK2API_API_KEY",
            prefix=raw[:12] if len(raw) >= 12 else raw,
            key_hash=_hash_key(raw),
            created_at=0,
            enabled=True,
            note="from environment",
        )

    h = _hash_key(raw)
    with _lock:
        try:
            data = _load_raw()
        except KeyStoreCorrupt:
            return None
        for k in data["keys"]:
            if not k.get("enabled", True):
                continue
            if secrets.compare_digest(k.get("key_hash", ""), h):
                rec = _from_dict(k)
                k["last_used_at"] = time.time()
                k["request_count"] = int(k.get("request_count") or 0) + 1
                _save_raw(data)
                return rec
    return None


def has_any_keys() -> bool:
    with _lock:
        try:
            data = _load_raw()
        except KeyStoreCorrupt:
            # Treat an unreadable store as populated so legacy "auto" callers
            # cannot turn a corrupt credential store into open access.
            return True
        if API_KEY:
            return True
        return any(k.get("enabled", True) for k in data["keys"])


def auth_required() -> bool:
    """Whether /v1 must present a valid API key."""
    try:
        _load_raw()
    except KeyStoreCorrupt:
        # Corruption always overrides an explicit dev-open setting.  An
        # operator must repair/remove the store before access can be opened.
        return True
    mode = (REQUIRE_API_KEY or "1").lower()
    if mode in ("0", "false", "no", "off"):
        return False
    # Production is fail-closed.  Open local mode must be an explicit 0/off.
    return True


def stats() -> dict[str, Any]:
    with _lock:
        try:
            data = _load_raw()
            store_ok = True
        except KeyStoreCorrupt:
            data = {"keys": []}
            store_ok = False
        keys = [_from_dict(k) for k in data["keys"]]
        return {
            "total": len(keys),
            "enabled": sum(1 for k in keys if k.enabled),
            "disabled": sum(1 for k in keys if not k.enabled),
            "total_requests": sum(k.request_count for k in keys),
            "auth_required": auth_required(),
            "legacy_env_key": bool(API_KEY),
            "store_ok": store_ok,
        }
