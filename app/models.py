from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def now() -> datetime:
    return datetime.now(timezone.utc)


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


# ── 사용자 · 지갑 · 카드 · 가맹점 ───────────────────────────────
class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: uid("usr"))
    name: Mapped[str] = mapped_column(String, default="")
    risk_profile: Mapped[str] = mapped_column(String, default="neutral")        # conservative|neutral|aggressive → γ0
    pay_assets: Mapped[list] = mapped_column(JSON, default=list)                # 결제에 쓸 코인 (빈 리스트 = 전부)
    delegation_agreed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lock_notice_agreed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expected_spend_14d_krw: Mapped[float] = mapped_column(Float, default=0.0)  # L* 계산용 (구독 엔진이 갱신)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Wallet(Base):
    __tablename__ = "wallets"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: uid("wal"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    address: Mapped[str] = mapped_column(String, unique=True)
    provider: Mapped[str] = mapped_column(String, default="sim")                # sim|local|privy
    provider_wallet_id: Mapped[str] = mapped_column(String, default="")         # Privy wallet id 등
    # 지갑 전체 잠금 (지침 0·1절). hold_payment_id 가 있으면 잠김.
    hold_payment_id: Mapped[str | None] = mapped_column(String, nullable=True)
    hold_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Card(Base):
    __tablename__ = "cards"
    token: Mapped[str] = mapped_column(String, primary_key=True)                # NFC UID 해시 등
    wallet_id: Mapped[str] = mapped_column(ForeignKey("wallets.id"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Merchant(Base):
    __tablename__ = "merchants"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: uid("mch"))
    name: Mapped[str] = mapped_column(String)
    bank_code: Mapped[str] = mapped_column(String, default="")
    account_num: Mapped[str] = mapped_column(String, default="")
    account_holder: Mapped[str] = mapped_column(String, default="")


# ── 결제 ─────────────────────────────────────────────────────────
class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: uid("pay"))
    checkout_id: Mapped[str] = mapped_column(String, unique=True)               # idempotency_key
    wallet_id: Mapped[str | None] = mapped_column(String, nullable=True)
    merchant_id: Mapped[str] = mapped_column(String)
    amount_krw: Mapped[float] = mapped_column(Float)
    required_krw: Mapped[float] = mapped_column(Float, default=0.0)            # H = A × (1 + MARGIN)
    wallet_total_krw: Mapped[float] = mapped_column(Float, default=0.0)
    usdkrw: Mapped[float] = mapped_column(Float, default=0.0)                  # 승인 시점 환율 (고정)
    target_usd: Mapped[float] = mapped_column(Float, default=0.0)              # 회수 목표 (A / 환율)

    auth_status: Mapped[str] = mapped_column(String)
    decline_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    hold_status: Mapped[str] = mapped_column(String, default="NONE")
    liq_status: Mapped[str] = mapped_column(String, default="PENDING")
    recovery_status: Mapped[str] = mapped_column(String, default="UNRECOVERED")
    payout_status: Mapped[str] = mapped_column(String, default="PENDING")

    collected_usd: Mapped[float] = mapped_column(Float, default=0.0)           # Treasury 로 들어온 금액
    reimbursed_usd: Mapped[float] = mapped_column(Float, default=0.0)          # 지갑에 남긴 차액
    loss_krw: Mapped[float] = mapped_column(Float, default=0.0)
    plan: Mapped[dict] = mapped_column(JSON, default=dict)
    auth_latency_ms: Mapped[float] = mapped_column(Float, default=0.0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    hold_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SwapLeg(Base):
    __tablename__ = "swap_legs"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: uid("leg"))
    payment_id: Mapped[str] = mapped_column(ForeignKey("payments.id"))
    seq: Mapped[int] = mapped_column(Integer)
    symbol: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String)                                   # swap|transfer
    sell_units: Mapped[float] = mapped_column(Float)                            # 토큰 수량
    planned_krw: Mapped[float] = mapped_column(Float)
    out_symbol: Mapped[str] = mapped_column(String, default="USDC")
    min_out: Mapped[float] = mapped_column(Float, default=0.0)
    received: Mapped[float] = mapped_column(Float, default=0.0)                 # 받은 USDC/USDT
    tx_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="PLANNED")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    is_alternate: Mapped[bool] = mapped_column(Boolean, default=False)


class MerchantPayout(Base):
    __tablename__ = "merchant_payouts"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: uid("po"))
    payment_id: Mapped[str] = mapped_column(String, unique=True)                # 결제당 1회 (멱등)
    merchant_id: Mapped[str] = mapped_column(String)
    amount_krw: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String, default="PENDING")
    bank_tran_id: Mapped[str | None] = mapped_column(String, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PaymentEvent(Base):
    __tablename__ = "payment_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    payment_id: Mapped[str] = mapped_column(String, index=True)
    type: Mapped[str] = mapped_column(String)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class DecisionLog(Base):
    """모든 결정의 입력·공식 버전·결과 (지침 알고리즘 원칙)."""
    __tablename__ = "decision_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String)                                   # sell_plan|limit_check|macro_update|sub_opt|llm_score
    ref_id: Mapped[str] = mapped_column(String, default="")
    formula_version: Mapped[str] = mapped_column(String)
    inputs: Mapped[dict] = mapped_column(JSON, default=dict)
    output: Mapped[dict] = mapped_column(JSON, default=dict)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Alert(Base):
    __tablename__ = "alerts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    level: Mapped[str] = mapped_column(String)                                  # info|warn|critical
    payment_id: Mapped[str | None] = mapped_column(String, nullable=True)
    user_id: Mapped[str | None] = mapped_column(String, nullable=True)
    audience: Mapped[str] = mapped_column(String, default="ops")                # ops|user
    message: Mapped[str] = mapped_column(String)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class DailySell(Base):
    """코인 1종당 하루 매도 상한(보유량 30%) 추적."""
    __tablename__ = "daily_sells"
    __table_args__ = (UniqueConstraint("wallet_id", "day", "symbol"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wallet_id: Mapped[str] = mapped_column(String)
    day: Mapped[str] = mapped_column(String)                                    # KST 날짜
    symbol: Mapped[str] = mapped_column(String)
    sold_units: Mapped[float] = mapped_column(Float, default=0.0)
    start_units: Mapped[float] = mapped_column(Float, default=0.0)              # 그날 첫 매도 직전 보유량


# ── 구독 · 동의 · 사용량 ─────────────────────────────────────────
class Consent(Base):
    __tablename__ = "consents"
    __table_args__ = (UniqueConstraint("user_id", "tier"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String)
    tier: Mapped[int] = mapped_column(Integer)                                  # 1 필수 / 2 사용시간 / 3 혜택·용량
    granted: Mapped[bool] = mapped_column(Boolean, default=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CardTransaction(Base):
    """결제 내역 (반복 결제 감지 입력). 우리 카드 결제 + 사용자가 연결한 다른 결제 내역."""
    __tablename__ = "card_transactions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String, index=True)
    merchant_name: Mapped[str] = mapped_column(String)
    amount_krw: Mapped[float] = mapped_column(Float)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (UniqueConstraint("user_id", "service"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: uid("sub"))
    user_id: Mapped[str] = mapped_column(String, index=True)
    service: Mapped[str] = mapped_column(String)                                # catalog 키 (netflix, youtube_premium …)
    plan: Mapped[str] = mapped_column(String)                                   # 현재 요금제 키
    share_count: Mapped[int] = mapped_column(Integer, default=1)                # m_s
    next_billing: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    value_per_unit: Mapped[float | None] = mapped_column(Float, nullable=True)  # v_s 개인 보정값
    sigma: Mapped[float | None] = mapped_column(Float, nullable=True)           # σ_s 개인 보정값
    snooze_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pending_change: Mapped[str | None] = mapped_column(String, nullable=True)  # 사용자가 수락한 변경 → 다음 달 검증
    pending_verify_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source: Mapped[str] = mapped_column(String, default="detected")


class UsageRecord(Base):
    __tablename__ = "usage_records"
    __table_args__ = (UniqueConstraint("user_id", "service", "month", "source"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String, index=True)
    service: Mapped[str] = mapped_column(String)
    month: Mapped[str] = mapped_column(String)                                  # YYYY-MM
    source: Mapped[str] = mapped_column(String)                                 # screen_time|spotify_api|netflix_csv|youtube_takeout|google_drive|manual
    hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    count: Mapped[float | None] = mapped_column(Float, nullable=True)
    benefit_krw: Mapped[float | None] = mapped_column(Float, nullable=True)
    storage_gb: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SubQuestion(Base):
    __tablename__ = "sub_questions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: uid("q"))
    user_id: Mapped[str] = mapped_column(String, index=True)
    subscription_id: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String)                                   # change_plan|idle_check|ask_usage
    proposed_plan: Mapped[str | None] = mapped_column(String, nullable=True)
    delta_krw: Mapped[float] = mapped_column(Float, default=0.0)
    message: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="pending")              # pending|answered|expired
    answer: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
