"""Verify webhook deliveries from an Unlyx shop.

Each delivery is a POST with a JSON body and these headers:

* ``X-Unlyx-Event`` — ``order.completed``, ``order.rejected``, ...
* ``X-Unlyx-Delivery`` — delivery id (a retry keeps it; use it to ignore duplicates)
* ``X-Unlyx-Timestamp`` — unix time the attempt was signed
* ``X-Unlyx-Signature`` — ``t=<unix>,v1=<hex HMAC-SHA256 of "<unix>.<raw body>" keyed with your whsec_ secret>``
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple, Union

from .errors import UnlyxError

SIGNATURE_HEADER = "X-Unlyx-Signature"
DEFAULT_TOLERANCE = 300

HeadersLike = Union[Mapping[str, Any], Iterable[Tuple[str, Any]]]


def _header(headers: HeadersLike, name: str) -> Optional[str]:
    wsgi = "HTTP_" + name.upper().replace("-", "_")  # WSGI environ style
    items = headers.items() if isinstance(headers, Mapping) else headers
    for k, v in items:
        if isinstance(k, str) and (k.lower() == name.lower() or k.upper() == wsgi):
            if isinstance(v, (list, tuple)):
                v = v[0] if v else None
            if isinstance(v, bytes):
                v = v.decode("latin-1")
            return v.strip() if isinstance(v, str) else None
    return None


def sign(secret: str, timestamp: int, payload: Union[bytes, str]) -> str:
    """hex HMAC-SHA256 of ``"<timestamp>.<payload>"`` — the v1 value."""
    body = payload.encode("utf-8") if isinstance(payload, str) else payload
    return hmac.new(secret.encode("utf-8"), str(int(timestamp)).encode("ascii") + b"." + body, hashlib.sha256).hexdigest()


def signature_header(secret: str, timestamp: int, payload: Union[bytes, str]) -> str:
    """A full ``X-Unlyx-Signature`` value — handy for testing your own receiver."""
    return "t=%d,v1=%s" % (int(timestamp), sign(secret, timestamp, payload))


def verify_webhook(
    payload: Union[bytes, str],
    headers: HeadersLike,
    secret: Union[str, Sequence[str]],
    tolerance: int = DEFAULT_TOLERANCE,
    now: Optional[int] = None,
) -> Dict[str, Any]:
    """Check the signature on the RAW body (before parsing) and return the decoded event.

    ``headers`` may be any mapping (Flask ``request.headers``, Django ``request.headers`` or ``request.META``, a dict)
    or a list of pairs; names are matched without regard to case. ``secret`` may be a list while you rotate.
    ``tolerance`` (seconds) refuses old or future timestamps to stop replays; 0 turns the check off.

    Raises :class:`UnlyxError` with code ``invalid_signature`` on any failure.
    """
    raw = _header(headers, SIGNATURE_HEADER)
    if not raw:
        raise _fail("Missing %s header" % SIGNATURE_HEADER)
    ts: Optional[int] = None
    sigs = []
    for part in raw.split(","):
        k, sep, v = part.strip().partition("=")
        if not sep:
            continue
        if k == "t" and v.isdigit():
            ts = int(v)
        elif k == "v1" and v:
            sigs.append(v.lower())
    if ts is None or not sigs:
        raise _fail("Malformed %s header" % SIGNATURE_HEADER)
    current = int(time.time()) if now is None else int(now)
    if tolerance > 0 and abs(current - ts) > tolerance:
        raise _fail("Signature timestamp is outside the allowed window (possible replay or clock drift)")

    secrets = [secret] if isinstance(secret, str) else list(secret)
    secrets = [s for s in secrets if isinstance(s, str) and s]
    if not secrets:
        raise UnlyxError("A webhook secret is required", "invalid_argument")
    valid = False
    for s in secrets:
        expected = sign(s, ts, payload)
        for sig in sigs:
            if hmac.compare_digest(expected, sig):
                valid = True
    if not valid:
        raise _fail("Signature does not match")

    try:
        text = payload.decode("utf-8") if isinstance(payload, bytes) else payload
        event = json.loads(text)
    except (UnicodeDecodeError, ValueError):
        raise _fail("Signed body is not valid JSON") from None
    if not isinstance(event, dict):
        raise _fail("Signed body is not a JSON object")
    return event


def is_valid_webhook(payload: Union[bytes, str], headers: HeadersLike, secret: Union[str, Sequence[str]],
                     tolerance: int = DEFAULT_TOLERANCE, now: Optional[int] = None) -> bool:
    try:
        verify_webhook(payload, headers, secret, tolerance, now)
        return True
    except UnlyxError:
        return False


class Webhook:
    """Namespace mirroring the PHP and Node SDKs: ``Webhook.verify(payload, headers, secret)``."""

    verify = staticmethod(verify_webhook)
    is_valid = staticmethod(is_valid_webhook)
    sign = staticmethod(sign)
    signature_header = staticmethod(signature_header)


def _fail(message: str) -> UnlyxError:
    return UnlyxError(message, "invalid_signature", 400)
