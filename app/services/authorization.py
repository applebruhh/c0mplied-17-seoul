"""동기 구간 (지침 결제흐름 1절): 승인 요청 → 한도 확인 → 지갑 잠금 → AUTHORIZED.

목표 ~1초. 네트워크는 타지 않는다 (시세는 메모리 캐시, 잔고는 캐시/1회 조회).
승인 직후 가맹점 원화 송금과 청산을 백그라운드로 띄운다.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from .. import runtime
from ..config import settings
from ..db import SessionLocal
from ..models import Card, Payment, User, Wallet
from ..states import AuthStatus, DeclineReason, Ev, HoldStatus, LiqStatus, RecoveryStatus
from .common import balances, decision, eligible_symbols, event


@dataclass
class AuthRequest:
    checkout_id: str               # = idempotency_key
    amount_krw: float
    merchant_id: str
    card_token: str | None = None
    wallet_id: str | None = None


@dataclass
class AuthResponse:
    payment_id: str
    checkout_id: str
    status: str
    reason: str | None
    amount_krw: float
    latency_ms: float
    idempotent_replay: bool = False

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _resp(p: Payment, replay: bool = False) -> AuthResponse:
    return AuthResponse(p.id, p.checkout_id, p.auth_status, p.decline_reason, p.amount_krw, p.auth_latency_ms, replay)


def _decline(s, req: AuthRequest, wallet_id: str | None, reason: DeclineReason, t0: float, **extra) -> AuthResponse:
    p = Payment(checkout_id=req.checkout_id, wallet_id=wallet_id, merchant_id=req.merchant_id, amount_krw=req.amount_krw,
                auth_status=AuthStatus.DECLINED.value, decline_reason=reason.value,
                hold_status=HoldStatus.NONE.value, liq_status="N/A", recovery_status="N/A", payout_status="N/A",
                **extra)
    p.auth_latency_ms = (time.perf_counter() - t0) * 1000
    s.add(p)
    s.flush()
    event(s, p.id, Ev.DECLINED, reason=reason.value, **{k: v for k, v in extra.items() if isinstance(v, (int, float))})
    s.commit()
    return _resp(p)


async def authorize(req: AuthRequest) -> AuthResponse:
    t0 = time.perf_counter()
    rt = runtime.rt
    s = SessionLocal()
    try:
        # 0) 멱등: 같은 checkout_id 는 같은 결과
        existing = s.query(Payment).filter_by(checkout_id=req.checkout_id).one_or_none()
        if existing:
            return _resp(existing, replay=True)

        # 1) 카드 → 지갑
        wallet_id = req.wallet_id
        if req.card_token:
            card = s.get(Card, req.card_token)
            wallet_id = card.wallet_id if card and card.active else None
        w = s.get(Wallet, wallet_id) if wallet_id else None
        if w is None:
            return _decline(s, req, wallet_id, DeclineReason.UNKNOWN_WALLET, t0)
        user = s.get(User, w.user_id)

        # 2) 이미 잠긴 지갑 → WALLET_BUSY (잠금 중에는 잔고가 바뀌는 중이라 한도 계산도 하지 않는다)
        if w.hold_payment_id:
            return _decline(s, req, w.id, DeclineReason.WALLET_BUSY, t0)

        # 3) 한도: 필요액 H = A × (1 + MARGIN) vs 지갑 총합 (모든 코인, 스테이블 포함, 현재가 원화 환산)
        bal = await balances(w)
        syms = eligible_symbols(user, bal)
        try:
            fx = rt.market.fx(settings.price_max_age_sec)
            values = {sym: bal[sym] * rt.market.price_krw(sym, settings.price_max_age_sec) for sym in syms}
        except (KeyError, TimeoutError) as e:
            return _decline(s, req, w.id, DeclineReason.PRICE_UNAVAILABLE, t0)
        total = sum(values.values())
        required = req.amount_krw * (1 + settings.margin)
        decision(s, "limit_check", req.checkout_id,
                 {"amount_krw": req.amount_krw, "margin": settings.margin, "balances": bal,
                  "values_krw": {k: round(v) for k, v in values.items()}, "usdkrw": fx},
                 {"required_krw": round(required), "wallet_total_krw": round(total), "ok": required <= total})
        if required > total:
            return _decline(s, req, w.id, DeclineReason.LIMIT_EXCEEDED, t0,
                            required_krw=required, wallet_total_krw=total)

        # 4) 지갑 전체 잠금 (CAS) + 결제 생성을 한 트랜잭션으로
        now = datetime.now(timezone.utc)
        p = Payment(checkout_id=req.checkout_id, wallet_id=w.id, merchant_id=req.merchant_id,
                    amount_krw=req.amount_krw, required_krw=required, wallet_total_krw=total,
                    usdkrw=fx, target_usd=req.amount_krw / fx,
                    auth_status=AuthStatus.AUTHORIZED.value, hold_status=HoldStatus.PLACED.value,
                    liq_status=LiqStatus.PENDING.value, recovery_status=RecoveryStatus.UNRECOVERED.value,
                    hold_expires_at=now + timedelta(seconds=settings.hold_ttl_sec))
        s.add(p)
        s.flush()
        locked = s.execute(update(Wallet).where(Wallet.id == w.id, Wallet.hold_payment_id.is_(None))
                           .values(hold_payment_id=p.id, hold_expires_at=p.hold_expires_at)).rowcount
        if locked != 1:
            s.rollback()
            return _decline(s, req, w.id, DeclineReason.WALLET_BUSY, t0)
        p.auth_latency_ms = (time.perf_counter() - t0) * 1000
        event(s, p.id, Ev.HOLD_PLACED, ttl_sec=settings.hold_ttl_sec)
        event(s, p.id, Ev.AUTHORIZED, required_krw=round(required), wallet_total_krw=round(total),
              latency_ms=round(p.auth_latency_ms, 1))
        s.commit()
    except IntegrityError:
        # 같은 checkout_id 가 동시에 들어온 경우: 먼저 들어간 결과를 돌려준다 (잠금은 롤백으로 풀림)
        s.rollback()
        return _resp(s.query(Payment).filter_by(checkout_id=req.checkout_id).one(), replay=True)
    finally:
        s.close()

    # 5) 비동기 구간 시작: 가맹점 원화 송금 + 청산
    from .liquidation import run_liquidation
    from .payout import run_payout
    rt.spawn(run_payout(p.id), f"payout:{p.id}")
    rt.spawn(run_liquidation(p.id), f"liq:{p.id}")
    return _resp(p)
