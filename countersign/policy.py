"""The mandate engine: pure, deterministic, no LLM. This is the only thing that decides."""
from __future__ import annotations

from typing import Optional

from .models import Decision, Invoice, Mandate, Outcome, RiskReport, RuleCheck


def evaluate(inv: Invoice, m: Mandate, vendor: Optional[dict], spent: dict[str, float], risk: RiskReport) -> Decision:
    """`spent` maps category -> amount already paid this month, plus "*" for the total."""
    c: list[RuleCheck] = []

    def add(rule: str, ok: bool, effect: str, ok_txt: str, bad_txt: str) -> None:
        c.append(RuleCheck(rule=rule, passed=ok, effect="ok" if ok else effect, detail=ok_txt if ok else bad_txt))

    add("currency", inv.currency in m.allowed_currencies, "block",
        f"{inv.currency} is an allowed currency.", f"{inv.currency} is not in your allowed currencies ({', '.join(m.allowed_currencies)}).")
    add("blocked_category", inv.category not in m.blocked_categories, "block",
        f"Category '{inv.category}' is permitted.", f"Your mandate never allows '{inv.category}' purchases.")
    add("hard_ceiling", inv.amount <= m.hard_block_above, "block",
        f"Under your hard ceiling of {m.hard_block_above:.2f}.", f"{inv.amount:.2f} exceeds your hard ceiling of {m.hard_block_above:.2f}.")
    dup = any(s.code == "duplicate_invoice" for s in risk.signals)
    add("not_duplicate", not dup, "block",
        "Not a duplicate of a processed invoice.", f"Invoice {inv.invoice_number} was already processed; paying again would double-pay.")
    add("risk", risk.level != "high", "block",
        f"Risk score {risk.score}/100 ({risk.level}).", f"Risk score {risk.score}/100 is high; payment blocked.")
    add("auto_limit", inv.amount <= m.auto_approve_max, "countersign",
        f"Within your auto-pay limit of {m.auto_approve_max:.2f}.", f"{inv.amount:.2f} is above your auto-pay limit of {m.auto_approve_max:.2f}.")
    if m.new_vendor_requires_approval:
        add("known_vendor", vendor is not None, "countersign",
            f"{inv.vendor_name} is a known vendor.", f"{inv.vendor_name} is a new vendor; your mandate asks you to approve new vendors.")
    if m.payee_change_requires_approval and vendor:
        same = not inv.payee_email or inv.payee_email == (vendor.get("payee_email") or "").lower()
        add("payee_on_file", same, "countersign",
            "Payee matches the address on file.", "Payee differs from the address on file.")
    add("risk_medium", risk.level == "low", "countersign",
        "No notable risk signals.", f"Risk signals present (score {risk.score}/100).")
    for key in (inv.category, "*"):
        cap = m.monthly_budgets.get(key)
        if cap is None:
            continue
        used = spent.get(key, 0.0)
        label = "overall" if key == "*" else f"'{key}'"
        add(f"budget:{key}", used + inv.amount <= cap, "countersign",
            f"{label} budget: {used + inv.amount:.2f} of {cap:.2f} after this payment.",
            f"Would take the {label} monthly budget to {used + inv.amount:.2f}, over its {cap:.2f} cap.")

    if any(x.effect == "block" for x in c):
        out = Outcome.BLOCK
    elif any(x.effect == "countersign" for x in c):
        out = Outcome.NEEDS_COUNTERSIGN
    else:
        out = Outcome.AUTO_PAY
    return Decision(outcome=out, checks=c)
