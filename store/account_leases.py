"""Redis-backed in-flight account leases for cross-worker request spreading."""

from __future__ import annotations

import hashlib
import os
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Iterable

from store.redis_client import (
    compare_and_delete,
    key,
    redis_enabled,
    renew_if_owner,
    set_nx_ex,
    worker_id,
)


def _lease_ttl() -> int:
    try:
        raw = int(os.getenv("GROK2API_ACCOUNT_LEASE_TTL", "180") or 180)
    except (TypeError, ValueError):
        raw = 180
    return max(30, min(900, raw))


def lease_key(account_id: str) -> str:
    digest = hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:32]
    return key("account_inflight", digest)


@dataclass(frozen=True)
class _LeaseEntry:
    key: str
    token: str
    ttl: int


_active_lock = threading.RLock()
_active: dict[str, tuple[_LeaseEntry, float]] = {}
_renew_thread: threading.Thread | None = None


def _renew_once() -> None:
    now = time.time()
    with _active_lock:
        due = [
            (token, entry)
            for token, (entry, next_at) in _active.items()
            if now >= next_at
        ]
    for token, entry in due:
        try:
            renewed = renew_if_owner(entry.key, entry.token, entry.ttl)
        except Exception:
            renewed = False
        with _active_lock:
            current = _active.get(token)
            if current is None or current[0] != entry:
                continue
            if renewed:
                _active[token] = (entry, time.time() + max(5.0, entry.ttl / 3.0))


def _renew_loop() -> None:
    while True:
        time.sleep(5.0)
        _renew_once()


def _ensure_renew_thread() -> None:
    global _renew_thread
    with _active_lock:
        if _renew_thread is not None and _renew_thread.is_alive():
            return
        _renew_thread = threading.Thread(
            target=_renew_loop,
            name="g2a-account-lease-renew",
            daemon=True,
        )
        _renew_thread.start()


class _LeaseGroup:
    def __init__(self, entries: list[_LeaseEntry]) -> None:
        self._entries = tuple(entries)
        self._lock = threading.Lock()
        self._released = False
        now = time.time()
        with _active_lock:
            for entry in self._entries:
                _active[entry.token] = (
                    entry,
                    now + max(5.0, entry.ttl / 3.0),
                )
        _ensure_renew_thread()

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        with _active_lock:
            for entry in self._entries:
                _active.pop(entry.token, None)
        for entry in self._entries:
            try:
                compare_and_delete(entry.key, entry.token)
            except Exception:
                # TTL remains the crash/runtime-error recovery boundary.
                pass


class LeasedAccountChain(list[Any]):
    def __init__(
        self,
        items: Iterable[Any],
        *,
        lease_group: _LeaseGroup | None = None,
        affinity_spillover: bool = False,
        degraded: bool = False,
        busy_count: int = 0,
    ) -> None:
        super().__init__(items)
        self._lease_group = lease_group
        self.affinity_spillover = bool(affinity_spillover)
        self.degraded = bool(degraded)
        self.busy_count = max(0, int(busy_count))


def reserve_chain(
    chain: Iterable[Any],
    *,
    preferred_account_id: str | None = None,
) -> LeasedAccountChain:
    items = list(chain)
    if not items or not redis_enabled():
        return LeasedAccountChain(items)

    ttl = _lease_ttl()
    busy_ids: set[str] = set()
    try:
        for index, credential in enumerate(items):
            account_id = str(getattr(credential, "auth_key", "") or "").strip()
            if not account_id:
                continue
            token = f"{worker_id()}:{uuid.uuid4().hex}"
            entry = _LeaseEntry(
                key=lease_key(account_id),
                token=token,
                ttl=ttl,
            )
            if set_nx_ex(entry.key, entry.token, entry.ttl):
                backups = [
                    candidate
                    for offset, candidate in enumerate(items)
                    if offset != index
                    and str(getattr(candidate, "auth_key", "") or "").strip()
                    not in busy_ids
                ]
                group = _LeaseGroup([entry])
                return LeasedAccountChain(
                    [credential] + backups,
                    lease_group=group,
                    affinity_spillover=bool(
                        preferred_account_id
                        and preferred_account_id in busy_ids
                    ),
                    busy_count=len(busy_ids),
                )
            busy_ids.add(account_id)
    except Exception:
        # Runtime Redis failure must not turn into a total API outage.
        return LeasedAccountChain(items, degraded=True)

    return LeasedAccountChain(
        [],
        affinity_spillover=False,
        busy_count=len(busy_ids),
    )


def release_chain(chain: Iterable[Any]) -> None:
    group = getattr(chain, "_lease_group", None)
    if isinstance(group, _LeaseGroup):
        group.release()
