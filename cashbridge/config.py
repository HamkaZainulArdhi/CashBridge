"""Central configuration for CashBridge, loaded from environment/.env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _num(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    db_path: Path = field(
        default_factory=lambda: Path(os.getenv("CASHBRIDGE_DB", str(BASE_DIR / "cashbridge.db")))
    )
    mock_llm: bool = field(default_factory=lambda: _flag("MOCK_LLM", True))
    openrouter_api_key: str = field(default_factory=lambda: os.getenv("OPENROUTER_API_KEY", ""))
    openrouter_base_url: str = field(
        default_factory=lambda: os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    )
    openrouter_model: str = field(
        default_factory=lambda: os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
    )
    sandbox_max_principal: float = field(
        default_factory=lambda: _num("SANDBOX_MAX_PRINCIPAL", 25_000_000)
    )


settings = Settings()

# --- Domain constants ---
HORIZON_DAYS = 14          # default forecast horizon shown to the owner
MAX_TENOR_DAYS = 45        # longest repayment tenor offered in the sandbox
MIN_LOAN_PRINCIPAL = 1_000_000
HISTORY_BASELINE_DAYS = 56  # "recent normal" window for capacity heuristics
CURRENCY = "IDR"
