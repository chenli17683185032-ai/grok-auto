"""Single-owner bounded retention for registration production state."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Iterable

from secure_storage import atomic_write_private_json, ensure_private_dir


_last_attempt_at = 0.0
_last_result: dict[str, Any] = {}


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.getenv(name, str(default)) or default))
    except (TypeError, ValueError):
        return max(minimum, default)


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default)) or default))
    except (TypeError, ValueError):
        return max(minimum, default)


def _enabled() -> bool:
    return os.getenv("GROK2API_RETENTION_ENABLED", "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _acquire_owner_lock(path: Path):
    """Return a locked fd, or None when another process owns maintenance."""
    ensure_private_dir(path.parent)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    os.chmod(path, 0o600)
    try:
        if os.name == "nt":  # pragma: no cover - production is Linux
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except (OSError, BlockingIOError):
        os.close(fd)
        return None


def _release_owner_lock(fd: int) -> None:
    try:
        if os.name == "nt":  # pragma: no cover - production is Linux
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        os.close(fd)


def _resolved_paths(paths: Iterable[str | Path]) -> set[str]:
    out: set[str] = set()
    for raw in paths:
        if not raw:
            continue
        try:
            out.add(str(Path(raw).resolve(strict=False)))
        except OSError:
            continue
    return out


def _sweep_pending(
    root: Path,
    *,
    protected_paths: Iterable[str | Path],
    max_age_sec: float,
    max_delete: int,
    now: float,
) -> int:
    """Metadata-only bounded cleanup; never opens pending SSO contents."""
    if not root.is_dir() or max_delete <= 0:
        return 0
    protected = _resolved_paths(protected_paths)
    candidates: list[tuple[float, Path]] = []
    try:
        paths = list(root.glob("*.json"))
    except OSError:
        return 0
    for path in paths:
        try:
            if path.is_symlink() or not path.is_file():
                continue
            if str(path.resolve(strict=False)) in protected:
                continue
            modified = path.stat().st_mtime
            if now - modified > max_age_sec:
                candidates.append((modified, path))
        except OSError:
            continue
    deleted = 0
    for _modified, path in sorted(
        candidates, key=lambda item: (item[0], item[1].name)
    )[:max_delete]:
        try:
            path.unlink(missing_ok=True)
            deleted += 1
        except OSError:
            continue
    return deleted


def status() -> dict[str, Any]:
    return dict(_last_result)


def run_if_due(
    *,
    force: bool = False,
    now: float | None = None,
    data_dir: Path | str | None = None,
    queue=None,
    metrics=None,
) -> dict[str, Any]:
    """Run one bounded cleanup as the cross-process retention owner."""
    global _last_attempt_at, _last_result

    current = time.time() if now is None else float(now)
    if not _enabled():
        return {"ran": False, "reason": "disabled"}
    interval = _env_float("GROK2API_RETENTION_INTERVAL_SEC", 3600.0, minimum=60.0)
    if not force and _last_attempt_at and current - _last_attempt_at < interval:
        return {"ran": False, "reason": "not_due", **status()}
    _last_attempt_at = current

    if data_dir is None:
        data_dir = os.getenv("GROK2API_DATA_DIR", "data")
    data = ensure_private_dir(Path(data_dir))
    lock_fd = _acquire_owner_lock(data / "retention.lock")
    if lock_fd is None:
        return {"ran": False, "reason": "owner_busy"}

    batch = _env_int("GROK2API_RETENTION_BATCH_SIZE", 200, minimum=1)
    queue_age = _env_float("GROK2API_QUEUE_RETENTION_SEC", 7 * 86400.0)
    metrics_age = _env_float("GROK2API_METRICS_RETENTION_SEC", 7 * 86400.0)
    temp_age = _env_float("GROK2API_TEMP_FILE_TTL_SEC", 48 * 3600.0)
    metrics_max_rows = _env_int(
        "GROK2API_METRICS_MAX_ROWS", 200_000, minimum=1000
    )
    result: dict[str, Any] = {
        "ran": True,
        "at": current,
        "queue_terminal_deleted": 0,
        "metrics_deleted": 0,
        "metrics_remaining": 0,
        "cookie_deleted": 0,
        "pending_deleted": 0,
        "errors": [],
    }
    try:
        if queue is None:
            from registration_queue import RegistrationQueue

            queue = RegistrationQueue()
        protected: set[str] = set()
        try:
            protected = set(queue.active_material_paths())
            result["queue_terminal_deleted"] = queue.purge_terminal(
                before=current - queue_age, limit=batch
            )
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"queue:{type(exc).__name__}")

        if metrics is None:
            from registration_metrics import get_metrics

            metrics = get_metrics()
        try:
            metric_result = metrics.purge(
                before=current - metrics_age,
                max_rows=metrics_max_rows,
                limit=batch,
            )
            result["metrics_deleted"] = int(metric_result.get("deleted") or 0)
            result["metrics_remaining"] = int(metric_result.get("remaining") or 0)
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"metrics:{type(exc).__name__}")

        try:
            import cookie_bundle

            result["cookie_deleted"] = cookie_bundle.sweep_expired(
                root=Path(
                    os.getenv("GROK2API_COOKIE_BUNDLE_DIR", "").strip()
                    or data / "cookie_bundles"
                ),
                max_age_sec=temp_age,
                max_delete=batch,
                protected_paths=protected,
                now=current,
            )
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"cookie:{type(exc).__name__}")

        try:
            pending_dir = Path(
                os.getenv("GROK2API_PENDING_SSO_DIR", "").strip()
                or data / "pending_sso"
            )
            result["pending_deleted"] = _sweep_pending(
                pending_dir,
                protected_paths=protected,
                max_age_sec=temp_age,
                max_delete=batch,
                now=current,
            )
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"pending:{type(exc).__name__}")

        result["ok"] = not result["errors"]
        _last_result = dict(result)
        atomic_write_private_json(data / "retention_status.json", result, pretty=True)
        return dict(result)
    finally:
        _release_owner_lock(lock_fd)
