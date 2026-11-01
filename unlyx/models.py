"""Typed value objects for API answers. Each keeps the full answer in ``raw``."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional


def _s(d: Mapping[str, Any], k: str, default: str = "") -> str:
    v = d.get(k)
    return str(v) if isinstance(v, (str, int, float)) and not isinstance(v, bool) else default


def _ns(d: Mapping[str, Any], k: str) -> Optional[str]:
    v = d.get(k)
    return str(v) if isinstance(v, (str, int, float)) and not isinstance(v, bool) else None


def _i(d: Mapping[str, Any], k: str, default: int = 0) -> int:
    v = d.get(k)
    try:
        return int(v) if v is not None and not isinstance(v, bool) else default
    except (TypeError, ValueError):
        return default


def _ni(d: Mapping[str, Any], k: str) -> Optional[int]:
    v = d.get(k)
    try:
        return int(v) if v is not None and not isinstance(v, bool) else None
    except (TypeError, ValueError):
        return None


def _b(d: Mapping[str, Any], k: str, default: bool = False) -> bool:
    v = d.get(k)
    return bool(v) if v is not None else default


def _d(d: Mapping[str, Any], k: str) -> Dict[str, Any]:
    v = d.get(k)
    return v if isinstance(v, dict) else {}


def _l(d: Mapping[str, Any], k: str) -> List[Any]:
    v = d.get(k)
    return v if isinstance(v, list) else []


def _money(d: Mapping[str, Any], k: str) -> str:
    """Money stays an exact decimal string ("1.25"); use decimal.Decimal(order.price) for maths."""
    v = d.get(k)
    if isinstance(v, bool) or v is None:
        return "0"
    if isinstance(v, float):
        return repr(v)
    return str(v)


@dataclass(frozen=True)
class Account:
    email: str
    name: str
    username: Optional[str]
    type: Optional[str]
    balance: str
    currency: str
    key_name: str
    scopes: List[str]
    sandbox: bool
    key_expires_at: Optional[str]
    rate_limit_per_min: int
    daily_order_limit: int
    raw: Dict[str, Any] = field(repr=False, compare=False)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Account":
        k = _d(d, "key")
        return cls(
            email=_s(d, "email"), name=_s(d, "name"), username=_ns(d, "username"), type=_ns(d, "type"),
            balance=_money(d, "balance"), currency=_s(d, "currency"), key_name=_s(k, "name"),
            scopes=[str(s) for s in _l(k, "scopes")], sandbox=_b(k, "sandbox"), key_expires_at=_ns(k, "expires_at"),
            rate_limit_per_min=_i(k, "rate_limit_per_min", 60), daily_order_limit=_i(k, "daily_order_limit"), raw=dict(d),
        )

    def can(self, scope: str) -> bool:
        return scope in self.scopes


@dataclass(frozen=True)
class ServiceField:
    key: str
    label: str
    type: str
    required: bool
    help: Optional[str]
    options: List[str]

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "ServiceField":
        return cls(
            key=_s(d, "key"), label=_s(d, "label"), type=_s(d, "type", "text"), required=_b(d, "required"),
            help=_ns(d, "help"), options=[str(o) for o in _l(d, "options") if isinstance(o, (str, int, float))],
        )


@dataclass(frozen=True)
class Service:
    id: int
    name: str
    #: imei | server | file | remote
    type: str
    group: Optional[str]
    price: str
    currency: str
    delivery_time: Optional[str]
    quantity_enabled: bool
    quantity_min: int
    quantity_max: int
    quantity_options: List[int]
    fields: List[ServiceField]
    can_cancel: bool
    #: only filled by Client.service(id)
    description: Optional[str]
    raw: Dict[str, Any] = field(repr=False, compare=False)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Service":
        q = _d(d, "quantity")
        opts: List[int] = []
        for o in _l(q, "options"):
            try:
                opts.append(int(o))
            except (TypeError, ValueError):
                pass
        return cls(
            id=_i(d, "id"), name=_s(d, "name"), type=_s(d, "type"), group=_ns(d, "group"), price=_money(d, "price"),
            currency=_s(d, "currency"), delivery_time=_ns(d, "delivery_time"), quantity_enabled=_b(q, "enabled"),
            quantity_min=_i(q, "min"), quantity_max=_i(q, "max"), quantity_options=opts,
            fields=[ServiceField.from_dict(f) for f in _l(d, "fields") if isinstance(f, dict)],
            can_cancel=_b(d, "can_cancel"), description=_ns(d, "description"), raw=dict(d),
        )

    def field(self, key: str) -> Optional[ServiceField]:
        return next((f for f in self.fields if f.key == key), None)


@dataclass(frozen=True)
class Order:
    """``status`` is the shop's state; ``dhru_status`` the classic code: 0 new, 1 in process, 3 rejected, 4 done."""

    reference: str
    status: str
    dhru_status: int
    service_id: Optional[int]
    service_name: Optional[str]
    service_type: Optional[str]
    quantity: int
    imei: Optional[str]
    fields: Dict[str, Any]
    price: str
    currency: Optional[str]
    #: result text (unlock code, reply) once done
    reply: Optional[str]
    placed_at: Optional[str]
    completed_at: Optional[str]
    sandbox: bool
    #: place_order() only: True when the Idempotency-Key matched an earlier order (no second charge)
    replayed: Optional[bool] = None
    #: place_order() / cancel_order() only: balance after the call
    balance: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any], replayed: Optional[bool] = None, balance: Optional[str] = None) -> "Order":
        s = _d(d, "service")
        return cls(
            reference=_s(d, "reference"), status=_s(d, "status"), dhru_status=_i(d, "dhru_status"),
            service_id=_ni(s, "id"), service_name=_ns(s, "name"), service_type=_ns(s, "type"),
            quantity=_i(d, "quantity", 1), imei=_ns(d, "imei"), fields=_d(d, "fields"), price=_money(d, "price"),
            currency=_ns(d, "currency"), reply=_ns(d, "reply"), placed_at=_ns(d, "placed_at"),
            completed_at=_ns(d, "completed_at"), sandbox=_b(d, "sandbox"), replayed=replayed, balance=balance, raw=dict(d),
        )

    @property
    def is_final(self) -> bool:
        """Done one way or the other — stop polling."""
        return self.dhru_status in (3, 4)

    @property
    def is_success(self) -> bool:
        """Delivered (completed / replied). Read the result from ``reply``."""
        return self.dhru_status == 4

    @property
    def is_rejected(self) -> bool:
        """Rejected, cancelled or refunded — the money went back to the balance."""
        return self.dhru_status == 3


@dataclass(frozen=True)
class OrderPage:
    orders: List[Order]
    page: int
    per_page: int
    total: int

    @property
    def has_more(self) -> bool:
        return self.page * self.per_page < self.total


@dataclass(frozen=True)
class BulkLine:
    line: int
    #: the reference you sent for this line (its idempotency key)
    reference: Optional[str]
    ok: bool
    order: Optional[Order]
    error_code: Optional[str]
    error_message: Optional[str]
    raw: Dict[str, Any] = field(repr=False, compare=False)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "BulkLine":
        ok = _b(d, "ok")
        err = _d(d, "error")
        return cls(
            line=_i(d, "line"), reference=_ns(d, "reference_in"), ok=ok, order=Order.from_dict(d) if ok else None,
            error_code=None if ok else _ns(err, "code"), error_message=None if ok else _ns(err, "message"), raw=dict(d),
        )


@dataclass(frozen=True)
class BulkResult:
    lines: List[BulkLine]
    balance: str

    @property
    def succeeded(self) -> List[BulkLine]:
        return [l for l in self.lines if l.ok]

    @property
    def failed(self) -> List[BulkLine]:
        return [l for l in self.lines if not l.ok]


@dataclass(frozen=True)
class Webhook:
    id: int
    name: Optional[str]
    url: str
    events: List[str]
    active: bool
    secret_preview: Optional[str]
    last_status: Optional[int]
    last_attempt_at: Optional[str]
    #: signing secret ("whsec_..."), returned ONCE by create_webhook(). Store it.
    secret: Optional[str]
    raw: Dict[str, Any] = field(repr=False, compare=False)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Webhook":
        return cls(
            id=_i(d, "id"), name=_ns(d, "name"), url=_s(d, "url"), events=[str(e) for e in _l(d, "events")],
            active=_b(d, "active"), secret_preview=_ns(d, "secret_preview"), last_status=_ni(d, "last_status"),
            last_attempt_at=_ns(d, "last_attempt_at"), secret=_ns(d, "secret"), raw=dict(d),
        )


@dataclass(frozen=True)
class Usage:
    since: Optional[str]
    calls: int
    errors: int
    by_action: Dict[str, int]
    #: each: at, protocol, action, status, ok, ms, error
    recent: List[Dict[str, Any]]
    rate_limit_per_min: int
    raw: Dict[str, Any] = field(repr=False, compare=False)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Usage":
        by = {}
        for k, v in _d(d, "by_action").items():
            try:
                by[str(k)] = int(v)
            except (TypeError, ValueError):
                pass
        return cls(
            since=_ns(d, "since"), calls=_i(d, "calls"), errors=_i(d, "errors"), by_action=by,
            recent=[r for r in _l(d, "recent") if isinstance(r, dict)], rate_limit_per_min=_i(d, "rate_limit_per_min", 60), raw=dict(d),
        )
