"""Customer app, terminal card enrollment and admin overview routes.

Demo scope: customer identity is an opaque ID kept by the phone (no password),
wallet balances are the mock engine's KRW-valued snapshots, and coin quantities
use fixed presentation prices. Nothing here moves real funds.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, Decimal
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import FastAPI, Header, Query
from pydantic import Field, field_validator
from sqlalchemy import func, select

from app.adapters.mock import WalletBusyError
from app.adapters.mock_fixtures import ASSETS, RANKINGS, STABLECOINS, USDC_KRW
from app.cards import enroll_external_card
from app.db import Database
from app.models import ContractModel
from app.services import APIError, POSService, payment_read
from app.signing import card_token_from_uid, normalize_uid
from app.tables import (
    AppProfile, CardEnrollment, Checkout, Customer, DemoCard, Merchant, MockCompanyCapacity, MockWallet,
    Payment, Terminal, WalletActivity,
)

Asset = Literal["BTC", "ETH", "USDC", "USDT"]
# Fixed presentation prices for showing coin quantities. Not market data.
DEMO_PRICES_KRW = {"BTC": 140_000_000, "ETH": 5_200_000, "USDC": USDC_KRW, "USDT": USDC_KRW}
ASSET_NAMES = {"BTC": "Bitcoin", "ETH": "Ethereum", "USDC": "USD Coin", "USDT": "Tether"}
ENROLLMENT_SECONDS = 120
MAX_DEPOSIT_KRW = 10_000_000


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class SignUp(ContractModel):
    name: Annotated[str, Field(min_length=1, max_length=40)]
    pay_assets: Annotated[list[Asset], Field(min_length=1, max_length=4)]
    risk_profile: Literal["conservative", "neutral", "aggressive"] = "neutral"
    delegation_agreed: bool
    lock_notice_agreed: bool

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str):
        value = value.strip()
        if not value:
            raise ValueError("Name is required")
        return value


class WalletMove(ContractModel):
    asset: Asset
    amount_krw: Annotated[int, Field(strict=True, ge=1, le=MAX_DEPOSIT_KRW)]
    address: Annotated[str | None, Field(max_length=120)] = None


class EnrollmentCreate(ContractModel):
    terminal_id: Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")] = "cafe-01"


class CardUid(ContractModel):
    uid: Annotated[str, Field(min_length=2, max_length=120)]


def _mask(alias: str) -> str:
    return alias if len(alias) <= 6 else f"{alias[:4]}…{alias[-4:]}"


def _coin(asset: str, value_krw: int) -> str:
    quantity = Decimal(value_krw) / Decimal(DEMO_PRICES_KRW[asset])
    places = Decimal("0.01") if asset in STABLECOINS else Decimal("0.000001")
    return str(quantity.quantize(places))


def register_customer_app(app: FastAPI, database: Database, service: POSService, adapter, ops_access: list):
    simulated = adapter.source == "simulated"

    def customer(session, customer_id: str) -> Customer:
        row = session.get(Customer, customer_id)
        if row is None:
            raise APIError(404, "UNKNOWN_CUSTOMER", "가입 정보를 찾을 수 없어요. 다시 가입해 주세요.")
        return row

    def wallet(session, customer_id: str) -> MockWallet | None:
        if not simulated:
            return None
        row = session.scalar(select(MockWallet).where(MockWallet.customer_id == customer_id))
        if row is None:
            # A demo reset removes wallets; recreate the customer's empty wallet.
            empty = {asset: 0 for asset in ASSETS}
            row = MockWallet(wallet_id=f"wallet-{customer_id}", customer_id=customer_id, holdings=empty,
                             lock_checkout_id=None, hold_expires_at=None,
                             sell_day=utcnow().date().isoformat(), daily_basis=dict(empty), daily_sold={})
            session.add(row)
            session.flush()
        return row

    def sellable(row: MockWallet, asset: str) -> int:
        value = row.holdings.get(asset, 0)
        if asset in STABLECOINS:
            return value
        cap = int(Decimal(row.daily_basis.get(asset, 0)) * Decimal("0.30"))
        return max(0, min(value, cap - row.daily_sold.get(asset, 0)))

    def wallet_view(session, customer_id: str):
        row = wallet(session, customer_id)
        if row is None:
            return {"mode": "external", "wallet_id": None, "total_krw": None, "assets": [], "locked": False}
        assets = [{
            "asset": asset, "name": ASSET_NAMES[asset], "value_krw": row.holdings.get(asset, 0),
            "quantity": _coin(asset, row.holdings.get(asset, 0)), "price_krw": DEMO_PRICES_KRW[asset],
            "sellable_today_krw": sellable(row, asset), "stablecoin": asset in STABLECOINS,
        } for asset in ASSETS]
        lock_checkout = session.get(Checkout, row.lock_checkout_id) if row.lock_checkout_id else None
        return {
            "mode": "simulated", "wallet_id": row.wallet_id,
            "address": "0x" + card_token_from_uid(row.wallet_id.encode().hex())[:40],
            "total_krw": sum(item["value_krw"] for item in assets),
            "sellable_today_krw": sum(item["sellable_today_krw"] for item in assets),
            "assets": assets, "locked": row.lock_checkout_id is not None,
            "lock_checkout_id": row.lock_checkout_id,
            "lock_amount_krw": lock_checkout.amount_krw if lock_checkout else None,
            "hold_expires_at": aware(row.hold_expires_at),
            "prices_note": "데모 고정 시세 기준 환산입니다.",
        }

    def cards(session, customer_id: str):
        rows = session.scalars(select(DemoCard).where(DemoCard.customer_id == customer_id,
                                                       DemoCard.enabled.is_(True))).all()
        result = []
        for row in rows:
            try:
                token = card_token_from_uid(row.card_alias)
            except ValueError:
                token = None
            result.append({"card_alias": row.card_alias, "masked": _mask(row.card_alias), "card_token": token,
                           "kind": "nfc" if token else "demo"})
        return result

    def profile(session, customer_id: str):
        row = customer(session, customer_id)
        extra = session.get(AppProfile, customer_id)
        return {
            "customer_id": customer_id, "name": row.display_name,
            "pay_assets": extra.pay_assets if extra else list(ASSETS),
            "risk_profile": extra.risk_profile if extra else "neutral",
            "created_at": aware(extra.created_at) if extra else None,
            "engine_mode": adapter.source,
        }

    def me(customer_id: str):
        with database.transaction() as session:
            return {"profile": profile(session, customer_id), "wallet": wallet_view(session, customer_id),
                    "cards": cards(session, customer_id)}

    @app.post("/api/app/users", status_code=201, tags=["Customer app"])
    def sign_up(body: SignUp):
        if not (body.delegation_agreed and body.lock_notice_agreed):
            raise APIError(422, "CONSENT_REQUIRED", "필수 동의 2개에 모두 체크해 주세요.")
        customer_id = f"cust-{uuid4().hex[:12]}"
        with database.transaction() as session:
            session.add(Customer(customer_id=customer_id, display_name=body.name))
            session.flush()
            session.add(AppProfile(customer_id=customer_id, pay_assets=list(dict.fromkeys(body.pay_assets)),
                                   risk_profile=body.risk_profile, delegation_agreed=True,
                                   lock_notice_agreed=True, created_at=utcnow()))
            wallet(session, customer_id)
        return me(customer_id)

    @app.get("/api/app/demo-accounts", tags=["Customer app"])
    def demo_accounts():
        """Existing customers with a registered NFC card, for joining an existing demo setup."""
        with database.transaction(write=False) as session:
            rows = session.execute(
                select(Customer.customer_id, Customer.display_name, func.count(DemoCard.card_alias))
                .join(DemoCard, DemoCard.customer_id == Customer.customer_id)
                .where(DemoCard.enabled.is_(True)).group_by(Customer.customer_id)
            ).all()
            accounts = []
            for customer_id, name, _ in rows:
                aliases = session.scalars(select(DemoCard.card_alias).where(DemoCard.customer_id == customer_id)).all()
                if customer_id == "customer-success" or customer_id.startswith("cust-") or any(
                        _is_uid(alias) for alias in aliases):
                    accounts.append({"customer_id": customer_id, "name": name,
                                     "cards": [_mask(alias) for alias in aliases if _is_uid(alias)]})
            return {"items": accounts}

    @app.get("/api/app/users/{customer_id}", tags=["Customer app"])
    def get_me(customer_id: str):
        return me(customer_id)

    @app.post("/api/app/users/{customer_id}/deposits", tags=["Customer app"])
    def deposit(customer_id: str, body: WalletMove, idempotency_key: Annotated[UUID, Header()]):
        if not simulated:
            raise APIError(409, "EXTERNAL_WALLET", "외부 엔진 모드에서는 엔진의 임베디드 지갑으로 직접 입금해야 해요.")
        with database.transaction() as session:
            customer(session, customer_id)
            wallet(session, customer_id)
        try:
            adapter.deposit(customer_id, body.asset, body.amount_krw, f"app-deposit-{idempotency_key}")
        except ValueError as error:
            raise APIError(409, "DEPOSIT_REJECTED", str(error)) from None
        with database.transaction() as session:
            if session.get(WalletActivity, str(idempotency_key)) is None:
                session.add(WalletActivity(activity_id=str(idempotency_key), customer_id=customer_id, kind="DEPOSIT",
                                           asset=body.asset, amount_krw=body.amount_krw, address=None,
                                           created_at=utcnow()))
        return me(customer_id)

    @app.post("/api/app/users/{customer_id}/withdrawals", tags=["Customer app"])
    def withdraw(customer_id: str, body: WalletMove, idempotency_key: Annotated[UUID, Header()]):
        if not simulated:
            raise APIError(409, "EXTERNAL_WALLET", "외부 엔진 모드에서는 엔진 출금 API를 이용해야 해요.")
        with database.transaction() as session:
            customer(session, customer_id)
            wallet(session, customer_id)
        try:
            adapter.withdraw(customer_id, body.asset, body.amount_krw, f"app-withdraw-{idempotency_key}")
        except WalletBusyError:
            raise APIError(409, "WALLET_LOCKED", "결제 정산 중이라 지갑이 잠겨 있어요. 정산이 끝나면 다시 시도해 주세요.") from None
        except ValueError as error:
            message = "잔액이 부족해요." if "Insufficient" in str(error) else str(error)
            raise APIError(409, "WITHDRAWAL_REJECTED", message) from None
        with database.transaction() as session:
            if session.get(WalletActivity, str(idempotency_key)) is None:
                session.add(WalletActivity(activity_id=str(idempotency_key), customer_id=customer_id,
                                           kind="WITHDRAWAL", asset=body.asset, amount_krw=body.amount_krw,
                                           address=body.address, created_at=utcnow()))
        return me(customer_id)

    @app.get("/api/app/users/{customer_id}/sell-preview", tags=["Customer app"])
    def sell_preview(customer_id: str, amount_krw: Annotated[int, Query(ge=1, le=10_000_000)]):
        if not simulated:
            raise APIError(409, "EXTERNAL_WALLET", "외부 엔진 모드에서는 엔진의 매도 미리보기를 사용해야 해요.")
        with database.transaction() as session:
            customer(session, customer_id)
            row = wallet(session, customer_id)
            margin = Decimal(str(getattr(adapter, "margin", Decimal("0.03"))))
            required = int((Decimal(amount_krw) * (1 + margin)).to_integral_value(rounding=ROUND_CEILING))
            total = sum(row.holdings.values())
            remaining, legs = required, []
            for asset in [item["asset"] for item in sorted(RANKINGS, key=lambda item: item["rank"])]:
                if remaining <= 0 or len(legs) >= 3:
                    break
                value = min(remaining, sellable(row, asset))
                if value > 0:
                    legs.append({"asset": asset, "value_krw": value, "quantity": _coin(asset, value)})
                    remaining -= value
            status = "LIMIT_EXCEEDED" if total < required else "OK" if remaining <= 0 else "EXECUTION_LIMITED"
            return {"amount_krw": amount_krw, "required_krw": required, "margin": str(margin), "total_krw": total,
                    "status": status, "legs": legs, "locked": row.lock_checkout_id is not None}

    @app.get("/api/app/users/{customer_id}/activity", tags=["Customer app"])
    def activity(customer_id: str, limit: Annotated[int, Query(ge=1, le=100)] = 50):
        with database.transaction(write=False) as session:
            customer(session, customer_id)
            items = []
            rows = session.execute(
                select(Payment, Checkout, Merchant).join(Checkout, Checkout.checkout_id == Payment.checkout_id)
                .join(Merchant, Merchant.merchant_id == Checkout.merchant_id)
                .where(Payment.customer_id == customer_id).order_by(Payment.created_at.desc()).limit(limit)
            ).all()
            for payment, checkout, merchant in rows:
                detail = payment_read(session, payment)
                submissions = (detail.mock_asset_metadata or {}).get("submissions", [])
                legs = [{"asset": leg["asset"], "value_krw": leg["input_krw"],
                         "quantity": _coin(leg["asset"], leg["input_krw"])}
                        for leg in submissions if leg.get("status") == "CONFIRMED"]
                items.append({
                    "kind": "PAYMENT", "id": payment.payment_id, "checkout_id": checkout.checkout_id,
                    "merchant": merchant.display_name, "amount_krw": checkout.amount_krw,
                    "status": checkout.status.value if hasattr(checkout.status, "value") else checkout.status,
                    "authorization_status": detail.authorization_status, "reason_code": detail.reason_code,
                    "hold_status": detail.hold_status, "liquidation_status": detail.liquidation_status,
                    "settlement_status": detail.settlement_status, "recovery_status": detail.recovery_status,
                    "sold": legs, "sold_krw": sum(leg["value_krw"] for leg in legs),
                    "excess_returned_usdc": detail.excess_returned_usdc,
                    "excess_returned_krw": (int(Decimal(detail.excess_returned_usdc) * USDC_KRW)
                                            if detail.excess_returned_usdc else 0),
                    "created_at": aware(payment.created_at), "financial_status_reported": detail.financial_status_reported,
                })
            for row in session.scalars(select(WalletActivity).where(WalletActivity.customer_id == customer_id)
                                       .order_by(WalletActivity.created_at.desc()).limit(limit)):
                items.append({"kind": row.kind, "id": row.activity_id, "asset": row.asset,
                              "amount_krw": row.amount_krw, "quantity": _coin(row.asset, row.amount_krw),
                              "address": row.address, "created_at": aware(row.created_at)})
            items.sort(key=lambda item: item["created_at"], reverse=True)
            return {"items": items[:limit]}

    # ---- NFC card registration -------------------------------------------------

    def bind_card(session, customer_id: str, raw_uid: str) -> tuple[str, str]:
        try:
            uid = normalize_uid(raw_uid)
        except ValueError:
            raise APIError(422, "INVALID_UID", "카드 UID 형식이 올바르지 않아요.") from None
        if len(uid) > 100:
            raise APIError(422, "INVALID_UID", "카드 UID가 너무 길어요.")
        token = card_token_from_uid(uid)
        customer(session, customer_id)
        if simulated:
            card = session.get(DemoCard, uid)
            if card is None:
                session.add(DemoCard(card_alias=uid, customer_id=customer_id, enabled=True))
            elif card.customer_id != customer_id:
                # Demo cards are reassigned to whoever registers them last.
                card.customer_id, card.enabled = customer_id, True
            else:
                card.enabled = True
        else:
            try:
                enroll_external_card(session, card_alias=uid, uid=uid, customer_id=customer_id)
            except ValueError:
                raise APIError(409, "CARD_IN_USE", "이 카드는 이미 다른 고객에게 등록돼 있어요.") from None
        session.flush()
        return uid, token

    def enrollment_view(row: CardEnrollment):
        return {"enrollment_id": row.enrollment_id, "customer_id": row.customer_id, "terminal_id": row.terminal_id,
                "status": row.status, "card_alias": row.card_alias,
                "masked": _mask(row.card_alias) if row.card_alias else None, "card_token": row.card_token,
                "error": row.error, "created_at": aware(row.created_at), "expires_at": aware(row.expires_at),
                "engine_registration_required": adapter.source == "external"}

    def expire_enrollments(session):
        now = utcnow()
        for row in session.scalars(select(CardEnrollment).where(CardEnrollment.status == "PENDING")):
            if aware(row.expires_at) <= now:
                row.status = "EXPIRED"

    @app.post("/api/app/users/{customer_id}/cards", tags=["Customer app"])
    def register_card(customer_id: str, body: CardUid):
        with database.transaction() as session:
            uid, token = bind_card(session, customer_id, body.uid)
        return {"card_alias": uid, "masked": _mask(uid), "card_token": token,
                "engine_registration_required": adapter.source == "external", **me(customer_id)}

    @app.delete("/api/app/users/{customer_id}/cards/{card_alias}", tags=["Customer app"])
    def remove_card(customer_id: str, card_alias: str):
        with database.transaction() as session:
            card = session.get(DemoCard, card_alias)
            if card is None or card.customer_id != customer_id:
                raise APIError(404, "UNKNOWN_CARD", "등록된 카드가 아니에요.")
            card.enabled = False
        return me(customer_id)

    @app.post("/api/app/users/{customer_id}/card-enrollments", status_code=201, tags=["Customer app"])
    def start_enrollment(customer_id: str, body: EnrollmentCreate):
        with database.transaction() as session:
            customer(session, customer_id)
            if session.get(Terminal, body.terminal_id) is None:
                raise APIError(404, "UNKNOWN_TERMINAL", "등록할 단말기를 찾을 수 없어요.")
            expire_enrollments(session)
            for row in session.scalars(select(CardEnrollment).where(
                    CardEnrollment.terminal_id == body.terminal_id, CardEnrollment.status == "PENDING")):
                row.status, row.error = "CANCELLED", "새 등록 요청으로 대체됨"
            now = utcnow()
            row = CardEnrollment(enrollment_id=str(uuid4()), customer_id=customer_id, terminal_id=body.terminal_id,
                                 status="PENDING", card_alias=None, card_token=None, error=None, created_at=now,
                                 expires_at=now + timedelta(seconds=ENROLLMENT_SECONDS))
            session.add(row)
            session.flush()
            return enrollment_view(row)

    @app.get("/api/app/card-enrollments/{enrollment_id}", tags=["Customer app"])
    def get_enrollment(enrollment_id: UUID):
        with database.transaction() as session:
            expire_enrollments(session)
            row = session.get(CardEnrollment, str(enrollment_id))
            if row is None:
                raise APIError(404, "UNKNOWN_ENROLLMENT", "카드 등록 요청을 찾을 수 없어요.")
            return enrollment_view(row)

    @app.post("/api/app/card-enrollments/{enrollment_id}/cancel", tags=["Customer app"])
    def cancel_enrollment(enrollment_id: UUID):
        with database.transaction() as session:
            row = session.get(CardEnrollment, str(enrollment_id))
            if row is None:
                raise APIError(404, "UNKNOWN_ENROLLMENT", "카드 등록 요청을 찾을 수 없어요.")
            if row.status == "PENDING":
                row.status = "CANCELLED"
            return enrollment_view(row)

    @app.get("/api/terminals/{terminal_id}/pending-enrollment", tags=["NFC"])
    def pending_enrollment(terminal_id: str):
        with database.transaction() as session:
            expire_enrollments(session)
            row = session.scalar(select(CardEnrollment).where(
                CardEnrollment.terminal_id == terminal_id, CardEnrollment.status == "PENDING",
            ).order_by(CardEnrollment.created_at.desc()))
            if row is None:
                return {"enrollment": None}
            view = enrollment_view(row)
            view["customer_name"] = session.get(Customer, row.customer_id).display_name
            return {"enrollment": view}

    @app.post("/api/terminals/{terminal_id}/enrollments/{enrollment_id}/complete", tags=["NFC"])
    def complete_enrollment(terminal_id: str, enrollment_id: UUID, body: CardUid):
        with database.transaction() as session:
            expire_enrollments(session)
            row = session.get(CardEnrollment, str(enrollment_id))
            if row is None or row.terminal_id != terminal_id:
                raise APIError(404, "UNKNOWN_ENROLLMENT", "이 단말기의 카드 등록 요청이 아니에요.")
            if row.status == "COMPLETED":
                if row.card_alias == normalize_uid(body.uid):
                    return enrollment_view(row)
                raise APIError(409, "ENROLLMENT_FINISHED", "이미 다른 카드로 등록이 끝났어요.")
            if row.status != "PENDING":
                raise APIError(409, "ENROLLMENT_FINISHED", "카드 등록 요청이 만료됐거나 취소됐어요.")
            try:
                uid, token = bind_card(session, row.customer_id, body.uid)
            except APIError as error:
                if error.code == "INVALID_UID":
                    raise
                row.status, row.error = "FAILED", error.message
                return enrollment_view(row)
            row.status, row.card_alias, row.card_token = "COMPLETED", uid, token
            return enrollment_view(row)

    # ---- Admin overview ---------------------------------------------------------

    @app.get("/api/ops/overview", tags=["Operations"], dependencies=ops_access)
    def overview():
        with database.transaction(write=False) as session:
            day_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=9)
            if utcnow() - day_start >= timedelta(days=1):
                day_start += timedelta(days=1)

            def count(*where):
                return session.scalar(select(func.count(Payment.payment_id)).where(*where)) or 0

            approved_today = session.scalar(
                select(func.coalesce(func.sum(Checkout.amount_krw), 0))
                .join(Payment, Payment.checkout_id == Checkout.checkout_id)
                .where(Payment.authorization_status == "AUTHORIZED", Payment.created_at >= day_start.replace(tzinfo=None))
            ) or 0
            company = session.get(MockCompanyCapacity, "demo")
            wallets = []
            for row in session.scalars(select(MockWallet)):
                person = session.get(Customer, row.customer_id)
                card_count = session.scalar(select(func.count(DemoCard.card_alias)).where(
                    DemoCard.customer_id == row.customer_id, DemoCard.enabled.is_(True))) or 0
                wallets.append({"wallet_id": row.wallet_id, "customer_id": row.customer_id,
                                "name": person.display_name if person else row.customer_id,
                                "app_user": row.customer_id.startswith("cust-"),
                                "total_krw": sum(row.holdings.values()), "holdings_krw": dict(row.holdings),
                                "locked": row.lock_checkout_id is not None, "lock_checkout_id": row.lock_checkout_id,
                                "cards": card_count})
            wallets.sort(key=lambda item: (not item["app_user"], not item["locked"], item["name"]))
            terminals = []
            for row in session.scalars(select(Terminal)):
                active = session.scalar(select(Checkout).where(
                    Checkout.terminal_id == row.terminal_id, Checkout.status.in_(("WAITING_FOR_CARD", "AUTHORIZING"))))
                terminals.append({"terminal_id": row.terminal_id, "merchant_id": row.merchant_id,
                                  "active_checkout": active.checkout_id if active else None,
                                  "active_amount_krw": active.amount_krw if active else None})
            return {
                "engine_mode": adapter.source,
                "payments_total": count(),
                "authorized_total": count(Payment.authorization_status == "AUTHORIZED"),
                "declined_total": count(Payment.authorization_status == "DECLINED"),
                "pending_authorization": count(Payment.authorization_status.in_(("PENDING", "UNKNOWN"))),
                "settlement_pending": count(Payment.settlement_status == "PENDING"),
                "settlement_completed": count(Payment.settlement_status == "COMPLETED"),
                "recovery_required": count(Payment.settlement_status == "RECOVERY_REQUIRED"),
                "approved_today_krw": int(approved_today),
                "company_capacity_krw": company.available_krw if company else None,
                "locked_wallets": sum(1 for item in wallets if item["locked"]),
                "wallets": wallets[:50], "terminals": terminals,
                "customers_total": session.scalar(select(func.count(Customer.customer_id))) or 0,
            }


def _is_uid(alias: str) -> bool:
    try:
        normalize_uid(alias)
        return len(alias) >= 8
    except ValueError:
        return False
