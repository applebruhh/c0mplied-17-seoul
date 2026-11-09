"""Frozen external wire schemas. POS public schemas remain in app.models."""
import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, RootModel, model_validator

from app.models import (AuthorizationStatus, CheckoutRead, CheckoutSnapshot, ContractModel,
                        EventRead, KRWAmount, MockEvent, PaymentsResponse)


ExternalDeclineReason = Literal["LIMIT_EXCEEDED", "WALLET_BUSY", "UNKNOWN_WALLET", "PRICE_UNAVAILABLE"]


class ExternalAuthorizeRequest(ContractModel):
    checkout_id: UUID
    amount_krw: KRWAmount
    merchant_id: Annotated[str, Field(strict=True, min_length=1, max_length=100)]
    card_token: Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]

    def raw_json(self) -> bytes:
        """Serialize once, sign once, transmit these same bytes using content=."""
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False).encode("utf-8")


class ExternalCardRegistration(ContractModel):
    token: Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]
    wallet_id: Annotated[str, Field(strict=True, min_length=1)]


class ExternalAuthorizeResponse(ContractModel):
    payment_id: Annotated[str, Field(strict=True, min_length=1, max_length=200)]
    status: AuthorizationStatus
    reason: ExternalDeclineReason | None
    latency_ms: Annotated[int, Field(strict=True, ge=0)]
    idempotent_replay: Annotated[bool, Field(strict=True)]

    @model_validator(mode="after")
    def consistent_reason(self):
        if (self.status == AuthorizationStatus.DECLINED) != (self.reason is not None):
            raise ValueError("Only DECLINED has a non-null supported reason")
        return self


class ErrorDetails(ContractModel):
    code: str
    message: str
    request_id: UUID


class ErrorResponse(ContractModel):
    error: ErrorDetails


class ResyncPayload(ContractModel):
    reason: Literal["cursor_ahead_of_database"]
    latest_event_id: int


# Parsed SSE metadata; the wire remains text/event-stream, not a JSON frame API.
class CheckoutEventPayload(ContractModel):
    checkout: CheckoutRead
    mock_engine_event: MockEvent | None = None


class SnapshotFrame(ContractModel):
    id: int
    event: Literal["snapshot"]
    data: CheckoutSnapshot | PaymentsResponse


class ResyncFrame(ContractModel):
    id: int
    event: Literal["resync"]
    data: ResyncPayload


class DurableEventFrame(ContractModel):
    id: int
    event: Literal[
        "checkout.created", "checkout.status_changed", "payment.authorization",
        "reservation.status_changed", "prefunding.status_changed", "merchant_payout.status_changed",
        "liquidation.status_changed", "reimbursement.status_changed", "settlement.status_changed",
        "payment.updated", "payment.reconciliation_pending", "demo.reset",
        "WALLET_LOCKED", "HOLD_PLACED", "AUTHORIZATION_DECLINED", "AUTHORIZATION_UNKNOWN",
        "MERCHANT_PAYOUT_COMPLETED", "AUTHORIZED", "LIQUIDATION_STARTED", "ASSET_SELECTED",
        "DEX_SWAP_SUBMITTED", "TRANSACTION_UNKNOWN", "DEX_SWAP_FAILED", "DEX_SWAP_CONFIRMED",
        "LIQUIDATION_COMPLETED", "USDC_RECEIVED_BY_TREASURY", "REIMBURSEMENT_COMPLETED",
        "LIQUIDATION_FAILED", "REIMBURSEMENT_FAILED", "EXPOSURE_RECORDED", "LOSS_ABSORBED",
        "HOLD_EXPIRED", "HOLD_RELEASED",
    ]
    data: EventRead


class SSEFrame(RootModel[SnapshotFrame | ResyncFrame | DurableEventFrame]):
    pass
