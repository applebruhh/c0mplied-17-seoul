"""백그라운드 워커: 시세, 매크로, 뉴스(LLM), 풀 깊이, TTL 만료, 구독 점검, 재시작 복구."""
from __future__ import annotations

import asyncio
import logging

from . import runtime
from .assets import ASSETS
from .config import settings
from .db import SessionLocal
from .market import macro, news_llm, prices
from .models import MerchantPayout, Payment, User
from .services.common import decision

log = logging.getLogger("workers")


async def _every(period: float, fn, name: str):
    while True:
        try:
            await fn()
        except Exception:  # noqa: BLE001
            log.exception("worker %s failed", name)
        await asyncio.sleep(period)


async def refresh_macro():
    res = await macro.refresh_all(runtime.rt.market)
    with SessionLocal() as s:
        decision(s, "macro_update", "", {}, res)
        s.commit()


async def refresh_news():
    res = await news_llm.refresh_news(runtime.rt.market)
    with SessionLocal() as s:
        decision(s, "llm_score", "", {}, res)
        s.commit()


async def refresh_depth():
    rt = runtime.rt
    fx = rt.market.fx()
    for sym in ASSETS:
        if sym not in rt.market.prices:
            continue
        try:
            rt.market.coin(sym).depth_krw = await rt.chain.depth_krw(sym, rt.market.price_usd(sym), fx)
        except Exception as e:  # noqa: BLE001
            log.warning("depth %s: %s", sym, e)


async def sweep_ttl():
    from .services.liquidation import sweep_expired_holds
    sweep_expired_holds()


async def subscription_daily():
    from .services.subscriptions import run_for_user, verify_changes
    with SessionLocal() as s:
        ids = [u.id for u in s.query(User)]
    for uid in ids:
        await run_for_user(uid)
        verify_changes(uid)


def recover_pending():
    """재시작 시: 승인됐는데 청산·송금이 시작 안 된 결제를 다시 띄운다."""
    from .services.liquidation import pending_liquidations, run_liquidation
    from .services.payout import run_payout
    rt = runtime.rt
    for pid in pending_liquidations():
        rt.spawn(run_liquidation(pid), f"liq:{pid}")
    with SessionLocal() as s:
        for po in s.query(MerchantPayout).filter_by(status="PENDING"):
            rt.spawn(run_payout(po.payment_id), f"payout:{po.payment_id}")
        for p in s.query(Payment).filter_by(auth_status="AUTHORIZED", payout_status="PENDING"):
            if not s.query(MerchantPayout).filter_by(payment_id=p.id).count():
                rt.spawn(run_payout(p.id), f"payout:{p.id}")


def start_all():
    rt = runtime.rt
    if settings.price_source == "sim" or settings.mode == "sim":
        rt.spawn(prices.sim_price_loop(rt.market), "prices:sim")
    else:
        rt.spawn(prices.binance_ws_loop(rt.market), "prices:binance")
        rt.spawn(prices.peg_fx_loop(rt.market), "prices:peg_fx")
    if settings.mode == "live":
        rt.spawn(_every(settings.macro_refresh_sec, refresh_macro, "macro"), "macro")
        rt.spawn(_every(settings.news_refresh_sec, refresh_news, "news"), "news")
    rt.spawn(_every(60, refresh_depth, "depth"), "depth")
    rt.spawn(_every(1.0, sweep_ttl, "ttl"), "ttl")
    rt.spawn(_every(6 * 3600, subscription_daily, "subs"), "subs")
    recover_pending()
