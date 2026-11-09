import os
from decimal import Decimal
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, Field, SecretStr, model_validator


class Settings(BaseModel):
    model_config = {"hide_input_in_errors": True}

    database_path: Path = Path("data/vaultpay-pos.sqlite3")
    demo_reset_token: str | None = None
    payment_engine_mode: Literal["mock", "external"] = "mock"
    payment_engine_base_url: str | None = Field(default=None, repr=False)
    sb_pos_hmac_secret: SecretStr | None = None
    ops_token: SecretStr | None = None
    demo_local_ops: bool = False
    demo_local_ops_access_code: SecretStr | None = None
    worker_interval: float = Field(default=0.25, gt=0)
    mock_stage_seconds: float = Field(default=1, gt=0)
    engine_timeout: float = Field(default=3, gt=0)
    mock_margin: Decimal = Field(default=Decimal("0.03"), ge=0, le=1)
    mock_max_slippage: Decimal = Field(default=Decimal("0.005"), ge=0, le=Decimal("0.005"))
    mock_hold_ttl: float = Field(default=300, gt=0)
    mock_unknown_seconds: float = Field(default=5, gt=0)

    @model_validator(mode="after")
    def external_credentials(self):
        if self.payment_engine_mode == "external":
            if not self.sb_pos_hmac_secret or not self.sb_pos_hmac_secret.get_secret_value():
                raise ValueError("SB_POS_HMAC_SECRET is required in external mode")
            engine_origin(self.payment_engine_base_url)
        if self.demo_local_ops and (
            self.demo_local_ops_access_code is None or len(self.demo_local_ops_access_code.get_secret_value()) < 24
        ):
            raise ValueError("DEMO_LOCAL_OPS_ACCESS_CODE must be at least 24 characters when demo Ops is enabled")
        return self

    @classmethod
    def from_env(cls):
        return cls(
            database_path=os.getenv("VAULTPAY_DATABASE_PATH", "data/vaultpay-pos.sqlite3"),
            demo_reset_token=os.getenv("VAULTPAY_DEMO_RESET_TOKEN") or None,
            payment_engine_mode=os.getenv("PAYMENT_ENGINE_MODE", "mock"),
            payment_engine_base_url=os.getenv("PAYMENT_ENGINE_BASE_URL") or None,
            sb_pos_hmac_secret=os.getenv("SB_POS_HMAC_SECRET") or None,
            ops_token=os.getenv("VAULTPAY_OPS_TOKEN") or None,
            demo_local_ops=os.getenv("DEMO_LOCAL_OPS", "false"),
            demo_local_ops_access_code=os.getenv("DEMO_LOCAL_OPS_ACCESS_CODE") or None,
            worker_interval=os.getenv("VAULTPAY_WORKER_INTERVAL", "0.25"),
            mock_stage_seconds=os.getenv("VAULTPAY_MOCK_STAGE_SECONDS", "1"),
            engine_timeout=os.getenv("VAULTPAY_ENGINE_TIMEOUT", "3"),
            mock_margin=os.getenv("VAULTPAY_MOCK_MARGIN", "0.03"),
            mock_max_slippage=os.getenv("VAULTPAY_MOCK_MAX_SLIPPAGE", "0.005"),
            mock_hold_ttl=os.getenv("VAULTPAY_MOCK_HOLD_TTL", "300"),
            mock_unknown_seconds=os.getenv("VAULTPAY_MOCK_UNKNOWN_SECONDS", "5"),
        )


def engine_origin(value: str | httpx.URL | None) -> httpx.URL:
    try:
        url = httpx.URL(value or "")
    except httpx.InvalidURL:
        raise ValueError("PAYMENT_ENGINE_BASE_URL must be an HTTP(S) origin") from None
    if (url.scheme not in ("http", "https") or not url.host or url.userinfo
            or url.path != "/" or url.query or url.fragment):
        raise ValueError("PAYMENT_ENGINE_BASE_URL must be an HTTP(S) origin without credentials, path, query or fragment")
    return url
