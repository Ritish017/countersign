"""Deterministic fraud / social-engineering / prompt-injection signals.

These run on the raw document and the extracted invoice. They never call an LLM, so an
attacker cannot talk their way past them.
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Optional

from .models import Invoice, RiskReport, RiskSignal

INJECTION = [
    r"ignore (?:all |any |the )?(?:previous|prior|above) (?:instructions|rules)",
    r"disregard (?:your|the) (?:rules|policy|instructions|mandate)",
    r"\b(?:ai|assistant|agent|llm)\b[^.\n]{0,40}\b(?:must|should|is authori[sz]ed to)\b[^.\n]{0,40}\b(?:approve|pay)",
    r"(?:^|\n)\s*(?:system|assistant)\s*:",
    r"auto[- ]?approve this",
    r"mark (?:this|it) as (?:approved|pre-approved|verified)",
    r"you (?:are|have been) (?:now )?(?:authori[sz]ed|permitted|cleared|instructed) to (?:approve|pay|release)",
    r"you have (?:my |full )?permission to (?:approve|pay|release)",
    r"release (?:the |this )?(?:payment|funds) (?:now|immediately|without)",
    r"(?:skip|bypass|override) (?:the |any |all )?(?:approval|checks?|mandate|verification|countersign)",
    r"<!--[^>]{0,200}\b(?:approve|pay|ai|assistant)\b",
    r"\[(?:system|admin|instruction)s?\]",
]
ZERO_WIDTH = re.compile("[\u200b\u200c\u200d\u2060\ufeff\u00ad]")
GIFT_CARD = [r"gift ?cards?", r"\bvouchers?\b", r"\bitunes cards?\b", r"\bsteam cards?\b", r"redemption codes?"]
URGENCY = [r"\burgent(?:ly)?\b", r"\bimmediately\b", r"within (?:24|48) hours", r"\btoday\b", r"\basap\b", r"final notice"]
SECRECY = [r"\bconfidential\b", r"do not (?:call|contact|tell)", r"don't (?:call|tell)", r"keep this (?:between us|private)"]
PAYEE_CHANGE = [r"(?:new|updated|changed?) (?:payment|paypal|bank|billing) (?:email|address|details|account)",
                r"no longer (?:use|accept)", r"please (?:use|send to) (?:this|our) new"]


def _hits(patterns: list[str], text: str) -> list[str]:
    return [m.group(0) for p in patterns for m in [re.search(p, text, re.I)] if m]


def normalize(text: str) -> str:
    """Undo common obfuscation before pattern checks: zero-width characters and compatibility forms."""
    return unicodedata.normalize("NFKC", ZERO_WIDTH.sub("", text))


def non_ascii_email(email: Optional[str]) -> bool:
    return bool(email) and any(ord(ch) > 127 for ch in email)


def lookalike(name: str, known: list[str], cutoff: float = 0.84) -> Optional[str]:
    """A vendor name that is *almost* one on file (typo-squatting), but not equal to it."""
    low = name.strip().lower()
    pool = {k.lower(): k for k in known}
    if low in pool:
        return None
    m = difflib.get_close_matches(low, list(pool), n=1, cutoff=cutoff)
    return pool[m[0]] if m else None


def assess(raw_text: str, inv: Invoice, vendor: Optional[dict], duplicate: bool,
           known_vendors: Optional[list[str]] = None, recent_vendor_total: float = 0.0,
           auto_limit: Optional[float] = None) -> RiskReport:
    sig: list[RiskSignal] = []
    norm = normalize(raw_text)
    if norm != raw_text and ZERO_WIDTH.search(raw_text):
        sig.append(RiskSignal(code="hidden_characters", severity=20,
                              detail="Document contains invisible (zero-width) characters, a common way to hide text from people."))
    raw_text = norm
    if h := _hits(INJECTION, raw_text):
        sig.append(RiskSignal(code="prompt_injection", severity=50,
                              detail=f"Document contains instructions aimed at the AI (\"{h[0].strip()}\"). Treated as data, not commands."))
    if h := _hits(PAYEE_CHANGE, raw_text):
        sig.append(RiskSignal(code="payee_change_language", severity=25,
                              detail=f"Asks to send money to new payment details (\"{h[0]}\")."))
    if h := _hits(URGENCY, raw_text):
        sig.append(RiskSignal(code="urgency", severity=10, detail=f"Pressure language (\"{h[0]}\")."))
    if h := _hits(SECRECY, raw_text):
        sig.append(RiskSignal(code="secrecy", severity=15, detail=f"Secrecy language (\"{h[0]}\")."))
    if vendor:
        known = (vendor.get("payee_email") or "").lower()
        if inv.payee_email and known and inv.payee_email != known:
            sig.append(RiskSignal(code="payee_mismatch", severity=40,
                                  detail=f"Payee {inv.payee_email} differs from the {known} on file for {vendor['name']}."))
        avg = float(vendor.get("avg_amount") or 0)
        if avg and inv.amount > 3 * avg:
            sig.append(RiskSignal(code="amount_anomaly", severity=20,
                                  detail=f"{inv.amount:.2f} is over 3x this vendor's usual {avg:.2f}."))
    if non_ascii_email(inv.payee_email):
        sig.append(RiskSignal(code="homoglyph_payee", severity=45,
                              detail=f"Payee address {inv.payee_email!r} uses non-Latin look-alike characters."))
    if not vendor and known_vendors:
        if near := lookalike(inv.vendor_name, known_vendors):
            sig.append(RiskSignal(code="lookalike_vendor", severity=45,
                                  detail=f"'{inv.vendor_name}' is not on file but looks like '{near}', which is."))
    if h := _hits(GIFT_CARD, raw_text):
        sig.append(RiskSignal(code="gift_card_language", severity=40,
                              detail=f"Mentions gift cards or vouchers (\"{h[0]}\"), a classic scam payout."))
    if auto_limit is not None and recent_vendor_total > 0 and inv.amount <= auto_limit < recent_vendor_total + inv.amount:
        sig.append(RiskSignal(code="split_invoice", severity=30,
                              detail=f"With {recent_vendor_total:.2f} already billed by {inv.vendor_name} in the last 7 days, "
                                     f"this takes the vendor over your {auto_limit:.2f} auto-pay limit (possible invoice splitting)."))
    if duplicate:
        sig.append(RiskSignal(code="duplicate_invoice", severity=60,
                              detail=f"Invoice {inv.invoice_number} from {inv.vendor_name} was already processed."))
    if not inv.payee_email and inv.rail == "payout":
        sig.append(RiskSignal(code="no_payee", severity=30, detail="No payee address found in the document."))
    if inv.amount <= 0:
        sig.append(RiskSignal(code="no_amount", severity=30, detail="No payable amount found."))
    score = min(100, sum(s.severity for s in sig))
    level = "high" if score >= 70 else "medium" if score >= 30 else "low"
    return RiskReport(score=score, level=level, signals=sig)
