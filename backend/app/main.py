import asyncio
import hmac
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import IntegrityError

from app.adapters.base import PaymentAdapter
from app.adapters.mock import MockPaymentAdapter
from app.config import Settings
from app.db import Database
from app.models import (
    ActiveCheckoutResponse, CheckoutCreate, CheckoutRead, CheckoutSnapshot, ContractModel, PaymentRead,
    PaymentsResponse, ResetResponse, SettlementStatus, TapRequest, TapResponse,
)
from app.services import APIError, POSService
from app.streaming import stream_events
from app.worker import authorize, run_worker


class HealthResponse(ContractModel):
    status: str = "ok"
    service: str = "vaultpay-pos"
    milestone: str = "backend"


# Explicit feature response retained for the existing frontend health shell.
class CapabilitiesResponse(ContractModel):
    demo_only: bool = True
    supported_assets: list[str] = ["ETH", "BTC", "USDC", "USDT"]
    payment_routes_enabled: bool = True
    nfc_scanning_enabled: bool = False
    sse_enabled: bool = True


def create_app(database_path: Path | None = None, *, settings: Settings | None = None,
               payment_adapter: PaymentAdapter | None = None) -> FastAPI:
    config = settings or Settings.from_env()
    database = Database(database_path or config.database_path)
    adapter = payment_adapter or MockPaymentAdapter(
        database, config.mock_stage_seconds, margin=config.mock_margin,
        max_slippage=config.mock_max_slippage, hold_ttl=config.mock_hold_ttl,
        unknown_seconds=config.mock_unknown_seconds,
    )
    service = POSService(database, adapter.source, lease_seconds=max(10, config.engine_timeout + 2))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await asyncio.to_thread(database.initialize)
        stop = asyncio.Event()
        task = asyncio.create_task(run_worker(service, adapter, config.worker_interval, config.engine_timeout, stop))
        try:
            yield
        finally:
            # Finish the current bounded engine call before closing pooled DB
            # connections, so shutdown does not abandon a worker lease.
            stop.set()
            await task
            await asyncio.to_thread(database.close)

    app = FastAPI(
        title="VaultPay POS Backend", version="0.2.0", lifespan=lifespan,
        description="Local hackathon demo. Mock financial events only; no real payments or NFC authentication.",
    )
    app.state.database, app.state.service, app.state.adapter = database, service, adapter

    @app.exception_handler(APIError)
    async def api_error(request: Request, error: APIError):
        return JSONResponse(status_code=error.status, content={"error": {
            "code": error.code, "message": error.message, "request_id": str(uuid4()),
        }})

    @app.exception_handler(IntegrityError)
    async def integrity_error(request: Request, error: IntegrityError):
        return JSONResponse(status_code=409, content={"error": {
            "code": "DATABASE_CONFLICT", "message": "Request conflicts with persisted state",
            "request_id": str(uuid4()),
        }})

    @app.get("/api/health", response_model=HealthResponse, tags=["System"])
    @app.get("/api/v1/health", response_model=HealthResponse, include_in_schema=False)
    def health():
        return HealthResponse()

    @app.get("/api/capabilities", response_model=CapabilitiesResponse, tags=["System"])
    @app.get("/api/v1/capabilities", response_model=CapabilitiesResponse, include_in_schema=False)
    def capabilities():
        return CapabilitiesResponse(demo_only=adapter.source == "simulated")

    @app.post("/api/checkouts", response_model=CheckoutRead, status_code=201, tags=["Checkouts"])
    def create_checkout(body: CheckoutCreate, response: Response,
                        idempotency_key: Annotated[UUID | None, Header()] = None):
        result, created = service.create_checkout(body, str(idempotency_key) if idempotency_key else None)
        response.status_code = 201 if created else 200
        return result

    @app.get("/api/checkouts/{checkout_id}", response_model=CheckoutRead, tags=["Checkouts"])
    def get_checkout(checkout_id: UUID):
        return service.get_checkout(str(checkout_id))["checkout"]

    @app.post("/api/checkouts/{checkout_id}/cancel", response_model=CheckoutRead, tags=["Checkouts"])
    def cancel(checkout_id: UUID):
        return service.cancel(str(checkout_id))

    @app.get("/api/terminals/{terminal_id}/active-checkout", response_model=ActiveCheckoutResponse, tags=["Checkouts"])
    def active_checkout(terminal_id: str):
        return service.active_checkout(terminal_id)

    @app.post("/api/nfc/tap", response_model=TapResponse, tags=["NFC"])
    async def tap(body: TapRequest, response: Response):
        payment_id, command = await asyncio.to_thread(service.claim_tap, body)
        if command:
            await authorize(service, adapter, command, config.engine_timeout)
        result = await asyncio.to_thread(service.tap_result, payment_id, command is None)
        response.status_code = 202 if result["checkout"].status == "AUTHORIZING" else 200
        return result

    @app.get("/api/ops/payments", response_model=PaymentsResponse, tags=["Operations"])
    def payments(limit: Annotated[int, Query(ge=1, le=100)] = 50,
                 offset: Annotated[int, Query(ge=0)] = 0, settlement_status: SettlementStatus | None = None):
        return service.payments(limit, offset, settlement_status)

    @app.get("/api/ops/payments/{payment_id}", response_model=PaymentRead, tags=["Operations"])
    def payment(payment_id: UUID):
        return service.payment(str(payment_id))

    def event_cursor(last_event_id: str | None, after: int | None):
        if last_event_id is not None:
            if not last_event_id.isascii() or not last_event_id.isdecimal() or len(last_event_id) > 18:
                raise APIError(422, "INVALID_CURSOR", "Last-Event-ID must be a nonnegative integer")
            return int(last_event_id)
        return after

    def stream(request: Request, snapshot: dict, checkout_id: str | None, after: int | None, follow: bool):
        return StreamingResponse(
            stream_events(request, service, snapshot, checkout_id, after, follow),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    sse_responses = {200: {"content": {"text/event-stream": {}}, "description": "Durable events and latest snapshot"}}

    @app.get("/api/checkouts/{checkout_id}/events", tags=["Events"], responses=sse_responses,
             response_class=StreamingResponse)
    async def checkout_events(
        checkout_id: UUID, request: Request,
        after: Annotated[int | None, Query(ge=0, le=10**18)] = None,
        follow: bool = True, last_event_id: Annotated[str | None, Header()] = None,
    ):
        requested = event_cursor(last_event_id, after)
        state = await asyncio.to_thread(service.get_checkout, str(checkout_id))
        snapshot = CheckoutSnapshot.model_validate(state).model_dump(mode="json")
        return stream(request, snapshot, str(checkout_id), requested, follow)

    @app.get("/api/ops/events", tags=["Events"], responses=sse_responses, response_class=StreamingResponse)
    async def ops_events(
        request: Request, after: Annotated[int | None, Query(ge=0, le=10**18)] = None,
        follow: bool = True, last_event_id: Annotated[str | None, Header()] = None,
    ):
        requested = event_cursor(last_event_id, after)
        state = await asyncio.to_thread(service.payments, 100)
        snapshot = PaymentsResponse.model_validate(state).model_dump(mode="json")
        return stream(request, snapshot, None, requested, follow)

    @app.post("/api/demo/reset", response_model=ResetResponse, tags=["Demo"],
              summary="Reset simulated checkouts, payments and events")
    def reset(x_demo_reset_token: Annotated[str | None, Header()] = None):
        if not config.demo_reset_token:
            raise APIError(503, "RESET_DISABLED", "Set VAULTPAY_DEMO_RESET_TOKEN to enable demo reset")
        if not x_demo_reset_token or not hmac.compare_digest(
            x_demo_reset_token.encode(), config.demo_reset_token.encode()
        ):
            raise APIError(403, "RESET_FORBIDDEN", "A valid X-Demo-Reset-Token is required")
        return service.reset()

    frontend = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if (frontend / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=frontend / "assets"), name="terminal-assets")

    @app.get("/terminal", include_in_schema=False)
    def terminal_page():
        index = frontend / "index.html"
        if not index.is_file():
            raise HTTPException(status_code=503, detail="Build the frontend before starting VaultPay on port 8000.")
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return app


app = create_app()
