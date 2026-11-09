"""비동기 구간 (지침 결제흐름 2·3절): 청산 → USDC 회수 → 차액 반환 → 잠금 해제.

1. 점수(S = −MC) 순으로 매도 계획 (algo.sell). 매도 상한 = 필요액 H = A × (1 + MARGIN).
2. 코인 → USDC 스왑 (0x / sim). 스왑 1건당 재시도 2회, 결제당 스왑 3건, 오라클 대비 슬리피지 0.5%.
   실패한 코인은 빼고 남은 금액을 다시 계획 (대체 코인) — 스왑 건수 한도 안에서.
3. 스왑 대금(USDC) + 계획된 스테이블을 회사 Treasury 로 송금. 목표 = 결제액 A / 승인 시점 환율.
4. 여유분으로 더 팔린 USDC 는 고객 지갑에 그대로 남긴다 (= 차액 반환).
5. 부족분은 회사가 흡수 (LOSS_ABSORBED). POS 승인 결과는 절대 바꾸지 않는다.
TTL: 스왑이 하나도 제출되지 않은 채 HOLD_TTL 이 지나면 HOLD_EXPIRED + 실패.
     스왑이 제출(또는 체결)됐으면 TTL 이 지나도 잠금을 유지한다.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import update

from .. import runtime
from ..algo.sell import Holding, SellContext
from ..assets import ASSETS
from ..chain.base import SwapError
from ..config import settings
from ..db import SessionLocal
from ..models import Payment, SwapLeg, User, Wallet
from ..states import Ev, HoldStatus, LegStatus, LiqStatus, RecoveryStatus
from .common import (alert, balances, daily_caps, decision, eligible_symbols, event, invalidate,
                     record_daily_sell, wallet_ref)

log = logging.getLogger("liquidation")
_locks: dict[str, asyncio.Lock] = {}
TOL_USD = 0.01


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


class Abort(Exception):
    pass


async def run_liquidation(payment_id: str) -> None:
    lock = _locks.setdefault(payment_id, asyncio.Lock())
    async with lock:
        try:
            await _run(payment_id)
        except Abort as e:
            log.info("liquidation %s aborted: %s", payment_id, e)
        except Exception as e:  # noqa: BLE001
            log.exception("liquidation %s crashed", payment_id)
            _crash(payment_id, repr(e))
        finally:
            _locks.pop(payment_id, None)


# ── 제출 전 확인: 잠금이 살아 있어야만 트랜잭션을 낸다 ─────────────
def _claim_submit(s, p: Payment, leg: SwapLeg, count_attempt: bool = False) -> None:
    s.refresh(p)
    if p.hold_status != HoldStatus.PLACED.value:
        raise Abort(f"hold {p.hold_status}")
    submitted_any = s.query(SwapLeg).filter(SwapLeg.payment_id == p.id,
                                            SwapLeg.status.in_([LegStatus.SUBMITTED.value, LegStatus.CONFIRMED.value])).count()
    if not submitted_any and _aware(p.hold_expires_at) < datetime.now(timezone.utc):
        s.commit()
        expire_one(p.id)
        raise Abort("TTL 만료 (제출 전)")
    leg.status = LegStatus.SUBMITTED.value
    if count_attempt:
        leg.attempts += 1
    s.commit()


async def _exec_swap(s, p: Payment, w: Wallet, leg: SwapLeg) -> bool:
    rt = runtime.rt
    wref = wallet_ref(w)
    for _ in range(1 + settings.swap_retries):
        leg.attempts += 1
        try:
            q = await rt.chain.quote(wref, leg.symbol, leg.out_symbol, leg.sell_units, settings.max_slippage)
            oracle_out = leg.sell_units * rt.market.price_usd(leg.symbol) / rt.market.price_usd(leg.out_symbol)
            dev = 1 - q.buy_units / oracle_out
            fee = ASSETS[leg.symbol].fee
            if dev > settings.max_slippage + fee:
                raise SwapError(f"오라클 대비 {dev:.2%} 손실 (한도 {settings.max_slippage:.2%}+수수료 {fee:.2%})")
            leg.min_out = q.min_buy_units
            _claim_submit(s, p, leg)
            tx = await rt.chain.swap(wref, q)
            leg.tx_hash = tx.tx_hash
            event(s, p.id, Ev.SWAP_SUBMITTED, leg=leg.id, symbol=leg.symbol, units=leg.sell_units,
                  expected_out=q.buy_units, min_out=q.min_buy_units, tx=tx.tx_hash)
            s.commit()
            rc = await rt.chain.wait(tx, wref, leg.out_symbol)
            if not rc.success:
                raise SwapError(rc.error or "tx failed")
            leg.received = rc.received.get(leg.out_symbol, 0.0)
            leg.status = LegStatus.CONFIRMED.value
            event(s, p.id, Ev.SWAP_CONFIRMED, leg=leg.id, symbol=leg.symbol, received=leg.received, tx=tx.tx_hash)
            s.commit()
            return True
        except SwapError as e:
            leg.error = str(e)[:300]
            if leg.status == LegStatus.SUBMITTED.value:
                leg.status = LegStatus.PLANNED.value     # 리버트 → 제출 중 아님
            event(s, p.id, Ev.SWAP_FAILED, leg=leg.id, symbol=leg.symbol, error=leg.error, retryable=e.retryable)
            s.commit()
            if not e.retryable:
                break
            await asyncio.sleep(0.2)
    leg.status = LegStatus.FAILED.value
    s.commit()
    return False


def _make_legs(s, p: Payment, plan, prices: dict[str, float], bal: dict[str, float], seq0: int, alternate: bool) -> list[SwapLeg]:
    rt = runtime.rt
    legs = []
    for i, (sym, krw) in enumerate(plan.legs()):
        units = min(bal.get(sym, 0.0), krw / prices[sym])
        if units <= 0:
            continue
        kind = "swap" if rt.optimizer._is_swap(sym) else "transfer"
        leg = SwapLeg(payment_id=p.id, seq=seq0 + i, symbol=sym, kind=kind, sell_units=units, planned_krw=krw,
                      out_symbol="USDC" if kind == "swap" else sym, is_alternate=alternate)
        s.add(leg)
        legs.append(leg)
    s.commit()
    return legs


async def _run(pid: str) -> None:
    rt = runtime.rt
    s = SessionLocal()
    try:
        p = s.get(Payment, pid)
        if not p or p.auth_status != "AUTHORIZED" or p.hold_status != HoldStatus.PLACED.value \
                or p.liq_status in (LiqStatus.COMPLETED.value, LiqStatus.FAILED.value):
            return
        w = s.get(Wallet, p.wallet_id)
        user = s.get(User, w.user_id)
        wref = wallet_ref(w)
        p.liq_status = LiqStatus.IN_PROGRESS.value
        event(s, pid, Ev.LIQUIDATION_STARTED)
        s.commit()

        # ── 1) 계획 ────────────────────────────────────────────
        invalidate(w.id)
        bal = await balances(w, max_age=0)
        syms = eligible_symbols(user, bal)
        prices = {sym: rt.market.price_krw(sym) for sym in syms}
        caps = daily_caps(s, w.id, bal)
        start_units = dict(bal)
        holdings = [Holding(sym, bal[sym], prices[sym], cap_krw=caps[sym] * prices[sym]) for sym in syms]
        ctx = SellContext(gamma0=settings.gamma0.get(user.risk_profile, 2.0),
                          expected_spend_14d_krw=user.expected_spend_14d_krw)
        A = p.amount_krw
        first = rt.optimizer.plan(holdings, A, ctx, allowed=set(syms))
        est_cost = sum(x * (first.diags[sym].cost + x / (2 * first.diags[sym].depth_krw)) for sym, x in first.legs())
        target = min(p.required_krw, A + est_cost)            # 팔 코인의 상한 = 필요액 H
        plan = rt.optimizer.plan(holdings, target, ctx, allowed=set(syms))
        p.plan = plan.to_dict()
        decision(s, "sell_plan", pid,
                 {"amount_krw": A, "target_krw": target, "holdings": {h.symbol: h.units for h in holdings},
                  "prices_krw": prices, "caps_units": {k: (v if v != float("inf") else None) for k, v in caps.items()},
                  "gamma0": ctx.gamma0, "market": {k: v for k, v in rt.market.snapshot().items() if k != "coins"}},
                 plan.to_dict())
        for sym, why in plan.excluded.items():
            if why == "depeg_guard":
                event(s, pid, Ev.STABLE_DEPEG_GUARD, symbol=sym, price=rt.market.price_usd(sym))
                alert(s, "warn", f"{sym} 가격이 1달러에서 2% 이상 벗어나 결제 매도에서 제외했어요", pid, user.id, "user")
        if plan.cap_relaxed:
            event(s, pid, Ev.DAILY_CAP_RELAXED)
            alert(s, "info", "하루 매도 상한(30%) 안에서 결제액을 채울 수 없어 상한을 풀고 매도", pid)
        s.commit()

        legs = _make_legs(s, p, plan, prices, bal, 0, False)
        swap_queue = [l for l in legs if l.kind == "swap"]
        transfer_legs = [l for l in legs if l.kind == "transfer"]

        # ── 2) 스왑 실행 (+대체 코인) ──────────────────────────
        failed: set[str] = set()
        attempted = 0
        proceeds = 0.0
        i = 0
        while i < len(swap_queue):
            leg = swap_queue[i]
            i += 1
            attempted += 1
            if await _exec_swap(s, p, w, leg):
                proceeds += leg.received
                record_daily_sell(s, w.id, leg.symbol, leg.sell_units, start_units.get(leg.symbol, leg.sell_units))
                s.commit()
                continue
            failed.add(leg.symbol)
            budget = settings.max_swaps_per_payment - attempted
            if budget <= 0:
                continue
            # 남은 계획(아직 안 한 스왑, 스테이블 송금분)은 빼고 대체 계획
            invalidate(w.id)
            bal_now = await balances(w, max_age=0)
            reserved: dict[str, float] = {}
            for l in swap_queue[i:] + transfer_legs:
                reserved[l.symbol] = reserved.get(l.symbol, 0.0) + l.sell_units
            caps_now = daily_caps(s, w.id, bal_now)
            alt_hold = []
            for sym in syms:
                if sym in failed:
                    continue
                u = bal_now.get(sym, 0.0) - reserved.get(sym, 0.0)
                cap_u = caps_now.get(sym, 0.0) - reserved.get(sym, 0.0)   # 예약된 매도분도 오늘 상한에서 뺀다
                if u > 0:
                    alt_hold.append(Holding(sym, u, prices[sym], cap_krw=max(0.0, cap_u) * prices[sym]))
            sold_krw = sum(l.planned_krw for l in swap_queue if l.status != LegStatus.FAILED.value) \
                + sum(l.planned_krw for l in transfer_legs)
            alt_target = min(leg.planned_krw * (1 + settings.max_slippage + 0.003), max(0.0, p.required_krw - sold_krw))
            alt = rt.optimizer.plan(alt_hold, alt_target, ctx, allowed={h.symbol for h in alt_hold},
                                    max_swap_assets=budget)
            decision(s, "sell_plan_alternate", pid, {"failed": sorted(failed), "amount_krw": leg.planned_krw}, alt.to_dict())
            new = _make_legs(s, p, alt, prices, {h.symbol: h.units for h in alt_hold}, len(legs) + i, True)
            swap_queue.extend(l for l in new if l.kind == "swap")
            transfer_legs.extend(l for l in new if l.kind == "transfer")

        # ── 3) Treasury 송금: 스왑 대금 먼저, 그다음 계획된 스테이블 ───────
        remaining = p.target_usd
        from_proceeds = min(proceeds, remaining)
        remaining -= from_proceeds
        send = {"USDC": from_proceeds, "USDT": 0.0}
        used_legs: list[tuple[SwapLeg, float]] = []
        for leg in transfer_legs:
            if remaining <= TOL_USD:
                leg.status, leg.error = LegStatus.FAILED.value, "not needed"
                continue
            px = 1.0 if leg.symbol == "USDC" else rt.market.price_usd(leg.symbol)
            units = min(leg.sell_units, remaining / px)
            send[leg.symbol] = send.get(leg.symbol, 0.0) + units
            remaining -= units * px
            used_legs.append((leg, units))
        # 슬리피지로 조금 모자라면, 필요액 H 안에서 지갑의 남은 스테이블로 채운다 (스왑 건수 추가 없음)
        if remaining > TOL_USD:
            spent_krw = sum(l.planned_krw for l in swap_queue if l.status == LegStatus.CONFIRMED.value) \
                + sum(u * rt.market.price_krw(l.symbol) for l, u in used_legs)
            room_usd = max(0.0, (p.required_krw - spent_krw) / p.usdkrw)
            invalidate(w.id)
            bal_now = await balances(w, max_age=0)
            for sym in ("USDC", "USDT"):
                if remaining <= TOL_USD or room_usd <= 0 or sym not in syms or sym in plan.excluded:
                    continue
                if sym == "USDT" and not settings.treasury_accepts_usdt:
                    continue
                px = 1.0 if sym == "USDC" else rt.market.price_usd(sym)
                free = bal_now.get(sym, 0.0) - send.get(sym, 0.0) - (proceeds if sym == "USDC" else 0.0) \
                    + (from_proceeds if sym == "USDC" else 0.0)
                units = max(0.0, min(free, remaining / px, room_usd / px))
                if units > 0:
                    send[sym] = send.get(sym, 0.0) + units
                    remaining -= units * px
                    room_usd -= units * px
                    event(s, pid, "STABLE_TOPUP", symbol=sym, units=units)
        s.commit()

        collected = 0.0
        sent_ok: dict[str, bool] = {}
        for sym, units in send.items():
            if units <= 1e-9:
                continue
            carrier = next((l for l, _ in used_legs if l.symbol == sym), None)
            if carrier is None:
                carrier = SwapLeg(payment_id=pid, seq=999, symbol=sym, kind="sweep", sell_units=units,
                                  planned_krw=units * rt.market.price_krw(sym), out_symbol=sym)
                s.add(carrier)
                s.commit()
            _claim_submit(s, p, carrier, count_attempt=True)
            tx = await rt.chain.transfer(wref, sym, units, settings.treasury_address)
            rc = await rt.chain.wait(tx, wref)
            px = 1.0 if sym == "USDC" else rt.market.price_usd(sym)
            sent_ok[sym] = rc.success
            if rc.success:
                collected += units * px
                for l, u in used_legs:
                    if l.symbol == sym:
                        l.status, l.tx_hash, l.received, l.sell_units = LegStatus.CONFIRMED.value, tx.tx_hash, u, u
                carrier.status, carrier.tx_hash = LegStatus.CONFIRMED.value, tx.tx_hash
                event(s, pid, Ev.TREASURY_RECEIVED, symbol=sym, units=units, usd=units * px, tx=tx.tx_hash)
            else:
                carrier.status, carrier.error = LegStatus.FAILED.value, rc.error
                for l, _ in used_legs:
                    if l.symbol == sym:
                        l.status, l.error = LegStatus.FAILED.value, rc.error
                event(s, pid, Ev.SWAP_FAILED, symbol=sym, kind="treasury_transfer", error=rc.error)
            s.commit()

        # ── 4) 정산 ────────────────────────────────────────────
        # 스왑 대금 중 Treasury 로 안 간 USDC 는 고객 지갑에 남는다 (= 차액 반환)
        reimbursed = proceeds - (from_proceeds if sent_ok.get("USDC") else 0.0)
        _settle(s, p, w, collected, max(0.0, reimbursed))
    finally:
        s.close()


def _release(s, p: Payment, w_id: str, status: HoldStatus, ev: Ev) -> None:
    s.execute(update(Wallet).where(Wallet.id == w_id, Wallet.hold_payment_id == p.id)
              .values(hold_payment_id=None, hold_expires_at=None))
    p.hold_status = status.value
    event(s, p.id, ev)
    invalidate(w_id)


def _settle(s, p: Payment, w: Wallet, collected_usd: float, reimbursed_usd: float) -> None:
    p.collected_usd = collected_usd
    p.reimbursed_usd = reimbursed_usd
    shortfall = p.target_usd - collected_usd
    p.settled_at = datetime.now(timezone.utc)
    if shortfall <= TOL_USD:
        p.liq_status = LiqStatus.COMPLETED.value
        p.recovery_status = RecoveryStatus.RECOVERED.value
        event(s, p.id, Ev.LIQUIDATION_COMPLETED, collected_usd=round(collected_usd, 6), target_usd=round(p.target_usd, 6))
    else:
        p.liq_status = LiqStatus.FAILED.value
        p.recovery_status = RecoveryStatus.LOSS_ABSORBED.value
        p.loss_krw = shortfall * p.usdkrw
        event(s, p.id, Ev.LIQUIDATION_FAILED, collected_usd=round(collected_usd, 6), shortfall_usd=round(shortfall, 6))
        event(s, p.id, Ev.LOSS_ABSORBED, loss_krw=round(p.loss_krw))
        alert(s, "warn", f"청산 부족분 {p.loss_krw:,.0f}원 회사 흡수 (고객 청구 없음)", p.id)
    event(s, p.id, Ev.REIMBURSEMENT_COMPLETED, usdc=round(reimbursed_usd, 6))
    _release(s, p, w.id, HoldStatus.RELEASED, Ev.HOLD_RELEASED)
    s.commit()


def _crash(pid: str, err: str) -> None:
    with SessionLocal() as s:
        p = s.get(Payment, pid)
        if not p or p.hold_status != HoldStatus.PLACED.value:
            return
        in_flight = s.query(SwapLeg).filter_by(payment_id=pid, status=LegStatus.SUBMITTED.value).count()
        if in_flight:
            alert(s, "critical", f"청산 중 오류, 제출된 트랜잭션 상태 불명 → 잠금 유지, 수동 확인 필요: {err[:200]}", pid)
            s.commit()
            return
        w = s.get(Wallet, p.wallet_id)
        done = s.query(SwapLeg).filter_by(payment_id=pid, status=LegStatus.CONFIRMED.value).count()
        if done:
            alert(s, "critical", f"청산 중 오류 (일부 체결됨) → 잠금 유지, 수동 정산 필요: {err[:200]}", pid)
            s.commit()
            return
        alert(s, "warn", f"청산 오류: {err[:200]}", pid)
        _settle(s, p, w, 0.0, 0.0)


def expire_one(pid: str) -> bool:
    """TTL 만료 처리: 스왑이 하나도 제출/체결되지 않았을 때만."""
    with SessionLocal() as s:
        p = s.get(Payment, pid)
        if not p or p.hold_status != HoldStatus.PLACED.value:
            return False
        active = s.query(SwapLeg).filter(SwapLeg.payment_id == pid,
                                         SwapLeg.status.in_([LegStatus.SUBMITTED.value, LegStatus.CONFIRMED.value])).count()
        if active:
            return False
        p.liq_status = LiqStatus.FAILED.value
        p.recovery_status = RecoveryStatus.LOSS_ABSORBED.value
        p.loss_krw = p.amount_krw
        p.settled_at = datetime.now(timezone.utc)
        _release(s, p, p.wallet_id, HoldStatus.EXPIRED, Ev.HOLD_EXPIRED)
        event(s, pid, Ev.LIQUIDATION_FAILED, reason="HOLD_TTL")
        event(s, pid, Ev.LOSS_ABSORBED, loss_krw=round(p.loss_krw))
        alert(s, "warn", f"잠금 만료: 스왑 제출 전 TTL 경과 → {p.loss_krw:,.0f}원 회사 흡수", pid)
        s.commit()
        return True


def sweep_expired_holds() -> list[str]:
    now = datetime.now(timezone.utc)
    with SessionLocal() as s:
        ids = [p.id for p in s.query(Payment).filter_by(hold_status=HoldStatus.PLACED.value)
               if p.hold_expires_at and _aware(p.hold_expires_at) < now]
    return [pid for pid in ids if expire_one(pid)]


def pending_liquidations() -> list[str]:
    with SessionLocal() as s:
        return [p.id for p in s.query(Payment).filter(Payment.hold_status == HoldStatus.PLACED.value,
                                                      Payment.liq_status == LiqStatus.PENDING.value)]
