"""Private filesystem helpers for credentials and operational state.

The permission migrator deliberately inspects metadata only.  It never opens
or logs the contents of an existing secret file.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any, Iterable


PRIVATE_FILE_MODE = 0o600
PRIVATE_DIR_MODE = 0o700


def _chmod(path: Path, mode: int) -> bool:
    """Apply a private mode without following symlinks."""
    try:
        if path.is_symlink():
            return False
        os.chmod(path, mode, follow_symlinks=False)
        return True
    except (NotImplementedError, OSError):
        return False


def ensure_private_dir(path: Path | str) -> Path:
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    if not _chmod(target, PRIVATE_DIR_MODE):
        raise OSError("unable to secure private directory")
    return target


def atomic_write_private(
    path: Path | str,
    payload: str | bytes,
    *,
    encoding: str = "utf-8",
) -> None:
    """Atomically replace *path* using a freshly-created 0600 temporary file."""
    target = Path(path)
    parent = ensure_private_dir(target.parent)
    tmp = parent / f".{target.name}.tmp.{os.getpid()}.{secrets.token_hex(4)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(str(tmp), flags, PRIVATE_FILE_MODE)
    try:
        mode = "wb" if isinstance(payload, bytes) else "w"
        kwargs = {} if isinstance(payload, bytes) else {"encoding": encoding}
        with os.fdopen(fd, mode, **kwargs) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        if not _chmod(target, PRIVATE_FILE_MODE):
            raise OSError("unable to secure private file")
        # Persist the directory entry where the platform supports directory fsync.
        try:
            dir_fd = os.open(str(parent), os.O_RDONLY)
        except OSError:
            dir_fd = -1
        if dir_fd >= 0:
            try:
                os.fsync(dir_fd)
            except OSError:
                pass
            finally:
                os.close(dir_fd)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def atomic_write_private_json(
    path: Path | str,
    data: Any,
    *,
    pretty: bool = False,
) -> None:
    separators = None if pretty else (",", ":")
    text = json.dumps(
        data,
        ensure_ascii=False,
        indent=2 if pretty else None,
        separators=separators,
    )
    atomic_write_private(path, text)


def _existing_regular_files(root: Path) -> Iterable[Path]:
    if not root.is_dir() or root.is_symlink():
        return
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        # Do not descend through a directory symlink.
        dirnames[:] = [
            name for name in dirnames if not (current_path / name).is_symlink()
        ]
        for filename in filenames:
            path = current_path / filename
            if path.is_symlink():
                continue
            try:
                if path.is_file():
                    yield path
            except OSError:
                continue


def migrate_secret_permissions(
    *,
    data_dir: Path | str | None = None,
    auth_file: Path | str | None = None,
    settings_file: Path | str | None = None,
    keys_file: Path | str | None = None,
    secret_dirs: Iterable[Path | str] | None = None,
    database_paths: Iterable[Path | str] | None = None,
    strict: bool = True,
) -> dict[str, int]:
    """Idempotently secure persisted secrets using metadata-only operations."""
    if data_dir is None or auth_file is None or settings_file is None or keys_file is None:
        from config import AUTH_FILE, DATA_DIR, KEYS_FILE, SETTINGS_FILE

        data_dir = DATA_DIR if data_dir is None else data_dir
        auth_file = AUTH_FILE if auth_file is None else auth_file
        settings_file = SETTINGS_FILE if settings_file is None else settings_file
        keys_file = KEYS_FILE if keys_file is None else keys_file

    data = Path(data_dir)
    auth = Path(auth_file)
    settings = Path(settings_file)
    keys = Path(keys_file)
    data.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)

    if secret_dirs is None:
        cookie_dir = Path(
            os.getenv("GROK2API_COOKIE_BUNDLE_DIR", "").strip()
            or data / "cookie_bundles"
        )
        pending_dir = Path(
            os.getenv("GROK2API_PENDING_SSO_DIR", "").strip()
            or data / "pending_sso"
        )
        secret_dirs = (pending_dir, cookie_dir, data / "register_sso")

    if database_paths is None:
        queue_db = Path(
            os.getenv("GROK2API_REGISTRATION_QUEUE_DB", "").strip()
            or data / "registration_queue.db"
        )
        metrics_db = Path(
            os.getenv("GROK2API_REGISTRATION_METRICS_DB", "").strip()
            or data / "registration_metrics.db"
        )
        database_paths = (queue_db, metrics_db)

    directories = {data, auth.parent, settings.parent, keys.parent}
    directories.update(Path(path) for path in secret_dirs)
    file_targets = {auth, settings, keys}

    # Auth backups and lock files carry the same confidentiality as auth.json.
    for pattern in (auth.name + ".bak*", "auth.bak.*", auth.name + ".lock"):
        try:
            file_targets.update(auth.parent.glob(pattern))
        except OSError:
            pass

    for db_path_raw in database_paths:
        db_path = Path(db_path_raw)
        directories.add(db_path.parent)
        file_targets.update(
            {
                db_path,
                Path(str(db_path) + "-wal"),
                Path(str(db_path) + "-shm"),
                Path(str(db_path) + "-journal"),
            }
        )

    counts = {"directories": 0, "files": 0, "failed": 0, "skipped": 0}
    for directory in sorted(directories, key=lambda item: str(item)):
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
        except OSError:
            counts["failed"] += 1
            continue
        if _chmod(directory, PRIVATE_DIR_MODE):
            counts["directories"] += 1
        else:
            counts["failed"] += 1

    for secret_dir_raw in secret_dirs:
        secret_dir = Path(secret_dir_raw)
        file_targets.update(_existing_regular_files(secret_dir))

    for path in sorted(file_targets, key=lambda item: str(item)):
        if path.is_symlink() or not path.exists():
            counts["skipped"] += 1
            continue
        try:
            is_file = path.is_file()
        except OSError:
            is_file = False
        if not is_file:
            counts["skipped"] += 1
            continue
        if _chmod(path, PRIVATE_FILE_MODE):
            counts["files"] += 1
        else:
            counts["failed"] += 1

    if strict and counts["failed"]:
        raise OSError(
            "secret permission migration failed for "
            f"{counts['failed']} filesystem object(s)"
        )
    return counts
