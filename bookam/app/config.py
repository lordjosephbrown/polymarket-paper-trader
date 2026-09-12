"""Environment-driven configuration."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _default_db_path() -> str:
    return str(Path(os.environ.get("BOOKAM_DATA_DIR", str(Path.home() / ".bookam"))) / "bookam.db")


@dataclass
class Settings:
    db_path: str = field(default_factory=lambda: os.environ.get("BOOKAM_DB", _default_db_path()))
    secret_key: str = field(default_factory=lambda: os.environ.get("BOOKAM_SECRET", "dev-secret-change-me"))
    base_url: str = field(default_factory=lambda: os.environ.get("BOOKAM_BASE_URL", "http://localhost:8000").rstrip("/"))
    paystack_secret_key: str = field(default_factory=lambda: os.environ.get("PAYSTACK_SECRET_KEY", ""))
    session_ttl_seconds: int = 60 * 60 * 24 * 30  # 30 days
    # WhatsApp Business Cloud API (Meta). All empty = WhatsApp demo mode:
    # the bot still works and messages are recorded in wa_outbox.
    wa_token: str = field(default_factory=lambda: os.environ.get("WHATSAPP_TOKEN", ""))
    wa_phone_id: str = field(default_factory=lambda: os.environ.get("WHATSAPP_PHONE_ID", ""))
    wa_verify_token: str = field(default_factory=lambda: os.environ.get("WHATSAPP_VERIFY_TOKEN", "bookam-verify"))
    wa_app_secret: str = field(default_factory=lambda: os.environ.get("WHATSAPP_APP_SECRET", ""))
    # The number customers message, in international format (for wa.me links).
    wa_public_number: str = field(default_factory=lambda: os.environ.get("WHATSAPP_PUBLIC_NUMBER", ""))

    @property
    def demo_payments(self) -> bool:
        """True when no Paystack key is configured — deposits auto-succeed."""
        return not self.paystack_secret_key


def load_settings() -> Settings:
    return Settings()
