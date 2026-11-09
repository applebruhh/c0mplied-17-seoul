import os
from pathlib import Path

from pydantic import BaseModel, Field


class Settings(BaseModel):
    database_path: Path = Path("data/vaultpay-pos.sqlite3")
    demo_reset_token: str | None = None
    worker_interval: float = Field(default=0.25, gt=0)
    mock_stage_seconds: float = Field(default=1, gt=0)
    engine_timeout: float = Field(default=3, gt=0)

    @classmethod
    def from_env(cls):
        return cls(
            database_path=os.getenv("VAULTPAY_DATABASE_PATH", "data/vaultpay-pos.sqlite3"),
            demo_reset_token=os.getenv("VAULTPAY_DEMO_RESET_TOKEN") or None,
            worker_interval=os.getenv("VAULTPAY_WORKER_INTERVAL", "0.25"),
            mock_stage_seconds=os.getenv("VAULTPAY_MOCK_STAGE_SECONDS", "1"),
            engine_timeout=os.getenv("VAULTPAY_ENGINE_TIMEOUT", "3"),
        )
