"""Thread/process-safe auth.json store for multi-account on Linux servers.

Centralizes read/write with:
  - process-local RLock (thread safety)
  - optional file lock via portalocker-like fcntl / msvcrt (best-effort)
  - atomic tmp + replace writes
  - mtime-based in-process cache so 700+ account maps aren't re-parsed constantly
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from config import AUTH_FILE

_thread_lock = threading.RLock()

# In-process read cache (invalidated on write / mtime change)
_cache_lock = threading.RLock()
_cache_path: str | None = None
_cache_mtime_ns: int | None = None
_cache_data: dict[str, Any] | None = None
_cache_stat_at = 0.0
_CACHE_STAT_MIN_INTERVAL = 0.25  # seconds between mtime checks under load


def _lock_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".lock")


@contextmanager
def _file_lock(path: Path, *, timeout: float = 10.0) -> Iterator[None]:
    """Best-effort exclusive file lock (Linux fcntl / Windows msvcrt)."""
    lock_file = _lock_path(path)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_file, "a+b")
    try:
        if fh.tell() == 0:
            fh.write(b"0")
            fh.flush()
    except OSError:
        pass
    deadline = time.time() + timeout
    locked = False
    try:
        while True:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except (OSError, BlockingIOError):
                if time.time() >= deadline:
                    # proceed without lock rather than deadlock the API
                    break
                time.sleep(0.05)
        yield
    finally:
        if locked:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        try:
            fh.close()
        except OSError:
            pass


@contextmanager
def auth_lock(timeout: float = 10.0) -> Iterator[None]:
    with _thread_lock:
        with _file_lock(AUTH_FILE, timeout=timeout):
            yield


def _invalidate_cache(path: Path | None = None) -> None:
    global _cache_path, _cache_mtime_ns, _cache_data, _cache_stat_at
    with _cache_lock:
        if path is None or _cache_path in (None, str(path)):
            _cache_path = None
            _cache_mtime_ns = None
            _cache_data = None
            _cache_stat_at = 0.0


def _set_cache(path: Path, data: dict[str, Any], mtime_ns: int | None) -> None:
    global _cache_path, _cache_mtime_ns, _cache_data, _cache_stat_at
    with _cache_lock:
        _cache_path = str(path)
        _cache_mtime_ns = mtime_ns
        # Store a shallow copy of the map; values are still shared dicts.
        # Callers that mutate must write via write/mutate APIs.
        _cache_data = dict(data)
        _cache_stat_at = time.time()


def _cached_read(path: Path) -> dict[str, Any] | None:
    """Return cached map if mtime unchanged; None on miss."""
    global _cache_stat_at
    with _cache_lock:
        if _cache_data is None or _cache_path != str(path):
            return None
        now = time.time()
        # Under write lock callers already have exclusive access; still cheap-check.
        if now - _cache_stat_at < _CACHE_STAT_MIN_INTERVAL and _cache_mtime_ns is not None:
            return dict(_cache_data)
        try:
            st = path.stat()
            mtime_ns = getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))
        except OSError:
            return None
        _cache_stat_at = now
        if _cache_mtime_ns is not None and mtime_ns == _cache_mtime_ns:
            return dict(_cache_data)
        return None


def _path_mtime_ns(path: Path) -> int | None:
    try:
        st = path.stat()
        return getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))
    except OSError:
        return None


def _dump_json(data: dict[str, Any]) -> str:
    """
    Compact JSON for large multi-account files (much faster + smaller than indent=2).
    Set GROK2API_AUTH_PRETTY=1 to keep human-readable formatting.
    """
    pretty = os.getenv("GROK2API_AUTH_PRETTY", "0").lower() in ("1", "true", "yes")
    if pretty:
        return json.dumps(data, ensure_ascii=False, indent=2)
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def read_auth_map(path: Path | None = None) -> dict[str, Any]:
    path = path or AUTH_FILE
    # Fast path: cache hit without taking file lock (safe for read-mostly)
    cached = _cached_read(path)
    if cached is not None:
        return cached

    with auth_lock():
        # re-check cache under lock (writer may have just finished)
        cached = _cached_read(path)
        if cached is not None:
            return cached
        if not path.is_file():
            _set_cache(path, {}, None)
            return {}
        try:
            text = path.read_text(encoding="utf-8")
            data = json.loads(text)
            if not isinstance(data, dict):
                data = {}
        except (OSError, json.JSONDecodeError):
            data = {}
        _set_cache(path, data, _path_mtime_ns(path))
        return dict(data)


def write_auth_map(data: dict[str, Any], path: Path | None = None) -> None:
    path = path or AUTH_FILE
    try:
        migrate_auth_permissions(path)
    except Exception:
        pass
    with auth_lock():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
        payload = _dump_json(data)
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        # Windows: replace may fail if dest open; retry briefly
        last_err: Exception | None = None
        for _ in range(8):
            try:
                os.replace(str(tmp), str(path))
                last_err = None
                break
            except OSError as e:
                last_err = e
                time.sleep(0.03)
        if last_err is not None:
            try:
                tmp.unlink(missing_ok=True)  # type: ignore[call-arg]
            except TypeError:
                if tmp.exists():
                    tmp.unlink()
            raise last_err
        try:
            os.chmod(path, 0o600)
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        _set_cache(path, data if isinstance(data, dict) else {}, _path_mtime_ns(path))


def mutate_auth_map(mutator) -> dict[str, Any]:
    """
    Read → mutate(dict) → write under one lock.
    mutator receives the map and may modify in place; return value is ignored.
    """
    with auth_lock():
        path = AUTH_FILE
        data: dict[str, Any] = {}
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data = raw
            except (OSError, json.JSONDecodeError):
                data = {}
        mutator(data)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
        payload = _dump_json(data)
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(str(tmp), str(path))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        _set_cache(path, data, _path_mtime_ns(path))
        return data


def migrate_auth_permissions(path: Path | None = None) -> dict[str, int]:
    """Idempotent: chmod auth.json and auth.bak.* to 0600; parent 0700. No content change."""
    path = path or AUTH_FILE
    fixed = skipped = failed = 0
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
    except OSError:
        failed += 1
    targets = []
    if path.exists():
        targets.append(path)
    try:
        targets.extend(sorted(path.parent.glob(path.name + ".bak*")))
        targets.extend(sorted(path.parent.glob("auth.bak.*")))
    except OSError:
        pass
    for fp in targets:
        try:
            os.chmod(fp, 0o600)
            fixed += 1
        except OSError:
            failed += 1
    return {"fixed": fixed, "skipped": skipped, "failed": failed}
