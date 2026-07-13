"""YYDS Mail temporary-inbox adapter used by the registration pipeline.

The adapter intentionally keeps the API-key lifetime short: it is used only
for inbox creation.  Subsequent reads and cleanup use the inbox-bound temporary
token returned by YYDS Mail.
"""
from __future__ import annotations

import email.utils
import math
import os
import re
import secrets
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx


DEFAULT_BASE_URL = "https://maliapi.215.im/v1"
_MAX_RETRY_DELAY = 30.0
_DEFAULT_MAX_ATTEMPTS = 3


class YYDSMailError(RuntimeError):
    """Base class for sanitized YYDS Mail failures."""


class YYDSMailConfigError(YYDSMailError):
    """Raised when the local adapter configuration is invalid."""


class YYDSMailAuthError(YYDSMailError):
    """Raised immediately for HTTP 401/403 responses."""


class YYDSMailRateLimitError(YYDSMailError):
    """Raised when a bounded rate-limit retry cannot proceed."""


class YYDSMailTransportError(YYDSMailError):
    """Raised after bounded network retries are exhausted."""


class YYDSMailProtocolError(YYDSMailError):
    """Raised for an invalid HTTP status or response envelope."""


class YYDSMailTimeout(YYDSMailError):
    """Raised when no new verification code arrives before the deadline."""


def _config_value(env_name: str, attr_name: str, default: str = "") -> str:
    """Read an environment setting, with an optional ``config`` fallback."""
    env_value = os.getenv(env_name)
    if env_value is not None:
        return env_value
    try:
        import config  # Imported lazily so this module also works standalone.

        value = getattr(config, attr_name, None)
        if value is None:
            value = getattr(config, env_name, default)
        return str(value or "")
    except Exception:
        return default


def normalize_yydsmail_base_url(base_url: str | None = None) -> str:
    """Return a YYDS Mail API base ending at the canonical ``/v1`` prefix.

    The public documentation URL is commonly copied into configuration by
    mistake.  Production documentation and any production API path both map
    to the actual API origin.  Custom/self-hosted origins retain their origin
    and gain (or are truncated at) the first ``/v1`` path segment.
    """
    raw = (
        base_url
        or _config_value(
            "GROK2API_YYDSMAIL_BASE_URL",
            "YYDSMAIL_BASE_URL",
            DEFAULT_BASE_URL,
        )
        or DEFAULT_BASE_URL
    ).strip()
    if "://" not in raw:
        raw = f"https://{raw}"
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise YYDSMailConfigError("YYDS Mail base URL must be an HTTP(S) URL")
    if parsed.username or parsed.password:
        raise YYDSMailConfigError("YYDS Mail base URL must not contain credentials")

    hostname = parsed.hostname.lower().rstrip(".")
    if hostname in {"vip.215.im", "maliapi.215.im"}:
        # vip.215.im is the documentation/UI host, not the JSON API host.
        return "https://maliapi.215.im/v1"

    segments = [segment for segment in parsed.path.split("/") if segment]
    if "v1" in segments:
        segments = segments[: segments.index("v1") + 1]
    else:
        # A copied custom /docs URL still denotes the origin, not an API path.
        if segments and segments[-1].lower() == "docs":
            segments.pop()
        segments.append("v1")
    path = "/" + "/".join(segments)
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _safe_error_code(payload: Any) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    value = payload.get("errorCode")
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value):
        return None
    return value


def _failure_message(operation: str, status_code: int, payload: Any = None) -> str:
    code = _safe_error_code(payload)
    suffix = f", errorCode={code}" if code else ""
    return f"YYDS Mail {operation} failed (HTTP {status_code}{suffix})"


def _json_payload(response: httpx.Response, operation: str) -> Mapping[str, Any]:
    try:
        payload = response.json()
    except Exception:
        raise YYDSMailProtocolError(
            f"YYDS Mail {operation} returned invalid JSON"
        ) from None
    if not isinstance(payload, Mapping):
        raise YYDSMailProtocolError(
            f"YYDS Mail {operation} returned an invalid response envelope"
        )
    return payload


def _envelope_data(response: httpx.Response, operation: str) -> Mapping[str, Any]:
    payload = _json_payload(response, operation)
    if payload.get("success") is not True:
        raise YYDSMailProtocolError(
            _failure_message(operation, response.status_code, payload)
        )
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise YYDSMailProtocolError(
            f"YYDS Mail {operation} response is missing object data"
        )
    return data


def _parse_retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After", "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(raw)
        return max(0.0, when.timestamp() - time.time())
    except (TypeError, ValueError, OverflowError):
        return None


def _backoff(attempt: int) -> float:
    return min(2.0, 0.25 * (2 ** max(0, attempt - 1)))


def _sleep_for_retry(
    delay: float,
    *,
    sleeper: Callable[[float], None],
    clock: Callable[[], float],
    deadline: float | None,
) -> bool:
    delay = max(0.0, float(delay))
    if delay > _MAX_RETRY_DELAY:
        return False
    if deadline is not None:
        remaining = deadline - clock()
        if remaining <= 0 or delay >= remaining:
            return False
    sleeper(delay)
    return True


def _request_with_retries(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    operation: str,
    max_attempts: int,
    sleeper: Callable[[float], None],
    clock: Callable[[], float],
    deadline: float | None = None,
    retry_transport: bool = True,
    retry_server_errors: bool = True,
    **kwargs: Any,
) -> httpx.Response:
    attempts = max(1, int(max_attempts))
    for attempt in range(1, attempts + 1):
        try:
            response = client.request(method, url, **kwargs)
        except httpx.RequestError:
            if not retry_transport or attempt >= attempts or not _sleep_for_retry(
                _backoff(attempt),
                sleeper=sleeper,
                clock=clock,
                deadline=deadline,
            ):
                raise YYDSMailTransportError(
                    f"YYDS Mail {operation} transport failed after bounded retries"
                ) from None
            continue

        if response.status_code in {401, 403}:
            raise YYDSMailAuthError(
                f"YYDS Mail {operation} authorization failed "
                f"(HTTP {response.status_code})"
            )

        if response.status_code == 429:
            payload: Any = None
            try:
                payload = response.json()
            except Exception:
                pass
            retry_after = _parse_retry_after(response)
            delay = retry_after if retry_after is not None else _backoff(attempt)
            if attempt >= attempts or not _sleep_for_retry(
                delay,
                sleeper=sleeper,
                clock=clock,
                deadline=deadline,
            ):
                raise YYDSMailRateLimitError(
                    _failure_message(operation, response.status_code, payload)
                )
            continue

        if 500 <= response.status_code <= 599:
            if not retry_server_errors or attempt >= attempts or not _sleep_for_retry(
                _backoff(attempt),
                sleeper=sleeper,
                clock=clock,
                deadline=deadline,
            ):
                raise YYDSMailTransportError(
                    f"YYDS Mail {operation} service unavailable after bounded retries"
                )
            continue

        return response

    raise YYDSMailTransportError(
        f"YYDS Mail {operation} failed after bounded retries"
    )


def _assert_status(
    response: httpx.Response,
    expected: set[int],
    operation: str,
) -> None:
    if response.status_code in expected:
        return
    payload: Any = None
    try:
        payload = response.json()
    except Exception:
        pass
    raise YYDSMailProtocolError(
        _failure_message(operation, response.status_code, payload)
    )


def _field_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return " ".join(_field_text(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return " ".join(_field_text(item) for item in value)
    return str(value)


def _normalize_direct_code(value: Any) -> str | None:
    if value is None:
        return None
    clean = re.sub(r"[\s-]+", "", str(value)).upper()
    return clean if re.fullmatch(r"[A-Z0-9]{6}", clean) else None


_XAI_DASHED_CODE = re.compile(
    r"(?<![A-Z0-9])([A-Z0-9]{3})-([A-Z0-9]{3})(?![A-Z0-9])",
    re.IGNORECASE,
)
_KEYWORD_CODE = re.compile(
    r"(?:verification\s+code|verify\s+code|code|验证码)"
    r"(?:\s*(?:is|[:：-])\s*)?([A-Z0-9]{6})(?![A-Z0-9])",
    re.IGNORECASE,
)
_SIX_DIGIT_CODE = re.compile(r"(?<![A-Z0-9])(\d{6})(?![A-Z0-9])", re.IGNORECASE)


def extract_yydsmail_code(message: Mapping[str, Any]) -> str | None:
    """Extract and normalize an xAI verification code from message detail."""
    direct = _normalize_direct_code(message.get("verificationCode"))
    if direct:
        return direct

    text = "\n".join(
        _field_text(message.get(field))
        for field in ("subject", "text", "html", "intro", "from")
    )[:500_000]
    dashed = _XAI_DASHED_CODE.search(text)
    if dashed:
        return "".join(dashed.groups()).upper()
    keyword = _KEYWORD_CODE.search(text)
    if keyword:
        return keyword.group(1).upper()
    numeric = _SIX_DIGIT_CODE.search(text)
    if numeric:
        return numeric.group(1)
    return None


class YYDSMailReceiver:
    """Persistent YYDS Mail client for one temporary inbox."""

    provider = "yyds"

    def __init__(
        self,
        *,
        address: str,
        account_id: str,
        temp_token: str,
        expires_at: str | None,
        base_url: str,
        client: httpx.Client,
        owns_client: bool = False,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not address or not account_id or not temp_token:
            raise YYDSMailProtocolError("YYDS Mail inbox metadata is incomplete")
        self.address = address
        self.email = address
        self.account_id = account_id
        self.email_id = account_id
        self.expires_at = expires_at
        self.base_url = normalize_yydsmail_base_url(base_url)
        self._temp_token = temp_token
        self._client = client
        self._owns_client = owns_client
        self._max_attempts = max(1, int(max_attempts))
        self._sleep = sleeper
        self._clock = clock
        self._seen_message_ids: set[str] = set()
        self._seen_codes: set[str] = set()
        self._released = False
        self._close_lock = threading.Lock()

    def __repr__(self) -> str:
        return (
            f"YYDSMailReceiver(address={self.address!r}, "
            f"account_id={self.account_id!r}, released={self._released})"
        )

    @property
    def released(self) -> bool:
        return self._released

    def _temp_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._temp_token}"}

    def wait_for_code(self, timeout: float = 120.0) -> str:
        """Long-poll until a new xAI code arrives or *timeout* expires."""
        if self._released:
            raise YYDSMailError("YYDS Mail inbox is already released")
        timeout = max(0.0, float(timeout))
        deadline = self._clock() + timeout

        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise YYDSMailTimeout(
                    "timeout waiting for xAI email verification code"
                )
            wait_seconds = min(30, max(0, int(math.floor(remaining))))
            request_started = self._clock()
            response = _request_with_retries(
                self._client,
                "GET",
                f"{self.base_url}/messages/next",
                operation="next message",
                max_attempts=self._max_attempts,
                sleeper=self._sleep,
                clock=self._clock,
                deadline=deadline,
                headers=self._temp_headers(),
                params={"wait": wait_seconds},
                timeout=max(5.0, float(wait_seconds) + 5.0),
            )
            _assert_status(response, {200, 204}, "next message")
            if response.status_code == 204:
                elapsed = max(0.0, self._clock() - request_started)
                # A proxy or overloaded service can return a nominal long-poll
                # immediately. Pace that degraded path to avoid a tight loop.
                if wait_seconds == 0 or elapsed < min(1.0, float(wait_seconds)):
                    tail = min(0.25, max(0.0, deadline - self._clock()))
                    if tail > 0:
                        self._sleep(tail)
                continue

            data = _envelope_data(response, "next message")
            message = data.get("message")
            if not isinstance(message, Mapping):
                raise YYDSMailProtocolError(
                    "YYDS Mail next message response is missing message detail"
                )

            raw_message_id = message.get("id") or message.get("messageId")
            message_id = str(raw_message_id) if raw_message_id else ""
            if message_id and message_id in self._seen_message_ids:
                continue
            if message_id:
                self._seen_message_ids.add(message_id)

            code = extract_yydsmail_code(message)
            if not code or code in self._seen_codes:
                continue
            self._seen_codes.add(code)
            return code

    def close(self) -> None:
        """Deactivate this temporary inbox; successful cleanup is idempotent."""
        with self._close_lock:
            if self._released:
                return
            try:
                response = _request_with_retries(
                    self._client,
                    "DELETE",
                    f"{self.base_url}/accounts/{self.account_id}",
                    operation="delete account",
                    max_attempts=min(self._max_attempts, 2),
                    sleeper=self._sleep,
                    clock=self._clock,
                    headers=self._temp_headers(),
                    timeout=10.0,
                )
                # A prior server-side cleanup makes DELETE effectively idempotent.
                _assert_status(response, {204, 404}, "delete account")
                self._released = True
                self._temp_token = ""
            finally:
                if self._owns_client:
                    self._client.close()

    def __enter__(self) -> "YYDSMailReceiver":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.close()


def create_yydsmail_receiver(
    *,
    prefix: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    domain: str | None = None,
    client: httpx.Client | None = None,
    timeout: float = 30.0,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[str, YYDSMailReceiver]:
    """Create a temporary inbox and return ``(address, receiver)``."""
    key = (
        api_key
        if api_key is not None
        else _config_value("GROK2API_YYDSMAIL_API_KEY", "YYDSMAIL_API_KEY")
    ).strip()
    if not key:
        raise YYDSMailConfigError(
            "YYDS Mail API key missing; set GROK2API_YYDSMAIL_API_KEY"
        )

    normalized_base = normalize_yydsmail_base_url(base_url)
    local_part = (prefix or secrets.token_hex(5)).strip().lower()
    if not re.fullmatch(r"[a-z0-9._-]{1,64}", local_part):
        raise YYDSMailConfigError(
            "YYDS Mail localPart must be 1-64 letters, digits, dots, underscores, or hyphens"
        )

    configured_domain = (
        domain
        if domain is not None
        else _config_value("GROK2API_YYDSMAIL_DOMAIN", "YYDSMAIL_DOMAIN")
    ).strip().lstrip("@").strip(".")
    payload: dict[str, str] = {"localPart": local_part}
    if configured_domain:
        payload["domain"] = configured_domain

    owns_client = client is None
    active_client = client or httpx.Client(timeout=float(timeout))
    try:
        response = _request_with_retries(
            active_client,
            "POST",
            f"{normalized_base}/accounts",
            operation="create account",
            max_attempts=max_attempts,
            sleeper=sleeper,
            clock=clock,
            headers={"X-API-Key": key},
            json=payload,
            timeout=float(timeout),
            retry_transport=False,
            retry_server_errors=False,
        )
        _assert_status(response, {201}, "create account")
        data = _envelope_data(response, "create account")
        account_id = str(data.get("id") or "").strip()
        address = str(data.get("address") or "").strip()
        temp_token = str(data.get("token") or "").strip()
        raw_expires_at = data.get("expiresAt")
        expires_at = str(raw_expires_at) if raw_expires_at is not None else None
        if not account_id or not address or not temp_token:
            raise YYDSMailProtocolError(
                "YYDS Mail create account response is missing id, address, or token"
            )
    except Exception:
        if owns_client:
            active_client.close()
        raise

    receiver = YYDSMailReceiver(
        address=address,
        account_id=account_id,
        temp_token=temp_token,
        expires_at=expires_at,
        base_url=normalized_base,
        client=active_client,
        owns_client=owns_client,
        max_attempts=max_attempts,
        sleeper=sleeper,
        clock=clock,
    )
    return address, receiver


# Naming aliases make integration call sites explicit while preserving the
# registration adapter's existing ``(address, receiver)`` factory convention.
make_yydsmail_receiver = create_yydsmail_receiver


__all__ = [
    "DEFAULT_BASE_URL",
    "YYDSMailAuthError",
    "YYDSMailConfigError",
    "YYDSMailError",
    "YYDSMailProtocolError",
    "YYDSMailRateLimitError",
    "YYDSMailReceiver",
    "YYDSMailTimeout",
    "YYDSMailTransportError",
    "create_yydsmail_receiver",
    "extract_yydsmail_code",
    "make_yydsmail_receiver",
    "normalize_yydsmail_base_url",
]
