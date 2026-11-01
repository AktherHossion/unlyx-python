import hashlib
import hmac
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unlyx import UnlyxError, Webhook, is_valid_webhook, signature_header, verify_webhook  # noqa: E402

SECRET = "whsec_test_0123456789"
BODY = b'{"event":"order.completed","tenant_id":1,"created_at":"2026-11-01T10:00:00+00:00","data":{"reference":"UX-1001","status":"completed","reply":"CODE: 12345678"}}'
TS = 1793000000


def server_header(secret, ts, body):
    """Same formula as the server (WebhookDispatcher::timedSignature)."""
    return "t=%d,v1=%s" % (ts, hmac.new(secret.encode(), str(ts).encode() + b"." + body, hashlib.sha256).hexdigest())


class WebhookTest(unittest.TestCase):
    def test_valid_delivery(self):
        event = verify_webhook(BODY, {"x-unlyx-signature": server_header(SECRET, TS, BODY)}, SECRET, now=TS + 10)
        self.assertEqual(event["event"], "order.completed")
        self.assertEqual(event["data"]["reference"], "UX-1001")

    def test_str_payload_and_header_shapes(self):
        h = server_header(SECRET, TS, BODY)
        self.assertTrue(is_valid_webhook(BODY.decode(), {"X-Unlyx-Signature": [h]}, SECRET, now=TS))
        self.assertTrue(is_valid_webhook(BODY, {"HTTP_X_UNLYX_SIGNATURE": h}, SECRET, now=TS))
        self.assertTrue(is_valid_webhook(BODY, [("X-Unlyx-Signature", h)], SECRET, now=TS))
        self.assertTrue(Webhook.is_valid(BODY, {"X-Unlyx-Signature": h}, SECRET, now=TS))

    def test_tampered_body(self):
        h = server_header(SECRET, TS, BODY)
        with self.assertRaises(UnlyxError) as c:
            verify_webhook(BODY.replace(b"12345678", b"00000000"), {"X-Unlyx-Signature": h}, SECRET, now=TS)
        self.assertEqual(c.exception.code, "invalid_signature")

    def test_wrong_secret(self):
        self.assertFalse(is_valid_webhook(BODY, {"X-Unlyx-Signature": server_header("whsec_other", TS, BODY)}, SECRET, now=TS))

    def test_old_timestamp(self):
        h = server_header(SECRET, TS, BODY)
        self.assertFalse(is_valid_webhook(BODY, {"X-Unlyx-Signature": h}, SECRET, now=TS + 301))
        self.assertTrue(is_valid_webhook(BODY, {"X-Unlyx-Signature": h}, SECRET, tolerance=0, now=TS + 99999))

    def test_missing_or_malformed(self):
        self.assertFalse(is_valid_webhook(BODY, {}, SECRET))
        self.assertFalse(is_valid_webhook(BODY, {"X-Unlyx-Signature": "garbage"}, SECRET))
        self.assertFalse(is_valid_webhook(BODY, {"X-Unlyx-Signature": "v1=abc"}, SECRET))

    def test_rotation(self):
        h = server_header(SECRET, TS, BODY)
        self.assertTrue(is_valid_webhook(BODY, {"X-Unlyx-Signature": h}, ["whsec_new", SECRET], now=TS))

    def test_signature_header_matches_server(self):
        self.assertEqual(signature_header(SECRET, TS, BODY), server_header(SECRET, TS, BODY))


if __name__ == "__main__":
    unittest.main()
