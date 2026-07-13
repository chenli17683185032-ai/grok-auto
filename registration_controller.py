"""Mint worker controller for pipeline v2.

When GROK2API_PIPELINE_V2=0 this module is inert for production paths.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from registration_jobs import (
    ErrorClass,
    JobState,
    RegistrationJob,
    classify_error,
    email_hash,
    is_retryable,
    new_job_id,
)
from registration_queue import (
    RegistrationQueue,
    dual_write_pending,
    pipeline_v2_enabled,
    read_sso_from_ref,
)
from route_registry import get_registry, route_sticky_enabled

try:
    from registration_metrics import emit as metrics_emit
except Exception:  # pragma: no cover

    def metrics_emit(event: str, **kwargs: Any) -> None:
        return None


def _env_flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def parallel_token_poll_enabled() -> bool:
    return _env_flag("GROK2API_PARALLEL_TOKEN_POLL", "0")


def adaptive_enabled() -> bool:
    return _env_flag("GROK2API_ADAPTIVE_SCHEDULER", "0")



class _LeaseHeartbeat:
    """Background lease renewer for long device-flow / probe work."""

    def __init__(self, controller: "RegistrationController", job: RegistrationJob, *, lease_sec: float = 300.0, every: float = 30.0):
        self.controller = controller
        self.job = job
        self.lease_sec = lease_sec
        self.every = max(5.0, every)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.alive = True

    def __enter__(self) -> "_LeaseHeartbeat":
        self._thread = threading.Thread(target=self._run, name=f"lease-hb-{self.job.job_id[:8]}", daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.wait(self.every):
            if not self.controller._heartbeat(self.job, lease_sec=self.lease_sec):
                self.alive = False
                self._stop.set()
                return

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None


class RegistrationController:
    def __init__(
        self,
        queue: RegistrationQueue | None = None,
        *,
        worker_id: str | None = None,
    ) -> None:
        self.queue = queue or RegistrationQueue()
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self._stop = threading.Event()

    def _fenced_save(self, job: RegistrationJob) -> bool:
        """Persist only if this worker still owns the lease generation."""
        if not job.lease_owner:
            # Terminal paths clear owner intentionally — use unfenced terminal write
            return self.queue.save_terminal(job) if hasattr(self.queue, "save_terminal") else self.queue.save(job, require_fence=False)
        ok = self.queue.save(job, require_fence=True)
        if not ok:
            print(
                f"[mint-worker] fenced-out job={job.job_id} gen={job.lease_generation}",
                flush=True,
            )
        return ok

    def _heartbeat(self, job: RegistrationJob, *, lease_sec: float = 300.0) -> bool:
        ok = self.queue.heartbeat(job, lease_sec=lease_sec)
        if not ok:
            print(
                f"[mint-worker] heartbeat lost job={job.job_id} gen={job.lease_generation}",
                flush=True,
            )
        return ok


    def enqueue_after_sso(
        self,
        *,
        session_id: str,
        email: str,
        sso: str,
        route_id: str | None = None,
        cookie_bundle_path: str = "",
        cookie_mode: str | None = None,
        session_cookies: dict | list | None = None,
        dual_write: bool = True,
    ) -> RegistrationJob:
        """Persist pending (mint-owned) + mint job. Does not perform device flow."""
        registry = get_registry()
        if route_id:
            registry.bind_existing(session_id, route_id)
            rid = route_id
        elif route_sticky_enabled():
            rid = registry.assign_route(session_id)
        else:
            # Legacy default: always route-1 (single global proxy semantics)
            rid = "route-1"
            try:
                registry.bind_existing(session_id, rid)
            except Exception:
                pass

        pending_path = None
        if dual_write:
            pending_path = dual_write_pending(
                session_id=session_id,
                email=email,
                sso=sso,
                owner="mint_queue",
            )

        bundle_path = cookie_bundle_path
        if not bundle_path and session_cookies is not None:
            try:
                import cookie_bundle as cb

                mode = cookie_mode or cb.resolve_mode_for_session(session_id)
                cookies = session_cookies
                if isinstance(cookies, dict):
                    cookies = dict(cookies)
                    cookies.setdefault("sso", sso)
                    cookies.setdefault("sso-rw", sso)
                meta = cb.write_bundle(cookies, session_id=session_id, mode=mode)
                bundle_path = str(meta.get("path") or "")
                cookie_mode = str(meta.get("mode") or mode)
            except Exception as exc:  # noqa: BLE001
                metrics_emit(
                    "cookie_bundle_error",
                    session_id=session_id,
                    error_class=classify_error(exc).value,
                )
                cookie_mode = "sso_only"
                bundle_path = ""

        # Resolve cookie experiment unless caller forced a mode via explicit non-default.
        # Use None as "auto" — empty string also means auto.
        try:
            import cookie_bundle as cb

            if cookie_mode in (None, "", "auto"):
                cookie_mode = cb.resolve_mode_for_session(session_id)
        except Exception:
            cookie_mode = cookie_mode or "sso_only"

        job = RegistrationJob(
            job_id=new_job_id(),
            session_id=session_id,
            route_id=rid,
            state=JobState.MINT_QUEUED.value,
            email_hash=email_hash(email),
            sso_ref=str(pending_path) if pending_path else "",
            cookie_bundle_path=bundle_path,
            cookie_mode=cookie_mode or "sso_only",
            payload={"email": email, "owner": "mint_queue"},
        )
        try:
            self.queue.enqueue(job)
        except Exception:
            # Compensating action: do not leave mint-owned orphan without a job.
            if pending_path:
                try:
                    # Relabel to legacy so recovery can pick it up
                    import json as _json

                    data = _json.loads(Path(pending_path).read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        data["owner"] = "legacy_inline"
                        data["pipeline_v2"] = False
                        data["enqueue_failed"] = True
                        tmp = Path(str(pending_path) + ".tmp")
                        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                        with os.fdopen(fd, "w", encoding="utf-8") as fh:
                            fh.write(_json.dumps(data, ensure_ascii=False))
                        os.replace(tmp, pending_path)
                except Exception:
                    pass
            raise
        metrics_emit(
            "sso_obtained",
            session_id=session_id,
            job_id=job.job_id,
            route_id=rid,
            cookie_mode=cookie_mode,
            ok=True,
        )
        metrics_emit(
            "mint_queued",
            session_id=session_id,
            job_id=job.job_id,
            route_id=rid,
            cookie_mode=cookie_mode,
        )
        return job

    def process_job(
        self,
        job: RegistrationJob,
        *,
        sso_to_token: Callable[..., dict | None] | None = None,
        import_entry: Callable[[dict, str], dict] | None = None,
        probe_fn: Callable[..., dict] | None = None,
        require_probe: bool | None = None,
    ) -> RegistrationJob:
        """Run mint for a claimed job. Inject callables for tests."""
        t0 = time.time()
        if require_probe is None:
            require_probe = not _env_flag("GROK2API_MINT_SKIP_PROBE", "0")

        metrics_emit(
            "mint_started",
            session_id=job.session_id,
            job_id=job.job_id,
            route_id=job.route_id,
            cookie_mode=job.cookie_mode,
        )
        if not self._heartbeat(job):
            return job
        sso = read_sso_from_ref(job.sso_ref)
        if not sso:
            job.transition(
                JobState.FAILED,
                error_class=ErrorClass.SSO_INVALID.value,
                error_code="missing_sso_ref",
            )
            self.queue.save_terminal(job)
            return job

        registry = get_registry()
        try:
            route = registry.get(job.route_id)
        except KeyError:
            route = None

        token_fn = sso_to_token
        if token_fn is None:
            import sso_to_auth_json as sso_mod

            token_fn = sso_mod.sso_to_token

        kwargs: dict[str, Any] = {}
        try:
            import inspect

            sig = inspect.signature(token_fn)
            params = sig.parameters
        except (TypeError, ValueError):
            params = {}

        if "route_id" in params and route is not None:
            kwargs["route_id"] = route.route_id
        if "approver_endpoint" in params and route is not None:
            kwargs["approver_endpoint"] = route.approver
        if "proxy" in params and route is not None:
            kwargs["proxy"] = route.token_proxy
        if "cookie_bundle_path" in params:
            kwargs["cookie_bundle_path"] = job.cookie_bundle_path
        if "cookie_mode" in params:
            kwargs["cookie_mode"] = job.cookie_mode
        if "extra_cookies" in params and job.cookie_bundle_path:
            try:
                import cookie_bundle as cb

                # Inline cookie values for approver when shared volume missing
                data = cb.read_bundle(job.cookie_bundle_path)
                if data and data.get("cookies"):
                    kwargs["extra_cookies"] = [
                        c
                        for c in data["cookies"]
                        if isinstance(c, dict) and c.get("name") not in ("sso", "sso-rw")
                    ]
            except Exception:
                pass
        if "parallel_poll" in params:
            kwargs["parallel_poll"] = parallel_token_poll_enabled()

        try:
            with _LeaseHeartbeat(self, job, lease_sec=300.0, every=20.0) as hb:
                if kwargs:
                    token = token_fn(sso, **kwargs)
                else:
                    token = token_fn(sso)
                if not hb.alive:
                    print(f"[mint-worker] heartbeat lost mid-token job={job.job_id}", flush=True)
                    return job
        except Exception as exc:  # noqa: BLE001
            return self._fail_or_retry(job, exc, t0)

        if not token or not token.get("access_token"):
            # Ambiguous None: prefer retryable network/timeout unless clearly denied.
            hint = ""
            if isinstance(token, dict):
                hint = str(token.get("_error") or token.get("error") or "")
            err = RuntimeError(hint or "device_flow_no_token")
            ec = classify_error(err, hint=hint or "timeout network busy")
            if ec == ErrorClass.BROWSER_DENIED and "denied" not in (hint or "").lower():
                ec = ErrorClass.TRANSIENT_NETWORK
            return self._fail_or_retry(job, err, t0, error_class=ec)

        if not self._heartbeat(job):
            return job
        job.transition(JobState.TOKEN_RECEIVED)
        metrics_emit(
            "token_received",
            session_id=job.session_id,
            job_id=job.job_id,
            route_id=job.route_id,
            ok=True,
            duration_ms=(time.time() - t0) * 1000,
        )
        if token.get("refresh_token"):
            metrics_emit(
                "refresh_token_received",
                session_id=job.session_id,
                job_id=job.job_id,
                route_id=job.route_id,
                ok=True,
            )
        else:
            job.transition(
                JobState.FAILED,
                error_class=ErrorClass.PERMANENT.value,
                error_code="missing_refresh_token",
            )
            self.queue.save_terminal(job)
            return job

        # Probe BEFORE import so failed probes never enter live pool.
        # Use real GrokCredentials fields (token/email/user_id/auth_key).
        if require_probe:
            if not self._heartbeat(job):
                return job
            job.transition(JobState.PROBE_RUNNING)
            if not self._fenced_save(job):
                return job
            with _LeaseHeartbeat(self, job, lease_sec=300.0, every=20.0) as hb:
                probe_result = self._run_probe(token, job=job, probe_fn=probe_fn)
                if not hb.alive:
                    print(f"[mint-worker] heartbeat lost mid-probe job={job.job_id}", flush=True)
                    return job
            if not probe_result.get("ok"):
                metrics_emit(
                    "probe_failed",
                    session_id=job.session_id,
                    job_id=job.job_id,
                    route_id=job.route_id,
                    ok=False,
                    error_class=ErrorClass.PROBE_FAILED.value,
                )
                return self._fail_or_retry(
                    job,
                    RuntimeError(str(probe_result.get("error") or "probe_failed")),
                    t0,
                    error_class=ErrorClass.PROBE_FAILED,
                )
            job.transition(JobState.PROBE_PASSED)
            metrics_emit(
                "probe_passed",
                session_id=job.session_id,
                job_id=job.job_id,
                route_id=job.route_id,
                ok=True,
            )
            if not self._fenced_save(job):
                return job

        try:
            if import_entry is not None:
                result = import_entry(token, job.session_id)
            else:
                result = self._default_import(token, job)
            if not result.get("ok"):
                raise RuntimeError(result.get("error") or "import failed")
        except Exception as exc:  # noqa: BLE001
            return self._fail_or_retry(
                job, exc, t0, error_class=ErrorClass.IMPORT_FAILED
            )

        job.transition(JobState.AUTH_IMPORTED)
        if not self.queue.save_terminal(job):
            # Fenced out — do not delete materials or claim success
            print(f"[mint-worker] auth_imported fenced-out job={job.job_id}", flush=True)
            return job
        self._cleanup_materials(job)
        metrics_emit(
            "auth_imported",
            session_id=job.session_id,
            job_id=job.job_id,
            route_id=job.route_id,
            cookie_mode=job.cookie_mode,
            ok=True,
            duration_ms=(time.time() - t0) * 1000,
        )
        return job

    def _cleanup_materials(self, job: RegistrationJob, *, keep_sso: bool = False) -> None:
        if not keep_sso:
            try:
                if job.sso_ref:
                    Path(job.sso_ref).unlink(missing_ok=True)
            except OSError:
                pass
        if job.cookie_bundle_path:
            try:
                import cookie_bundle as cb

                cb.delete_bundle(job.cookie_bundle_path)
            except Exception:
                try:
                    Path(job.cookie_bundle_path).unlink(missing_ok=True)
                except OSError:
                    pass

    def _default_import(self, token: dict, job: RegistrationJob) -> dict:
        import sso_to_auth_json as sso_mod

        email = str(job.payload.get("email") or "")
        _key, entry = sso_mod.token_to_auth_entry(token, email=email)
        # Ensure auth.json ends as 0600 via import path
        aid = sso_mod.import_into_project_auth(entry)
        try:
            from config import AUTH_FILE

            if AUTH_FILE.exists():
                os.chmod(AUTH_FILE, 0o600)
                os.chmod(AUTH_FILE.parent, 0o700)
        except Exception:
            pass
        return {"ok": True, "imported": [aid] if aid else []}

    def _run_probe(
        self,
        token: dict,
        *,
        job: RegistrationJob,
        probe_fn: Callable[..., dict] | None = None,
    ) -> dict:
        if probe_fn is not None:
            return probe_fn(token)
        if _env_flag("GROK2API_MINT_SKIP_PROBE", "0"):
            return {"ok": True, "skipped": True}
        try:
            from auth import GrokCredentials
            from model_health import probe_model_for_creds
            from sso_to_auth_json import decode_jwt_payload
        except Exception:
            return {"ok": True, "skipped": True, "reason": "probe_unavailable"}

        access = str(token.get("access_token") or "")
        if not access:
            return {"ok": False, "error": "missing_access_token"}
        payload = decode_jwt_payload(access)
        email = str(job.payload.get("email") or payload.get("email") or "") or None
        user_id = str(payload.get("sub") or payload.get("principal_id") or "") or None
        exp = None
        if "exp" in payload:
            try:
                exp = float(payload["exp"])
            except (TypeError, ValueError):
                exp = None
        creds = GrokCredentials(
            token=access,
            email=email,
            user_id=user_id,
            expires_at=exp,
            auth_key=None,  # pre-import: do not mutate pool meta
            refresh_token=str(token.get("refresh_token") or "") or None,
        )
        try:
            probe_proxy = None
            try:
                route = get_registry().get(job.route_id)
                probe_proxy = route.token_proxy or route.register_proxy
            except Exception:
                probe_proxy = None
            result = probe_model_for_creds(
                creds,
                model=os.getenv("GROK2API_PRODUCER_TARGET_MODEL", "grok-4.5").strip()
                or "grok-4.5",
                auto_disable=False,
                source="registration_mint",
                report_stats=False,
                proxy=probe_proxy,
            )
            ok = bool(result.get("ok") or result.get("available"))
            return {
                "ok": ok,
                "error": None if ok else str(result.get("error") or "probe_failed")[:120],
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": type(exc).__name__}

    def _fail_or_retry(
        self,
        job: RegistrationJob,
        exc: BaseException,
        t0: float,
        *,
        error_class: ErrorClass | None = None,
    ) -> RegistrationJob:
        ec = error_class or classify_error(exc)
        metrics_emit(
            "mint_failed",
            session_id=job.session_id,
            job_id=job.job_id,
            route_id=job.route_id,
            ok=False,
            error_class=ec.value,
            duration_ms=(time.time() - t0) * 1000,
        )
        max_attempts = int(os.getenv("GROK2API_MINT_MAX_ATTEMPTS", "5") or 5)
        if is_retryable(ec) and job.attempts < max_attempts:
            delay = min(3600.0, 30.0 * (2 ** min(job.attempts, 6)))
            if ec == ErrorClass.RATE_LIMITED:
                delay = max(delay, 60.0)
            ok = self.queue.requeue(
                job,
                delay_sec=delay,
                error_class=ec.value,
                error_code=type(exc).__name__,
                state=JobState.MINT_QUEUED.value,
            )
            if not ok:
                print(f"[mint-worker] requeue fenced-out job={job.job_id}", flush=True)
            return job
        terminal = (
            JobState.DEAD_LETTER
            if (not is_retryable(ec) or job.attempts >= max_attempts)
            else JobState.FAILED
        )
        if job.attempts >= max_attempts:
            terminal = JobState.DEAD_LETTER
        try:
            job.force_terminal(
                terminal,
                error_class=ec.value,
                error_code=type(exc).__name__,
            )
        except Exception:
            job.state = terminal.value
            job.error_class = ec.value
            job.error_code = type(exc).__name__
        # Terminal write first; only cleanup if we still own the fence
        ok = (
            self.queue.save_terminal(job)
            if hasattr(self.queue, "save_terminal")
            else self.queue.save(job, require_fence=False)
        )
        if ok:
            # Keep SSO for manual recovery; drop cookie bundle only
            self._cleanup_materials(job, keep_sso=True)
        else:
            print(f"[mint-worker] terminal fenced-out job={job.job_id}", flush=True)
        return job

    def claim_and_process_once(self, **kwargs: Any) -> RegistrationJob | None:
        try:
            job = self.queue.claim(self.worker_id)
        except Exception as exc:  # noqa: BLE001
            print(f"[mint-worker] claim error: {type(exc).__name__}", flush=True)
            return None
        if not job:
            return None
        try:
            return self.process_job(job, **kwargs)
        except Exception as exc:  # noqa: BLE001
            try:
                return self._fail_or_retry(job, exc, time.time())
            except Exception as inner:  # noqa: BLE001
                print(
                    f"[mint-worker] isolated job failure job={job.job_id} "
                    f"err={type(inner).__name__}",
                    flush=True,
                )
                try:
                    job.force_terminal(
                        JobState.DEAD_LETTER,
                        error_class=ErrorClass.UNKNOWN.value,
                        error_code=type(inner).__name__,
                    )
                    self.queue.save_terminal(job) if hasattr(self.queue, "save_terminal") else self.queue.save(job, require_fence=False)
                except Exception:
                    pass
                return job

    def run_forever(self, *, idle_sec: float = 2.0, **kwargs: Any) -> None:
        # Startup: repair mint-owned orphans
        try:
            from registration_queue import repair_orphan_mint_pending

            stats = repair_orphan_mint_pending(queue=self.queue)
            if stats.get("repaired"):
                print(f"[mint-worker] repaired orphans={stats}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[mint-worker] orphan repair skipped: {type(exc).__name__}", flush=True)

        while not self._stop.is_set():
            try:
                try:
                    Path(os.getenv("GROK2API_PRODUCER_HEARTBEAT", "/tmp/producer-heartbeat")).touch()
                except OSError:
                    pass

                if adaptive_enabled():
                    try:
                        from registration_metrics import protection_action

                        action = protection_action()
                        if action.get("pause_mint"):
                            time.sleep(max(idle_sec, 5.0))
                            continue
                    except Exception:
                        pass
                job = self.claim_and_process_once(**kwargs)
                if job is None:
                    time.sleep(idle_sec)
                else:
                    time.sleep(0.05)
            except Exception as exc:  # noqa: BLE001
                # Last line of defence: never exit the worker loop.
                print(
                    f"[mint-worker] loop isolated error: {type(exc).__name__}",
                    flush=True,
                )
                time.sleep(max(idle_sec, 1.0))

    def stop(self) -> None:
        self._stop.set()


def maybe_enqueue_after_sso(**kwargs: Any) -> RegistrationJob | None:
    """Public hook for adapter: no-op when pipeline v2 disabled."""
    if not pipeline_v2_enabled():
        return None
    return RegistrationController().enqueue_after_sso(**kwargs)
