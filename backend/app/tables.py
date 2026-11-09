"""SQLite persistence; financial columns contain engine projections, not a ledger."""
from datetime import datetime

from sqlalchemy import JSON, Boolean, CheckConstraint, Enum, ForeignKey, Index, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.models import (
    AssetReservationStatus, AuthorizationStatus, CheckoutStatus, LiquidationStatus,
    PrefundingStatus, ReimbursementStatus, SettlementStatus,
)


class Base(DeclarativeBase):
    pass


def state(enum):
    return Enum(enum, native_enum=False, create_constraint=True, validate_strings=True)


class Merchant(Base):
    __tablename__ = "merchants"
    merchant_id: Mapped[str] = mapped_column(String, primary_key=True)
    display_name: Mapped[str]


class Terminal(Base):
    __tablename__ = "terminals"
    terminal_id: Mapped[str] = mapped_column(String, primary_key=True)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.merchant_id"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Customer(Base):
    __tablename__ = "customers"
    customer_id: Mapped[str] = mapped_column(String, primary_key=True)
    display_name: Mapped[str]


class DemoCard(Base):
    __tablename__ = "demo_cards"
    card_alias: Mapped[str] = mapped_column(String, primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.customer_id"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Checkout(Base):
    __tablename__ = "checkouts"
    checkout_id: Mapped[str] = mapped_column(String, primary_key=True)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.merchant_id"))
    terminal_id: Mapped[str] = mapped_column(ForeignKey("terminals.terminal_id"))
    amount_krw: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(default="KRW")
    status: Mapped[CheckoutStatus] = mapped_column(state(CheckoutStatus))
    creation_key: Mapped[str] = mapped_column(unique=True)
    creation_fingerprint: Mapped[str]
    created_at: Mapped[datetime]
    expires_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    __table_args__ = (
        CheckConstraint("typeof(amount_krw) = 'integer' AND amount_krw BETWEEN 1 AND 10000000"),
        CheckConstraint("currency = 'KRW'"),
        Index("one_active_checkout_per_terminal", "terminal_id", unique=True,
              sqlite_where=status.in_([CheckoutStatus.WAITING_FOR_CARD, CheckoutStatus.AUTHORIZING])),
    )


class Payment(Base):
    __tablename__ = "payments"
    payment_id: Mapped[str] = mapped_column(String, primary_key=True)
    checkout_id: Mapped[str] = mapped_column(ForeignKey("checkouts.checkout_id"), unique=True)
    idempotency_key: Mapped[str] = mapped_column(unique=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.customer_id"))
    authorization_status: Mapped[AuthorizationStatus] = mapped_column(state(AuthorizationStatus))
    asset_reservation_status: Mapped[AssetReservationStatus] = mapped_column(state(AssetReservationStatus))
    prefunding_status: Mapped[PrefundingStatus] = mapped_column(state(PrefundingStatus))
    liquidation_status: Mapped[LiquidationStatus] = mapped_column(state(LiquidationStatus))
    reimbursement_status: Mapped[ReimbursementStatus] = mapped_column(state(ReimbursementStatus))
    settlement_status: Mapped[SettlementStatus] = mapped_column(state(SettlementStatus))
    source: Mapped[str]
    engine_payment_id: Mapped[str | None] = mapped_column(unique=True)
    engine_version: Mapped[int] = mapped_column(default=0)
    engine_detail: Mapped[dict] = mapped_column(JSON, default=dict)
    reason_code: Mapped[str | None]
    last_error: Mapped[str | None]
    lease_until: Mapped[datetime]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    authorized_at: Mapped[datetime | None]
    settled_at: Mapped[datetime | None]
    __table_args__ = (
        CheckConstraint("idempotency_key = checkout_id"),
        CheckConstraint("source IN ('simulated', 'external')"),
        CheckConstraint("authorization_status = 'AUTHORIZED' OR settlement_status = 'NOT_STARTED'"),
        Index("payments_reconciliation", "settlement_status", "lease_until"),
    )


class TapEvent(Base):
    __tablename__ = "tap_events"
    client_event_id: Mapped[str] = mapped_column(String, primary_key=True)
    payload_fingerprint: Mapped[str]
    checkout_id: Mapped[str] = mapped_column(ForeignKey("checkouts.checkout_id"))
    payment_id: Mapped[str] = mapped_column(ForeignKey("payments.payment_id"))
    created_at: Mapped[datetime]


class Event(Base):
    __tablename__ = "events"
    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str]
    checkout_id: Mapped[str | None] = mapped_column(ForeignKey("checkouts.checkout_id"))
    payment_id: Mapped[str | None] = mapped_column(ForeignKey("payments.payment_id"))
    source: Mapped[str]
    occurred_at: Mapped[datetime]
    payload: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (
        CheckConstraint("source IN ('simulated', 'external')"),
        Index("events_checkout_cursor", "checkout_id", "event_id"),
        {"sqlite_autoincrement": True},
    )


class MockEngineRecord(Base):
    """Durable simulated upstream fixture, kept outside the POS financial projection."""
    __tablename__ = "mock_engine_records"
    checkout_id: Mapped[str] = mapped_column(String, primary_key=True)
    request_fingerprint: Mapped[str]
    request: Mapped[dict] = mapped_column(JSON)
    initial_result: Mapped[dict] = mapped_column(JSON)
    scenario: Mapped[str]
    created_at: Mapped[datetime]
