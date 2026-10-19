"""Runtime configuration, read from environment variables (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Settings:
    llm_provider: str          # "groq" | "mock"
    groq_api_key: str
    groq_model: str
    paypal_mode: str           # "sandbox" | "mock"
    paypal_client_id: str
    paypal_client_secret: str
    paypal_webhook_id: str
    paypal_base_url: str
    public_base_url: str
    db_path: str
    currency: str

    @staticmethod
    def load() -> "Settings":
        groq_key = _env("GROQ_API_KEY")
        llm = _env("LLM_PROVIDER", "auto").lower()
        if llm == "auto":
            llm = "groq" if groq_key else "mock"

        cid, secret = _env("PAYPAL_CLIENT_ID"), _env("PAYPAL_CLIENT_SECRET")
        pp = _env("PAYPAL_MODE", "auto").lower()
        if pp == "auto":
            pp = "sandbox" if (cid and secret) else "mock"

        return Settings(
            llm_provider=llm,
            groq_api_key=groq_key,
            groq_model=_env("GROQ_MODEL", "openai/gpt-oss-20b"),
            paypal_mode=pp,
            paypal_client_id=cid,
            paypal_client_secret=secret,
            paypal_webhook_id=_env("PAYPAL_WEBHOOK_ID"),
            # Sandbox only. This project never targets the live API.
            paypal_base_url=_env("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com"),
            public_base_url=_env("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/"),
            db_path=_env("DB_PATH", "countersign.db"),
            currency=_env("DEFAULT_CURRENCY", "USD"),
        )
