from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from .. import runtime
from ..assets import ASSETS
from ..chain.base import WalletRef
from ..config import DECISION_FORMULA_VERSION, settings
from ..models import Alert, DailySell, DecisionLog, PaymentEvent, User, Wallet

KST = timezone(timedelta(hours=9))


def kst_day(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).astimezone(KST).date().isoformat()


def event(s: Session, payment_id: str, type_: str, **data) -> None:
    s.add(PaymentEvent(payment_id=payment_id, type=str(getattr(type_, "value", type_)), data=data))


def decision(s: Session, kind: str, ref_id: str, inputs: dict, output: dict) -> None:
    s.add(DecisionLog(kind=kind, ref_id=ref_id, formula_version=DECISION_FORMULA_VERSION, inputs=inputs, output=output))


def alert(s: Session, level: str, message: str, payment_id: str | None = None,
          user_id: str | None = None, audience: str = "ops") -> None:
    s.add(Alert(level=level, message=message, payment_id=payment_id, user_id=user_id, audience=audience))


def wallet_ref(w: Wallet) -> WalletRef:
    return WalletRef(w.address, w.provider, w.provider_wallet_id)


async def balances(w: Wallet, max_age: float = 15.0) -> dict[str, float]:
    """잔고 캐시. 출금은 서비스가 통제하므로 캐시 사이에 줄어드는 일은 결제·출금뿐이고,
    둘 다 이 서비스가 처리한다. 입금(증가)만 늦게 보일 수 있다."""
    rt = runtime.rt
    hit = rt.balance_cache.get(w.id)
    if hit and time.time() - hit[1] < max_age:
        return hit[0]
    bal = await rt.chain.balances(wallet_ref(w))
    rt.balance_cache[w.id] = (bal, time.time())
    return bal


def invalidate(wallet_id: str) -> None:
    runtime.rt.balance_cache.pop(wallet_id, None)


def eligible_symbols(user: User, bal: dict[str, float]) -> list[str]:
    """결제에 쓸 코인: 가입 때 고른 코인 (없으면 지갑 안 전부). 스테이블도 같은 코인으로 취급."""
    allowed = {a.upper() for a in (user.pay_assets or [])}
    return [s for s in bal if s in ASSETS and (not allowed or s in allowed)]


def daily_caps(s: Session, wallet_id: str, bal: dict[str, float]) -> dict[str, float]:
    """코인별 오늘 남은 매도 가능 수량 (그날 첫 매도 직전 보유량의 30% − 오늘 판 수량). 스테이블은 무제한."""
    day = kst_day()
    rows = {r.symbol: r for r in s.query(DailySell).filter_by(wallet_id=wallet_id, day=day)}
    caps = {}
    for sym, units in bal.items():
        if sym in ASSETS and ASSETS[sym].is_stable:
            caps[sym] = float("inf")
            continue
        r = rows.get(sym)
        start = r.start_units if r else units
        sold = r.sold_units if r else 0.0
        caps[sym] = max(0.0, settings.daily_sell_cap * start - sold)
    return caps


def record_daily_sell(s: Session, wallet_id: str, sym: str, units: float, start_units: float) -> None:
    day = kst_day()
    r = s.query(DailySell).filter_by(wallet_id=wallet_id, day=day, symbol=sym).one_or_none()
    if r is None:
        r = DailySell(wallet_id=wallet_id, day=day, symbol=sym, sold_units=0.0, start_units=start_units)
        s.add(r)
    r.sold_units += units
