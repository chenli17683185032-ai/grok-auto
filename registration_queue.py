"""Persistent registration / mint job queue (SQLite WAL).

Atomic claim, lease generation fencing, reclaim of in-flight states after
lease expiry, and hard-limit enqueue in a single write transaction.
"""

from __future__ import annotations

import json
import os
import sqlite3
from sqlite3 import IntegrityError
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from registration_jobs import (
    JobState,
    RECLAIMABLE_RUNNING,
    RegistrationJob,
    email_hash,
    new_job_id,
)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


def pipeline_v2_enabled() -> bool:
    return os.getenv("GROK2API_PIPELINE_V2", "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def soft_limit() -> int:
    return max(1, _env_int("GROK2API_REGISTRATION_QUEUE_SOFT_LIMIT", 12))


def hard_limit() -> int:
    return max(soft_limit(), _env_int("GROK2API_REGISTRATION_QUEUE_HARD_LIMIT", 30))


def default_db_path() -> Path:
    raw = os.getenv("GROK2API_REGISTRATION_QUEUE_DB", "").strip()
    if raw:
        return Path(raw)
    data = Path(os.getenv("GROK2API_DATA_DIR", "data"))
    return data / "registration_queue.db"


_CAPACITY_TERMINAL = (
    JobState.AUTH_IMPORTED.value,
    JobState.DEAD_LETTER.value,
    JobState.FAILED.value,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  route_id TEXT NOT NULL,
  state TEXT NOT NULL,
  email_hash TEXT,
  sso_ref TEXT,
  cookie_bundle_path TEXT,
  cookie_mode TEXT,
  error_class TEXT,
  error_code TEXT,
  attempts INTEGER DEFAULT 0,
  lease_owner TEXT,
  lease_until REAL DEFAULT 0,
  lease_generation INTEGER DEFAULT 0,
  next_run_at REAL DEFAULT 0,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  payload_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_state_next ON jobs(state, next_run_at, lease_until);
CREATE INDEX IF NOT EXISTS idx_jobs_session ON jobs(session_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_session_one_active
  ON jobs(session_id)
  WHERE state NOT IN ('auth_imported','dead_letter','failed');
"""


class RegistrationQueue:
    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path else default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=60,
            check_same_thread=False,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=60000")
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA)
                # Migrate older DBs missing lease_generation
                cols = {
                    r["name"]
                    for r in conn.execute("PRAGMA table_info(jobs)").fetchall()
                }
                if "lease_generation" not in cols:
                    conn.execute(
                        "ALTER TABLE jobs ADD COLUMN lease_generation INTEGER DEFAULT 0"
                    )
            finally:
                conn.close()
            try:
                os.chmod(self.db_path, 0o600)
            except OSError:
                pass

    def count_open(self) -> int:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    f"SELECT COUNT(*) AS c FROM jobs WHERE state NOT IN ({','.join('?' * len(_CAPACITY_TERMINAL))})",
                    _CAPACITY_TERMINAL,
                )
                return int(cur.fetchone()["c"])
            finally:
                conn.close()

    def remaining_capacity(self, *, hard: bool = True) -> int:
        limit = hard_limit() if hard else soft_limit()
        return max(0, limit - self.count_open())

    def can_accept(self, *, hard: bool = False) -> bool:
        return self.remaining_capacity(hard=hard) > 0

    def _row_params(self, job: RegistrationJob) -> tuple[Any, ...]:
        row = job.to_row()
        payload = row.get("payload_json") or {}
        payload_json = (
            json.dumps(payload, ensure_ascii=False)
            if not isinstance(payload, str)
            else payload
        )
        return (
            row["job_id"],
            row["session_id"],
            row["route_id"],
            row["state"],
            row["email_hash"],
            row["sso_ref"],
            row["cookie_bundle_path"],
            row["cookie_mode"],
            row["error_class"],
            row["error_code"],
            row["attempts"],
            row["lease_owner"],
            row["lease_until"],
            int(row.get("lease_generation") or 0),
            row["next_run_at"],
            row["created_at"],
            row["updated_at"],
            payload_json,
        )

    def enqueue(self, job: RegistrationJob) -> RegistrationJob:
        """Insert job; hard-limit check is inside the same write transaction."""
        job.updated_at = time.time()
        params = self._row_params(job)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                cur = conn.execute(
                    f"SELECT COUNT(*) AS c FROM jobs WHERE state NOT IN ({','.join('?' * len(_CAPACITY_TERMINAL))})",
                    _CAPACITY_TERMINAL,
                )
                open_count = int(cur.fetchone()["c"])
                if open_count >= hard_limit():
                    conn.execute("ROLLBACK")
                    raise RuntimeError("registration queue hard limit reached")
                try:
                    conn.execute(
                        """
                        INSERT INTO jobs (
                          job_id, session_id, route_id, state, email_hash, sso_ref,
                          cookie_bundle_path, cookie_mode, error_class, error_code,
                          attempts, lease_owner, lease_until, lease_generation,
                          next_run_at, created_at, updated_at, payload_json
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        params,
                    )
                    conn.execute("COMMIT")
                except IntegrityError:
                    conn.execute("ROLLBACK")
                    existing = None
                    conn2 = self._connect()
                    try:
                        cur = conn2.execute(
                            "SELECT * FROM jobs WHERE session_id=? ORDER BY created_at DESC LIMIT 1",
                            (job.session_id,),
                        )
                        row = cur.fetchone()
                        if row:
                            return RegistrationJob.from_row(dict(row))
                    finally:
                        conn2.close()
                    raise RuntimeError("duplicate active session job")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                conn.close()
            try:
                os.chmod(self.db_path, 0o600)
            except OSError:
                pass
        return job

    def save(self, job: RegistrationJob, *, require_fence: bool = False) -> bool:
        """Persist job. If require_fence, only when lease_owner+generation match DB."""
        job.updated_at = time.time()
        params = self._row_params(job)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                if require_fence and job.lease_owner:
                    cur = conn.execute(
                        """
                        UPDATE jobs SET
                          session_id=?, route_id=?, state=?, email_hash=?, sso_ref=?,
                          cookie_bundle_path=?, cookie_mode=?, error_class=?, error_code=?,
                          attempts=?, lease_owner=?, lease_until=?, lease_generation=?,
                          next_run_at=?, updated_at=?, payload_json=?
                        WHERE job_id=? AND lease_owner=? AND lease_generation=?
                        """,
                        (
                            params[1],
                            params[2],
                            params[3],
                            params[4],
                            params[5],
                            params[6],
                            params[7],
                            params[8],
                            params[9],
                            params[10],
                            params[11],
                            params[12],
                            params[13],
                            params[14],
                            params[16],
                            params[17],
                            params[0],
                            job.lease_owner,
                            int(job.lease_generation),
                        ),
                    )
                    ok = cur.rowcount == 1
                    conn.execute("COMMIT")
                    return ok
                conn.execute(
                    """
                    INSERT INTO jobs (
                      job_id, session_id, route_id, state, email_hash, sso_ref,
                      cookie_bundle_path, cookie_mode, error_class, error_code,
                      attempts, lease_owner, lease_until, lease_generation,
                      next_run_at, created_at, updated_at, payload_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(job_id) DO UPDATE SET
                      session_id=excluded.session_id,
                      route_id=excluded.route_id,
                      state=excluded.state,
                      email_hash=excluded.email_hash,
                      sso_ref=excluded.sso_ref,
                      cookie_bundle_path=excluded.cookie_bundle_path,
                      cookie_mode=excluded.cookie_mode,
                      error_class=excluded.error_class,
                      error_code=excluded.error_code,
                      attempts=excluded.attempts,
                      lease_owner=excluded.lease_owner,
                      lease_until=excluded.lease_until,
                      lease_generation=excluded.lease_generation,
                      next_run_at=excluded.next_run_at,
                      updated_at=excluded.updated_at,
                      payload_json=excluded.payload_json
                    """,
                    params,
                )
                conn.execute("COMMIT")
                return True
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                conn.close()

    def save_terminal(self, job: RegistrationJob) -> bool:
        """Final state write: only if fence still holds; then clear lease in same UPDATE."""
        job.updated_at = time.time()
        owner = job.lease_owner
        gen = int(job.lease_generation or 0)
        # Allow terminal write with owner still set for fencing; clear after match
        params = self._row_params(job)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                if owner:
                    cur = conn.execute(
                        """
                        UPDATE jobs SET
                          state=?, error_class=?, error_code=?, attempts=?,
                          lease_owner='', lease_until=0, lease_generation=?,
                          next_run_at=?, updated_at=?, payload_json=?
                        WHERE job_id=? AND lease_owner=? AND lease_generation=?
                        """,
                        (
                            params[3], params[8], params[9], params[10],
                            gen, params[14], params[16], params[17],
                            params[0], owner, gen,
                        ),
                    )
                    ok = cur.rowcount == 1
                else:
                    # already cleared — only write if generation still matches last known
                    cur = conn.execute(
                        """
                        UPDATE jobs SET
                          state=?, error_class=?, error_code=?, attempts=?,
                          lease_owner='', lease_until=0,
                          next_run_at=?, updated_at=?, payload_json=?
                        WHERE job_id=? AND lease_generation=?
                        """,
                        (
                            params[3], params[8], params[9], params[10],
                            params[14], params[16], params[17],
                            params[0], gen,
                        ),
                    )
                    ok = cur.rowcount == 1
                conn.execute("COMMIT")
                if ok:
                    job.lease_owner = ""
                    job.lease_until = 0.0
                return ok
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                conn.close()

    def heartbeat(
        self,
        job: RegistrationJob,
        *,
        lease_sec: float = 300.0,
        now: float | None = None,
    ) -> bool:
        """Extend lease for current owner+generation. Returns False if fenced out."""
        current = time.time() if now is None else now
        if not job.lease_owner:
            return False
        new_until = current + max(5.0, float(lease_sec))
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                cur = conn.execute(
                    """
                    UPDATE jobs SET lease_until=?, updated_at=?
                    WHERE job_id=? AND lease_owner=? AND lease_generation=?
                    """,
                    (
                        new_until,
                        current,
                        job.job_id,
                        job.lease_owner,
                        int(job.lease_generation),
                    ),
                )
                ok = cur.rowcount == 1
                conn.execute("COMMIT")
                if ok:
                    job.lease_until = new_until
                    job.updated_at = current
                return ok
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                conn.close()

    def get(self, job_id: str) -> RegistrationJob | None:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,))
                row = cur.fetchone()
                return RegistrationJob.from_row(dict(row)) if row else None
            finally:
                conn.close()

    def get_by_session(self, session_id: str) -> RegistrationJob | None:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT * FROM jobs WHERE session_id=? ORDER BY created_at DESC LIMIT 1",
                    (session_id,),
                )
                row = cur.fetchone()
                return RegistrationJob.from_row(dict(row)) if row else None
            finally:
                conn.close()

    def has_active_job_for_session(self, session_id: str) -> bool:
        job = self.get_by_session(session_id)
        if not job:
            return False
        return job.state not in _CAPACITY_TERMINAL

    def claim(
        self,
        worker_id: str,
        *,
        states: Iterable[str] | None = None,
        lease_sec: float = 300.0,
        now: float | None = None,
    ) -> RegistrationJob | None:
        current = time.time() if now is None else now
        reclaim_states = tuple(
            s.value if isinstance(s, JobState) else s
            for s in (states or (JobState.MINT_QUEUED.value, *RECLAIMABLE_RUNNING))
        )
        lease_for = max(5.0, float(lease_sec))
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                placeholders = ",".join("?" * len(reclaim_states))
                cur = conn.execute(
                    f"""
                    SELECT * FROM jobs
                    WHERE state IN ({placeholders})
                      AND next_run_at <= ?
                      AND (
                        (state = ? AND (lease_until IS NULL OR lease_until <= ? OR lease_owner = '' OR lease_owner = ?))
                        OR
                        (state != ? AND lease_until IS NOT NULL AND lease_until <= ?)
                      )
                    ORDER BY created_at ASC
                    LIMIT 1
                    """,
                    (
                        *reclaim_states,
                        current,
                        JobState.MINT_QUEUED.value,
                        current,
                        worker_id,
                        JobState.MINT_QUEUED.value,
                        current,
                    ),
                )
                row = cur.fetchone()
                if not row:
                    conn.execute("COMMIT")
                    return None
                job_id = str(row["job_id"])
                prev_state = str(row["state"])
                prev_attempts = int(row["attempts"] or 0)
                prev_gen = int(row["lease_generation"] or 0)
                new_gen = prev_gen + 1
                new_until = current + lease_for
                # On reclaim of in-flight states, reset to mint_running and bump replay
                new_state = JobState.MINT_RUNNING.value
                cur2 = conn.execute(
                    """
                    UPDATE jobs SET
                      state = ?,
                      lease_owner = ?,
                      lease_until = ?,
                      lease_generation = ?,
                      attempts = ?,
                      updated_at = ?,
                      error_class = CASE WHEN state = ? THEN error_class ELSE error_class END
                    WHERE job_id = ?
                      AND state = ?
                      AND (
                        lease_until IS NULL OR lease_until <= ? OR lease_owner = '' OR lease_owner = ?
                      )
                    """,
                    (
                        new_state,
                        worker_id,
                        new_until,
                        new_gen,
                        prev_attempts + 1,
                        current,
                        JobState.MINT_QUEUED.value,
                        job_id,
                        prev_state,
                        current,
                        worker_id,
                    ),
                )
                if cur2.rowcount != 1:
                    conn.execute("COMMIT")
                    return None
                # Record replay if reclaimed from non-queued state
                if prev_state != JobState.MINT_QUEUED.value:
                    payload = row["payload_json"] or "{}"
                    try:
                        pdata = json.loads(payload) if isinstance(payload, str) else dict(payload)
                    except json.JSONDecodeError:
                        pdata = {}
                    pdata["crash_replays"] = int(pdata.get("crash_replays") or 0) + 1
                    pdata["reclaimed_from"] = prev_state
                    conn.execute(
                        "UPDATE jobs SET payload_json=? WHERE job_id=?",
                        (json.dumps(pdata, ensure_ascii=False), job_id),
                    )
                cur3 = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,))
                claimed = cur3.fetchone()
                conn.execute("COMMIT")
                return RegistrationJob.from_row(dict(claimed)) if claimed else None
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                conn.close()

    def release(self, job: RegistrationJob, *, clear_lease: bool = True) -> bool:
        if clear_lease:
            job.lease_owner = ""
            job.lease_until = 0.0
        return self.save(job, require_fence=False)

    def requeue(
        self,
        job: RegistrationJob,
        *,
        delay_sec: float,
        error_class: str = "",
        error_code: str = "",
        state: str = JobState.MINT_QUEUED.value,
    ) -> bool:
        """Return job to mint_queued only if current lease fence still holds."""
        job.state = state
        job.error_class = error_class or job.error_class
        job.error_code = error_code or job.error_code
        job.next_run_at = time.time() + max(0.0, delay_sec)
        owner = job.lease_owner
        gen = int(job.lease_generation or 0)
        job.updated_at = time.time()
        params = self._row_params(job)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                if owner:
                    cur = conn.execute(
                        """
                        UPDATE jobs SET
                          state=?, error_class=?, error_code=?, attempts=?,
                          lease_owner='', lease_until=0, lease_generation=?,
                          next_run_at=?, updated_at=?, payload_json=?
                        WHERE job_id=? AND lease_owner=? AND lease_generation=?
                        """,
                        (
                            params[3], params[8], params[9], params[10],
                            gen, params[14], params[16], params[17],
                            params[0], owner, gen,
                        ),
                    )
                    ok = cur.rowcount == 1
                else:
                    ok = False
                conn.execute("COMMIT")
                if ok:
                    job.lease_owner = ""
                    job.lease_until = 0.0
                return ok
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                conn.close()

    def import_pending_json(
        self,
        path: Path,
        *,
        route_id: str,
        cookie_mode: str = "sso_only",
    ) -> RegistrationJob | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        sso = payload.get("sso")
        if not isinstance(sso, str) or not sso.strip():
            return None
        session_id = str(payload.get("session_id") or path.stem)
        existing = self.get_by_session(session_id)
        if existing:
            # Never create a second job for the same session (including dead_letter).
            return existing
        job = RegistrationJob(
            job_id=new_job_id(),
            session_id=session_id,
            route_id=route_id,
            state=JobState.MINT_QUEUED.value,
            email_hash=email_hash(str(payload.get("email") or "")),
            sso_ref=str(path),
            cookie_mode=cookie_mode,
            created_at=float(payload.get("created_at") or time.time()),
            payload={
                "source": "pending_sso",
                "pending_name": path.name,
                "legacy": True,
                "owner": "mint_queue",
            },
        )
        return self.enqueue(job)

    def list_states(self) -> dict[str, int]:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute("SELECT state, COUNT(*) AS c FROM jobs GROUP BY state")
                return {str(r["state"]): int(r["c"]) for r in cur.fetchall()}
            finally:
                conn.close()

    def oldest_open_age_sec(self, now: float | None = None) -> float | None:
        current = time.time() if now is None else now
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    f"""
                    SELECT MIN(created_at) AS m FROM jobs
                    WHERE state NOT IN ({','.join('?' * len(_CAPACITY_TERMINAL))})
                    """,
                    _CAPACITY_TERMINAL,
                )
                row = cur.fetchone()
                if not row or row["m"] is None:
                    return None
                return max(0.0, current - float(row["m"]))
            finally:
                conn.close()


def read_sso_from_ref(sso_ref: str) -> str:
    path = Path(sso_ref)
    if not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if isinstance(data, dict):
        raw = data.get("sso")
        return raw if isinstance(raw, str) else ""
    return ""


def dual_write_pending(
    *,
    session_id: str,
    email: str,
    sso: str,
    pending_dir: Path | None = None,
    owner: str = "mint_queue",
) -> Path:
    base = pending_dir or Path(os.getenv("GROK2API_DATA_DIR", "data")) / "pending_sso"
    base.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(base, 0o700)
    except OSError:
        pass
    path = base / f"{session_id}.json"
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    payload = {
        "session_id": session_id,
        "email": email,
        "sso": sso,
        "created_at": time.time(),
        "owner": owner,
        "pipeline_v2": owner == "mint_queue",
    }
    # Create temp as 0600 from the start
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False))
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def pending_owned_by_mint_queue(payload: dict[str, Any]) -> bool:
    owner = str(payload.get("owner") or "").strip()
    if owner == "mint_queue" or payload.get("pipeline_v2") is True:
        return True
    return False


def repair_orphan_mint_pending(
    *,
    pending_dir: Path | None = None,
    queue: RegistrationQueue | None = None,
    route_id: str = "route-1",
) -> dict[str, int]:
    """Re-enqueue mint-owned pending files that have no active job."""
    base = pending_dir or Path(os.getenv("GROK2API_DATA_DIR", "data")) / "pending_sso"
    q = queue or RegistrationQueue()
    repaired = skipped = failed = 0
    if not base.is_dir():
        return {"repaired": 0, "skipped": 0, "failed": 0}
    for path in base.glob("*.json"):
        if ".processing." in path.name:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            failed += 1
            continue
        if not isinstance(payload, dict) or not pending_owned_by_mint_queue(payload):
            skipped += 1
            continue
        sid = str(payload.get("session_id") or path.stem)
        # Skip if any job exists for session (including terminal dead_letter/auth_imported)
        if q.get_by_session(sid) is not None:
            skipped += 1
            continue
        try:
            job = q.import_pending_json(path, route_id=route_id)
            if job:
                repaired += 1
            else:
                failed += 1
        except Exception:
            failed += 1
    return {"repaired": repaired, "skipped": skipped, "failed": failed}
