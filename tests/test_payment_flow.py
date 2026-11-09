import asyncio
from datetime import datetime, timedelta, timezone

from app import db, runtime
from app.config import settings
from app.models import MerchantPayout, Payment, PaymentEvent, SwapLeg, Wallet
from app.services.authorization import AuthRequest, authorize
from app.services.liquidation import sweep_expired_holds


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


async def _auth_and_drain(req):
    r = await authorize(req)
    await runtime.rt.drain()
    return r


def _p(checkout):
    with db.SessionLocal() as s:
        return s.query(Payment).filter_by(checkout_id=checkout).one()


def _events(pid):
    with db.SessionLocal() as s:
        return [e.type for e in s.query(PaymentEvent).filter_by(payment_id=pid).order_by(PaymentEvent.id)]


def test_happy_path_authorize_liquidate_recover(setup_user):
    rt = runtime.rt
    r = run(_auth_and_drain(AuthRequest("co-1", 50_000, "m1", card_token="card1")))
    assert r.status == "AUTHORIZED" and r.latency_ms < 1000
    p = _p("co-1")
    assert (p.hold_status, p.liq_status, p.recovery_status) == ("RELEASED", "COMPLETED", "RECOVERED")
    assert abs(p.collected_usd - 50_000 / 1400) < 0.01
    assert rt.chain.ledger[settings.treasury_address.lower()]["USDC"] > 35.7
    with db.SessionLocal() as s:
        assert s.get(Wallet, p.wallet_id).hold_payment_id is None
        assert s.query(MerchantPayout).filter_by(payment_id=p.id).one().status == "SENT"
    ev = _events(p.id)
    for e in ("HOLD_PLACED", "AUTHORIZED", "LIQUIDATION_STARTED", "TREASURY_RECEIVED",
              "LIQUIDATION_COMPLETED", "REIMBURSEMENT_COMPLETED", "HOLD_RELEASED", "MERCHANT_PAYOUT_SENT"):
        assert e in ev, e
    assert rt.bank.sent[0]["amount"] == 50_000


def test_idempotent_replay(setup_user):
    r1 = run(_auth_and_drain(AuthRequest("co-2", 10_000, "m1", card_token="card1")))
    r2 = run(authorize(AuthRequest("co-2", 10_000, "m1", card_token="card1")))
    assert r2.idempotent_replay and r2.payment_id == r1.payment_id
    with db.SessionLocal() as s:
        assert s.query(Payment).filter_by(checkout_id="co-2").count() == 1


def test_limit_exceeded_does_not_lock(setup_user):
    r = run(authorize(AuthRequest("co-3", 10_000_000, "m1", card_token="card1")))
    assert (r.status, r.reason) == ("DECLINED", "LIMIT_EXCEEDED")
    with db.SessionLocal() as s:
        assert s.get(Wallet, setup_user["wallet"]).hold_payment_id is None


def test_limit_uses_margin(setup_user):
    # 지갑 총합 ≈ 140만 + 245만 + 140만 + 42만 = 567만. A×1.03 이 총합을 넘으면 거절
    total = 0.01 * 100_000 * 1400 + 0.5 * 3500 * 1400 + 50 * 20 * 1400 + 300 * 1400
    r = run(authorize(AuthRequest("co-4", total / 1.03 + 1000, "m1", card_token="card1")))
    assert r.reason == "LIMIT_EXCEEDED"


def test_wallet_busy_during_hold(setup_user):
    async def go():
        a = await authorize(AuthRequest("co-5", 20_000, "m1", card_token="card1"))
        b = await authorize(AuthRequest("co-6", 20_000, "m1", card_token="card1"))   # 첫 결제 정산 전
        await runtime.rt.drain()
        c = await authorize(AuthRequest("co-7", 20_000, "m1", card_token="card1"))   # 정산 후
        await runtime.rt.drain()
        return a, b, c
    a, b, c = run(go())
    assert a.status == "AUTHORIZED"
    assert (b.status, b.reason) == ("DECLINED", "WALLET_BUSY")
    assert c.status == "AUTHORIZED"


def test_swap_failure_uses_alternate_coin(setup_user):
    rt = runtime.rt
    # 큰 결제로 코인 매도가 필요하게 만들고, 첫 번째로 고른 코인을 계속 실패시킨다
    from app.api import sell_preview
    preview = run(sell_preview(setup_user["wallet"], 1_000_000))
    first_coin = next(s for s in preview["alloc_krw"] if s not in ("USDC", "USDT"))
    rt.chain.inject_failure(first_coin, "noliq")
    run(_auth_and_drain(AuthRequest("co-8", 1_000_000, "m1", card_token="card1")))
    p = _p("co-8")
    with db.SessionLocal() as s:
        legs = s.query(SwapLeg).filter_by(payment_id=p.id).all()
    assert any(l.symbol == first_coin and l.status == "FAILED" for l in legs)
    assert any(l.is_alternate and l.status == "CONFIRMED" for l in legs)
    assert p.recovery_status == "RECOVERED" and p.auth_status == "AUTHORIZED"


def test_all_swaps_fail_loss_absorbed_auth_unchanged(setup_user):
    rt = runtime.rt
    for sym in ("BTC", "ETH", "LINK"):
        rt.chain.inject_failure(sym, *["revert"] * 10)
    run(_auth_and_drain(AuthRequest("co-9", 1_000_000, "m1", card_token="card1")))
    p = _p("co-9")
    assert p.auth_status == "AUTHORIZED"                  # 승인 결과는 바뀌지 않는다
    assert (p.liq_status, p.recovery_status, p.hold_status) == ("FAILED", "LOSS_ABSORBED", "RELEASED")
    assert p.loss_krw > 0
    with db.SessionLocal() as s:
        legs = s.query(SwapLeg).filter_by(payment_id=p.id, kind="swap").all()
    assert len({l.symbol for l in legs}) <= settings.max_swaps_per_payment
    assert all(l.attempts <= 1 + settings.swap_retries for l in legs)
    assert "LOSS_ABSORBED" in _events(p.id)


def test_ttl_expiry_before_submit(setup_user):
    async def go():
        rt = runtime.rt
        rt.spawn = lambda coro, name="": coro.close()   # 청산 워커가 안 돈 상황
        await authorize(AuthRequest("co-10", 30_000, "m1", card_token="card1"))
    run(go())
    with db.SessionLocal() as s:
        p = s.query(Payment).filter_by(checkout_id="co-10").one()
        p.hold_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        s.commit()
    assert sweep_expired_holds()
    p = _p("co-10")
    assert (p.hold_status, p.liq_status, p.recovery_status) == ("EXPIRED", "FAILED", "LOSS_ABSORBED")
    with db.SessionLocal() as s:
        assert s.get(Wallet, p.wallet_id).hold_payment_id is None


def test_ttl_does_not_expire_after_swap_submitted(setup_user):
    run(_auth_and_drain(AuthRequest("co-11", 1_000_000, "m1", card_token="card1")))
    with db.SessionLocal() as s:
        p = s.query(Payment).filter_by(checkout_id="co-11").one()
        p.hold_status = "PLACED"                         # 정산 직전 상태로 되돌려 확인
        p.hold_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        s.commit()
    assert sweep_expired_holds() == []                   # 체결된 스왑이 있으면 잠금 유지


def test_reimbursement_stays_in_wallet(setup_user):
    rt = runtime.rt
    run(_auth_and_drain(AuthRequest("co-12", 1_000_000, "m1", card_token="card1")))
    p = _p("co-12")
    assert p.reimbursed_usd >= 0
    if p.reimbursed_usd > 0:
        assert rt.chain.ledger["0xuser1"]["USDC"] >= p.reimbursed_usd - 1e-6


def test_withdraw_blocked_while_locked(setup_user):
    from fastapi import HTTPException

    from app.api import WithdrawIn, withdraw

    async def go():
        rt = runtime.rt
        rt.spawn = lambda coro, name="": coro.close()
        await authorize(AuthRequest("co-13", 30_000, "m1", card_token="card1"))
        try:
            await withdraw(setup_user["wallet"], WithdrawIn(symbol="USDC", units=10, to="0xext"))
        except HTTPException as e:
            return e.status_code
    assert run(go()) == 409


def test_stable_depeg_guard(setup_user):
    rt = runtime.rt
    rt.chain.fund("0xuser1", "USDT", 1000)
    rt.market.set_price("USDT", 0.975)
    run(_auth_and_drain(AuthRequest("co-14", 100_000, "m1", card_token="card1")))
    p = _p("co-14")
    assert p.plan["excluded"].get("USDT") == "depeg_guard"
    assert "STABLE_DEPEG_GUARD" in _events(p.id)
    assert p.recovery_status == "RECOVERED"


def _dump(checkout):
    from app.api import ops_payment
    import json
    print(json.dumps(ops_payment(checkout), default=str, ensure_ascii=False, indent=1)[:6000])
