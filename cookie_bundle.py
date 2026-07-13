"""Cookie bundle storage with whitelist, TTL, and path safety.

Modes:
  sso_only     — only sso / sso-rw (production default)
  auth_bundle  — SSO + optional allow-listed session cookies

Never log cookie values. Files are 0600 under a 0700 directory.
"""

from __future__ import annotations

def bundle_root() -> Path:
    raw = os.getenv("GROK2API_COOKIE_BUNDLE_DIR", "").strip()
    if raw:
        return Path(raw)
    return Path(os.getenv("GROK2API_DATA_DIR", "data")) / "cookie_bundles"



import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Iterable

from registration_jobs import experiment_bucket, in_experiment
from secure_storage import atomic_write_private_json, ensure_private_dir

SSO_NAMES = frozenset({"sso", "sso-rw"})
CF_NAMES = frozenset({"cf_clearance", "__cf_bm"})

_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,96}$")


def _env_flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def cookie_mode_default() -> str:
    mode = os.getenv("GROK2API_COOKIE_MODE", "sso_only").strip().lower()
    return mode if mode in ("sso_only", "auth_bundle") else "sso_only"


def experiment_percent() -> float:
    try:
        return max(0.0, min(100.0, float(os.getenv("GROK2API_COOKIE_EXPERIMENT_PERCENT", "0") or 0)))
    except (TypeError, ValueError):
        return 0.0


def allow_cf_cookies() -> bool:
    return _env_flag("GROK2API_COOKIE_ALLOW_CF", "0")


def default_bundle_dir() -> Path:
    raw = os.getenv("GROK2API_COOKIE_BUNDLE_DIR", "").strip()
    if raw:
        return Path(raw)
    return Path(os.getenv("GROK2API_DATA_DIR", "data")) / "cookie_bundles"


def ensure_bundle_dir(path: Path | None = None) -> Path:
    root = path or default_bundle_dir()
    return ensure_private_dir(root)


def resolve_mode_for_session(session_id: str) -> str:
    """A/B: only a percent of sessions get auth_bundle when configured."""
    base = cookie_mode_default()
    if base != "auth_bundle":
        # Explicit sso_only, or auth_bundle forced only via experiment percent
        # when mode is sso_only but experiment > 0 → treat experiment as opt-in.
        pct = experiment_percent()
        if pct > 0 and in_experiment(session_id, pct):
            return "auth_bundle"
        return "sso_only"
    # mode auth_bundle: still respect percent for gradual rollout
    pct = experiment_percent()
    if pct <= 0:
        return "sso_only"
    if pct >= 100:
        return "auth_bundle"
    return "auth_bundle" if in_experiment(session_id, pct) else "sso_only"


def allowed_names(mode: str) -> frozenset[str]:
    names = set(SSO_NAMES)
    if mode == "auth_bundle" and allow_cf_cookies():
        names |= set(CF_NAMES)
    return frozenset(names)


def normalize_cookie_items(
    cookies: Any,
    *,
    mode: str = "sso_only",
) -> list[dict[str, Any]]:
    """Filter and normalize cookies. Values are kept for storage only."""
    allow = allowed_names(mode)
    # auth_bundle without CF still only SSO in v1 unless CF flag on —
    # keep room for future non-CF names via env.
    extra = os.getenv("GROK2API_COOKIE_EXTRA_ALLOW", "").strip()
    if mode == "auth_bundle" and extra:
        allow = frozenset(set(allow) | {x.strip() for x in extra.split(",") if x.strip()})

    items: list[dict[str, Any]] = []
    raw_list: list[Any]
    if not cookies:
        return items
    if isinstance(cookies, dict):
        raw_list = [{"name": k, "value": v, "domain": ".x.ai", "path": "/"} for k, v in cookies.items()]
    elif isinstance(cookies, (list, tuple)):
        raw_list = list(cookies)
    else:
        return items

    for c in raw_list:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or c.get("Name") or "").strip()
        value = c.get("value") if "value" in c else c.get("Value")
        if not name or value is None:
            continue
        if name not in allow and not (mode == "auth_bundle" and name in SSO_NAMES):
            if name not in allow:
                continue
        if mode == "sso_only" and name not in SSO_NAMES:
            continue
        item = {
            "name": name,
            "value": str(value),
            "domain": str(c.get("domain") or c.get("Domain") or ".x.ai"),
            "path": str(c.get("path") or c.get("Path") or "/"),
        }
        for src, dst in (
            ("secure", "secure"),
            ("httpOnly", "httpOnly"),
            ("sameSite", "sameSite"),
            ("expiry", "expiry"),
            ("expires", "expiry"),
        ):
            if src in c and c[src] is not None:
                item[dst] = c[src]
        items.append(item)

    # Always expand SSO names onto accounts/auth hosts for device flow.
    extras: list[dict[str, Any]] = []
    seen = {(i["name"], i["domain"], i["path"]) for i in items}
    for item in list(items):
        if item["name"] not in SSO_NAMES and item["name"] not in CF_NAMES:
            continue
        for dom in (".x.ai", "accounts.x.ai", ".accounts.x.ai", "auth.x.ai", ".auth.x.ai"):
            key = (item["name"], dom, item["path"])
            if key in seen:
                continue
            clone = dict(item)
            clone["domain"] = dom
            extras.append(clone)
            seen.add(key)
    items.extend(extras)
    return items


def cookie_names(items: Iterable[dict[str, Any]]) -> list[str]:
    return sorted({str(i.get("name")) for i in items if i.get("name")})


def _safe_resolve(root: Path, name: str) -> Path:
    if not _SAFE_ID.match(name):
        raise ValueError("invalid bundle id")
    root = root.resolve()
    path = (root / name).resolve()
    if path.parent != root:
        raise ValueError("path traversal rejected")
    if path.is_symlink():
        raise ValueError("symlink rejected")
    return path


def write_bundle(
    cookies: Any,
    *,
    session_id: str,
    mode: str | None = None,
    bundle_dir: Path | None = None,
    ttl_sec: float | None = None,
) -> dict[str, Any]:
    """Persist a bundle. Returns public metadata (no values)."""
    resolved_mode = mode or resolve_mode_for_session(session_id)
    items = normalize_cookie_items(cookies, mode=resolved_mode)
    # Guarantee SSO presence if provided as plain string fields elsewhere — caller
    # should pass a dict including sso. Empty bundle is allowed only for sso_only
    # metadata bookkeeping; injectors must still receive sso separately.
    root = ensure_bundle_dir(bundle_dir)
    bundle_id = f"{session_id}_{secrets.token_hex(4)}"
    # Sanitize session_id for filesystem
    bundle_id = re.sub(r"[^A-Za-z0-9_.-]", "_", bundle_id)[:96]
    path = _safe_resolve(root, bundle_id + ".json")
    ttl = float(ttl_sec if ttl_sec is not None else os.getenv("GROK2API_COOKIE_BUNDLE_TTL_SEC", "7200"))
    now = time.time()
    payload = {
        "bundle_id": bundle_id,
        "session_id": session_id,
        "mode": resolved_mode,
        "created_at": now,
        "expires_at": now + max(60.0, ttl),
        "cookies": items,
        "names": cookie_names(items),
    }
    atomic_write_private_json(path, payload)
    return {
        "ok": True,
        "bundle_id": bundle_id,
        "path": str(path),
        "mode": resolved_mode,
        "names": payload["names"],
        "expires_at": payload["expires_at"],
        "count": len(items),
    }


def read_bundle(path: str | Path, *, root: Path | None = None) -> dict[str, Any] | None:
    """Read bundle; enforce root containment when root is provided."""
    p = Path(path)
    if root is not None:
        try:
            p = _safe_resolve(Path(root), p.name)
        except ValueError:
            return None
    if p.is_symlink() or not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    exp = float(data.get("expires_at") or 0)
    if exp and time.time() > exp:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    return data


def inject_list_for_approver(
    path: str | Path | None,
    *,
    sso: str,
    mode: str = "sso_only",
) -> list[dict[str, str]]:
    """Build the minimal cookie list for ruyiPage (values included for injection only)."""
    cookies: list[dict[str, str]] = [
        {"name": "sso", "value": sso, "domain": ".x.ai", "path": "/"},
        {"name": "sso-rw", "value": sso, "domain": ".x.ai", "path": "/"},
    ]
    if not path or mode == "sso_only":
        return cookies
    data = read_bundle(path)
    if not data:
        return cookies
    allow = allowed_names(str(data.get("mode") or mode))
    for c in data.get("cookies") or []:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "")
        if name in SSO_NAMES:
            continue  # already set from sso arg
        if name not in allow:
            continue
        cookies.append(
            {
                "name": name,
                "value": str(c.get("value") or ""),
                "domain": str(c.get("domain") or ".x.ai"),
                "path": str(c.get("path") or "/"),
            }
        )
    return cookies


def public_meta(path: str | Path | None) -> dict[str, Any]:
    if not path:
        return {"present": False}
    data = read_bundle(path)
    if not data:
        return {"present": False}
    return {
        "present": True,
        "bundle_id": data.get("bundle_id"),
        "mode": data.get("mode"),
        "names": data.get("names") or cookie_names(data.get("cookies") or []),
        "expires_at": data.get("expires_at"),
        "count": len(data.get("cookies") or []),
    }


def delete_bundle(path: str | Path) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def experiment_info(session_id: str) -> dict[str, Any]:
    pct = experiment_percent()
    return {
        "mode_default": cookie_mode_default(),
        "resolved_mode": resolve_mode_for_session(session_id),
        "percent": pct,
        "bucket": experiment_bucket("cookie_bundle", session_id),
        "in_experiment": in_experiment(session_id, pct),
    }


def sweep_expired(
    *,
    root: Path | None = None,
    max_age_sec: float | None = None,
    max_delete: int = 200,
    protected_paths: Iterable[str | Path] = (),
    now: float | None = None,
) -> int:
    """Delete expired cookie bundle files without requiring a read."""
    base = Path(root) if root else bundle_root()
    if max_age_sec is None:
        try:
            max_age_sec = float(
                os.getenv("GROK2API_COOKIE_BUNDLE_TTL_SEC", "172800") or 172800
            )
        except (TypeError, ValueError):
            max_age_sec = 172800.0
    if not base.is_dir():
        return 0
    current = time.time() if now is None else float(now)
    limit = max(0, int(max_delete))
    if limit == 0:
        return 0
    protected = set()
    for raw in protected_paths:
        try:
            protected.add(str(Path(raw).resolve(strict=False)))
        except OSError:
            continue
    deleted = 0
    candidates: list[tuple[float, Path]] = []
    for path in base.glob("*.json"):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            if str(path.resolve(strict=False)) in protected:
                continue
            mtime = path.stat().st_mtime
            if current - mtime > max_age_sec:
                candidates.append((mtime, path))
        except OSError:
            continue
    for _mtime, path in sorted(candidates, key=lambda item: (item[0], item[1].name))[:limit]:
        try:
            path.unlink(missing_ok=True)
            deleted += 1
        except OSError:
            continue
    return deleted
