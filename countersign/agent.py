"""The Countersign agent: an explicit, inspectable state graph.

    ingest -> extract(LLM) -> verify(rules) -> decide(mandate engine) -> brief(LLM)
           -> [AUTO_PAY] execute(PayPal) -> reconcile(PayPal)
           -> [NEEDS_COUNTERSIGN] wait for human -> execute -> reconcile
           -> [BLOCK] stop

The LLM never sees PayPal credentials and has no tool that moves money. Only `execute`
calls PayPal, and only `decide` (deterministic) or a human approval can route there.
"""
from __future__ import annotations

import hashlib
from typing import Any, Optional

from . import policy, risk
from .models import Decision, Invoice, Mandate, Outcome, RiskReport
from .paypal_client import PayPalError, approve_link
from .store import Store, now

DEFAULT_POLICY = (
    "Auto-approve bills up to $150. Monthly budget of $400 for design and budget of $300 for hosting. "
    "Overall total spend $1500 per month. Only pay in USD. Always ask me before paying a new vendor. "
    "Never buy gift cards or crypto. Block anything over $2000."
)

SEED_VENDORS = [
    ("Priya Sharma Design", "priya.design@example.com", "design", 120.0),
    ("Arjun Writes", "arjun.writes@example.com", "content", 90.0),
    ("CloudNest Hosting", "billing@cloudnest.example.com", "hosting", 40.0),
    ("Inkwell Print Shop", "orders@inkwell.example.com", "office", 60.0),
]


def idempotency_key(inv: Invoice, case_id: int) -> str:
    basis = f"{inv.vendor_name.lower()}|{inv.invoice_number or f'case{case_id}'}|{inv.amount:.2f}|{inv.currency}"
    return "CS-" + hashlib.sha256(basis.encode()).hexdigest()[:24].upper()


class Agent:
    def __init__(self, store: Store, llm, paypal, public_base_url: str):
        self.store, self.llm, self.paypal, self.base = store, llm, paypal, public_base_url.rstrip("/")

    # ---------------------------------------------------------------- setup
    def ensure_seed(self) -> None:
        if not self.store.vendors():
            for v in SEED_VENDORS:
                self.store.upsert_vendor(*v)
        if not self.store.get_mandate():
            self.compile_mandate(DEFAULT_POLICY, actor="system")

    def compile_mandate(self, policy_text: str, actor: str = "owner") -> dict:
        m = self.llm.compile_mandate(policy_text)
        by = self.llm.name + (" (fallback: mock)" if getattr(self.llm, "last_fallback", None) else "")
        self.store.set_mandate(policy_text, m.model_dump(), by)
        self.store.log(None, actor, "mandate_compiled", f"Mandate compiled by {by}: {m.model_dump_json()}")
        return self.store.get_mandate()

    def mandate(self) -> Mandate:
        return Mandate.model_validate(self.store.get_mandate()["rules"])

    # ---------------------------------------------------------------- graph
    def _trace(self, case_id: int, node: str, summary: str, data: Any = None) -> None:
        c = self.store.get_case(case_id)
        t = c["trace"] or []
        t.append({"node": node, "at": now(), "summary": summary, "data": data})
        self.store.update_case(case_id, trace=t)

    def process(self, raw_text: str, write_brief: bool = True) -> dict:
        cid = self.store.create_case(raw_text)
        self.store.log(cid, "inbox", "received", raw_text.strip().splitlines()[0][:120] if raw_text.strip() else "(empty)")
        self._trace(cid, "ingest", f"Received {len(raw_text)} characters of untrusted text.")

        # extract (LLM)
        inv = self.llm.extract_invoice(raw_text)
        fb = getattr(self.llm, "last_fallback", None)
        self._trace(cid, "extract", f"{self.llm.name}{' -> mock fallback' if fb else ''} extracted "
                    f"{inv.vendor_name}, {inv.amount:.2f} {inv.currency}, rail={inv.rail}.", inv.model_dump())
        self.store.update_case(cid, invoice=inv.model_dump(), status="extracted")

        # verify (deterministic)
        vendor = self.store.find_vendor(inv.vendor_name)
        dup = self.store.is_duplicate(inv.vendor_name, inv.invoice_number, cid)
        mandate = self.mandate()
        rep = risk.assess(raw_text, inv, vendor, dup,
                          known_vendors=[v["name"] for v in self.store.vendors()],
                          recent_vendor_total=self.store.recent_vendor_total(inv.vendor_name, cid),
                          auto_limit=mandate.auto_approve_max)
        self._trace(cid, "verify", f"Risk {rep.score}/100 ({rep.level}); vendor {'on file' if vendor else 'not on file'}.",
                    rep.model_dump())
        self.store.update_case(cid, risk=rep.model_dump())

        # decide (mandate engine, deterministic)
        dec = policy.evaluate(inv, self.mandate(), vendor, self.store.spent_this_month(), rep)
        self._trace(cid, "decide", f"Mandate engine: {dec.outcome.value}.", dec.model_dump())

        # brief (LLM explains, never decides)
        if write_brief:
            brief = self.llm.explain(inv, dec, rep)
            self._trace(cid, "brief", "Wrote plain-language brief for the owner.")
        else:
            brief = f"{dec.outcome.value}: " + "; ".join(k.detail for k in dec.checks if not k.passed)[:400]
        self.store.update_case(cid, decision=dec.model_dump(), brief=brief)
        self.store.log(cid, "agent", "decision", f"{dec.outcome.value}: {brief}")

        if dec.outcome == Outcome.BLOCK:
            self.store.update_case(cid, status="blocked")
        elif dec.outcome == Outcome.NEEDS_COUNTERSIGN:
            self.store.update_case(cid, status="awaiting_countersign")
        else:
            self._execute(cid, inv, actor="agent (within mandate)")
        return self.store.get_case(cid)

    def countersign(self, case_id: int, approve: bool, approver: str = "owner", note: str = "") -> dict:
        c = self.store.get_case(case_id)
        if not c:
            raise KeyError(case_id)
        if c["status"] != "awaiting_countersign":
            raise ValueError(f"Case {case_id} is '{c['status']}', not awaiting countersign.")
        if not approve:
            self.store.update_case(case_id, status="rejected")
            self._trace(case_id, "countersign", f"Rejected by {approver}. {note}".strip())
            self.store.log(case_id, approver, "rejected", note or "Rejected")
            return self.store.get_case(case_id)
        self._trace(case_id, "countersign", f"Approved by {approver}. {note}".strip())
        self.store.log(case_id, approver, "countersigned", note or "Approved")
        return self._execute(case_id, Invoice.model_validate(c["invoice"]), actor=approver)

    # ---------------------------------------------------------------- PayPal
    def _execute(self, cid: int, inv: Invoice, actor: str) -> dict:
        key = idempotency_key(inv, cid)
        try:
            if inv.rail == "payout":
                res = self.paypal.create_payout(
                    sender_batch_id=key, receiver=inv.payee_email, amount=inv.amount, currency=inv.currency,
                    note=f"{inv.vendor_name} invoice {inv.invoice_number or ''} via Countersign".strip(),
                    item_id=f"case-{cid}")
                bid = res["batch_header"]["payout_batch_id"]
                pp = {"rail": "payout", "idempotency_key": key, "payout_batch_id": bid,
                      "batch_status": res["batch_header"]["batch_status"]}
                self.store.update_case(cid, paypal=pp, status="payout_pending", paid_amount=inv.amount, paid_at=now())
                self._trace(cid, "execute", f"PayPal Payouts batch {bid} created ({pp['batch_status']}).", pp)
                self.store.log(cid, actor, "payout_created", f"{inv.amount:.2f} {inv.currency} to {inv.payee_email}, batch {bid}")
                return self.reconcile(cid)
            res = self.paypal.create_order(
                reference_id=f"case-{cid}", amount=inv.amount, currency=inv.currency,
                description=inv.description or inv.vendor_name,
                return_url=f"{self.base}/paypal/return?case={cid}", cancel_url=f"{self.base}/paypal/cancel?case={cid}",
                request_id=key)
            pp = {"rail": "checkout", "idempotency_key": key, "order_id": res["id"],
                  "order_status": res["status"], "approve_url": approve_link(res)}
            self.store.update_case(cid, paypal=pp, status="awaiting_buyer_approval")
            self._trace(cid, "execute", f"PayPal order {res['id']} created; buyer approval required on PayPal.", pp)
            self.store.log(cid, actor, "order_created", f"Order {res['id']} for {inv.amount:.2f} {inv.currency}")
        except PayPalError as e:
            self.store.update_case(cid, status="failed", paypal={"error": str(e)})
            self._trace(cid, "execute", f"PayPal error: {e}")
            self.store.log(cid, "paypal", "error", str(e))
        return self.store.get_case(cid)

    def reconcile(self, cid: int) -> dict:
        c = self.store.get_case(cid)
        pp = dict(c.get("paypal") or {})
        if pp.get("rail") == "payout" and pp.get("payout_batch_id"):
            res = self.paypal.get_payout(pp["payout_batch_id"])
            status = res["batch_header"]["batch_status"]
            pp["batch_status"] = status
            items = res.get("items") or []
            if items:
                pp["transaction_status"] = items[0].get("transaction_status")
                pp["transaction_id"] = items[0].get("transaction_id")
            new = "paid" if status == "SUCCESS" else "failed" if status in ("DENIED", "CANCELED") else "payout_pending"
            self.store.update_case(cid, paypal=pp, status=new)
            self._trace(cid, "reconcile", f"Payout batch status {status}.", {"batch_status": status})
        elif pp.get("rail") == "checkout" and pp.get("order_id"):
            o = self.paypal.get_order(pp["order_id"])
            pp["order_status"] = o["status"]
            self.store.update_case(cid, paypal=pp)
        return self.store.get_case(cid)

    def capture(self, cid: int) -> dict:
        c = self.store.get_case(cid)
        pp = dict(c.get("paypal") or {})
        if c["status"] != "awaiting_buyer_approval" or not pp.get("order_id"):
            raise ValueError(f"Case {cid} has no order awaiting capture.")
        try:
            o = self.paypal.capture_order(pp["order_id"], request_id=pp["idempotency_key"] + "-CAP")
        except PayPalError as e:
            self._trace(cid, "capture", f"Capture refused by PayPal: {e}")
            raise
        cap = o["purchase_units"][0]["payments"]["captures"][0]
        pp.update(order_status=o["status"], capture_id=cap["id"], capture_status=cap["status"])
        amt = float(cap["amount"]["value"])
        self.store.update_case(cid, paypal=pp, status="paid" if o["status"] == "COMPLETED" else "failed",
                               paid_amount=amt, paid_at=now())
        self._trace(cid, "capture", f"Captured order {pp['order_id']} ({cap['status']}).", pp)
        self.store.log(cid, "buyer+agent", "order_captured", f"Capture {cap['id']} {amt:.2f}")
        return self.store.get_case(cid)
