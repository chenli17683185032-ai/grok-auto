#!/usr/bin/env python3
"""
批量导入 xAI SSO cookie 到项目 auth.json（纯 HTTP Device Flow）

用法:
  # 单个 / 批量 SSO，每个导入后按 user_id 合并到 data/auth.json
  python3 sso_to_auth_json.py --sso sso_list.txt

  # 写出多个独立 auth 文件（每个可直接 cp 到 ~/.grok/auth.json）
  python3 sso_to_auth_json.py --sso sso_list.txt --out-dir ./auth_out

  # 合并到指定 json（key 带 user_id 后缀，避免覆盖）
  python3 sso_to_auth_json.py --sso sso_list.txt --out auth_merged.json --merge

  # 单行 sso
  python3 sso_to_auth_json.py --sso-cookie 'eyJ...'

环境变量:
  GROK2API_AUTH_FILE  - 导入目标 auth.json（默认项目 data/auth.json）
  GROK2API_PROXY      - 代理地址，例如 http://127.0.0.1:7890
  GROK2API_RUYIPAGE_APPROVERS - 多个无头审批 sidecar URL，逗号分隔并轮询
  GROK2API_RUYIPAGE_APPROVER  - 单 sidecar URL（向后兼容）
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from curl_cffi import requests
except ImportError:  # pragma: no cover - optional for unit tests without network stack
    requests = None  # type: ignore

# Use project config when available, otherwise fall back to defaults
try:
    from config import AUTH_FILE, GROK_CLI_CLIENT_ID, OIDC_ISSUER, OIDC_SCOPES
except Exception:  # pragma: no cover - standalone fallback
    AUTH_FILE = Path(os.getenv("GROK2API_AUTH_FILE", str(Path.home() / ".grok" / "auth.json")))
    GROK_CLI_CLIENT_ID = os.getenv("GROK2API_OIDC_CLIENT_ID", "b1a00492-073a-47ea-816f-4c329264a828")
    OIDC_ISSUER = os.getenv("GROK2API_OIDC_ISSUER", "https://auth.x.ai")
    OIDC_SCOPES = os.getenv(
        "GROK2API_OIDC_SCOPES",
        "openid profile email offline_access grok-cli:access "
        "api:access conversations:read conversations:write",
    )

AUTH_KEY = f"{OIDC_ISSUER}::{GROK_CLI_CLIENT_ID}"

_approver_rotation_lock = threading.Lock()
_approver_rotation_index = 0


def _proxy_kwargs() -> dict:
    """Return curl_cffi compatible proxy kwargs from env."""
    proxy = os.getenv("GROK2API_PROXY") or os.getenv("GROK_CLI_PROXY") or ""
    if proxy:
        return {"proxies": {"http": proxy, "https": proxy}}
    return {}

def _open_url(req: urllib.request.Request, *, timeout: float = 15, proxy: str | None = None):
    """urlopen with optional per-call proxy (does not mutate process env)."""
    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        opener = urllib.request.build_opener(handler)
        return opener.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def _lease_cancelled(
    cancel_event: threading.Event | None = None,
    lease_guard: Callable[[], bool] | None = None,
) -> bool:
    return bool(
        (cancel_event is not None and cancel_event.is_set())
        or (lease_guard is not None and not lease_guard())
    )


def _cancel_aware_wait(
    seconds: float,
    *,
    cancel_event: threading.Event | None = None,
    lease_guard: Callable[[], bool] | None = None,
    local_cancel: threading.Event | None = None,
) -> bool:
    """Wait at most `seconds`; return True as soon as cancellation is observed."""
    deadline = time.monotonic() + max(0.0, float(seconds))
    while True:
        if (
            _lease_cancelled(cancel_event, lease_guard)
            or (local_cancel is not None and local_cancel.is_set())
        ):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        # Event.wait wakes immediately for the common external lease-loss path.
        if cancel_event is not None:
            cancel_event.wait(timeout=min(0.25, remaining))
        elif local_cancel is not None:
            local_cancel.wait(timeout=min(0.25, remaining))
        else:
            time.sleep(min(0.25, remaining))


def b64url_decode(seg: str) -> bytes:
    seg += "=" * (-len(seg) % 4)
    return base64.urlsafe_b64decode(seg)


def decode_jwt_payload(token: str) -> dict:
    try:
        return json.loads(b64url_decode(token.split(".")[1]))
    except Exception:
        return {}


def rfc3339_ns(ts: float | None = None) -> str:
    """2026-07-10T01:00:00.000000000Z"""
    if ts is None:
        ts = time.time()
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + ".000000000Z"


def request_device_code(
    *,
    proxy: str | None = None,
    cancel_event: threading.Event | None = None,
    lease_guard: Callable[[], bool] | None = None,
) -> dict | None:
    """Request OAuth device code; optional per-call proxy for route sticky."""
    data = urllib.parse.urlencode({"client_id": GROK_CLI_CLIENT_ID, "scope": OIDC_SCOPES}).encode()
    req = urllib.request.Request(
        f"{OIDC_ISSUER}/oauth2/device/code",
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if _lease_cancelled(cancel_event, lease_guard):
        return None
    try:
        with _open_url(req, timeout=15, proxy=proxy) as resp:
            result = json.loads(resp.read())
        return None if _lease_cancelled(cancel_event, lease_guard) else result
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode()[:200]
        except Exception:
            body = ""
        print(f"  ❌ device/code HTTP {e.code}")
        # never print body — may contain secrets
        _ = body
        return None
    except Exception as e:
        print(f"  ❌ device/code network: {type(e).__name__}")
        return None


def poll_token(device_code: str, interval: int, expires_in: int, timeout: int = 60) -> dict | None:
    deadline = time.time() + min(expires_in, timeout)
    while time.time() < deadline:
        time.sleep(interval)
        data = urllib.parse.urlencode(
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": GROK_CLI_CLIENT_ID,
                "device_code": device_code,
            }
        ).encode()
        req = urllib.request.Request(
            f"{OIDC_ISSUER}/oauth2/token",
            data=data,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            err = json.loads(e.read())
            error = err.get("error", "")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            print(f"  ❌ token: {error}")
            return None
    print("  ❌ 轮询超时")
    return None



def _configured_approver_endpoints() -> tuple[str, ...]:
    """Return configured sidecars, preferring the comma-separated plural setting."""
    raw = os.getenv("GROK2API_RUYIPAGE_APPROVERS", "").strip()
    if not raw:
        raw = os.getenv("GROK2API_RUYIPAGE_APPROVER", "").strip()

    endpoints: list[str] = []
    seen: set[str] = set()
    for value in raw.replace(";", ",").replace("\n", ",").split(","):
        endpoint = value.strip().rstrip("/")
        if endpoint and endpoint not in seen:
            seen.add(endpoint)
            endpoints.append(endpoint)
    return tuple(endpoints)


def _approver_endpoints_for_flow() -> tuple[str, ...]:
    """Choose one sidecar round-robin and retain the others as failover targets."""
    global _approver_rotation_index

    endpoints = _configured_approver_endpoints()
    if len(endpoints) < 2:
        return endpoints
    with _approver_rotation_lock:
        start = _approver_rotation_index % len(endpoints)
        _approver_rotation_index = (start + 1) % len(endpoints)
    return endpoints[start:] + endpoints[:start]


def _try_lock_approver(endpoint: str):
    """Reserve one sidecar across API/producer/recovery containers.

    All three services bind-mount the same ``/app/data`` directory.  An
    advisory lock per endpoint prevents two Device Flows from driving the same
    browser at once without reducing the two-sidecar pool to one global slot.
    The kernel releases the lease automatically if a worker crashes.
    """
    lock_dir = Path(
        os.getenv("GROK2API_APPROVER_LOCK_DIR", "/app/data/approver_locks")
    )
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_name = hashlib.sha256(endpoint.encode("utf-8")).hexdigest()[:24] + ".lock"
    handle = open(lock_dir / lock_name, "a+b")
    try:
        if os.name == "nt":  # pragma: no cover - server deployment is Linux
            import msvcrt

            handle.seek(0)
            if handle.read(1) == b"":
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except (OSError, BlockingIOError):
        handle.close()
        return None


def _unlock_approver(handle) -> None:
    try:
        if os.name == "nt":  # pragma: no cover - server deployment is Linux
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        handle.close()


def browser_approve_device(
    sso_cookie: str,
    dc: dict,
    *,
    approver_endpoint: str | None = None,
    sticky: bool = False,
    cookie_bundle_path: str = "",
    cookie_mode: str = "sso_only",
    extra_cookies: list | None = None,
    cancel_event: threading.Event | None = None,
    lease_guard: Callable[[], bool] | None = None,
) -> dict | None:
    """Approve one Device Flow on one free sidecar.

    Transport failures may fail over to another sidecar **unless** sticky=True
    (route affinity): then only the pinned endpoint is used and a structured
    business denial is never replayed elsewhere.
    """
    if approver_endpoint:
        endpoints = (approver_endpoint.rstrip("/"),)
    else:
        endpoints = _approver_endpoints_for_flow()
    if not endpoints:
        return None
    timeout = int(os.getenv("GROK2API_RUYIPAGE_TIMEOUT", "120"))
    lock_wait = max(
        0.0, float(os.getenv("GROK2API_APPROVER_LOCK_WAIT_SEC", str(timeout + 30)))
    )
    lock_poll = max(
        0.05, float(os.getenv("GROK2API_APPROVER_LOCK_POLL_SEC", "0.25"))
    )
    body: dict[str, Any] = {
        "sso": sso_cookie,
        "verification_url": dc.get("verification_uri_complete") or dc.get("verification_uri"),
        "user_code": dc.get("user_code", ""),
        "timeout": timeout,
        "cookie_mode": cookie_mode or "sso_only",
    }
    if cookie_bundle_path:
        # Path only — approver reads file itself if volume mounted.
        body["cookie_bundle_path"] = cookie_bundle_path
    if extra_cookies:
        # Inline allow-listed cookies so approver works without shared FS.
        body["extra_cookies"] = extra_cookies
    payload = json.dumps(body).encode()
    remaining = list(endpoints)
    deadline = time.monotonic() + lock_wait
    while remaining:
        if _lease_cancelled(cancel_event, lease_guard):
            return {"ok": False, "cancelled": True, "error": "lease_cancelled"}
        saw_busy = False
        for endpoint in tuple(remaining):
            if _lease_cancelled(cancel_event, lease_guard):
                return {"ok": False, "cancelled": True, "error": "lease_cancelled"}
            lease = _try_lock_approver(endpoint)
            if lease is None:
                saw_busy = True
                continue
            try:
                req = urllib.request.Request(
                    endpoint + "/approve",
                    data=payload,
                    method="POST",
                    headers={"Content-Type": "application/json"},
                )
                try:
                    if _lease_cancelled(cancel_event, lease_guard):
                        return {
                            "ok": False,
                            "cancelled": True,
                            "error": "lease_cancelled",
                        }
                    with urllib.request.urlopen(req, timeout=timeout + 30) as resp:
                        result = json.loads(resp.read())
                    if _lease_cancelled(cancel_event, lease_guard):
                        return {
                            "ok": False,
                            "cancelled": True,
                            "error": "lease_cancelled",
                        }
                    if not isinstance(result, dict):
                        raise ValueError("invalid non-object response")
                except Exception as e:
                    print(f"  ❌ ruyiPage approve 异常 ({endpoint}): {type(e).__name__}")
                    remaining.remove(endpoint)
                    if remaining and not sticky:
                        print("  ↪️ 切换下一个 ruyiPage sidecar")
                    continue
                # Structured business result (ok/denied/timeout/rate_limited) is final
                # for this Device Code — never cross-route replay.
                result.setdefault("approver_endpoint", endpoint)
                return result
            finally:
                _unlock_approver(lease)

        if sticky or not saw_busy or time.monotonic() >= deadline:
            break
        if _cancel_aware_wait(
            min(lock_poll, max(0.0, deadline - time.monotonic())),
            cancel_event=cancel_event,
            lease_guard=lease_guard,
        ):
            return {"ok": False, "cancelled": True, "error": "lease_cancelled"}

    if remaining:
        print("  ⏳ 所有 ruyiPage sidecar 忙，等待租约超时")
        return {"ok": False, "busy": True, "error": "busy"}
    # Preserve the legacy HTTP fallback only when every configured sidecar was
    # unreachable or returned malformed transport data.
    return None



def poll_token_cancellable(
    device_code: str,
    interval: int,
    expires_in: int,
    timeout: int = 60,
    *,
    cancel: threading.Event | None = None,
    proxy: str | None = None,
    cancel_event: threading.Event | None = None,
    lease_guard: Callable[[], bool] | None = None,
) -> dict | None:
    """Token poll that honours cancel + optional per-call proxy for route sticky."""
    deadline = time.time() + min(expires_in, timeout)
    current_interval = max(1, int(interval or 5))
    while time.time() < deadline:
        if _lease_cancelled(cancel_event, lease_guard) or (
            cancel is not None and cancel.is_set()
        ):
            print("  ⏹ token poll cancelled")
            return None
        if _cancel_aware_wait(
            current_interval,
            cancel_event=cancel_event,
            lease_guard=lease_guard,
            local_cancel=cancel,
        ):
            return None
        data = urllib.parse.urlencode(
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": GROK_CLI_CLIENT_ID,
                "device_code": device_code,
            }
        ).encode()
        req = urllib.request.Request(
            f"{OIDC_ISSUER}/oauth2/token",
            data=data,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            if _lease_cancelled(cancel_event, lease_guard):
                return None
            with _open_url(req, timeout=15, proxy=proxy) as resp:
                result = json.loads(resp.read())
            return None if _lease_cancelled(cancel_event, lease_guard) else result
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read())
            except Exception:
                err = {}
            error = err.get("error", "")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                current_interval += 5
                continue
            if error in ("access_denied", "expired_token"):
                print(f"  ❌ token: {error}")
                return None
            print(f"  ❌ token: {error or e.code}")
            return None
        except Exception as e:
            print(f"  ❌ token poll network: {type(e).__name__}")
            return None
    print("  ❌ 轮询超时")
    return None


def sso_to_token(
    sso_cookie: str,
    _attempt: int = 0,
    *,
    route_id: str | None = None,
    approver_endpoint: str | None = None,
    proxy: str | None = None,
    cookie_bundle_path: str = "",
    cookie_mode: str = "sso_only",
    parallel_poll: bool | None = None,
    extra_cookies: list | None = None,
    cancel_event: threading.Event | None = None,
    lease_guard: Callable[[], bool] | None = None,
) -> dict | None:
    """SSO cookie → token dict (access/refresh/expires_in).

    Optional kwargs enable route affinity / cookie bundle / parallel poll.
    When omitted, behaviour matches the legacy serial path.
    Proxy is applied per-call (curl_cffi proxies= + urllib ProxyHandler),
    never via mutating process-global env under concurrency.
    """
    if _lease_cancelled(cancel_event, lease_guard):
        return None
    sticky = bool(route_id or approver_endpoint)
    if sticky and not approver_endpoint and route_id:
        try:
            from route_registry import get_registry

            approver_endpoint = get_registry().approver_for(route_id)
            if not proxy:
                proxy = get_registry().proxy_for(route_id, "token")
        except Exception:
            pass

    session_proxy_kwargs: dict = {}
    if proxy:
        session_proxy_kwargs = {"proxies": {"http": proxy, "https": proxy}}
    else:
        session_proxy_kwargs = _proxy_kwargs()

    if requests is None:
        print("  ❌ curl_cffi not installed")
        return None
    s = requests.Session()
    s.cookies.set("sso", sso_cookie, domain=".x.ai")

    try:
        if _lease_cancelled(cancel_event, lease_guard):
            return None
        r = s.get(
            "https://accounts.x.ai/",
            impersonate="chrome",
            timeout=15,
            **session_proxy_kwargs,
        )
    except Exception as e:
        print(f"  ❌ 网络错误: {type(e).__name__}")
        return None
    if _lease_cancelled(cancel_event, lease_guard):
        return None
    if "sign-in" in r.url or "sign-up" in r.url:
        print("  ❌ sso 无效")
        return None
    print("  ✅ sso 有效")

    print("  🔑 Device Flow...")
    dc = request_device_code(
        proxy=proxy, cancel_event=cancel_event, lease_guard=lease_guard
    )
    if not dc:
        return None
    print("  Device code issued")

    use_parallel = (
        parallel_poll
        if parallel_poll is not None
        else os.getenv("GROK2API_PARALLEL_TOKEN_POLL", "0").strip().lower()
        in ("1", "true", "yes", "on")
    )

    cancel = threading.Event()
    poll_holder: dict[str, Any] = {"token": None, "done": False}

    def _poll_worker() -> None:
        poll_holder["token"] = poll_token_cancellable(
            dc["device_code"],
            dc.get("interval", 5),
            dc.get("expires_in", 1800),
            cancel=cancel,
            proxy=proxy,
            cancel_event=cancel_event,
            lease_guard=lease_guard,
        )
        poll_holder["done"] = True

    poll_thread: threading.Thread | None = None
    if use_parallel:
        poll_thread = threading.Thread(
            target=_poll_worker, name="token-poll", daemon=True
        )
        poll_thread.start()

    try:
        browser_result = browser_approve_device(
            sso_cookie,
            dc,
            approver_endpoint=approver_endpoint,
            sticky=sticky,
            cookie_bundle_path=cookie_bundle_path,
            cookie_mode=cookie_mode,
            extra_cookies=extra_cookies,
            cancel_event=cancel_event,
            lease_guard=lease_guard,
        )
        if _lease_cancelled(cancel_event, lease_guard):
            cancel.set()
            return None
        if browser_result is not None:
            if not browser_result.get("ok"):
                # Cancel parallel poll on structured failure
                cancel.set()
                if poll_thread is not None:
                    poll_thread.join(timeout=2)
                # Never cross-route replay a structured denial
                print(f"  ❌ ruyiPage approve 失败: ok=false keys={sorted(browser_result.keys())}")
                if browser_result.get("rate_limited") and _attempt < 3:
                    delay = (30, 60, 120)[_attempt]
                    print(f"  ⏳ Device Flow 限流，{delay}s 后申请新 Device Code 重试")
                    if _cancel_aware_wait(
                        delay,
                        cancel_event=cancel_event,
                        lease_guard=lease_guard,
                    ):
                        return None
                    return sso_to_token(
                        sso_cookie,
                        _attempt + 1,
                        route_id=route_id,
                        approver_endpoint=approver_endpoint,
                        proxy=proxy,
                        cookie_bundle_path=cookie_bundle_path,
                        cookie_mode=cookie_mode,
                        parallel_poll=use_parallel,
                        extra_cookies=extra_cookies,
                        cancel_event=cancel_event,
                        lease_guard=lease_guard,
                    )
                return None
            print("  ✅ ruyiPage 无头浏览器授权确认")
        else:
            if sticky:
                # No HTTP fallback cross-route when sticky; fail this attempt.
                cancel.set()
                if poll_thread is not None:
                    poll_thread.join(timeout=2)
                print("  ❌ sticky route approver unavailable")
                return None
            try:
                if _lease_cancelled(cancel_event, lease_guard):
                    cancel.set()
                    return None
                s.get(
                    dc["verification_uri_complete"],
                    impersonate="chrome",
                    timeout=15,
                    **session_proxy_kwargs,
                )
                if _lease_cancelled(cancel_event, lease_guard):
                    cancel.set()
                    return None
                r = s.post(
                    f"{OIDC_ISSUER}/oauth2/device/verify",
                    data={"user_code": dc["user_code"]},
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    impersonate="chrome",
                    timeout=15,
                    allow_redirects=True,
                    **session_proxy_kwargs,
                )
                if _lease_cancelled(cancel_event, lease_guard):
                    cancel.set()
                    return None
                if "consent" not in r.url:
                    print("  ❌ verify 失败")
                    cancel.set()
                    return None
                r = s.post(
                    f"{OIDC_ISSUER}/oauth2/device/approve",
                    data={
                        "user_code": dc["user_code"],
                        "action": "allow",
                        "principal_type": "User",
                        "principal_id": "",
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    impersonate="chrome",
                    timeout=15,
                    allow_redirects=True,
                    **session_proxy_kwargs,
                )
                if _lease_cancelled(cancel_event, lease_guard):
                    cancel.set()
                    return None
                if "done" not in r.url:
                    print("  ❌ approve 失败")
                    cancel.set()
                    return None
                print("  ✅ HTTP 授权确认")
            except Exception as e:
                print(f"  ❌ HTTP approve 异常: {type(e).__name__}")
                cancel.set()
                return None

        if use_parallel and poll_thread is not None:
            poll_thread.join(
                timeout=float(os.getenv("GROK2API_TOKEN_POLL_JOIN_SEC", "90") or 90)
            )
            token = poll_holder.get("token")
            if not token:
                token = poll_token_cancellable(
                    dc["device_code"],
                    dc.get("interval", 5),
                    dc.get("expires_in", 1800),
                    timeout=30,
                    proxy=proxy,
                    cancel=cancel,
                    cancel_event=cancel_event,
                    lease_guard=lease_guard,
                )
        else:
            token = poll_token_cancellable(
                dc["device_code"],
                dc.get("interval", 5),
                dc.get("expires_in", 1800),
                proxy=proxy,
                cancel=cancel,
                cancel_event=cancel_event,
                lease_guard=lease_guard,
            )
        if _lease_cancelled(cancel_event, lease_guard):
            return None
        if not token:
            return None
        print(
            f"  ✅ access_token (expires_in={token.get('expires_in')}s)"
            + (" + refresh_token" if token.get("refresh_token") else "")
        )
        return token
    finally:
        # Always cancel background poller on exit paths that return early after start.
        if use_parallel:
            cancel.set()


def token_to_auth_entry(token: dict, email: str = "") -> tuple[str, dict]:
    """
    返回 (top_level_key, entry)
    top_level_key 固定为 issuer::client_id（与 ~/.grok/auth.json 一致）
    """
    access = token.get("access_token") or token.get("key") or ""
    refresh = token.get("refresh_token") or ""
    payload = decode_jwt_payload(access)

    user_id = payload.get("sub") or payload.get("principal_id") or ""
    principal_id = payload.get("principal_id") or user_id
    principal_type = payload.get("principal_type") or "User"

    expires_in = int(token.get("expires_in") or 21600)
    if "exp" in payload:
        expires_at = rfc3339_ns(float(payload["exp"]))
    else:
        expires_at = rfc3339_ns(time.time() + expires_in)

    iat = payload.get("iat")
    create_time = rfc3339_ns(float(iat) if iat else time.time())

    entry = {
        "key": access,
        "auth_mode": "oidc",
        "create_time": create_time,
        "user_id": user_id,
        "email": email or payload.get("email") or "",
        "principal_type": principal_type,
        "principal_id": principal_id,
        "refresh_token": refresh,
        "expires_at": expires_at,
        "oidc_issuer": OIDC_ISSUER,
        "oidc_client_id": GROK_CLI_CLIENT_ID,
    }
    return AUTH_KEY, entry


def write_auth_json(path: Path, auth_key: str, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {auth_key: entry}
    tmp = path.with_suffix(path.suffix + ".tmp")
    _payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    _fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(_fd, "w", encoding="utf-8") as _fh:
        _fh.write(_payload)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def merge_auth_json(path: Path, auth_key: str, entry: dict, unique: bool = True) -> None:
    """
    合并写入。unique=True 时 key 变成 issuer::client_id::user_id，避免多账号互相覆盖。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
    key = auth_key
    if unique and entry.get("user_id"):
        key = f"{auth_key}::{entry['user_id']}"
    existing[key] = entry
    tmp = path.with_suffix(path.suffix + ".tmp")
    _payload = json.dumps(existing, indent=2, ensure_ascii=False) + "\n"
    _fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(_fd, "w", encoding="utf-8") as _fh:
        _fh.write(_payload)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def import_into_project_auth(entry: dict) -> str:
    """Use project's account manager to merge entry into AUTH_FILE."""
    import accounts as _accounts

    # Build a single-entry payload; _normalize_entry will derive user_id/email/expires_at.
    payload = {
        "key": entry["key"],
        "auth_mode": entry.get("auth_mode", "oidc"),
        "email": entry.get("email", ""),
        "refresh_token": entry.get("refresh_token", ""),
        "expires_at": entry.get("expires_at"),
        "oidc_issuer": entry.get("oidc_issuer", OIDC_ISSUER),
        "oidc_client_id": entry.get("oidc_client_id", GROK_CLI_CLIENT_ID),
    }
    result = _accounts.import_auth_payload(payload, merge=True)
    if not result.get("ok"):
        raise RuntimeError(result.get("error") or "import failed")
    imported = result.get("imported", [])
    return imported[0] if imported else ""


def load_sso_list(path: str | None, single: str | None) -> list[tuple[str, str]]:
    """Return list of (email_or_name, sso_cookie) tuples."""
    if single:
        return [("", single.strip())]
    if not path:
        return []
    out: list[tuple[str, str]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        email = ""
        # 兼容 邮箱----密码----sso 或 邮箱:密码:sso
        if "----" in line:
            parts = line.split("----")
            email = parts[0].strip()
            line = parts[-1].strip()
        elif ":" in line and not line.startswith("eyJ"):
            parts = line.rsplit(":", 1)
            email = parts[0].strip()
            line = parts[-1].strip()
        out.append((email, line))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="SSO cookie → grok auth.json (纯 HTTP)")
    ap.add_argument("--sso", metavar="FILE", help="sso 列表文件（一行一个 JWT，或 邮箱----密码----sso）")
    ap.add_argument("--sso-cookie", metavar="JWT", help="单个 sso cookie")
    ap.add_argument("--out", default=None, help="输出 auth.json 路径（单账号或 --merge）")
    ap.add_argument(
        "--out-dir",
        default=None,
        help="批量时每个账号写一个 {user_id}.json（可直接 cp 到 ~/.grok/auth.json）",
    )
    ap.add_argument(
        "--merge",
        action="store_true",
        help="合并到 --out，key 用 issuer::client_id::user_id",
    )
    ap.add_argument(
        "--into-project",
        action="store_true",
        default=True,
        help=f"默认导入到项目 auth.json: {AUTH_FILE}",
    )
    ap.add_argument(
        "--no-into-project",
        dest="into_project",
        action="store_false",
        help="不导入项目 auth.json，仅 --out / --out-dir 输出",
    )
    ap.add_argument("--delay", type=int, default=0, help="每个间隔秒数")
    ap.add_argument("--email", default="", help="写入 entry.email（可选）")
    args = ap.parse_args()

    cookies = load_sso_list(args.sso, args.sso_cookie)
    if not cookies:
        ap.error("需要 --sso 或 --sso-cookie")

    if len(cookies) > 1 and not args.out_dir and not args.merge and not args.into_project:
        args.out_dir = args.out_dir or "./auth_out"
        print(f"批量模式默认 --out-dir {args.out_dir}")

    if args.out is None and args.out_dir is None and len(cookies) == 1 and not args.into_project:
        args.out = str(Path.home() / ".grok" / "auth.json")

    target = "项目 auth.json" if args.into_project else (args.out or args.out_dir or "stdout")
    print(f"🚀 SSO → auth.json: {len(cookies)} 个, target={target}, delay={args.delay}s")
    ok = 0
    fail = 0

def process_one_sso(
    index: int,
    email_hint: str,
    sso: str,
    *,
    args_email: str,
    into_project: bool,
    out_dir: Path | None,
    out: Path | None,
    merge: bool,
    total: int,
) -> dict[str, Any]:
    """Process a single SSO cookie. Thread-safe for independent accounts."""
    result: dict[str, Any] = {"index": index, "email_hint": email_hint, "sso_hint": sso[:12] + "..." if len(sso) > 12 else "..."}
    try:
        token = sso_to_token(sso)
        if not token:
            result["status"] = "failed"
            result["error"] = "device flow failed or invalid sso"
            return result
        key, entry = token_to_auth_entry(token, email=args_email or email_hint)
        uid = entry.get("user_id") or secrets.token_hex(4)

        if out_dir:
            p = out_dir / f"{uid}.json"
            write_auth_json(p, key, entry)
            result["wrote"] = str(p)
        if out:
            if merge or total > 1:
                merge_auth_json(out, key, entry, unique=True)
                result["merged"] = str(out)
            else:
                write_auth_json(out, key, entry)
                result["wrote"] = str(out)
        if into_project:
            aid = import_into_project_auth(entry)
            result["imported_key"] = aid

        result["status"] = "ok"
        result["user_id"] = uid
        result["email"] = entry.get("email")
        return result
    except Exception as e:
        result["status"] = "failed"
        result["error"] = str(e)
        return result


def run_concurrent(
    cookies: list[tuple[str, str]],
    *,
    max_workers: int,
    delay: int,
    args_email: str,
    into_project: bool,
    out_dir: Path | None,
    out: Path | None,
    merge: bool,
) -> tuple[int, int, list[dict[str, Any]]]:
    """Run SSO imports concurrently with per-item delay handled inside threads."""
    results: list[dict[str, Any]] = [None] * len(cookies)
    ok = 0
    fail = 0

    def _worker(args: tuple[int, str, str]) -> tuple[int, dict[str, Any]]:
        i, email_hint, sso = args
        if delay > 0 and i > 1:
            time.sleep(delay * (i - 1))
        res = process_one_sso(
            i,
            email_hint,
            sso,
            args_email=args_email,
            into_project=into_project,
            out_dir=out_dir,
            out=out,
            merge=merge,
            total=len(cookies),
        )
        print(
            f"\n{'=' * 60}\n[{i}/{len(cookies)}] {email_hint or ''}\n{'=' * 60}"
        )
        for k, v in res.items():
            if k in ("index", "email_hint", "sso_hint"):
                continue
            if k == "status":
                mark = "✅" if v == "ok" else "❌"
                print(f"  {mark} [{i}] {v}")
            elif isinstance(v, str):
                print(f"  💾 {k}: {v}")
            else:
                print(f"  • {k}: {v}")
        return i - 1, res

    workers = min(max_workers, max(1, len(cookies)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sso-") as ex:
        for idx, res in ex.map(_worker, ((i, e, s) for i, (e, s) in enumerate(cookies, 1))):
            results[idx] = res
            if res.get("status") == "ok":
                ok += 1
            else:
                fail += 1

    return ok, fail, results


def main() -> int:
    ap = argparse.ArgumentParser(description="SSO cookie → grok auth.json (纯 HTTP)")
    ap.add_argument("--sso", metavar="FILE", help="sso 列表文件（一行一个 JWT，或 邮箱----密码----sso）")
    ap.add_argument("--sso-cookie", metavar="JWT", help="单个 sso cookie")
    ap.add_argument("--out", default=None, help="输出 auth.json 路径（单账号或 --merge）")
    ap.add_argument(
        "--out-dir",
        default=None,
        help="批量时每个账号写一个 {user_id}.json（可直接 cp 到 ~/.grok/auth.json）",
    )
    ap.add_argument(
        "--merge",
        action="store_true",
        help="合并到 --out，key 用 issuer::client_id::user_id",
    )
    ap.add_argument(
        "--into-project",
        action="store_true",
        default=True,
        help=f"默认导入到项目 auth.json: {AUTH_FILE}",
    )
    ap.add_argument(
        "--no-into-project",
        dest="into_project",
        action="store_false",
        help="不导入项目 auth.json，仅 --out / --out-dir 输出",
    )
    ap.add_argument("--delay", type=int, default=0, help="每个间隔秒数")
    ap.add_argument("--email", default="", help="写入 entry.email（可选）")
    ap.add_argument(
        "--threads",
        type=int,
        default=4,
        help="并发线程数（默认 4，最大 8；大量 SSO 时过高会冻 WSL）",
    )
    args = ap.parse_args()

    cookies = load_sso_list(args.sso, args.sso_cookie)
    if not cookies:
        ap.error("需要 --sso 或 --sso-cookie")

    # Hard cap: each worker opens a curl_cffi chrome session; 700× freezes WSL
    threads = max(1, min(int(args.threads or 4), 8))
    if threads != args.threads:
        print(f"⚠️  threads {args.threads} → capped to {threads}")
    args.threads = threads

    if len(cookies) > 1 and not args.out_dir and not args.merge and not args.into_project:
        args.out_dir = args.out_dir or "./auth_out"
        print(f"批量模式默认 --out-dir {args.out_dir}")

    if args.out is None and args.out_dir is None and len(cookies) == 1 and not args.into_project:
        args.out = str(Path.home() / ".grok" / "auth.json")

    target = "项目 auth.json" if args.into_project else (args.out or args.out_dir or "stdout")
    print(f"🚀 SSO → auth.json: {len(cookies)} 个, target={target}, delay={args.delay}s, threads={args.threads}")

    ok, fail, results = run_concurrent(
        cookies,
        max_workers=args.threads,
        delay=args.delay,
        args_email=args.email,
        into_project=args.into_project,
        out_dir=Path(args.out_dir) if args.out_dir else None,
        out=Path(args.out) if args.out else None,
        merge=args.merge,
    )

    print(f"\n{'=' * 60}\n📊 完成: {ok}/{len(cookies)} 成功, {fail} 失败")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
