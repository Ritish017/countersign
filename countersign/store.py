"""SQLite persistence: vendors, mandate, cases, audit events."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS vendors(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE COLLATE NOCASE, payee_email TEXT,
  category TEXT, avg_amount REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS mandate(
  id INTEGER PRIMARY KEY CHECK(id=1), policy_text TEXT, rules TEXT, compiled_by TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS cases(
  id INTEGER PRIMARY KEY, created_at TEXT, raw_text TEXT, status TEXT,
  invoice TEXT, risk TEXT, decision TEXT, brief TEXT, paypal TEXT, trace TEXT,
  paid_amount REAL DEFAULT 0, paid_at TEXT);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, at TEXT, case_id INTEGER, actor TEXT, kind TEXT, detail TEXT);
"""

JSON_COLS = ("invoice", "risk", "decision", "paypal", "trace")


def _norm_inv(n) -> str:
    """'PSD-1042', 'psd 1042' and 'PSD1042' are the same invoice."""
    return "".join(ch for ch in str(n or "").lower() if ch.isalnum())


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.db.executescript(SCHEMA)
            self.db.commit()

    # vendors
    def upsert_vendor(self, name: str, payee_email: str, category: str, avg_amount: float) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO vendors(name,payee_email,category,avg_amount) VALUES(?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET payee_email=excluded.payee_email, category=excluded.category, avg_amount=excluded.avg_amount",
                (name, payee_email.lower(), category, avg_amount))
            self.db.commit()

    def find_vendor(self, name: str) -> Optional[dict]:
        r = self.db.execute("SELECT * FROM vendors WHERE name=? COLLATE NOCASE", (name.strip(),)).fetchone()
        return dict(r) if r else None

    def vendors(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM vendors ORDER BY name")]

    # mandate
    def set_mandate(self, policy_text: str, rules: dict, compiled_by: str) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO mandate(id,policy_text,rules,compiled_by,updated_at) VALUES(1,?,?,?,?)",
                            (policy_text, json.dumps(rules), compiled_by, now()))
            self.db.commit()

    def get_mandate(self) -> Optional[dict]:
        r = self.db.execute("SELECT * FROM mandate WHERE id=1").fetchone()
        if not r:
            return None
        d = dict(r)
        d["rules"] = json.loads(d["rules"])
        return d

    # cases
    def create_case(self, raw_text: str) -> int:
        with self.lock:
            cur = self.db.execute("INSERT INTO cases(created_at,raw_text,status,trace) VALUES(?,?,?,?)",
                                  (now(), raw_text, "received", "[]"))
            self.db.commit()
            return int(cur.lastrowid)

    def update_case(self, case_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols, vals = [], []
        for k, v in fields.items():
            cols.append(f"{k}=?")
            vals.append(json.dumps(v) if k in JSON_COLS else v)
        with self.lock:
            self.db.execute(f"UPDATE cases SET {', '.join(cols)} WHERE id=?", (*vals, case_id))
            self.db.commit()

    def get_case(self, case_id: int) -> Optional[dict]:
        r = self.db.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        return self._row(r) if r else None

    def cases(self) -> list[dict]:
        return [self._row(r) for r in self.db.execute("SELECT * FROM cases ORDER BY id DESC")]

    def _row(self, r: sqlite3.Row) -> dict:
        d = dict(r)
        for k in JSON_COLS:
            d[k] = json.loads(d[k]) if d.get(k) else None
        return d

    def find_case_by_order(self, order_id: str) -> Optional[dict]:
        for c in self.cases():
            if (c.get("paypal") or {}).get("order_id") == order_id:
                return c
        return None

    def recent_vendor_total(self, vendor: str, exclude_id: int, days: int = 7) -> float:
        """Amount billed by this vendor in recent non-rejected cases (for invoice-splitting checks)."""
        from datetime import datetime, timedelta, timezone
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        total = 0.0
        for c in self.cases():
            inv = c.get("invoice") or {}
            if c["id"] == exclude_id or c["status"] in ("blocked", "rejected", "failed"):
                continue
            if inv.get("vendor_name", "").lower() != vendor.lower():
                continue
            try:
                created = datetime.fromisoformat(str(c.get("created_at")).replace("Z", "+00:00"))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
            except ValueError:
                created = datetime.now(timezone.utc)
            if created >= cutoff:
                total += float(inv.get("amount") or 0)
        return round(total, 2)

    def is_duplicate(self, vendor: str, invoice_number: Optional[str], exclude_id: int) -> bool:
        if not invoice_number:
            return False
        for c in self.cases():
            inv = c.get("invoice") or {}
            if (c["id"] != exclude_id and c["status"] not in ("blocked", "rejected", "failed")
                    and _norm_inv(inv.get("invoice_number")) == _norm_inv(invoice_number)
                    and inv.get("vendor_name", "").lower() == vendor.lower()):
                return True
        return False

    def spent_this_month(self) -> dict[str, float]:
        month = now()[:7]
        out: dict[str, float] = {"*": 0.0}
        for c in self.cases():
            if c["status"] in ("paid", "payout_pending") and (c.get("paid_at") or "")[:7] == month:
                cat = (c.get("invoice") or {}).get("category", "general")
                out[cat] = out.get(cat, 0.0) + float(c["paid_amount"] or 0)
                out["*"] += float(c["paid_amount"] or 0)
        return out

    # audit
    def log(self, case_id: Optional[int], actor: str, kind: str, detail: str) -> None:
        with self.lock:
            self.db.execute("INSERT INTO events(at,case_id,actor,kind,detail) VALUES(?,?,?,?,?)",
                            (now(), case_id, actor, kind, detail))
            self.db.commit()

    def events(self, limit: int = 100) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]
