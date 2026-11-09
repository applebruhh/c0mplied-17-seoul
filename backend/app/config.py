import os
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, Field


class Settings(BaseModel):
    database_path: Path = Path("data/vaultpay-pos.sqlite3")
    demo_reset_token: str | None = None
    worker_interval: float = Field(default=0.25, gt=0)
    mock_stage_seconds: float = Field(default=1, gt=0)
    engine_timeout: float = Field(default=3, gt=0)
    mock_margin: Decimal = Field(default=Decimal("0.03"), ge=0, le=1)
    mock_max_slippage: Decimal = Field(default=Decimal("0.005"), ge=0, le=Decimal("0.005"))
    mock_hold_ttl: float = Field(default=300, gt=0)
    mock_unknown_seconds: float = Field(default=5, gt=0)

    @classmethod
    def from_env(cls):
        return cls(
            database_path=os.getenv("VAULTPAY_DATABASE_PATH", "data/vaultpay-pos.sqlite3"),
            demo_reset_token=os.getenv("VAULTPAY_DEMO_RESET_TOKEN") or None,
            worker_interval=os.getenv("VAULTPAY_WORKER_INTERVAL", "0.25"),
            mock_stage_seconds=os.getenv("VAULTPAY_MOCK_STAGE_SECONDS", "1"),
            engine_timeout=os.getenv("VAULTPAY_ENGINE_TIMEOUT", "3"),
            mock_margin=os.getenv("VAULTPAY_MOCK_MARGIN", "0.03"),
            mock_max_slippage=os.getenv("VAULTPAY_MOCK_MAX_SLIPPAGE", "0.005"),
            mock_hold_ttl=os.getenv("VAULTPAY_MOCK_HOLD_TTL", "300"),
            mock_unknown_seconds=os.getenv("VAULTPAY_MOCK_UNKNOWN_SECONDS", "5"),
        )
