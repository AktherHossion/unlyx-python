"""Unlyx REST API v1 client."""

from __future__ import annotations

import email.utils
import hashlib
import hmac
import json
import platform
import random
import re
import time
import uuid
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Union
from urllib.parse import quote, urlencode, urlsplit

from ._version import __version__
from .errors import UnlyxError
from .models import Account, BulkLine, BulkResult, Order, OrderPage, Service, Usage, Webhook
from .transport import HttpClientTransport, HttpRequest, HttpResponse, Transport, TransportError

SERVICE_TYPES = ("imei", "server", "file", "remote")
MAX_BULK = 100
_ORDER_FILTERS = ("status", "since", "imei", "page", "per_page")


def request_signature(secret: str, method: str, path: str, timestamp: str, raw_body: bytes) -> str:
    """hex HMAC-SHA256 over ``"{METHOD}\\n{path}\\n{timestamp}\\n{rawBody}"``; ``path`` has no query string."""
    msg = ("%s\n%s\n%s\n" % (method.upper(), path, timestamp)).encode("utf-8") + raw_body
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


class Client:
    """Unlyx REST API v1 client.

    >>> unlyx = Client("https://yourshop.com", os.environ["UNLYX_API_KEY"])
    >>> order = unlyx.place_order(service=42, imei="356938035643809", idempotency_key="my-order-1001")

    Retries (``max_retries``, default 2) are only made when they cannot cause a second order:

    * the connection failed before the request was sent (any method);
    * HTTP 429 with a ``Retry-After`` header (refused before processing);
    * HTTP 5xx or a lost response, for GET only;
    * the shop's own 503 ``temporary`` answer on a POST (nothing placed), with the same key.

    Every POST carries an ``Idempotency-Key``; a retry re-sends the SAME key.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 30.0,
        connect_timeout: float = 10.0,
        max_retries: int = 2,
        max_retry_wait: int = 60,
        signing_key_id: Optional[str] = None,
        signing_secret: Optional[str] = None,
        user_agent: Optional[str] = None,
        ca_bundle: Optional[str] = None,
        transport: Optional[Transport] = None,
        sleep: Callable[[float], None] = time.sleep,
        id_generator: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self._api_base = _normalise_base_url(base_url)
        if not isinstance(api_key, str) or not api_key or re.search(r"\s", api_key):
            raise UnlyxError("api_key must be a non-empty string without spaces", "invalid_argument")
        self._api_key = api_key
        self._timeout = float(timeout)
        self._connect_timeout = float(connect_timeout)
        self._max_retries = max(0, int(max_retries))
        self._max_retry_wait = max(0, int(max_retry_wait))
        if bool(signing_key_id) != bool(signing_secret):
            raise UnlyxError("signing_key_id and signing_secret must be set together", "invalid_argument")
        if signing_key_id and re.search(r"[\r\n]", signing_key_id):
            raise UnlyxError("signing_key_id must not contain line breaks", "invalid_argument")
        self._signing_key_id = signing_key_id or None
        self._signing_secret = signing_secret or None
        ua = "unlyx-python/%s (Python %s)" % (__version__, platform.python_version())
        self._user_agent = (re.sub(r"[\r\n]", "", user_agent).strip() + " " + ua) if user_agent else ua
        self._transport: Transport = transport or HttpClientTransport(ca_bundle)
        self._sleep = sleep
        self._new_id = id_generator

    # ------------------------------------------------------------------ account

    def account(self) -> Account:
        """GET /account — needs scope account.read."""
        return Account.from_dict(self._data(self.request("GET", "/account")))

    def usage(self, days: int = 7) -> Usage:
        """GET /usage — calls, errors and recent requests for this key over the last ``days`` (1-90)."""
        return Usage.from_dict(self._data(self.request("GET", "/usage", {"days": max(1, min(90, int(days)))})))

    # ------------------------------------------------------------------ services

    def services(self, type: Optional[str] = None) -> List[Service]:  # noqa: A002 - mirrors the API's name
        """GET /services — every service this key may order, at its API price. ``type``: imei|server|file|remote."""
        if type is not None and type not in SERVICE_TYPES:
            raise UnlyxError("type must be imei, server, file or remote", "invalid_argument")
        rows = self._data(self.request("GET", "/services", {"type": type} if type else None))
        return [Service.from_dict(r) for r in rows if isinstance(r, dict)]

    def service(self, service_id: int) -> Service:
        """GET /services/{id} — one service, with its description."""
        return Service.from_dict(self._data(self.request("GET", "/services/%d" % int(service_id))))

    # ------------------------------------------------------------------ orders

    def place_order(
        self,
        service: int,
        imei: Optional[str] = None,
        *,
        quantity: Optional[int] = None,
        fields: Optional[Mapping[str, Any]] = None,
        note: Optional[str] = None,
        file: Optional[Mapping[str, str]] = None,
        idempotency_key: Optional[str] = None,
        **extra: Any,
    ) -> Order:
        """POST /orders — place one order (scope orders.place).

        Pass your own ``idempotency_key`` (e.g. your internal order id) so re-running your code after a crash can
        never charge twice. Without one a random key is made; it still protects the SDK's own retries.
        ``file`` is ``{"name": "backup.bin", "data": <base64>}``.
        """
        body: Dict[str, Any] = {"service": service}
        if imei is not None:
            body["imei"] = imei
        if quantity is not None:
            body["quantity"] = quantity
        if fields:
            body["fields"] = dict(fields)
        if note is not None:
            body["note"] = note
        if file is not None:
            body["file"] = dict(file)
        body.update(extra)
        key = idempotency_key if idempotency_key is not None else self._new_id()
        _check_idempotency_key(key)
        env = self.request("POST", "/orders", body=body, headers={"Idempotency-Key": key})
        return Order.from_dict(self._data(env), bool(env.get("replayed", False)), _opt_str(env.get("balance")))

    def place_orders(self, orders: Sequence[Mapping[str, Any]]) -> BulkResult:
        """POST /orders/bulk — up to 100 lines; each answers on its own.

        A line's ``reference`` is its idempotency key. Lines without one get a random reference so retries are safe;
        set your own to make re-runs safe too.
        """
        if not orders or len(orders) > MAX_BULK:
            raise UnlyxError("orders must hold 1 to %d lines" % MAX_BULK, "invalid_argument")
        lines = []
        for i, line in enumerate(orders):
            if not isinstance(line, Mapping):
                raise UnlyxError("order line %d must be a mapping" % i, "invalid_argument")
            line = dict(line)
            if not line.get("reference"):
                line["reference"] = self._new_id()
            lines.append(line)
        env = self.request("POST", "/orders/bulk", body={"orders": lines})
        rows = [BulkLine.from_dict(r) for r in self._data(env) if isinstance(r, dict)]
        return BulkResult(lines=rows, balance=_opt_str(env.get("balance")) or "0")

    def orders(
        self,
        *,
        status: Optional[str] = None,
        since: Union[str, date, datetime, None] = None,
        imei: Optional[str] = None,
        page: Optional[int] = None,
        per_page: Optional[int] = None,
    ) -> OrderPage:
        """GET /orders — newest first."""
        q: Dict[str, Any] = {}
        if status:
            q["status"] = status
        if since:
            q["since"] = since.isoformat() if isinstance(since, (date, datetime)) else str(since)
        if imei:
            q["imei"] = imei
        if page is not None:
            q["page"] = int(page)
        if per_page is not None:
            q["per_page"] = int(per_page)
        env = self.request("GET", "/orders", q)
        orders = [Order.from_dict(r) for r in self._data(env) if isinstance(r, dict)]
        return OrderPage(orders=orders, page=int(env.get("page", 1) or 1), per_page=int(env.get("per_page", len(orders)) or 0),
                         total=int(env.get("total", len(orders)) or 0))

    def order(self, reference: str) -> Order:
        """GET /orders/{reference} (scope orders.read)."""
        return Order.from_dict(self._data(self.request("GET", "/orders/" + _ref(reference))))

    def cancel_order(self, reference: str) -> Order:
        """POST /orders/{reference}/cancel — only inside the service's cancel window (scope orders.cancel)."""
        env = self.request("POST", "/orders/" + _ref(reference) + "/cancel")
        return Order.from_dict(self._data(env), None, _opt_str(env.get("balance")))

    # ------------------------------------------------------------------ webhooks

    def webhooks(self) -> List[Webhook]:
        """GET /webhooks (scope webhooks.manage)."""
        return [Webhook.from_dict(r) for r in self._data(self.request("GET", "/webhooks")) if isinstance(r, dict)]

    def create_webhook(self, url: str, events: Optional[Sequence[str]] = None, name: Optional[str] = None) -> Webhook:
        """POST /webhooks — at most 5 per key. ``secret`` on the result is shown only now: store it."""
        body: Dict[str, Any] = {"url": url, "events": list(events) if events else ["order.*"]}
        if name is not None:
            body["name"] = name
        return Webhook.from_dict(self._data(self.request("POST", "/webhooks", body=body)))

    def delete_webhook(self, webhook_id: int) -> bool:
        """DELETE /webhooks/{id}."""
        return bool(self._data(self.request("DELETE", "/webhooks/%d" % int(webhook_id))).get("deleted", False))

    # ------------------------------------------------------------------ raw

    def request(
        self,
        method: str,
        path: str,
        query: Optional[Mapping[str, Any]] = None,
        body: Optional[Any] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> Dict[str, Any]:
        """Escape hatch: call any v1 endpoint; returns the whole decoded envelope (``{"ok": true, "data": ...}``).

        ``path`` is relative to /api/v1, e.g. ``"/orders"``. Errors still raise :class:`UnlyxError`.
        """
        method = method.upper()
        if not re.fullmatch(r"[A-Z]+", method):
            raise UnlyxError("Bad HTTP method", "invalid_argument")
        if not path.startswith("/"):
            path = "/" + path
        if "://" in path or ".." in path or "?" in path or "#" in path or re.search(r"[\x00-\x20\x7f]", path):
            raise UnlyxError('path must be a plain API path like "/orders" (put the query in query=)', "invalid_argument")
        extra = dict(headers or {})
        for k, v in extra.items():
            if re.search(r"[\r\n]", str(k) + str(v)):
                raise UnlyxError("Header values must not contain line breaks", "invalid_argument")
        q = {k: v for k, v in (query or {}).items() if v is not None}
        url = self._api_base + path + ("?" + urlencode(q, quote_via=quote) if q else "")
        raw: Optional[bytes] = None
        if body is not None:
            try:
                raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            except (TypeError, ValueError) as e:
                raise UnlyxError("Body cannot be encoded as JSON: %s" % e, "invalid_argument") from e
        if method == "POST" and _find(extra, "Idempotency-Key") is None:
            extra["Idempotency-Key"] = self._new_id()  # made once; retries re-send it
        return self._send(method, url, raw, extra)

    # ------------------------------------------------------------------ internals

    def _send(self, method: str, url: str, raw: Optional[bytes], extra: Dict[str, str]) -> Dict[str, Any]:
        safe = method in ("GET", "HEAD")
        attempt = 0
        while True:
            req = HttpRequest(method, url, self._headers(method, url, raw, extra), raw, self._timeout, self._connect_timeout)
            try:
                resp = self._transport.send(req)
            except TransportError as e:
                if (not e.sent or safe) and attempt < self._max_retries:
                    self._sleep(_backoff(attempt))
                    attempt += 1
                    continue
                hint = (" The request may have reached the shop: check the order (or repeat it with the SAME "
                        "Idempotency-Key) before placing it again.") if e.sent and not safe else ""
                raise UnlyxError(str(e) + hint, "connection_error") from e

            status = resp.status
            if 300 <= status < 400:
                raise UnlyxError("The API answered with a redirect (HTTP %d). Check the base URL; redirects are never "
                                 "followed." % status, "unexpected_redirect", status, None, _text(resp.body))
            retry_after = _retry_after(resp)
            if status == 429 and retry_after is not None and retry_after <= self._max_retry_wait and attempt < self._max_retries:
                attempt += 1
                self._sleep(float(retry_after))
                continue
            if status >= 500 and safe and attempt < self._max_retries:
                self._sleep(_backoff(attempt))
                attempt += 1
                continue
            if (status == 503 and method == "POST" and _find(extra, "Idempotency-Key") is not None
                    and _error_code(resp) == "temporary" and attempt < self._max_retries):
                self._sleep(_backoff(attempt))
                attempt += 1
                continue
            return _decode(resp, retry_after)

    def _headers(self, method: str, url: str, raw: Optional[bytes], extra: Mapping[str, str]) -> Dict[str, str]:
        h = {"Authorization": "Bearer " + self._api_key, "Accept": "application/json", "User-Agent": self._user_agent}
        if raw is not None:
            h["Content-Type"] = "application/json"
        if self._signing_key_id and self._signing_secret:
            ts = str(int(time.time()))  # fresh on every attempt
            h["X-Key-Id"] = self._signing_key_id
            h["X-Timestamp"] = ts
            h["X-Signature"] = request_signature(self._signing_secret, method, urlsplit(url).path, ts, raw or b"")
        for k, v in extra.items():
            h[str(k)] = str(v)
        return h

    @staticmethod
    def _data(env: Mapping[str, Any]) -> Any:
        d = env.get("data")
        if not isinstance(d, (dict, list)):
            raise UnlyxError('The API answer has no "data"', "invalid_response", 200, dict(env))
        return d


# ---------------------------------------------------------------------- helpers

def _normalise_base_url(base_url: str) -> str:
    if not isinstance(base_url, str):
        raise UnlyxError("base_url must be a string", "invalid_argument")
    p = urlsplit(base_url.strip())
    if p.scheme.lower() != "https" or not p.hostname:
        raise UnlyxError("base_url must be an https:// address, e.g. https://yourshop.com", "invalid_argument")
    if p.username or p.password or p.query or p.fragment:
        raise UnlyxError("base_url must not contain credentials, a query or a fragment", "invalid_argument")
    path = p.path.rstrip("/")
    if path.lower().endswith("/api/v1"):
        path = path[:-7]
    host = p.hostname.lower()
    if ":" in host:
        host = "[" + host + "]"
    return "https://" + host + (":%d" % p.port if p.port else "") + path + "/api/v1"


def _decode(resp: HttpResponse, retry_after: Optional[int]) -> Dict[str, Any]:
    status = resp.status
    text = _text(resp.body)
    decoded: Any = None
    if text:
        try:
            decoded = json.loads(text)
        except ValueError:
            decoded = None
    if not isinstance(decoded, dict):
        if status >= 400:
            raise UnlyxError("HTTP %d from the API (no JSON body)" % status, "rate_limited" if status == 429 else "http_%d" % status,
                             status, None, text, retry_after)
        raise UnlyxError("The API answered with something that is not JSON (HTTP %d)" % status, "invalid_response", status, None, text)
    if status >= 400 or decoded.get("ok") is False:
        err = decoded.get("error") if isinstance(decoded.get("error"), dict) else {}
        code = err.get("code") if isinstance(err.get("code"), str) and err.get("code") else ("rate_limited" if status == 429 else "http_%d" % status)
        message = err.get("message") if isinstance(err.get("message"), str) and err.get("message") else "HTTP %d" % status
        raise UnlyxError(message, code, status, decoded, text, retry_after)
    return decoded


def _error_code(resp: HttpResponse) -> Optional[str]:
    try:
        d = json.loads(_text(resp.body))
    except ValueError:
        return None
    err = d.get("error") if isinstance(d, dict) else None
    code = err.get("code") if isinstance(err, dict) else None
    return code.lower() if isinstance(code, str) else None


def _retry_after(resp: HttpResponse) -> Optional[int]:
    v = (resp.header("Retry-After") or "").strip()
    if not v:
        return None
    if v.isdigit():
        return int(v)
    try:
        dt = email.utils.parsedate_to_datetime(v)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    return max(0, int(dt.timestamp() - time.time()))


def _backoff(attempt: int) -> float:
    return min(8.0, 0.5 * (2 ** attempt)) + random.uniform(0, 0.25)


def _text(body: bytes) -> str:
    return body.decode("utf-8", errors="replace") if body else ""


def _find(headers: Mapping[str, str], name: str) -> Optional[str]:
    for k, v in headers.items():
        if k.lower() == name.lower():
            return v
    return None


def _ref(reference: str) -> str:
    reference = str(reference).strip()
    if not reference or len(reference) > 120:
        raise UnlyxError("reference must be 1-120 characters", "invalid_argument")
    return quote(reference, safe="")


def _check_idempotency_key(key: str) -> None:
    if not isinstance(key, str) or not key or len(key) > 120 or re.search(r"[\r\n]", key):
        raise UnlyxError("Idempotency-Key must be 1-120 characters on one line", "invalid_argument")


def _opt_str(v: Any) -> Optional[str]:
    if v is None or isinstance(v, bool):
        return None
    return repr(v) if isinstance(v, float) else str(v)
