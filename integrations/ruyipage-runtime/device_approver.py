"""xAI Device Flow headless approver (ruyiPage / Firefox).

Default: cold browser per request (production-compatible).
Optional: GROK2API_RUYIPAGE_WARM_BROWSER=1 reuses one Firefox process with
per-task session cleanup and recycle thresholds.

Never log SSO or cookie values.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from ruyipage import FirefoxOptions, FirefoxPage, resolve_firefox_path

app = FastAPI()
lock = threading.Lock()

# ── Warm browser state (single process; serialised by `lock`) ───────────────
_warm_page: FirefoxPage | None = None
_warm_served: int = 0
_warm_started_at: float = 0.0
_warm_timeout_streak: int = 0
_warm_generation: int = 0


def _env_flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def warm_enabled() -> bool:
    return _env_flag("GROK2API_RUYIPAGE_WARM_BROWSER", "0") or _env_flag(
        "RUYIPAGE_WARM_BROWSER", "0"
    )


def recycle_tasks() -> int:
    try:
        return max(1, int(os.getenv("GROK2API_RUYIPAGE_RECYCLE_TASKS", "10") or 10))
    except (TypeError, ValueError):
        return 10


def recycle_sec() -> float:
    try:
        return max(60.0, float(os.getenv("GROK2API_RUYIPAGE_RECYCLE_SEC", "5400") or 5400))
    except (TypeError, ValueError):
        return 5400.0


def poll_interval_sec() -> float:
    try:
        v = float(os.getenv("GROK2API_RUYIPAGE_POLL_MS", "200") or 200) / 1000.0
        return min(1.0, max(0.1, v))
    except (TypeError, ValueError):
        return 0.2


class Req(BaseModel):
    sso: str
    verification_url: str
    user_code: str = ""
    timeout: int = 120
    cookie_mode: str = "sso_only"
    cookie_bundle_path: str = ""
    extra_cookies: list[dict[str, Any]] = Field(default_factory=list)


class RecoverReq(BaseModel):
    url: str
    timeout: int = 60


def _make_options() -> FirefoxOptions:
    opts = FirefoxOptions()
    opts.set_browser_path(resolve_firefox_path())
    opts.headless(True)
    opts.close_on_exit(True)
    opts.set_window_size(1440, 1000)
    proxy = os.getenv("RUYIPAGE_PROXY", "").strip()
    if proxy:
        opts.set_proxy(proxy)
    return opts


def _quit_page(page: FirefoxPage | None) -> None:
    if page is None:
        return
    try:
        page.quit()
    except Exception:
        pass


def _clear_session(page: FirefoxPage) -> bool:
    """Best-effort isolation between tasks on a warm browser."""
    try:
        page.get("about:blank", wait="none")
    except Exception:
        return False
    try:
        page.run_js(
            "try{localStorage.clear()}catch(e){};"
            "try{sessionStorage.clear()}catch(e){};"
        )
    except Exception:
        pass
    try:
        # Clear cookies if API exists
        cookies = page.get_cookies(all_info=True) or []
        for c in cookies:
            name = getattr(c, "name", None) or (c.get("name") if isinstance(c, dict) else None)
            if not name:
                continue
            try:
                page.remove_cookies(name)
            except Exception:
                try:
                    page.set_cookies([{"name": name, "value": "", "domain": ".x.ai", "path": "/"}])
                except Exception:
                    pass
    except Exception:
        return False
    return True


def _recycle_warm(reason: str) -> None:
    global _warm_page, _warm_served, _warm_started_at, _warm_timeout_streak, _warm_generation
    _quit_page(_warm_page)
    _warm_page = None
    _warm_served = 0
    _warm_started_at = 0.0
    _warm_timeout_streak = 0
    _warm_generation += 1


def _acquire_page() -> tuple[FirefoxPage, bool, int]:
    """Return (page, owned, generation). owned=True → caller must quit if not warm."""
    global _warm_page, _warm_served, _warm_started_at
    if not warm_enabled():
        return FirefoxPage(_make_options()), True, _warm_generation

    now = time.time()
    if _warm_page is not None:
        age = now - _warm_started_at if _warm_started_at else 0
        if _warm_served >= recycle_tasks() or age >= recycle_sec():
            _recycle_warm("threshold")
        elif not _clear_session(_warm_page):
            _recycle_warm("clear_failed")

    if _warm_page is None:
        _warm_page = FirefoxPage(_make_options())
        _warm_started_at = now
        _warm_served = 0
    return _warm_page, False, _warm_generation


def _release_page(page: FirefoxPage, owned: bool, *, timeout: bool = False) -> None:
    global _warm_served, _warm_timeout_streak
    if owned:
        _quit_page(page)
        return
    if timeout:
        _warm_timeout_streak += 1
        if _warm_timeout_streak >= 2:
            _recycle_warm("timeout_streak")
            return
    else:
        _warm_timeout_streak = 0
    _warm_served += 1
    if not _clear_session(page):
        _recycle_warm("post_clear_failed")



def _bundle_root() -> Path:
    return Path(os.getenv("GROK2API_COOKIE_BUNDLE_DIR", "/app/data/cookie_bundles")).resolve()

def _allowed_domain(domain: str) -> bool:
    d = (domain or "").strip().lower().lstrip(".")
    if not d:
        return True
    return d == "x.ai" or d.endswith(".x.ai")

def _load_bundle_safe(path_str: str) -> list:
    """Only read ordinary files under cookie bundle root."""
    if not path_str:
        return []
    try:
        root = _bundle_root()
        path = Path(path_str).resolve()
        if not str(path).startswith(str(root) + os.sep) and path != root:
            return []
        if not path.is_file() or path.is_symlink():
            # reject symlink files
            if path.is_symlink():
                return []
            if not path.is_file():
                return []
        # re-check is_file after resolve
        if not path.is_file():
            return []
        import json
        data = json.loads(path.read_text(encoding="utf-8"))
        cookies = data.get("cookies") if isinstance(data, dict) else data
        if not isinstance(cookies, list):
            return []
        out = []
        allow = {"sso", "sso-rw"}
        mode = str((data or {}).get("mode") or "sso_only") if isinstance(data, dict) else "sso_only"
        if mode != "sso_only":
            allow |= {"sso", "sso-rw"}  # auth_bundle still only sso family for v1
        for c in cookies:
            if not isinstance(c, dict):
                continue
            name = str(c.get("name") or "")
            if name not in allow:
                continue
            domain = str(c.get("domain") or ".x.ai")
            if not _allowed_domain(domain):
                continue
            out.append({
                "name": name,
                "value": str(c.get("value") or ""),
                "domain": domain if domain.startswith(".") else f".{domain.lstrip('.')}" if domain else ".x.ai",
                "path": str(c.get("path") or "/"),
                "secure": True,
            })
        return out
    except Exception:
        return []


def _cookie_list(r: Req) -> list:
    """SSO + optional allowlisted cookies; domain restricted to x.ai."""
    cookies = [
        {"name": "sso", "value": r.sso, "domain": ".x.ai", "path": "/", "secure": True},
        {"name": "sso-rw", "value": r.sso, "domain": ".x.ai", "path": "/", "secure": True},
    ]
    mode = str(getattr(r, "cookie_mode", None) or "sso_only")
    allow = {"sso", "sso-rw"}
    if mode == "auth_bundle":
        allow = {"sso", "sso-rw", "cf_clearance"}
    elif mode not in ("sso_only", ""):
        mode = "sso_only"
    # bundle path
    bundle_path = str(getattr(r, "cookie_bundle_path", None) or "")
    if bundle_path and mode == "auth_bundle":
        for c in _load_bundle_safe(bundle_path):
            if c["name"] not in {x["name"] for x in cookies}:
                cookies.append(c)
    for c in getattr(r, "extra_cookies", None) or []:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "")
        if name not in allow:
            continue
        value = c.get("value")
        if value is None:
            continue
        domain = str(c.get("domain") or ".x.ai")
        if not _allowed_domain(domain):
            continue
        # replace existing same name
        cookies = [x for x in cookies if x["name"] != name]
        cookies.append(
            {
                "name": name,
                "value": str(value),
                "domain": domain if domain.startswith(".") else "." + domain.lstrip("."),
                "path": str(c.get("path") or "/"),
                "secure": True,
            }
        )
    return cookies


def _rate_limited_url(url: str) -> bool:
    return "rate_limited" in (url or "").lower()


def _denied_url(url: str) -> bool:
    """Business denial / error. Checked BEFORE success path markers.

    Important: ``.../done?error=access_denied`` or ``.../done?denied=1`` must
    NOT be treated as success just because the path contains ``done``.
    """
    u = (url or "").lower()
    if "rate_limited" in u:
        return False
    # Query / fragment denial markers
    if any(
        x in u
        for x in (
            "access_denied",
            "error=denied",
            "error=access_denied",
            "denied=1",
            "denied=true",
            "/denied",
            "error=",
        )
    ):
        # allow harmless "error" free paths? require error= or denied token
        if "denied" in u or "error=" in u:
            return True
    return False


def _success_url(url: str) -> bool:
    u = (url or "").lower()
    if _denied_url(u) or _rate_limited_url(u):
        return False
    return any(x in u for x in ("/done", "success", "approved"))


def approve(r: Req) -> dict[str, Any]:
    page, owned, generation = _acquire_page()
    clicks: list[str] = []
    timed_out = False
    try:
        page.get("https://accounts.x.ai/", wait="none")
        # State-driven short settle instead of fixed multi-second sleeps
        settle_deadline = time.time() + 2.5
        while time.time() < settle_deadline:
            if page.url:
                break
            page.wait(poll_interval_sec())
        page.set_cookies(_cookie_list(r))
        page.get(r.verification_url, wait="none")
        load_deadline = time.time() + 5.0
        while time.time() < load_deadline:
            url = page.url or ""
            if url and ("accounts.x.ai" in url or "auth.x.ai" in url or "oauth" in url):
                break
            page.wait(poll_interval_sec())

        deadline = time.time() + max(5, int(r.timeout or 120))
        candidates = [
            "xpath://button[contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'allow')]",
            "xpath://button[contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'authorize')]",
            "xpath://button[contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'approve')]",
            "xpath://button[contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'continue')]",
            "xpath://button[contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'confirm')]",
            "css:button[type='submit']",
        ]
        while time.time() < deadline:
            url = page.url or ""
            # Order: rate limit → denied → success (never success-before-denied)
            if _rate_limited_url(url):
                return {
                    "ok": False,
                    "rate_limited": True,
                    "url": url,
                    "clicks": clicks,
                    "browser_generation": generation,
                }
            if _denied_url(url):
                return {
                    "ok": False,
                    "denied": True,
                    "url": url,
                    "clicks": clicks,
                    "browser_generation": generation,
                }
            if _success_url(url):
                return {
                    "ok": True,
                    "url": url,
                    "clicks": clicks,
                    "browser_generation": generation,
                    "warm": warm_enabled(),
                }
            clicked = False
            for q in candidates:
                try:
                    e = page.ele(q, timeout=0.25)
                    if e and e.states.is_displayed:
                        txt = (e.text or "")[:80]
                        e.click()
                        clicks.append(txt or q)
                        # Brief post-click wait, state loop continues
                        page.wait(poll_interval_sec())
                        clicked = True
                        break
                except Exception:
                    pass
            if not clicked:
                page.wait(poll_interval_sec())
        timed_out = True
        return {
            "ok": False,
            "url": page.url,
            "title": page.title,
            "clicks": clicks,
            "timeout": True,
            "browser_generation": generation,
            "warm": warm_enabled(),
        }
    finally:
        _release_page(page, owned, timeout=timed_out)


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "warm": warm_enabled(),
        "warm_served": _warm_served,
        "browser_generation": _warm_generation,
    }


@app.post("/approve")
def api_approve(r: Req) -> dict[str, Any]:
    if not r.sso or not r.verification_url:
        raise HTTPException(400, "missing fields")
    with lock:
        return approve(r)


@app.post("/recover")
def recover(r: RecoverReq) -> dict[str, Any]:
    with lock:
        page = FirefoxPage(_make_options())
        try:
            page.get(r.url, wait="none")
            deadline = time.time() + r.timeout
            while time.time() < deadline:
                for c in page.get_cookies(all_info=True):
                    if getattr(c, "name", "") in ("sso", "sso-rw") and getattr(c, "value", ""):
                        return {"ok": True, "sso": c.value, "url": page.url}
                page.wait(1)
            return {
                "ok": False,
                "url": page.url,
                "cookies": [getattr(c, "name", "") for c in page.get_cookies(all_info=True)],
            }
        finally:
            page.quit()
