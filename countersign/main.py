"""FastAPI app: JSON API + the single-page UI + PayPal return/webhook endpoints."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .agent import Agent
from .config import Settings
from .llm import make_provider
from .paypal_client import MockPayPalClient, PayPalError, make_paypal
from .store import Store

HERE = Path(__file__).parent
SAMPLES = HERE.parent / "samples" / "inbox.json"


class ProcessIn(BaseModel):
    text: str


class PolicyIn(BaseModel):
    policy_text: str


class CountersignIn(BaseModel):
    approve: bool
    approver: str = "owner"
    note: str = ""


class VendorIn(BaseModel):
    name: str
    payee_email: str
    category: str = "general"
    avg_amount: float = 0


def create_app(settings: Optional[Settings] = None, llm=None, paypal=None) -> FastAPI:
    s = settings or Settings.load()
    store = Store(s.db_path)
    agent = Agent(store, llm or make_provider(s), paypal or make_paypal(s), s.public_base_url)
    agent.ensure_seed()

    app = FastAPI(title="Countersign", version="0.1.0",
                  description="Mandate-guarded AI accounts-payable agent on PayPal (sandbox).")
    app.state.agent, app.state.settings = agent, s
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(HERE / "static" / "index.html")

    @app.get("/api/health")
    def health():
        return {"ok": True, "llm": agent.llm.name, "llm_model": getattr(agent.llm, "model", None),
                "paypal": agent.paypal.mode}

    @app.get("/api/samples")
    def samples():
        return json.loads(SAMPLES.read_text())

    @app.get("/api/mandate")
    def get_mandate():
        return store.get_mandate()

    @app.post("/api/mandate")
    def set_mandate(body: PolicyIn):
        if not body.policy_text.strip():
            raise HTTPException(400, "policy_text is empty")
        return agent.compile_mandate(body.policy_text)

    @app.get("/api/vendors")
    def vendors():
        return store.vendors()

    @app.post("/api/vendors")
    def add_vendor(v: VendorIn):
        store.upsert_vendor(v.name, v.payee_email, v.category, v.avg_amount)
        store.log(None, "owner", "vendor_saved", f"{v.name} <{v.payee_email}>")
        return store.find_vendor(v.name)

    @app.post("/api/cases")
    def process(body: ProcessIn):
        if not body.text.strip():
            raise HTTPException(400, "text is empty")
        return agent.process(body.text)

    @app.get("/api/cases")
    def cases():
        return store.cases()

    @app.get("/api/cases/{cid}")
    def case(cid: int):
        c = store.get_case(cid)
        if not c:
            raise HTTPException(404, "case not found")
        return c

    @app.post("/api/cases/{cid}/countersign")
    def countersign(cid: int, body: CountersignIn):
        try:
            return agent.countersign(cid, body.approve, body.approver, body.note)
        except KeyError:
            raise HTTPException(404, "case not found")
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.post("/api/cases/{cid}/reconcile")
    def reconcile(cid: int):
        if not store.get_case(cid):
            raise HTTPException(404, "case not found")
        return agent.reconcile(cid)

    @app.post("/api/cases/{cid}/capture")
    def capture(cid: int):
        try:
            return agent.capture(cid)
        except ValueError as e:
            raise HTTPException(409, str(e))
        except PayPalError as e:
            raise HTTPException(422, str(e))

    @app.post("/api/lab/run")
    def lab_run():
        from .redteam import run_lab
        rep = run_lab(agent)
        sm = rep["summary"]
        store.log(None, "attack-lab", "lab_run",
                  f"{sm['attacks_stopped']}/{sm['attacks']} attacks stopped, {sm['controls_auto_paid']}/{sm['controls']} controls paid")
        return rep

    @app.get("/api/lab/corpus")
    def lab_corpus():
        from .redteam import CORPUS
        return [{"id": c["id"], "technique": c["technique"], "control": bool(c.get("control")), "text": c["text"]} for c in CORPUS]

    @app.get("/api/ledger")
    def ledger():
        m = store.get_mandate()["rules"]
        spent = store.spent_this_month()
        budgets = [{"category": k, "cap": v, "spent": round(spent.get(k, 0.0), 2)}
                   for k, v in m["monthly_budgets"].items()]
        return {"spent": spent, "budgets": budgets, "events": store.events(60)}

    # PayPal sends the buyer back here after they approve an order.
    @app.get("/paypal/return", include_in_schema=False)
    def paypal_return(case: int, token: str = ""):
        try:
            agent.capture(case)
        except (ValueError, PayPalError) as e:
            store.log(case, "paypal", "return_error", str(e))
        return RedirectResponse(f"/?case={case}")

    @app.get("/paypal/cancel", include_in_schema=False)
    def paypal_cancel(case: int):
        store.log(case, "buyer", "checkout_cancelled", "Buyer cancelled on PayPal")
        return RedirectResponse(f"/?case={case}")

    @app.post("/api/webhooks/paypal")
    async def webhook(request: Request):
        event = await request.json()
        ok = agent.paypal.verify_webhook(dict(request.headers), event, s.paypal_webhook_id)
        if not ok:
            store.log(None, "paypal", "webhook_rejected", event.get("event_type", "?"))
            raise HTTPException(400, "signature verification failed")
        etype = event.get("event_type", "")
        res = event.get("resource", {})
        store.log(None, "paypal", "webhook", etype)
        batch = res.get("payout_batch_id") or (res.get("batch_header") or {}).get("payout_batch_id")
        if etype.startswith("PAYMENT.PAYOUTS") and batch:
            for c in store.cases():
                if (c.get("paypal") or {}).get("payout_batch_id") == batch:
                    agent.reconcile(c["id"])
        return {"received": etype}

    # Stand-in for PayPal's buyer approval page; only exists in mock mode.
    if isinstance(agent.paypal, MockPayPalClient):
        @app.get("/mock-paypal/checkout/{oid}", include_in_schema=False, response_class=HTMLResponse)
        def mock_checkout(oid: str):
            o = agent.paypal.orders.get(oid)
            if not o:
                raise HTTPException(404)
            amt = o["purchase_units"][0]["amount"]
            html = (HERE / "static" / "mock_checkout.html").read_text()
            return (html.replace("{{OID}}", oid)
                        .replace("{{AMOUNT}}", f"{amt['value']} {amt['currency_code']}")
                        .replace("{{DESC}}", o["purchase_units"][0].get("description", "")))

        @app.post("/mock-paypal/checkout/{oid}/approve", include_in_schema=False)
        def mock_approve(oid: str):
            if oid not in agent.paypal.orders:
                raise HTTPException(404)
            return RedirectResponse(agent.paypal.approve(oid), status_code=303)

    return app


app = create_app()
