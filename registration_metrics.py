"""Structured registration funnel metrics (no secrets).

Stage 0: observation only. Writes to SQLite; never changes pipeline behaviour.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from registration_jobs import redact_text


def metrics_enabled() -> bool:
    return os.getenv("GROK2API_METRICS_ENABLED", "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def default_metrics_db() -> Path:
    raw = os.getenv("GROK2API_REGISTRATION_METRICS_DB", "").strip()
    if raw:
        return Path(raw)
    data = Path(os.getenv("GROK2API_DATA_DIR", "data"))
    try:
        data.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Fall back to process-private temp when data dir is not writable
        # (unit tests / restricted sandboxes).
        data = Path(os.getenv("TMPDIR") or os.getenv("TMP") or "/tmp") / "grok-metrics"
        data.mkdir(parents=True, exist_ok=True)
    return data / "registration_metrics.db"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  event TEXT NOT NULL,
  session_id TEXT,
  job_id TEXT,
  route_id TEXT,
  approver_id TEXT,
  cookie_mode TEXT,
  browser_generation TEXT,
  producer_version TEXT,
  hour_bucket TEXT,
  duration_ms REAL,
  ok INTEGER,
  error_class TEXT,
  fields_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_event ON events(event);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
"""


class RegistrationMetrics:
    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path else default_metrics_db()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init()

    def _init(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA)
                conn.commit()
            finally:
                conn.close()
            self._chmod_storage_files()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        self._chmod_storage_files()
        return conn

    def _chmod_storage_files(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            path = Path(f"{self.db_path}{suffix}")
            try:
                if path.exists() and not path.is_symlink():
                    os.chmod(path, 0o600)
            except OSError:
                pass

    def emit(
        self,
        event: str,
        *,
        session_id: str = "",
        job_id: str = "",
        route_id: str = "",
        approver_id: str = "",
        cookie_mode: str = "",
        browser_generation: str = "",
        producer_version: str = "",
        duration_ms: float | None = None,
        ok: bool | None = None,
        error_class: str = "",
        **fields: Any,
    ) -> None:
        if not metrics_enabled():
            return
        # Drop any accidental secret-looking fields
        safe_fields: dict[str, Any] = {}
        banned = (
            "sso",
            "cookie",
            "token",
            "password",
            "authorization",
            "api_key",
            "refresh",
            "access_token",
            "refresh_token",
        )
        for k, v in fields.items():
            lk = k.lower()
            if any(b in lk for b in banned):
                continue
            if isinstance(v, str):
                safe_fields[k] = redact_text(v)[:500]
            elif isinstance(v, (int, float, bool)) or v is None:
                safe_fields[k] = v
            else:
                safe_fields[k] = redact_text(str(v))[:500]

        ts = time.time()
        hour_bucket = time.strftime("%Y%m%d%H", time.gmtime(ts))
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        with self._lock:
            try:
                conn = self._connect()
            except sqlite3.Error:
                return
            try:
                conn.execute(
                    """
                    INSERT INTO events (
                      ts, event, session_id, job_id, route_id, approver_id,
                      cookie_mode, browser_generation, producer_version,
                      hour_bucket, duration_ms, ok, error_class, fields_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        ts,
                        event[:64],
                        (session_id or "")[:96],
                        (job_id or "")[:96],
                        (route_id or "")[:32],
                        (approver_id or "")[:32],
                        (cookie_mode or "")[:32],
                        (browser_generation or "")[:32],
                        (producer_version or "")[:64],
                        hour_bucket,
                        duration_ms,
                        None if ok is None else (1 if ok else 0),
                        (error_class or "")[:64],
                        json.dumps(safe_fields, ensure_ascii=False),
                    ),
                )
                conn.commit()
            except sqlite3.Error:
                return
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
            self._chmod_storage_files()

    def count_event(self, event: str, *, since: float | None = None) -> int:
        with self._lock:
            conn = self._connect()
            try:
                if since is None:
                    cur = conn.execute(
                        "SELECT COUNT(*) AS c FROM events WHERE event=?", (event,)
                    )
                else:
                    cur = conn.execute(
                        "SELECT COUNT(*) AS c FROM events WHERE event=? AND ts>=?",
                        (event, since),
                    )
                return int(cur.fetchone()["c"])
            finally:
                conn.close()

    def funnel(self, *, since: float | None = None) -> dict[str, int]:
        events = [
            "signup_started",
            "turnstile_solved",
            "otp_received",
            "signup_complete",
            "sso_obtained",
            "mint_started",
            "browser_done",
            "browser_denied",
            "browser_timeout",
            "token_received",
            "refresh_token_received",
            "probe_passed",
            "auth_imported",
            "first_refresh_ok",
        ]
        return {e: self.count_event(e, since=since) for e in events}

    def purge(
        self,
        *,
        before: float,
        max_rows: int = 200_000,
        limit: int = 200,
    ) -> dict[str, int]:
        """Bound retention by age and row cap without a long SQLite lock."""
        batch = max(0, int(limit))
        row_cap = max(0, int(max_rows))
        if batch == 0:
            return {"deleted": 0, "remaining": 0}
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                old = conn.execute(
                    """
                    DELETE FROM events
                    WHERE id IN (
                      SELECT id FROM events WHERE ts < ? ORDER BY ts ASC, id ASC LIMIT ?
                    )
                    """,
                    (float(before), batch),
                )
                deleted = max(0, int(old.rowcount or 0))
                remaining = int(
                    conn.execute("SELECT COUNT(*) AS c FROM events").fetchone()["c"]
                )
                capacity = max(0, batch - deleted)
                overflow = max(0, remaining - row_cap)
                if capacity and overflow:
                    trim = min(capacity, overflow)
                    capped = conn.execute(
                        """
                        DELETE FROM events
                        WHERE id IN (
                          SELECT id FROM events ORDER BY ts ASC, id ASC LIMIT ?
                        )
                        """,
                        (trim,),
                    )
                    removed = max(0, int(capped.rowcount or 0))
                    deleted += removed
                    remaining -= removed
                conn.execute("COMMIT")
                return {"deleted": deleted, "remaining": max(0, remaining)}
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                conn.close()

    def sample_resources(self) -> dict[str, Any]:
        """Best-effort host resource sample (Linux /proc; else empty)."""
        out: dict[str, Any] = {"ts": time.time()}
        try:
            load = os.getloadavg()
            out["loadavg"] = list(load)
        except OSError:
            pass
        try:
            meminfo = Path("/proc/meminfo")
            if meminfo.is_file():
                data = {}
                for line in meminfo.read_text().splitlines():
                    if ":" in line:
                        k, v = line.split(":", 1)
                        data[k.strip()] = v.strip()
                # MemAvailable: 12345 kB
                for key in ("MemAvailable", "MemFree", "MemTotal"):
                    raw = data.get(key, "")
                    parts = raw.split()
                    if parts:
                        out[key] = int(parts[0]) * 1024  # bytes
        except (OSError, ValueError):
            pass
        self.emit("resource_sample", **{k: v for k, v in out.items() if k != "ts"})
        return out


_METRICS: RegistrationMetrics | None = None
_LOCK = threading.Lock()


def get_metrics() -> RegistrationMetrics:
    global _METRICS
    with _LOCK:
        if _METRICS is None:
            _METRICS = RegistrationMetrics()
        return _METRICS


def reset_metrics_for_tests(db_path: Path | str) -> RegistrationMetrics:
    global _METRICS
    with _LOCK:
        _METRICS = RegistrationMetrics(db_path)
        return _METRICS


def emit(event: str, **kwargs: Any) -> None:
    get_metrics().emit(event, **kwargs)


# ── Adaptive protection helpers (stage 6 hooks; decision only) ──────────────


def protection_action(sample: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return recommended action from resource sample. Does not apply it."""
    sample = sample or get_metrics().sample_resources()
    mem_avail = sample.get("MemAvailable")
    action = {
        "pause_registration": False,
        "pause_mint": False,
        "force_concurrency": None,
        "mint_single_route": False,
        "recycle_browser": False,
        "reason": "",
    }
    if isinstance(mem_avail, int):
        gib = mem_avail / (1024**3)
        if gib < 1.8:
            action.update(
                pause_registration=True,
                pause_mint=True,
                reason="mem_available_lt_1.8g",
            )
        elif gib < 2.5:
            action.update(
                mint_single_route=True,
                reason="mem_available_lt_2.5g",
            )
        elif gib < 3.0:
            action.update(
                pause_registration=True,
                recycle_browser=True,
                reason="mem_available_lt_3.0g",
            )
    return action
