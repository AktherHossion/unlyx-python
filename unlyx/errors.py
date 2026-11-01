"""The one exception the SDK raises."""

from __future__ import annotations

from typing import Any, Dict, Optional


class UnlyxError(Exception):
    """Raised for every failure.

    ``code`` is the API's ``error.code`` (``low_balance``, ``invalid_input``, ``not_found``, ``service_disabled``,
    ``idempotency_conflict``, ``limit``, ``temporary``, ``rate_limited``, ``unauthorized``, ``forbidden`` ...) or an
    SDK code: ``connection_error``, ``invalid_response``, ``unexpected_redirect``, ``invalid_signature``,
    ``invalid_argument``.
    """

    def __init__(
        self,
        message: str,
        code: str,
        http_status: int = 0,
        body: Optional[Dict[str, Any]] = None,
        raw_body: str = "",
        retry_after: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        #: HTTP status, or 0 when no response was received.
        self.http_status = http_status
        #: Decoded JSON body, when there was one.
        self.body = body
        self.raw_body = raw_body
        #: Seconds from the Retry-After header (429 responses).
        self.retry_after = retry_after

    @property
    def reference(self) -> Optional[str]:
        """Short support reference the API attaches to every error. Quote it to the shop."""
        err = (self.body or {}).get("error")
        ref = err.get("reference") if isinstance(err, dict) else None
        return ref if isinstance(ref, str) else None

    def __repr__(self) -> str:
        return "UnlyxError(code=%r, http_status=%r, message=%r)" % (self.code, self.http_status, self.message)
