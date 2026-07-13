"""Registration / mint job model, state machine, and error classification."""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class JobState(str, Enum):
    CREATED = "created"
    SIGNUP_RUNNING = "signup_running"
    SSO_OBTAINED = "sso_obtained"
    MINT_QUEUED = "mint_queued"
    MINT_RUNNING = "mint_running"
    BROWSER_DONE = "browser_done"
    BROWSER_DENIED = "browser_denied"
    BROWSER_TIMEOUT = "browser_timeout"
    TOKEN_RECEIVED = "token_received"
    PROBE_RUNNING = "probe_running"
    PROBE_PASSED = "probe_passed"
    AUTH_IMPORTED = "auth_imported"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


# Allowed forward transitions (plus self for heartbeats / lease renew).
# Terminal FAILED/DEAD_LETTER reachable from any in-flight mint state so a
# denied/empty-token path never raises illegal-transition and kills the worker.
_TRANSITIONS: dict[JobState, set[JobState]] = {
    JobState.CREATED: {JobState.SIGNUP_RUNNING, JobState.FAILED, JobState.DEAD_LETTER},
    JobState.SIGNUP_RUNNING: {
        JobState.SSO_OBTAINED,
        JobState.FAILED,
        JobState.DEAD_LETTER,
    },
    JobState.SSO_OBTAINED: {JobState.MINT_QUEUED, JobState.FAILED, JobState.DEAD_LETTER},
    JobState.MINT_QUEUED: {
        JobState.MINT_RUNNING,
        JobState.FAILED,
        JobState.DEAD_LETTER,
    },
    JobState.MINT_RUNNING: {
        JobState.BROWSER_DONE,
        JobState.BROWSER_DENIED,
        JobState.BROWSER_TIMEOUT,
        JobState.TOKEN_RECEIVED,
        JobState.PROBE_RUNNING,
        JobState.FAILED,
        JobState.DEAD_LETTER,
        JobState.MINT_QUEUED,  # crash reset / requeue
    },
    JobState.BROWSER_DONE: {
        JobState.TOKEN_RECEIVED,
        JobState.FAILED,
        JobState.DEAD_LETTER,
        JobState.BROWSER_TIMEOUT,
        JobState.MINT_QUEUED,
    },
    JobState.BROWSER_DENIED: {
        JobState.FAILED,
        JobState.DEAD_LETTER,
        JobState.MINT_QUEUED,
    },
    JobState.BROWSER_TIMEOUT: {
        JobState.MINT_QUEUED,
        JobState.FAILED,
        JobState.DEAD_LETTER,
    },
    JobState.TOKEN_RECEIVED: {
        JobState.PROBE_RUNNING,
        JobState.AUTH_IMPORTED,
        JobState.FAILED,
        JobState.DEAD_LETTER,
        JobState.MINT_QUEUED,  # crash recovery restarts mint from SSO
    },
    JobState.PROBE_RUNNING: {
        JobState.PROBE_PASSED,
        JobState.FAILED,
        JobState.DEAD_LETTER,
        JobState.MINT_QUEUED,
        JobState.MINT_RUNNING,
    },
    JobState.PROBE_PASSED: {
        JobState.AUTH_IMPORTED,
        JobState.FAILED,
        JobState.DEAD_LETTER,
        JobState.MINT_QUEUED,
    },
    JobState.AUTH_IMPORTED: set(),
    JobState.FAILED: {JobState.MINT_QUEUED, JobState.DEAD_LETTER},
    JobState.DEAD_LETTER: set(),
}


TERMINAL_STATES = frozenset(
    {JobState.AUTH_IMPORTED, JobState.DEAD_LETTER, JobState.FAILED}
)
# In-flight states reclaimable after lease expiry (token not persisted → restart mint).
RECLAIMABLE_RUNNING = frozenset(
    {
        JobState.MINT_RUNNING,
        JobState.TOKEN_RECEIVED,
        JobState.PROBE_RUNNING,
        JobState.PROBE_PASSED,
        JobState.BROWSER_DONE,
        JobState.BROWSER_TIMEOUT,
        JobState.BROWSER_DENIED,
    }
)
MINT_CLAIMABLE = frozenset({JobState.MINT_QUEUED}) | RECLAIMABLE_RUNNING


class ErrorClass(str, Enum):
    TRANSIENT_NETWORK = "transient_network"
    RATE_LIMITED = "rate_limited"
    BROWSER_TIMEOUT = "browser_timeout"
    BROWSER_DENIED = "browser_denied"
    SSO_INVALID = "sso_invalid"
    PROBE_FAILED = "probe_failed"
    IMPORT_FAILED = "import_failed"
    PERMANENT = "permanent"
    UNKNOWN = "unknown"


_RETRYABLE = frozenset(
    {
        ErrorClass.TRANSIENT_NETWORK,
        ErrorClass.RATE_LIMITED,
        ErrorClass.BROWSER_TIMEOUT,
        ErrorClass.IMPORT_FAILED,
        ErrorClass.PROBE_FAILED,
    }
)


def can_transition(src: JobState | str, dst: JobState | str) -> bool:
    s = JobState(src)
    d = JobState(dst)
    if s == d:
        return True
    return d in _TRANSITIONS.get(s, set())


def classify_error(exc: BaseException | str | None, *, hint: str = "") -> ErrorClass:
    text = f"{hint} {exc}".lower() if exc is not None else hint.lower()
    if "rate_limited" in text or "slow_down" in text or "429" in text:
        return ErrorClass.RATE_LIMITED
    if "timeout" in text or "timed out" in text:
        return ErrorClass.BROWSER_TIMEOUT
    if "denied" in text or "access_denied" in text:
        return ErrorClass.BROWSER_DENIED
    if "sso" in text and ("invalid" in text or "无效" in text):
        return ErrorClass.SSO_INVALID
    if "probe" in text:
        return ErrorClass.PROBE_FAILED
    if "import" in text:
        return ErrorClass.IMPORT_FAILED
    if any(
        k in text
        for k in ("connection", "network", "temporarily", "reset", "refused", "busy")
    ):
        return ErrorClass.TRANSIENT_NETWORK
    if any(k in text for k in ("permanent", "invalid_grant", "revoked")):
        return ErrorClass.PERMANENT
    return ErrorClass.UNKNOWN


def is_retryable(error_class: ErrorClass | str) -> bool:
    try:
        return ErrorClass(error_class) in _RETRYABLE
    except ValueError:
        return False


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def email_hash(email: str) -> str:
    """Non-reversible short label for metrics (not a security boundary)."""
    import hashlib

    raw = (email or "").strip().lower().encode("utf-8")
    if not raw:
        return ""
    return hashlib.sha256(raw).hexdigest()[:16]


def redact_text(value: str, *, secrets: list[str] | None = None) -> str:
    """Strip emails, JWTs, and explicit secrets from free-form text."""
    clean = value or ""
    for secret in secrets or []:
        if secret:
            clean = clean.replace(secret, "<redacted>")
    clean = _EMAIL_RE.sub("<redacted-email>", clean)
    clean = re.sub(r"eyJ[A-Za-z0-9_-]{20,}(?:\.[A-Za-z0-9_-]+){0,2}", "<redacted-jwt>", clean)
    return clean


def new_session_id(prefix: str = "gba") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def new_job_id() -> str:
    return f"job_{uuid.uuid4().hex[:16]}"


@dataclass
class RegistrationJob:
    job_id: str
    session_id: str
    route_id: str
    state: str = JobState.CREATED.value
    email_hash: str = ""
    sso_ref: str = ""  # path or pending filename; never log contents
    cookie_bundle_path: str = ""
    cookie_mode: str = "sso_only"
    error_class: str = ""
    error_code: str = ""
    attempts: int = 0
    lease_owner: str = ""
    lease_until: float = 0.0
    lease_generation: int = 0
    next_run_at: float = 0.0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    payload: dict[str, Any] = field(default_factory=dict)

    def transition(self, new_state: JobState | str, **fields: Any) -> None:
        dst = JobState(new_state)
        if not can_transition(self.state, dst):
            raise ValueError(f"illegal transition {self.state} → {dst.value}")
        self.state = dst.value
        self.updated_at = time.time()
        for k, v in fields.items():
            if hasattr(self, k):
                setattr(self, k, v)
            else:
                self.payload[k] = v

    def force_terminal(self, new_state: JobState | str, **fields: Any) -> None:
        """Move to FAILED/DEAD_LETTER even from unexpected states (worker safety)."""
        dst = JobState(new_state)
        if dst not in (JobState.FAILED, JobState.DEAD_LETTER, JobState.AUTH_IMPORTED):
            raise ValueError(f"force_terminal only for terminal states, got {dst}")
        if self.state != dst.value and not can_transition(self.state, dst):
            # Last-resort: allow any → FAILED/DEAD_LETTER for worker isolation
            if dst in (JobState.FAILED, JobState.DEAD_LETTER):
                self.state = dst.value
            else:
                raise ValueError(f"illegal transition {self.state} → {dst.value}")
        else:
            self.state = dst.value
        self.updated_at = time.time()
        # Keep lease_owner/generation for fenced terminal write; queue.save_terminal clears them.
        for k, v in fields.items():
            if k in ("lease_owner", "lease_until"):
                continue
            if hasattr(self, k):
                setattr(self, k, v)
            else:
                self.payload[k] = v

    def to_public_dict(self) -> dict[str, Any]:
        """Safe for logs/metrics — no SSO/token/email plaintext."""
        return {
            "job_id": self.job_id,
            "session_id": self.session_id,
            "route_id": self.route_id,
            "state": self.state,
            "email_hash": self.email_hash,
            "cookie_mode": self.cookie_mode,
            "error_class": self.error_class,
            "error_code": self.error_code,
            "attempts": self.attempts,
            "lease_generation": self.lease_generation,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "has_sso_ref": bool(self.sso_ref),
            "has_cookie_bundle": bool(self.cookie_bundle_path),
            "payload_keys": sorted(self.payload.keys()),
        }

    def to_row(self) -> dict[str, Any]:
        row = asdict(self)
        row["payload_json"] = row.pop("payload")
        return row

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "RegistrationJob":
        payload = row.get("payload_json") or row.get("payload") or {}
        if isinstance(payload, str):
            import json

            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}
        return cls(
            job_id=str(row["job_id"]),
            session_id=str(row["session_id"]),
            route_id=str(row["route_id"]),
            state=str(row.get("state") or JobState.CREATED.value),
            email_hash=str(row.get("email_hash") or ""),
            sso_ref=str(row.get("sso_ref") or ""),
            cookie_bundle_path=str(row.get("cookie_bundle_path") or ""),
            cookie_mode=str(row.get("cookie_mode") or "sso_only"),
            error_class=str(row.get("error_class") or ""),
            error_code=str(row.get("error_code") or ""),
            attempts=int(row.get("attempts") or 0),
            lease_owner=str(row.get("lease_owner") or ""),
            lease_until=float(row.get("lease_until") or 0),
            lease_generation=int(row.get("lease_generation") or 0),
            next_run_at=float(row.get("next_run_at") or 0),
            created_at=float(row.get("created_at") or time.time()),
            updated_at=float(row.get("updated_at") or time.time()),
            payload=dict(payload) if isinstance(payload, dict) else {},
        )


def experiment_bucket(experiment_id: str, session_id: str) -> int:
    """Stable 0..9999 bucket for A/B assignment.

    Uses full digest as big-endian int so distribution is uniform across
    0..9999 (digest[0]%10000 only spanned 0..255 and broke 10%→~39% rollouts).
    """
    import hashlib

    digest = hashlib.sha256(f"{experiment_id}:{session_id}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % 10000


def in_experiment(session_id: str, percent: float, *, experiment_id: str = "cookie_bundle") -> bool:
    pct = max(0.0, min(100.0, float(percent)))
    if pct <= 0:
        return False
    if pct >= 100:
        return True
    # percent=10 → bucket < 1000 (of 10000)
    return experiment_bucket(experiment_id, session_id) < int(round(pct * 100))
