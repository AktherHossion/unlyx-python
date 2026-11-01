import hashlib
import hmac
import json
import os
import re
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from mock_transport import MockTransport  # noqa: E402
from unlyx import Client, UnlyxError  # noqa: E402

UUID4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def order_row(**over):
    row = {
        "reference": "UX-1001", "status": "processing", "dhru_status": 1,
        "service": {"id": 42, "name": "iPhone Carrier Check (GSX)", "type": "imei"},
        "quantity": 1, "imei": "356938035643809", "fields": {}, "price": "1.25", "currency": "USD",
        "reply": None, "placed_at": "2026-11-01T10:00:00+00:00", "completed_at": None,
    }
    row.update(over)
    return row


def placed(**over):
    return {"ok": True, "data": order_row(**over), "replayed": False, "balance": "248.75"}


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.http = MockTransport()
        self.slept = []

    def client(self, **kw):
        kw.setdefault("transport", self.http)
        kw.setdefault("sleep", self.slept.append)
        return Client("https://yourshop.com", "live_test_key_123", **kw)

    def body(self, i=-1):
        return json.loads(self.http.requests[i].body.decode())

    # ---- construction

    def test_rejects_plain_http(self):
        with self.assertRaises(UnlyxError) as c:
            Client("http://yourshop.com", "k")
        self.assertEqual(c.exception.code, "invalid_argument")

    def test_base_url_strips_api_suffix(self):
        self.http.json(200, {"ok": True, "data": {"key": {}}})
        Client("https://YourShop.com/api/v1/", "k", transport=self.http).account()
        self.assertEqual(self.http.last.url, "https://yourshop.com/api/v1/account")

    def test_signing_needs_both_parts(self):
        with self.assertRaises(UnlyxError):
            Client("https://yourshop.com", "k", signing_key_id="kid_1")

    # ---- endpoints

    def test_account(self):
        self.http.json(200, {"ok": True, "data": {
            "email": "reseller@example.com", "name": "North Unlock", "username": "north", "type": "reseller",
            "balance": "250.40", "currency": "USD",
            "key": {"name": "Panel", "scopes": ["account.read", "orders.place"], "sandbox": False, "expires_at": None,
                    "rate_limit_per_min": 120, "daily_order_limit": 0}}})
        a = self.client().account()
        r = self.http.last
        self.assertEqual(r.method, "GET")
        self.assertEqual(r.header("Authorization"), "Bearer live_test_key_123")
        self.assertIsNone(r.body)
        self.assertEqual(a.balance, "250.40")
        self.assertEqual(a.rate_limit_per_min, 120)
        self.assertTrue(a.can("orders.place"))
        self.assertFalse(a.can("orders.cancel"))

    def test_services(self):
        self.http.json(200, {"ok": True, "count": 1, "data": [{
            "id": 7, "name": "Samsung FRP Remove (Server)", "type": "server", "group": "Samsung", "price": 3.5, "currency": "USD",
            "delivery_time": "1-30 min", "quantity": {"enabled": True, "min": 1, "max": 10, "options": [1, 5, 10]},
            "fields": [{"key": "serial", "label": "Serial number", "type": "text", "required": True, "help": None, "options": []}],
            "can_cancel": True}]})
        rows = self.client().services("server")
        self.assertTrue(self.http.last.url.endswith("/api/v1/services?type=server"))
        self.assertEqual(rows[0].price, "3.5")
        self.assertEqual(rows[0].quantity_options, [1, 5, 10])
        self.assertTrue(rows[0].field("serial").required)

    def test_services_rejects_unknown_type(self):
        with self.assertRaises(UnlyxError):
            self.client().services("phone")

    def test_service_one(self):
        self.http.json(200, {"ok": True, "data": {"id": 9, "name": "Xiaomi Mi Account Remove", "type": "remote", "description": "Remote job"}})
        s = self.client().service(9)
        self.assertTrue(self.http.last.url.endswith("/services/9"))
        self.assertEqual(s.description, "Remote job")

    def test_place_order(self):
        self.http.json(201, placed())
        o = self.client().place_order(42, "356938035643809", fields={"note": "x"}, idempotency_key="shop-order-1001")
        r = self.http.last
        self.assertEqual((r.method, r.url), ("POST", "https://yourshop.com/api/v1/orders"))
        self.assertEqual(r.header("Idempotency-Key"), "shop-order-1001")
        self.assertEqual(r.header("Content-Type"), "application/json")
        self.assertEqual(self.body(), {"service": 42, "imei": "356938035643809", "fields": {"note": "x"}})
        self.assertEqual(o.reference, "UX-1001")
        self.assertFalse(o.replayed)
        self.assertEqual(o.balance, "248.75")
        self.assertFalse(o.is_final)

    def test_place_order_generates_uuid_key(self):
        self.http.json(201, placed())
        self.client().place_order(42, "356938035643809")
        self.assertRegex(self.http.last.header("Idempotency-Key"), UUID4)

    # ---- retry rules

    def test_post_retried_when_not_sent_with_same_key(self):
        self.http.fail(False, "Name or service not known").json(201, placed())
        self.client().place_order(42, "356938035643809")
        self.assertEqual(len(self.http.requests), 2)
        self.assertEqual(self.http.requests[0].header("Idempotency-Key"), self.http.requests[1].header("Idempotency-Key"))

    def test_post_never_retried_when_maybe_sent(self):
        self.http.fail(True, "timed out")
        with self.assertRaises(UnlyxError) as c:
            self.client().place_order(42, "356938035643809")
        self.assertEqual(c.exception.code, "connection_error")
        self.assertEqual(c.exception.http_status, 0)
        self.assertIn("SAME Idempotency-Key", c.exception.message)
        self.assertEqual(len(self.http.requests), 1)

    def test_post_not_retried_on_plain_5xx(self):
        self.http.json(502, {"ok": False, "error": {"code": "server_error", "message": "Bad gateway"}})
        with self.assertRaises(UnlyxError):
            self.client().place_order(42, "356938035643809")
        self.assertEqual(len(self.http.requests), 1)

    def test_post_retried_on_shop_temporary_503(self):
        self.http.json(503, {"ok": False, "error": {"code": "temporary", "message": "Temporary error"}}).json(201, placed())
        self.client().place_order(42, "356938035643809", idempotency_key="k-503")
        self.assertEqual(len(self.http.requests), 2)
        self.assertEqual(self.http.requests[1].header("Idempotency-Key"), "k-503")

    def test_get_retried_on_5xx_and_lost_response(self):
        self.http.json(503, {}).fail(True, "reset").json(200, {"ok": True, "data": order_row(status="completed", dhru_status=4, reply="CODE: 12345678")})
        o = self.client().order("UX-1001")
        self.assertEqual(len(self.http.requests), 3)
        self.assertEqual(len(self.slept), 2)
        self.assertTrue(o.is_success)
        self.assertEqual(o.reply, "CODE: 12345678")

    def test_retries_stop_at_max(self):
        self.http.json(500, {}).json(500, {}).json(500, {"ok": False, "error": {"code": "server_error", "message": "down"}})
        with self.assertRaises(UnlyxError) as c:
            self.client().account()
        self.assertEqual((c.exception.code, c.exception.http_status), ("server_error", 500))
        self.assertEqual(len(self.http.requests), 3)

    def test_429_retry_after_honoured_same_key(self):
        self.http.json(429, {"ok": False, "error": {"code": "rate_limited", "message": "Rate limit exceeded"}}, {"Retry-After": "3"}).json(201, placed())
        self.client().place_order(42, "356938035643809", idempotency_key="k-1")
        self.assertEqual(self.slept, [3.0])
        self.assertEqual(self.http.requests[1].header("Idempotency-Key"), "k-1")

    def test_429_without_retry_after_or_too_long(self):
        self.http.json(429, {"ok": False, "error": {"code": "limit", "message": "Daily API order limit reached for this account"}})
        with self.assertRaises(UnlyxError) as c:
            self.client().place_order(42)
        self.assertEqual(c.exception.code, "limit")
        self.http.json(429, {"ok": False, "error": {"code": "rate_limited", "message": "x"}}, {"Retry-After": "600"})
        with self.assertRaises(UnlyxError) as c:
            self.client().account()
        self.assertEqual(c.exception.retry_after, 600)
        self.assertEqual(self.slept, [])

    def test_max_retries_zero(self):
        self.http.fail(False)
        with self.assertRaises(UnlyxError):
            self.client(max_retries=0).account()
        self.assertEqual(len(self.http.requests), 1)

    # ---- errors

    def test_error_envelope(self):
        self.http.json(402, {"ok": False, "error": {"code": "low_balance", "message": "Insufficient Credit", "reference": "a1b2c3d4"}})
        with self.assertRaises(UnlyxError) as c:
            self.client().place_order(42, "356938035643809")
        e = c.exception
        self.assertEqual((e.code, e.http_status, e.message, e.reference), ("low_balance", 402, "Insufficient Credit", "a1b2c3d4"))
        self.assertEqual(e.body["error"]["code"], "low_balance")

    def test_redirect_never_followed(self):
        self.http.raw(301, b"", {"Location": "https://elsewhere.example/"})
        with self.assertRaises(UnlyxError) as c:
            self.client().account()
        self.assertEqual(c.exception.code, "unexpected_redirect")
        self.assertEqual(len(self.http.requests), 1)

    def test_non_json(self):
        self.http.raw(200, b"<html>maintenance</html>")
        with self.assertRaises(UnlyxError) as c:
            self.client().account()
        self.assertEqual(c.exception.code, "invalid_response")
        self.assertEqual(c.exception.raw_body, "<html>maintenance</html>")

    # ---- bulk, list, cancel, webhooks, usage

    def test_bulk(self):
        self.http.json(200, {"ok": True, "balance": "200.00", "data": [
            dict(order_row(), line=0, reference_in="a-1", ok=True),
            {"line": 1, "reference_in": "gen", "ok": False, "error": {"code": "invalid_input", "message": "IMEI must be 15 digits"}},
        ]})
        r = self.client(id_generator=lambda: "gen").place_orders([
            {"service": 42, "imei": "356938035643809", "reference": "a-1"}, {"service": 42, "imei": "123"}])
        sent = self.body()
        self.assertTrue(self.http.last.url.endswith("/api/v1/orders/bulk"))
        self.assertEqual([l["reference"] for l in sent["orders"]], ["a-1", "gen"])
        self.assertEqual(len(r.succeeded), 1)
        self.assertEqual(r.failed[0].error_code, "invalid_input")
        self.assertEqual(r.lines[0].order.reference, "UX-1001")
        self.assertEqual(r.balance, "200.00")

    def test_bulk_limits(self):
        with self.assertRaises(UnlyxError):
            self.client().place_orders([{"service": 1}] * 101)
        with self.assertRaises(UnlyxError):
            self.client().place_orders([])

    def test_orders_filters(self):
        self.http.json(200, {"ok": True, "page": 1, "per_page": 2, "total": 5, "data": [order_row(), order_row(reference="UX-1002")]})
        page = self.client().orders(status="completed", since=datetime(2026, 10, 1, tzinfo=timezone.utc), per_page=2)
        self.assertEqual(self.http.last.url, "https://yourshop.com/api/v1/orders?status=completed&since=2026-10-01T00%3A00%3A00%2B00%3A00&per_page=2")
        self.assertEqual(len(page.orders), 2)
        self.assertTrue(page.has_more)

    def test_cancel_encodes_reference(self):
        self.http.json(200, {"ok": True, "balance": "10.00", "data": order_row(status="cancelled", dhru_status=3)})
        o = self.client().cancel_order("UX 1/2")
        self.assertEqual(self.http.last.url, "https://yourshop.com/api/v1/orders/UX%201%2F2/cancel")
        self.assertIsNotNone(self.http.last.header("Idempotency-Key"))
        self.assertTrue(o.is_rejected)
        self.assertEqual(o.balance, "10.00")

    def test_webhooks_crud(self):
        self.http.json(201, {"ok": True, "data": {"id": 3, "name": "Main", "url": "https://panel.yourshop.com/hooks/unlyx",
                                                  "events": ["order.completed"], "active": True, "secret": "whsec_abcdef"}})
        self.http.json(200, {"ok": True, "data": [{"id": 3, "url": "https://panel.yourshop.com/hooks/unlyx", "events": ["order.completed"], "active": True}]})
        self.http.json(200, {"ok": True, "data": {"deleted": True}})
        c = self.client()
        w = c.create_webhook("https://panel.yourshop.com/hooks/unlyx", ["order.completed"], "Main")
        self.assertEqual(w.secret, "whsec_abcdef")
        self.assertEqual(self.body(), {"url": "https://panel.yourshop.com/hooks/unlyx", "events": ["order.completed"], "name": "Main"})
        self.assertEqual(c.webhooks()[0].id, 3)
        self.assertTrue(c.delete_webhook(3))
        self.assertEqual(self.http.last.method, "DELETE")
        self.assertTrue(self.http.last.url.endswith("/webhooks/3"))

    def test_usage(self):
        self.http.json(200, {"ok": True, "data": {"since": "2026-10-25T00:00:00+00:00", "calls": 120, "errors": 3,
                                                  "by_action": {"v1:orders.store": 40}, "recent": [], "rate_limit_per_min": 60}})
        u = self.client().usage(500)
        self.assertTrue(self.http.last.url.endswith("/usage?days=90"))
        self.assertEqual(u.calls, 120)
        self.assertEqual(u.by_action, {"v1:orders.store": 40})

    # ---- raw + signing

    def test_raw_request(self):
        self.http.json(200, {"ok": True, "data": [], "count": 0})
        env = self.client().request("get", "services", {"type": "imei"})
        self.assertEqual(env["count"], 0)
        self.assertTrue(self.http.last.url.endswith("/api/v1/services?type=imei"))

    def test_raw_request_refuses_bad_paths(self):
        for bad in ("https://elsewhere.example/x", "/../admin", "/orders?x=1"):
            with self.assertRaises(UnlyxError):
                self.client().request("GET", bad)
        self.assertEqual(self.http.requests, [])

    def test_request_signing(self):
        self.http.json(201, placed())
        self.client(signing_key_id="kid_1", signing_secret="sign-secret").place_order(42, "356938035643809", idempotency_key="k-9")
        r = self.http.last
        ts = r.header("X-Timestamp")
        self.assertEqual(r.header("X-Key-Id"), "kid_1")
        self.assertRegex(ts, r"^\d{10}$")
        expected = hmac.new(b"sign-secret", b"POST\n/api/v1/orders\n" + ts.encode() + b"\n" + r.body, hashlib.sha256).hexdigest()
        self.assertEqual(r.header("X-Signature"), expected)

    def test_no_signing_by_default(self):
        self.http.json(200, {"ok": True, "data": {"key": {}}})
        self.client().account()
        self.assertIsNone(self.http.last.header("X-Signature"))


if __name__ == "__main__":
    unittest.main()
