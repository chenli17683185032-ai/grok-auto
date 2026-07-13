"""Load Grok session tokens from project data/auth.json (multi-account aware)."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from auth_store import read_auth_map
from config import (
    AUTH_FILE,
    CLI_VERSION,
    CLIENT_IDENTIFIER,
    CLIENT_SURFACE,
    TOKEN_REFRESH_SKEW,
)
from oidc_auth import parse_expires_at


class AuthError(Exception):
    """Raised when credentials cannot be loaded or are expired."""


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


@dataclass
class GrokCredentials:
    token: str
    email: str | None = None
    user_id: str | None = None
    expires_at: float | None = None
    auth_key: str | None = None
    team_id: str | None = None
    refresh_token: str | None = None
    oidc_client_id: str | None = None
    refresh_status: str | None = None
    refresh_failure_count: int = 0
    refresh_first_failed_at: float | None = None
    refresh_last_failed_at: float | None = None
    refresh_next_retry_at: float | None = None
    refresh_terminal_at: float | None = None
    refresh_confirmed_after_expiry: bool = False

    @property
    def expired(self) -> bool:
        if self.expires_at is None:
            return False
        # refresh a bit early
        return time.time() >= (self.expires_at - 60)

    @property
    def needs_refresh(self) -> bool:
        if not self.refresh_token:
            return False
        if self.expires_at is None:
            return False
        return time.time() >= (self.expires_at - TOKEN_REFRESH_SKEW)


def _read_auth(path: Path) -> dict[str, Any]:
    # Prefer locked store for default AUTH_FILE (multi-account safe on Linux)
    if path == AUTH_FILE or path.resolve() == AUTH_FILE.resolve():
        data = read_auth_map(path)
        if not data and not path.is_file():
            raise AuthError(
                f"Auth file not found: {path}. "
                "Use device-code login or import a token first."
            )
        if not data and path.is_file():
            raise AuthError(f"Unexpected/empty auth.json format in {path}")
        return data
    if not path.is_file():
        raise AuthError(
            f"Auth file not found: {path}. "
            "Use device-code login or import a token first."
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise AuthError(f"Failed to read {path}: {e}") from e
    if not isinstance(data, dict):
        raise AuthError(f"Unexpected auth.json format in {path}")
    return data


def _entry_to_creds(name: str, entry: dict[str, Any]) -> GrokCredentials:
    token = entry.get("key") or entry.get("access_token") or entry.get("token")
    if not token or not isinstance(token, str):
        raise AuthError(f"Entry {name} has no usable token")
    expires_at = parse_expires_at(entry.get("expires_at"), token)
    return GrokCredentials(
        token=token,
        email=entry.get("email"),
        user_id=entry.get("user_id") or entry.get("principal_id"),
        expires_at=expires_at,
        auth_key=name,
        team_id=entry.get("team_id"),
        refresh_token=entry.get("refresh_token")
        if isinstance(entry.get("refresh_token"), str)
        else None,
        oidc_client_id=entry.get("oidc_client_id"),
        refresh_status=entry.get("refresh_status"),
        refresh_failure_count=_safe_int(entry.get("refresh_failure_count")),
        refresh_first_failed_at=entry.get("refresh_first_failed_at"),
        refresh_last_failed_at=entry.get("refresh_last_failed_at"),
        refresh_next_retry_at=entry.get("refresh_next_retry_at"),
        refresh_terminal_at=entry.get("refresh_terminal_at"),
        refresh_confirmed_after_expiry=bool(
            entry.get("refresh_confirmed_after_expiry")
        ),
    )


def _refresh_retry_deferred(entry: dict[str, Any], *, now: float | None = None) -> bool:
    if entry.get("refresh_status") != "refresh_pending_confirmation":
        return False
    try:
        return (time.time() if now is None else now) < float(
            entry.get("refresh_next_retry_at") or 0
        )
    except (TypeError, ValueError):
        return True


def _iter_entries(data: dict[str, Any]) -> list[tuple[str, dict[str, Any], float]]:
    candidates: list[tuple[str, dict[str, Any], float]] = []
    for key, value in data.items():
        if not isinstance(value, dict):
            continue
        # Only the multi-cycle terminal state is durable.  Legacy
        # refresh_invalid and pending confirmation continue serving a still-live
        # access token while background maintenance gathers fresh evidence.
        if value.get("refresh_status") == "refresh_terminal":
            tok = value.get("key") or value.get("access_token") or value.get("token")
            exp = parse_expires_at(value.get("expires_at"), tok if isinstance(tok, str) else None)
            if not tok or (exp is not None and float(exp) <= time.time() + 60):
                continue
        token = value.get("key") or value.get("access_token") or value.get("token")
        if not token or not isinstance(token, str):
            continue
        exp = parse_expires_at(value.get("expires_at"), token)
        exp_f = float(exp) if exp is not None else 0.0
        candidates.append((key, value, exp_f))
    return candidates


def _pick_entry(data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """
    auth.json keys look like:
      - https://auth.x.ai::<user_id>     (multi-account)
      - https://auth.x.ai::<client_id>   (legacy Grok CLI single slot)
    Pick any non-expired entry (newest expires_at first). Pool rotation
    is handled by account_pool — this is only a fallback for status checks.
    """
    candidates = _iter_entries(data)
    if not candidates:
        raise AuthError(
            f"No usable token in {AUTH_FILE}. Login or import a token first."
        )

    now = time.time()
    live = [c for c in candidates if c[2] == 0.0 or c[2] > now]
    pool = live or candidates
    pool.sort(key=lambda c: c[2], reverse=True)
    name, entry, _ = pool[0]
    return name, entry


def list_live_credentials(
    path: Path | None = None,
    *,
    include_expired: bool = False,
    auto_refresh: bool = True,
) -> list[GrokCredentials]:
    """Return all accounts with tokens from auth.json."""
    path = path or AUTH_FILE
    if not path.is_file():
        return []
    try:
        data = _read_auth(path)
    except AuthError:
        return []

    # IMPORTANT: never fan-out refresh across the whole pool here.
    # With 700+ accounts this used to open hundreds of OIDC calls + rewrite
    # auth.json per account and freeze WSL. Background token_maintainer owns
    # bulk refresh; here we only refresh on demand for already-expired entries
    # when explicitly requested, and only a small number per call.
    out: list[GrokCredentials] = []
    refreshed = 0
    max_inline_refresh = 3 if auto_refresh else 0
    for name, entry, _exp in _iter_entries(data):
        try:
            creds = _entry_to_creds(name, entry)
        except AuthError:
            continue
        # still usable if has refresh_token even when access near-expired
        if include_expired or not creds.expired or creds.refresh_token:
            if (
                auto_refresh
                and creds.expired
                and creds.refresh_token
                and refreshed < max_inline_refresh
                and entry.get("refresh_status") != "refresh_terminal"
                and not _refresh_retry_deferred(entry)
            ):
                # only hard-refresh a tiny number of already-expired entries
                try:
                    from oidc_auth import refresh_and_persist

                    r = refresh_and_persist(name, entry)
                    creds = _entry_to_creds(r["account_id"], r["entry"])
                    refreshed += 1
                except Exception:
                    if not include_expired:
                        continue
            if include_expired or not creds.expired:
                out.append(creds)
    # newest expiry first for stable ordering
    out.sort(key=lambda c: c.expires_at or 0.0, reverse=True)
    return out


def load_credentials(
    path: Path | None = None,
) -> GrokCredentials:
    path = path or AUTH_FILE
    data = _read_auth(path)

    name, entry = _pick_entry(data)

    # auto refresh if needed
    try:
        from oidc_auth import ensure_fresh_entry

        entry = ensure_fresh_entry(name, entry, skew_seconds=TOKEN_REFRESH_SKEW)
        # re-read id if remounted
        data = _read_auth(path)
        name, entry = _pick_entry(data)
    except Exception:
        pass

    creds = _entry_to_creds(name, entry)
    if creds.expired and not creds.refresh_token:
        raise AuthError(
            "Session token expired. Use device-code login or import a fresh token."
        )
    if creds.expired and creds.refresh_token:
        status = str(entry.get("refresh_status") or "")
        if status == "refresh_terminal":
            raise AuthError("Token expired and refresh is terminal. Re-login or import.")
        if _refresh_retry_deferred(entry):
            raise AuthError("Token expired; refresh retry is scheduled.")
        # ensure_fresh_entry already attempted the exchange once in this load.
        raise AuthError("Token expired and refresh failed. Re-login or import.")
    return creds


def load_credentials_by_id(account_id: str, path: Path | None = None) -> GrokCredentials:
    path = path or AUTH_FILE
    data = _read_auth(path)
    entry = data.get(account_id)
    if not isinstance(entry, dict):
        # try match by user_id suffix
        for k, v in data.items():
            if isinstance(v, dict) and (
                k == account_id
                or v.get("user_id") == account_id
                or k.endswith(f"::{account_id}")
            ):
                entry = v
                account_id = k
                break
        else:
            raise AuthError(f"Account not found: {account_id}")

    try:
        from oidc_auth import ensure_fresh_entry

        entry = ensure_fresh_entry(account_id, entry, skew_seconds=TOKEN_REFRESH_SKEW)
        # account_id may have changed after remount
        data = _read_auth(path)
        if account_id not in data:
            for k, v in data.items():
                if isinstance(v, dict) and v.get("user_id") == entry.get("user_id"):
                    account_id = k
                    entry = v
                    break
    except Exception:
        pass

    creds = _entry_to_creds(account_id, entry)
    if creds.expired:
        if creds.refresh_token:
            status = str(entry.get("refresh_status") or "")
            if status == "refresh_terminal":
                raise AuthError("Account token expired; refresh is terminal")
            if _refresh_retry_deferred(entry):
                raise AuthError("Account token expired; refresh retry is scheduled")
            # ensure_fresh_entry already attempted once above.
            raise AuthError("Account token expired / refresh failed")
        raise AuthError(f"Account token expired: {account_id}")
    return creds


def upstream_headers(token: str, model: str) -> dict[str, str]:
    """Headers required by cli-chat-proxy (mirror Grok CLI)."""
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "X-XAI-Token-Auth": "xai-grok-cli",
        "x-grok-model-override": model,
        # Required — without this, proxy returns 426 with version=(none)
        "x-grok-client-version": CLI_VERSION,
        "x-grok-client-surface": CLIENT_SURFACE,
        "x-grok-client-identifier": CLIENT_IDENTIFIER,
        "User-Agent": f"grok-cli/{CLI_VERSION}",
        "Accept": "text/event-stream, application/json",
    }
