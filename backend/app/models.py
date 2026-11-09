from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CheckoutStatus(StrEnum):
    WAITING = "WAITING"
    PROCESSING = "PROCESSING"
    APPROVED = "APPROVED"
    DECLINED = "DECLINED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class AuthorizationStatus(StrEnum):
    PENDING = "PENDING"
    UNKNOWN = "UNKNOWN"
    AUTHORIZED = "AUTHORIZED"
    DECLINED = "DECLINED"


class SettlementStatus(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    PENDING = "PENDING"
    SETTLED = "SETTLED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


class ReservationStatus(StrEnum):
    NONE = "NONE"
    RESERVED = "RESERVED"
    PARTIALLY_RELEASED = "PARTIALLY_RELEASED"
    RELEASED = "RELEASED"
    UNKNOWN = "UNKNOWN"


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


KRWAmount = Annotated[int, Field(strict=True, ge=1, le=10_000_000)]


class CheckoutCreate(ContractModel):
    terminal_id: Annotated[str, Field(min_length=1, max_length=100)]
    amount_krw: KRWAmount


class TapRequest(ContractModel):
    terminal_id: Annotated[str, Field(min_length=1, max_length=100)]
    checkout_id: UUID
    card_id: Annotated[str, Field(min_length=1, max_length=100)]


class AuthorizationRequest(ContractModel):
    payment_id: UUID
    checkout_id: UUID
    customer_id: str
    amount_krw: KRWAmount
    currency: Literal["KRW"] = "KRW"


class EnginePayment(ContractModel):
    payment_id: UUID
    engine_payment_id: str
    amount_krw: KRWAmount
    currency: Literal["KRW"] = "KRW"
    authorization_status: AuthorizationStatus
    settlement_status: SettlementStatus
    asset_reservation_status: ReservationStatus
    krw_reservation_status: ReservationStatus
    source: Literal["simulated", "external"]
    assets: list[Literal["ETH", "BTC", "USDC"]] = Field(default_factory=list)
    reason_code: str | None = None

    @model_validator(mode="after")
    def validate_reservations(self) -> "EnginePayment":
        if self.authorization_status == AuthorizationStatus.AUTHORIZED:
            # Final settlement may have released holds; pending approval needs both.
            if self.settlement_status == SettlementStatus.NOT_STARTED:
                raise ValueError("Authorized payment must have entered settlement")
            if self.settlement_status == SettlementStatus.PENDING and (
                self.asset_reservation_status != ReservationStatus.RESERVED
                or self.krw_reservation_status != ReservationStatus.RESERVED
            ):
                raise ValueError("Authorization requires both reservations")
        elif self.settlement_status != SettlementStatus.NOT_STARTED:
            raise ValueError("Settlement requires an authorized payment")
        return self
