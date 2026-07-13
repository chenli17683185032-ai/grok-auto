"""Account-level route affinity for registration / mint / token poll.

Default behaviour with GROK2API_ROUTE_STICKY=0: callers may ignore this module
and keep the legacy global proxy + round-robin approver behaviour.
"""

from __future__ import annotations

import hashlib
import os
import threading
from dataclasses import dataclass
from typing import Any


def _env_flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def route_sticky_enabled() -> bool:
    return _env_flag("GROK2API_ROUTE_STICKY", "0")


@dataclass(frozen=True)
class Route:
    route_id: str
    register_proxy: str
    token_proxy: str
    approver: str
    approver_id: str
    mihomo_id: str

    def as_dict(self) -> dict[str, str]:
        return {
            "route_id": self.route_id,
            "register_proxy": self.register_proxy,
            "token_proxy": self.token_proxy,
            "approver": self.approver,
            "approver_id": self.approver_id,
            "mihomo_id": self.mihomo_id,
        }


def _default_routes() -> tuple[Route, ...]:
    """Build the two production routes from env with safe defaults."""
    p1 = (
        os.getenv("GROK2API_ROUTE1_PROXY", "").strip()
        or os.getenv("GROK2API_PROXY", "").strip()
        or os.getenv("GROK2API_XAI_PROXY", "").strip()
        or "http://grok-mihomo:7890"
    )
    p2 = (
        os.getenv("GROK2API_ROUTE2_PROXY", "").strip()
        or "http://grok-mihomo-2:7890"
    )
    a1 = (
        os.getenv("GROK2API_ROUTE1_APPROVER", "").strip()
        or "http://ruyipage-approver:8765"
    )
    a2 = (
        os.getenv("GROK2API_ROUTE2_APPROVER", "").strip()
        or "http://ruyipage-approver-2:8765"
    )
    # Honour GROK2API_RUYIPAGE_APPROVERS order when present.
    raw = os.getenv("GROK2API_RUYIPAGE_APPROVERS", "").strip()
    if raw:
        parts = [
            v.strip().rstrip("/")
            for v in raw.replace(";", ",").split(",")
            if v.strip()
        ]
        if len(parts) >= 1:
            a1 = parts[0]
        if len(parts) >= 2:
            a2 = parts[1]
    return (
        Route(
            route_id="route-1",
            register_proxy=p1,
            token_proxy=p1,
            approver=a1.rstrip("/"),
            approver_id="approver-1",
            mihomo_id="mihomo-1",
        ),
        Route(
            route_id="route-2",
            register_proxy=p2,
            token_proxy=p2,
            approver=a2.rstrip("/"),
            approver_id="approver-2",
            mihomo_id="mihomo-2",
        ),
    )


class RouteRegistry:
    """Process-local registry with stable session → route assignment."""

    def __init__(self, routes: tuple[Route, ...] | None = None) -> None:
        self._routes = routes or _default_routes()
        self._by_id = {r.route_id: r for r in self._routes}
        self._session_map: dict[str, str] = {}
        self._lock = threading.Lock()
        self._rr = 0

    def list_routes(self) -> list[Route]:
        return list(self._routes)

    def get(self, route_id: str) -> Route:
        if route_id not in self._by_id:
            raise KeyError(f"unknown route_id={route_id!r}")
        return self._by_id[route_id]

    def assign_route(self, session_id: str, *, prefer: str | None = None) -> str:
        """Pin session_id to one route. Once assigned, never changes."""
        sid = (session_id or "").strip() or "anonymous"
        with self._lock:
            existing = self._session_map.get(sid)
            if existing:
                return existing
            if prefer and prefer in self._by_id:
                rid = prefer
            else:
                # Stable hash keeps A/B and restarts consistent for same session_id.
                digest = hashlib.sha256(sid.encode("utf-8")).digest()
                idx = digest[0] % len(self._routes)
                rid = self._routes[idx].route_id
            self._session_map[sid] = rid
            return rid

    def assign_round_robin(self, session_id: str) -> str:
        sid = (session_id or "").strip() or "anonymous"
        with self._lock:
            existing = self._session_map.get(sid)
            if existing:
                return existing
            rid = self._routes[self._rr % len(self._routes)].route_id
            self._rr += 1
            self._session_map[sid] = rid
            return rid

    def route_for_session(self, session_id: str) -> Route | None:
        with self._lock:
            rid = self._session_map.get((session_id or "").strip())
        return self._by_id.get(rid) if rid else None

    def proxy_for(self, route_id: str, phase: str = "token") -> str:
        route = self.get(route_id)
        if phase == "register":
            return route.register_proxy
        return route.token_proxy

    def approver_for(self, route_id: str) -> str:
        return self.get(route_id).approver

    def bind_existing(self, session_id: str, route_id: str) -> None:
        """Restore a previously persisted binding (queue recovery)."""
        if route_id not in self._by_id:
            raise KeyError(f"unknown route_id={route_id!r}")
        with self._lock:
            self._session_map[(session_id or "").strip()] = route_id


_REGISTRY: RouteRegistry | None = None
_REGISTRY_LOCK = threading.Lock()


def get_registry() -> RouteRegistry:
    global _REGISTRY
    with _REGISTRY_LOCK:
        if _REGISTRY is None:
            _REGISTRY = RouteRegistry()
        return _REGISTRY


def reset_registry_for_tests(routes: tuple[Route, ...] | None = None) -> RouteRegistry:
    global _REGISTRY
    with _REGISTRY_LOCK:
        _REGISTRY = RouteRegistry(routes)
        return _REGISTRY


def public_route_snapshot() -> list[dict[str, Any]]:
    """Safe for metrics/admin: no secrets beyond hostnames already in env."""
    return [r.as_dict() for r in get_registry().list_routes()]
