import json
from typing import Any, Dict, List, Optional, Union

from unlyx.transport import HttpRequest, HttpResponse, TransportError


class MockTransport:
    """Plays back queued responses (or transport failures) and records every request."""

    def __init__(self) -> None:
        self.queue: List[Union[HttpResponse, TransportError]] = []
        self.requests: List[HttpRequest] = []

    def json(self, status: int, body: Any, headers: Optional[Dict[str, str]] = None) -> "MockTransport":
        self.queue.append(HttpResponse(status, {k.lower(): v for k, v in (headers or {}).items()}, json.dumps(body).encode()))
        return self

    def raw(self, status: int, body: bytes, headers: Optional[Dict[str, str]] = None) -> "MockTransport":
        self.queue.append(HttpResponse(status, {k.lower(): v for k, v in (headers or {}).items()}, body))
        return self

    def fail(self, sent: bool, message: str = "boom") -> "MockTransport":
        self.queue.append(TransportError(message, sent))
        return self

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        if not self.queue:
            raise AssertionError("MockTransport: nothing queued for %s %s" % (request.method, request.url))
        nxt = self.queue.pop(0)
        if isinstance(nxt, TransportError):
            raise nxt
        return nxt

    @property
    def last(self) -> HttpRequest:
        return self.requests[-1]
