"""PayPal REST integration (sandbox) plus a faithful in-memory mock.

Endpoints used (all on https://api-m.sandbox.paypal.com):
  POST /v1/oauth2/token                              client-credentials access token
  POST /v1/payments/payouts                          pay a vendor's PayPal email (Payouts)
  GET  /v1/payments/payouts/{payout_batch_id}        reconcile payout status
  POST /v2/checkout/orders                           create an order for a merchant purchase
  GET  /v2/checkout/orders/{id}                      read order status
  POST /v2/checkout/orders/{id}/capture              capture after the buyer approves on PayPal
  POST /v1/notifications/verify-webhook-signature    verify incoming webhooks

Both clients expose the same methods and return PayPal-shaped JSON, so the agent code
does not know which one it is talking to.
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Optional

import httpx


class PayPalError(RuntimeError):
    def __init__(self, status: int, body: Any):
        super().__init__(f"PayPal API error {status}: {body}")
        self.status, self.body = status, body


def _money(v: float) -> str:
    return f"{v:.2f}"


def payout_body(sender_batch_id: str, receiver: str, amount: float, currency: str, note: str, item_id: str) -> dict:
    return {
        "sender_batch_header": {
            "sender_batch_id": sender_batch_id,
            "email_subject": "You have a payment",
            "email_message": note[:255],
        },
        "items": [{
            "recipient_type": "EMAIL",
            "amount": {"value": _money(amount), "currency": currency},
            "receiver": receiver,
            "note": note[:4000],
            "sender_item_id": item_id[:63],
        }],
    }


def order_body(reference_id: str, amount: float, currency: str, description: str,
               return_url: str, cancel_url: str) -> dict:
    return {
        "intent": "CAPTURE",
        "purchase_units": [{
            "reference_id": reference_id[:256],
            "description": description[:127],
            "amount": {"currency_code": currency, "value": _money(amount)},
        }],
        "payment_source": {"paypal": {"experience_context": {
            "brand_name": "Countersign",
            "user_action": "PAY_NOW",
            "shipping_preference": "NO_SHIPPING",
            "return_url": return_url,
            "cancel_url": cancel_url,
        }}},
    }


def approve_link(order: dict) -> Optional[str]:
    for link in order.get("links", []):
        if link.get("rel") in ("payer-action", "approve"):
            return link["href"]
    return None


class PayPalSandboxClient:
    mode = "sandbox"

    def __init__(self, client_id: str, client_secret: str, base_url: str,
                 http: Optional[httpx.Client] = None):
        if "sandbox" not in base_url and "localhost" not in base_url and "testserver" not in base_url:
            raise ValueError("Countersign only talks to the PayPal sandbox.")
        self.client_id, self.client_secret, self.base = client_id, client_secret, base_url.rstrip("/")
        self.http = http or httpx.Client(timeout=30)
        self._token: Optional[str] = None
        self._exp = 0.0

    # -- auth
    def token(self) -> str:
        if self._token and time.time() < self._exp - 60:
            return self._token
        r = self.http.post(f"{self.base}/v1/oauth2/token", auth=(self.client_id, self.client_secret),
                           data={"grant_type": "client_credentials"},
                           headers={"Accept": "application/json"})
        if r.status_code != 200:
            raise PayPalError(r.status_code, r.text)
        d = r.json()
        self._token, self._exp = d["access_token"], time.time() + int(d.get("expires_in", 300))
        return self._token

    def _req(self, method: str, path: str, json: Any = None, request_id: Optional[str] = None) -> dict:
        h = {"Authorization": f"Bearer {self.token()}", "Content-Type": "application/json",
             "Prefer": "return=representation"}
        if request_id:
            h["PayPal-Request-Id"] = request_id
        r = self.http.request(method, f"{self.base}{path}", json=json, headers=h)
        if r.status_code >= 300:
            try:
                body = r.json()
            except Exception:  # noqa: BLE001
                body = r.text
            raise PayPalError(r.status_code, body)
        return r.json() if r.content else {}

    # -- payouts
    def create_payout(self, sender_batch_id: str, receiver: str, amount: float, currency: str,
                      note: str, item_id: str) -> dict:
        return self._req("POST", "/v1/payments/payouts",
                         payout_body(sender_batch_id, receiver, amount, currency, note, item_id),
                         request_id=sender_batch_id)

    def get_payout(self, batch_id: str) -> dict:
        return self._req("GET", f"/v1/payments/payouts/{batch_id}")

    # -- orders
    def create_order(self, reference_id: str, amount: float, currency: str, description: str,
                     return_url: str, cancel_url: str, request_id: str) -> dict:
        return self._req("POST", "/v2/checkout/orders",
                         order_body(reference_id, amount, currency, description, return_url, cancel_url),
                         request_id=request_id)

    def get_order(self, order_id: str) -> dict:
        return self._req("GET", f"/v2/checkout/orders/{order_id}")

    def capture_order(self, order_id: str, request_id: str) -> dict:
        return self._req("POST", f"/v2/checkout/orders/{order_id}/capture", json={}, request_id=request_id)

    # -- webhooks
    def verify_webhook(self, headers: dict[str, str], event: dict, webhook_id: str) -> bool:
        h = {k.lower(): v for k, v in headers.items()}
        body = {
            "auth_algo": h.get("paypal-auth-algo"),
            "cert_url": h.get("paypal-cert-url"),
            "transmission_id": h.get("paypal-transmission-id"),
            "transmission_sig": h.get("paypal-transmission-sig"),
            "transmission_time": h.get("paypal-transmission-time"),
            "webhook_id": webhook_id,
            "webhook_event": event,
        }
        return self._req("POST", "/v1/notifications/verify-webhook-signature", body).get("verification_status") == "SUCCESS"


class MockPayPalClient:
    """In-memory PayPal with the same response shapes and state machine as the sandbox.

    Payout batches go PENDING -> SUCCESS on the first status read. Orders go
    CREATED -> APPROVED (via the mock approval page) -> COMPLETED on capture.
    Duplicate sender_batch_id is rejected exactly like PayPal does.
    """

    mode = "mock"

    def __init__(self, public_base_url: str = "http://localhost:8000"):
        self.base = public_base_url.rstrip("/")
        self.payouts: dict[str, dict] = {}
        self.batch_ids: set[str] = set()
        self.orders: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []

    def token(self) -> str:
        return "MOCK-ACCESS-TOKEN"

    def create_payout(self, sender_batch_id, receiver, amount, currency, note, item_id) -> dict:
        self.calls.append(("POST", "/v1/payments/payouts"))
        body = payout_body(sender_batch_id, receiver, amount, currency, note, item_id)
        if sender_batch_id in self.batch_ids:
            raise PayPalError(400, {"name": "USER_BUSINESS_ERROR", "details": [{"issue": "DUPLICATE_SENDER_BATCH_ID"}]})
        self.batch_ids.add(sender_batch_id)
        bid = "MOCKBATCH" + uuid.uuid4().hex[:10].upper()
        rec = {
            "batch_header": {"payout_batch_id": bid, "batch_status": "PENDING",
                             "sender_batch_header": body["sender_batch_header"]},
            "items": [{"payout_item_id": "MOCKITEM" + uuid.uuid4().hex[:8].upper(),
                       "transaction_status": "PENDING", "payout_item": body["items"][0]}],
            "links": [{"href": f"https://api-m.sandbox.paypal.com/v1/payments/payouts/{bid}", "rel": "self", "method": "GET"}],
        }
        self.payouts[bid] = rec
        return {"batch_header": {k: rec["batch_header"][k] for k in ("payout_batch_id", "batch_status", "sender_batch_header")},
                "links": rec["links"]}

    def get_payout(self, batch_id) -> dict:
        self.calls.append(("GET", f"/v1/payments/payouts/{batch_id}"))
        rec = self.payouts[batch_id]
        rec["batch_header"]["batch_status"] = "SUCCESS"
        for it in rec["items"]:
            it["transaction_status"] = "SUCCESS"
            it.setdefault("transaction_id", "MOCKTXN" + uuid.uuid4().hex[:10].upper())
        return rec

    def create_order(self, reference_id, amount, currency, description, return_url, cancel_url, request_id) -> dict:
        self.calls.append(("POST", "/v2/checkout/orders"))
        oid = "MOCKORDER" + uuid.uuid4().hex[:8].upper()
        body = order_body(reference_id, amount, currency, description, return_url, cancel_url)
        self.orders[oid] = {"id": oid, "status": "PAYER_ACTION_REQUIRED", "intent": "CAPTURE",
                            "purchase_units": body["purchase_units"],
                            "_return_url": return_url, "_cancel_url": cancel_url,
                            "links": [
                                {"href": f"https://api-m.sandbox.paypal.com/v2/checkout/orders/{oid}", "rel": "self", "method": "GET"},
                                {"href": f"{self.base}/mock-paypal/checkout/{oid}", "rel": "payer-action", "method": "GET"},
                            ]}
        return self._public(oid)

    def _public(self, oid: str) -> dict:
        return {k: v for k, v in self.orders[oid].items() if not k.startswith("_")}

    def approve(self, oid: str) -> str:
        """Simulates the buyer clicking 'Pay Now' on PayPal. Returns the redirect URL."""
        o = self.orders[oid]
        o["status"] = "APPROVED"
        return f"{o['_return_url']}{'&' if '?' in o['_return_url'] else '?'}token={oid}"

    def get_order(self, order_id) -> dict:
        self.calls.append(("GET", f"/v2/checkout/orders/{order_id}"))
        return self._public(order_id)

    def capture_order(self, order_id, request_id) -> dict:
        self.calls.append(("POST", f"/v2/checkout/orders/{order_id}/capture"))
        o = self.orders[order_id]
        if o["status"] != "APPROVED":
            raise PayPalError(422, {"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "ORDER_NOT_APPROVED"}]})
        o["status"] = "COMPLETED"
        amt = o["purchase_units"][0]["amount"]
        o["purchase_units"][0]["payments"] = {"captures": [{
            "id": "MOCKCAP" + uuid.uuid4().hex[:8].upper(), "status": "COMPLETED",
            "amount": {"currency_code": amt["currency_code"], "value": amt["value"]}}]}
        return self._public(order_id)

    def verify_webhook(self, headers, event, webhook_id) -> bool:
        return headers.get("paypal-transmission-sig") == "mock-valid"


def make_paypal(settings):
    if settings.paypal_mode == "sandbox":
        return PayPalSandboxClient(settings.paypal_client_id, settings.paypal_client_secret, settings.paypal_base_url)
    return MockPayPalClient(settings.public_base_url)
