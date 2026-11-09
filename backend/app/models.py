"""Validated API and engine contracts. ORM records live in tables.py."""
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CheckoutStatus(StrEnum):
    WAITING_FOR_CARD = "WAITING_FOR_CARD"
    AUTHORIZING = "AUTHORIZING"
    AUTHORIZED = "AUTHORIZED"
    DECLINED = "DECLINED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class AuthorizationStatus(StrEnum):
    PENDING = "PENDING"
    AUTHORIZED = "AUTHORIZED"
    DECLINED = "DECLINED"


class AssetReservationStatus(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    PENDING = "PENDING"
    RESERVED = "RESERVED"
    FAILED = "FAILED"
    RELEASED = "RELEASED"


class PrefundingStatus(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    PENDING = "PENDING"
    RESERVED = "RESERVED"
    FAILED = "FAILED"


class MerchantPayoutStatus(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class LiquidationStatus(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ReimbursementStatus(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class SettlementStatus(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


KRWAmount = Annotated[int, Field(strict=True, ge=1, le=10_000_000)]
DemoIdentifier = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")]


class CheckoutCreate(ContractModel):
    merchant_id: DemoIdentifier
    terminal_id: DemoIdentifier
    amount_krw: KRWAmount
    expires_in_seconds: Annotated[int, Field(strict=True, ge=1, le=3600)] = 300


class TapRequest(ContractModel):
    terminal_id: DemoIdentifier
    card_alias: DemoIdentifier
    client_event_id: UUID
    # Recommended for delayed-tap protection. Omission resolves the current session.
    checkout_id: UUID | None = None


class AuthorizationRequest(ContractModel):
    payment_id: UUID
    checkout_id: UUID
    merchant_id: str
    terminal_id: str
    customer_id: str
    amount_krw: KRWAmount
    currency: Literal["KRW"] = "KRW"

    @property
    def idempotency_key(self) -> str:
        return str(self.checkout_id)


class EnginePayment(ContractModel):
    payment_id: UUID
    checkout_id: UUID
    engine_payment_id: str
    amount_krw: KRWAmount
    currency: Literal["KRW"] = "KRW"
    authorization_status: AuthorizationStatus
    asset_reservation_status: AssetReservationStatus
    prefunding_status: PrefundingStatus
    # Optional on the wire for existing integrations/records. New approval
    # requires explicit COMPLETED at the POS projection boundary.
    merchant_payout_status: MerchantPayoutStatus | None = None
    liquidation_status: LiquidationStatus
    reimbursement_status: ReimbursementStatus
    settlement_status: SettlementStatus
    source: Literal["simulated", "external"]
    version: Annotated[int, Field(ge=1)]
    assets: list[Literal["ETH", "BTC", "USDC"]] = Field(default_factory=list)
    reason_code: str | None = None
    timestamps: dict[str, datetime] = Field(default_factory=dict)
    settlement_reference: str | None = None

    @model_validator(mode="after")
    def consistent_projection(self) -> "EnginePayment":
        if self.authorization_status != AuthorizationStatus.AUTHORIZED:
            if (self.settlement_status != SettlementStatus.NOT_STARTED
                    or self.liquidation_status != LiquidationStatus.NOT_STARTED
                    or self.reimbursement_status != ReimbursementStatus.NOT_STARTED):
                raise ValueError("Settlement requires authorization")
            return self
        if self.prefunding_status != PrefundingStatus.RESERVED:
            raise ValueError("Authorization requires reserved company prefunding")
        if self.merchant_payout_status not in (None, MerchantPayoutStatus.COMPLETED):
            raise ValueError("Authorization requires completed merchant payout")
        failed = (self.liquidation_status == LiquidationStatus.FAILED
                  or self.reimbursement_status == ReimbursementStatus.FAILED)
        if failed != (self.settlement_status == SettlementStatus.RECOVERY_REQUIRED):
            raise ValueError("Post-authorization failures require recovery")
        if self.settlement_status == SettlementStatus.COMPLETED:
            if (self.liquidation_status != LiquidationStatus.COMPLETED
                    or self.reimbursement_status != ReimbursementStatus.COMPLETED
                    or self.asset_reservation_status != AssetReservationStatus.RELEASED):
                raise ValueError("Completed settlement requires reimbursement and release")
        elif self.asset_reservation_status != AssetReservationStatus.RESERVED:
            raise ValueError("Pending settlement or recovery must preserve reservation")
        if self.settlement_status == SettlementStatus.NOT_STARTED:
            raise ValueError("Authorized payment must enter settlement")
        return self


class PaymentRead(ContractModel):
    payment_id: str
    checkout_id: str
    idempotency_key: str
    merchant_id: str
    terminal_id: str
    customer_id: str
    amount_krw: int
    currency: Literal["KRW"] = "KRW"
    authorization_status: AuthorizationStatus
    asset_reservation_status: AssetReservationStatus
    prefunding_status: PrefundingStatus
    merchant_payout_status: MerchantPayoutStatus | None = None
    liquidation_status: LiquidationStatus
    reimbursement_status: ReimbursementStatus
    settlement_status: SettlementStatus
    source: Literal["simulated", "external"]
    engine_payment_id: str | None
    engine_version: int
    reason_code: str | None
    last_error: str | None
    assets: list[Literal["ETH", "BTC", "USDC"]]
    timestamps: dict[str, datetime]
    settlement_reference: str | None
    created_at: datetime
    updated_at: datetime
    authorized_at: datetime | None
    settled_at: datetime | None


class CheckoutRead(ContractModel):
    checkout_id: str
    merchant_id: str
    terminal_id: str
    amount_krw: int
    currency: Literal["KRW"] = "KRW"
    status: CheckoutStatus
    created_at: datetime
    expires_at: datetime
    updated_at: datetime
    payment_id: str | None
    payment: PaymentRead | None


class CheckoutSnapshot(ContractModel):
    checkout: CheckoutRead
    latest_event_id: int


class ActiveCheckoutResponse(ContractModel):
    checkout: CheckoutRead | None
    latest_event_id: int


class TapResponse(ContractModel):
    checkout: CheckoutRead
    payment: PaymentRead
    duplicate: bool
    latest_event_id: int


class PaymentsResponse(ContractModel):
    items: list[PaymentRead]
    total: int
    limit: int
    offset: int
    latest_event_id: int


class EventRead(ContractModel):
    schema_version: Literal[1] = 1
    event_id: int
    event_type: str
    checkout_id: str | None
    payment_id: str | None
    source: Literal["simulated", "external"]
    occurred_at: datetime
    payload: dict


class ResetResponse(ContractModel):
    reset: bool
    latest_event_id: int
