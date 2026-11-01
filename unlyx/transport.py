"""HTTP transport. The default one uses only the standard library (http.client + ssl)."""

from __future__ import annotations

import http.client
import ssl
from dataclasses import dataclass, field
from typing import Dict, Optional, Protocol
from urllib.parse import urlsplit


@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str
    headers: Dict[str, str]
    body: Optional[bytes]
    timeout: float
    connect_timeout: float

    def header(self, name: str) -> Optional[str]:
        for k, v in self.headers.items():
            if k.lower() == name.lower():
                return v
        return None


@dataclass(frozen=True)
class HttpResponse:
    status: int
    #: lower-case header names
    headers: Dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    def header(self, name: str) -> Optional[str]:
        return self.headers.get(name.lower())


class TransportError(Exception):
    """No complete HTTP response.

    ``sent`` is False when the request certainly never left (DNS, refused, TLS handshake), so any request is safe to
    retry; True when the server may have processed it (timeout while waiting, connection reset mid-response).
    """

    def __init__(self, message: str, sent: bool) -> None:
        super().__init__(message)
        self.sent = sent


class Transport(Protocol):
    """Send one request. Must not follow redirects; any HTTP status is a response, not an error."""

    def send(self, request: HttpRequest) -> HttpResponse:  # pragma: no cover - protocol
        ...


class HttpClientTransport:
    """Default transport: HTTPS only, certificates verified, redirects never followed.

    Note: like http.client, it does not read HTTPS_PROXY. Pass your own Transport if you need a proxy.
    """

    def __init__(self, ca_bundle: Optional[str] = None) -> None:
        self._context = ssl.create_default_context(cafile=ca_bundle) if ca_bundle else ssl.create_default_context()

    def send(self, request: HttpRequest) -> HttpResponse:
        parts = urlsplit(request.url)
        if parts.scheme != "https" or not parts.hostname:
            raise TransportError("Only https:// URLs are allowed", sent=False)
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        conn = http.client.HTTPSConnection(parts.hostname, parts.port or 443, timeout=request.connect_timeout, context=self._context)
        try:
            try:
                conn.connect()
            except (OSError, ssl.SSLError, http.client.HTTPException) as e:
                raise TransportError("Connection failed: %s" % (e,), sent=False) from e
            if conn.sock is not None:
                conn.sock.settimeout(request.timeout)
            try:
                conn.request(request.method, target, body=request.body, headers=request.headers)
                resp = conn.getresponse()
                body = resp.read()
            except (OSError, http.client.HTTPException) as e:  # socket.timeout is an OSError
                raise TransportError("Connection failed after sending: %s" % (e,), sent=True) from e
            headers = {k.lower(): v for k, v in resp.getheaders()}
            return HttpResponse(status=resp.status, headers=headers, body=body)
        finally:
            conn.close()
