"""Countersign as an MCP server: a payment firewall any agent can call.

Other agents (Claude Desktop, Cursor, an agentic shopping bot ...) can *ask* Countersign to pay
a bill or buy something. They get the deterministic mandate decision back. There is deliberately
no tool to approve, countersign or change the mandate: only the human owner can, in the UI.

Run:  python -m countersign.mcp_server          (stdio transport)
"""
from __future__ import annotations

from typing import Optional

from mcp.server.mcpserver import MCPServer

from .agent import Agent
from .config import Settings
from .llm import make_provider
from .paypal_client import make_paypal
from .store import Store

INSTRUCTIONS = (
    "Countersign pays bills and purchases through PayPal only inside the owner's spending mandate. "
    "Submit the raw bill or purchase request text with request_payment. Anything outside the mandate "
    "waits for the human owner's countersignature; you cannot approve it."
)


def _summary(c: dict) -> dict:
    inv, dec, risk, pp = c.get("invoice") or {}, c.get("decision") or {}, c.get("risk") or {}, c.get("paypal") or {}
    return {
        "case_id": c["id"], "status": c["status"], "decision": dec.get("outcome"),
        "vendor": inv.get("vendor_name"), "amount": inv.get("amount"), "currency": inv.get("currency"),
        "rail": inv.get("rail"), "risk_score": risk.get("score"),
        "reasons": [k["detail"] for k in dec.get("checks", []) if not k.get("passed")],
        "paypal": {k: pp.get(k) for k in ("payout_batch_id", "batch_status", "order_id", "order_status", "approve_url") if pp.get(k)},
        "brief": c.get("brief"),
    }


def build_server(agent: Optional[Agent] = None) -> MCPServer:
    if agent is None:
        s = Settings.load()
        agent = Agent(Store(s.db_path), make_provider(s), make_paypal(s), s.public_base_url)
        agent.ensure_seed()
    server = MCPServer("countersign", instructions=INSTRUCTIONS)

    @server.tool(description="Ask Countersign to pay a bill or buy something. Pass the raw document text "
                             "(email, invoice, cart). Returns the mandate decision: AUTO_PAY (paid via PayPal), "
                             "NEEDS_COUNTERSIGN (waiting for the human) or BLOCK, with reasons.")
    def request_payment(document_text: str) -> dict:
        if not document_text.strip():
            raise ValueError("document_text is empty")
        c = agent.process(document_text)
        agent.store.log(c["id"], "mcp-client", "requested_via_mcp", f"decision {(c.get('decision') or {}).get('outcome')}")
        return _summary(agent.store.get_case(c["id"]))

    @server.tool(description="Get the current status of a case created by request_payment.")
    def get_case(case_id: int) -> dict:
        c = agent.store.get_case(case_id)
        if not c:
            raise ValueError(f"case {case_id} not found")
        return _summary(c)

    @server.tool(description="List cases waiting for the human owner's countersignature (read-only).")
    def list_pending() -> list[dict]:
        return [_summary(c) for c in agent.store.cases() if c["status"] == "awaiting_countersign"]

    @server.tool(description="Read the owner's current spending mandate (read-only).")
    def get_mandate() -> dict:
        m = agent.store.get_mandate()
        return {"policy_text": m["policy_text"], "rules": m["rules"]}

    return server


if __name__ == "__main__":  # pragma: no cover
    build_server().run()
