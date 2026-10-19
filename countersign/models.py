"""Typed data shared across the agent."""
from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


class LineItem(BaseModel):
    description: str
    amount: float


class Invoice(BaseModel):
    """What the extractor pulls out of a free-text bill or purchase request."""
    vendor_name: str
    payee_email: Optional[str] = None
    amount: float = Field(ge=0)
    currency: str = "USD"
    invoice_number: Optional[str] = None
    due_date: Optional[str] = None
    category: str = "general"
    description: str = ""
    rail: Literal["payout", "checkout"] = "payout"
    line_items: list[LineItem] = []

    @field_validator("currency")
    @classmethod
    def _upper(cls, v: str) -> str:
        return (v or "USD").upper()[:3]

    @field_validator("category")
    @classmethod
    def _cat(cls, v: str) -> str:
        return (v or "general").strip().lower().replace(" ", "_")

    @field_validator("payee_email")
    @classmethod
    def _email(cls, v: Optional[str]) -> Optional[str]:
        return v.strip().lower() if v else v


class Mandate(BaseModel):
    """Deterministic spending rules compiled from the owner's plain-English policy."""
    auto_approve_max: float = 100.0
    hard_block_above: float = 2000.0
    monthly_budgets: dict[str, float] = {}       # category -> cap; "*" = overall
    allowed_currencies: list[str] = ["USD"]
    new_vendor_requires_approval: bool = True
    payee_change_requires_approval: bool = True
    blocked_categories: list[str] = []

    @field_validator("allowed_currencies")
    @classmethod
    def _cur(cls, v: list[str]) -> list[str]:
        return sorted({c.upper()[:3] for c in v}) or ["USD"]

    @field_validator("blocked_categories")
    @classmethod
    def _blk(cls, v: list[str]) -> list[str]:
        return sorted({c.strip().lower().replace(" ", "_") for c in v})

    @field_validator("monthly_budgets")
    @classmethod
    def _bud(cls, v: dict[str, float]) -> dict[str, float]:
        return {k.strip().lower().replace(" ", "_"): float(x) for k, x in v.items()}


class Outcome(str, Enum):
    AUTO_PAY = "AUTO_PAY"
    NEEDS_COUNTERSIGN = "NEEDS_COUNTERSIGN"
    BLOCK = "BLOCK"


class RuleCheck(BaseModel):
    rule: str
    passed: bool
    effect: Literal["ok", "countersign", "block"] = "ok"
    detail: str


class Decision(BaseModel):
    outcome: Outcome
    checks: list[RuleCheck]

    @property
    def reasons(self) -> list[str]:
        return [c.detail for c in self.checks if not c.passed]


class RiskSignal(BaseModel):
    code: str
    severity: int  # points added to the risk score
    detail: str


class RiskReport(BaseModel):
    score: int
    level: Literal["low", "medium", "high"]
    signals: list[RiskSignal]
