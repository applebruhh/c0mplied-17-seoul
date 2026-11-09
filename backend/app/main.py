import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel

from app.db import initialize


class HealthResponse(BaseModel):
    status: str = "ok"
    service: str = "vaultpay-pos"
    milestone: str = "scaffold"


class CapabilitiesResponse(BaseModel):
    demo_only: bool = True
    supported_assets: list[str] = ["ETH", "BTC", "USDC"]
    implemented: list[str] = ["health", "schema_initialization", "adapter_contracts", "route_shells"]
    payment_routes_enabled: bool = False
    nfc_scanning_enabled: bool = False
    sse_enabled: bool = False


def create_app(database_path: Path | None = None) -> FastAPI:
    path = database_path or Path(os.getenv("VAULTPAY_DATABASE_PATH", "data/vaultpay.sqlite3"))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        initialize(path)
        yield

    app = FastAPI(title="VaultPay POS", version="0.1.0", lifespan=lifespan)

    @app.get("/api/v1/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse()

    @app.get("/api/v1/capabilities", response_model=CapabilitiesResponse)
    def capabilities() -> CapabilitiesResponse:
        return CapabilitiesResponse()

    return app


app = create_app()
