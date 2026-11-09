from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import update

from . import runtime
from .algo.sell import Holding, SellContext
from .assets import ASSETS
from .config import settings
from .db import SessionLocal
from .market.validator import ReportValidator
from .models import (Alert, Card, DecisionLog, Merchant, MerchantPayout, Payment, PaymentEvent, SubQuestion,
                     Subscription, SwapLeg, User, Wallet, uid)
from .services import usage as usage_svc
from .services.authorization import AuthRequest, authorize
from .services.common import balances, daily_caps, eligible_symbols, invalidate, wallet_ref

router = APIRouter()


# ── POS Backend → 엔진 인증 (HMAC-SHA256(body)) ──────────────────
async def verify_pos(request: Request):
    if not settings.require_hmac:
        return
    body = await request.body()
    sig = request.headers.get("X-SB-Signature", "")
    want = hmac.new(settings.pos_hmac_secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, want):
        raise HTTPException(401, "bad signature")


# ═══ 결제 (POS Backend 가 호출) ═════════════════════════════════
class AuthorizeIn(BaseModel):
    checkout_id: str = Field(..., description="idempotency_key 로 사용")
    amount_krw: float = Field(..., gt=0)
    merchant_id: str
    card_token: str | None = None
    wallet_id: str | None = None


@router.post("/v1/payments/authorize", dependencies=[Depends(verify_pos)], tags=["pos"])
async def api_authorize(body: AuthorizeIn):
    if not body.card_token and not body.wallet_id:
        raise HTTPException(422, "card_token 또는 wallet_id 필요")
    res = await authorize(AuthRequest(**body.model_dump()))
    return res.to_dict()


@router.get("/v1/payments/{checkout_id}/pos", tags=["pos"])
def api_pos_status(checkout_id: str):
    """POS 화면용: 승인 결과만 (청산·회수 상태는 노출하지 않는다)."""
    with SessionLocal() as s:
        p = s.query(Payment).filter_by(checkout_id=checkout_id).one_or_none()
        if not p:
            raise HTTPException(404)
        return {"checkout_id": checkout_id, "status": p.auth_status, "reason": p.decline_reason, "amount_krw": p.amount_krw}


# ═══ Ops ═════════════════════════════════════════════════════════
def _payment_view(s, p: Payment) -> dict:
    po = s.query(MerchantPayout).filter_by(payment_id=p.id).one_or_none()
    return {
        "id": p.id, "checkout_id": p.checkout_id, "wallet_id": p.wallet_id, "merchant_id": p.merchant_id,
        "amount_krw": p.amount_krw, "required_krw": p.required_krw, "wallet_total_krw": p.wallet_total_krw,
        "usdkrw": p.usdkrw, "target_usd": p.target_usd,
        "axes": {"authorization": p.auth_status, "decline_reason": p.decline_reason, "hold": p.hold_status,
                 "liquidation": p.liq_status, "recovery": p.recovery_status},
        "merchant_payout": po and {"status": po.status, "bank_tran_id": po.bank_tran_id, "attempts": po.attempts, "error": po.error},
        "collected_usd": p.collected_usd, "reimbursed_usd": p.reimbursed_usd, "loss_krw": p.loss_krw,
        "auth_latency_ms": p.auth_latency_ms, "created_at": p.created_at, "settled_at": p.settled_at,
    }


@router.get("/ops/payments", tags=["ops"])
def ops_payments(limit: int = 50):
    with SessionLocal() as s:
        return [_payment_view(s, p) for p in s.query(Payment).order_by(Payment.created_at.desc()).limit(limit)]


@router.get("/ops/payments/{pid}", tags=["ops"])
def ops_payment(pid: str):
    with SessionLocal() as s:
        p = s.get(Payment, pid) or s.query(Payment).filter_by(checkout_id=pid).one_or_none()
        if not p:
            raise HTTPException(404)
        v = _payment_view(s, p)
        v["plan"] = p.plan
        v["legs"] = [{k: getattr(l, k) for k in ("seq", "symbol", "kind", "sell_units", "planned_krw", "out_symbol",
                                                 "min_out", "received", "tx_hash", "status", "attempts", "error", "is_alternate")}
                     for l in s.query(SwapLeg).filter_by(payment_id=p.id).order_by(SwapLeg.seq)]
        v["events"] = [{"type": e.type, "data": e.data, "at": e.at}
                       for e in s.query(PaymentEvent).filter_by(payment_id=p.id).order_by(PaymentEvent.id)]
        return v


@router.get("/ops/alerts", tags=["ops"])
def ops_alerts(limit: int = 100, audience: str | None = None):
    with SessionLocal() as s:
        q = s.query(Alert).order_by(Alert.id.desc())
        if audience:
            q = q.filter_by(audience=audience)
        return [{"id": a.id, "level": a.level, "payment_id": a.payment_id, "user_id": a.user_id,
                 "audience": a.audience, "message": a.message, "at": a.at} for a in q.limit(limit)]


@router.get("/ops/decisions", tags=["ops"])
def ops_decisions(kind: str | None = None, ref_id: str | None = None, limit: int = 20):
    with SessionLocal() as s:
        q = s.query(DecisionLog).order_by(DecisionLog.id.desc())
        if kind:
            q = q.filter_by(kind=kind)
        if ref_id:
            q = q.filter_by(ref_id=ref_id)
        return [{"id": d.id, "kind": d.kind, "ref_id": d.ref_id, "formula_version": d.formula_version,
                 "inputs": d.inputs, "output": d.output, "at": d.at} for d in q.limit(limit)]


# ═══ 사용자 · 지갑 · 카드 · 가맹점 ═══════════════════════════════
class UserIn(BaseModel):
    name: str = ""
    risk_profile: str = "neutral"
    pay_assets: list[str] = []
    delegation_agreed: bool = False      # 회사가 지갑 안 코인을 매도할 수 있다는 위임
    lock_notice_agreed: bool = False     # 결제 중 지갑 전체가 몇 초간 잠긴다는 안내


@router.post("/v1/users", tags=["wallet"])
def create_user(body: UserIn):
    if body.risk_profile not in settings.gamma0:
        raise HTTPException(422, f"risk_profile ∈ {list(settings.gamma0)}")
    now = datetime.now(timezone.utc)
    with SessionLocal() as s:
        u = User(name=body.name, risk_profile=body.risk_profile, pay_assets=[a.upper() for a in body.pay_assets],
                 delegation_agreed_at=now if body.delegation_agreed else None,
                 lock_notice_agreed_at=now if body.lock_notice_agreed else None)
        s.add(u)
        s.commit()
        return {"id": u.id}


class WalletIn(BaseModel):
    address: str
    provider: str = "sim"
    provider_wallet_id: str = ""


@router.post("/v1/users/{user_id}/wallets", tags=["wallet"])
def create_wallet(user_id: str, body: WalletIn):
    with SessionLocal() as s:
        if not s.get(User, user_id):
            raise HTTPException(404)
        w = Wallet(user_id=user_id, **body.model_dump())
        s.add(w)
        s.commit()
        return {"id": w.id, "address": w.address}


class CardIn(BaseModel):
    token: str
    wallet_id: str


@router.post("/v1/cards", tags=["wallet"])
def register_card(body: CardIn):
    with SessionLocal() as s:
        w = s.get(Wallet, body.wallet_id)
        if not w:
            raise HTTPException(404, "wallet")
        u = s.get(User, w.user_id)
        if not (u.delegation_agreed_at and u.lock_notice_agreed_at):
            raise HTTPException(409, "매도 위임·지갑 잠금 안내 동의가 있어야 카드를 활성화할 수 있습니다")
        s.merge(Card(token=body.token, wallet_id=body.wallet_id, active=True))
        s.commit()
        return {"token": body.token, "wallet_id": body.wallet_id}


@router.get("/v1/wallets/{wallet_id}", tags=["wallet"])
async def wallet_view(wallet_id: str):
    rt = runtime.rt
    with SessionLocal() as s:
        w = s.get(Wallet, wallet_id)
        if not w:
            raise HTTPException(404)
        bal = await balances(w, max_age=0)
        caps = daily_caps(s, w.id, bal)
    vals = {}
    for sym, units in bal.items():
        try:
            vals[sym] = {"units": units, "krw": round(units * rt.market.price_krw(sym)),
                         "sellable_today_units": None if caps[sym] == float("inf") else caps[sym]}
        except KeyError:
            vals[sym] = {"units": units, "krw": None}
    return {"id": w.id, "address": w.address, "locked": bool(w.hold_payment_id), "hold_payment_id": w.hold_payment_id,
            "assets": vals, "total_krw": sum(v["krw"] or 0 for v in vals.values())}


class WithdrawIn(BaseModel):
    symbol: str
    units: float = Field(..., gt=0)
    to: str


@router.post("/v1/wallets/{wallet_id}/withdraw", tags=["wallet"])
async def withdraw(wallet_id: str, body: WithdrawIn):
    """출금은 서비스를 거친다. 잠금 중이면 거절하고, 출금하는 동안은 지갑을 잠가 결제가 WALLET_BUSY 가 되게 한다."""
    rt = runtime.rt
    hold_id = uid("wd")
    with SessionLocal() as s:
        w = s.get(Wallet, wallet_id)
        if not w:
            raise HTTPException(404)
        got = s.execute(update(Wallet).where(Wallet.id == wallet_id, Wallet.hold_payment_id.is_(None))
                        .values(hold_payment_id=hold_id)).rowcount
        s.commit()
        if got != 1:
            raise HTTPException(409, "결제 처리 중이라 지갑이 잠겨 있어요. 잠시 후 다시 시도해 주세요")
        try:
            bal = await balances(w, max_age=0)
            if bal.get(body.symbol.upper(), 0) + 1e-12 < body.units:
                raise HTTPException(422, "잔고 부족")
            tx = await rt.chain.transfer(wallet_ref(w), body.symbol.upper(), body.units, body.to)
            rc = await rt.chain.wait(tx, wallet_ref(w))
            return {"tx_hash": tx.tx_hash, "success": rc.success, "error": rc.error}
        finally:
            s.execute(update(Wallet).where(Wallet.id == wallet_id, Wallet.hold_payment_id == hold_id)
                      .values(hold_payment_id=None))
            s.commit()
            invalidate(wallet_id)


class MerchantIn(BaseModel):
    name: str
    bank_code: str = ""
    account_num: str = ""
    account_holder: str = ""
    id: str | None = None


@router.post("/v1/merchants", tags=["wallet"])
def create_merchant(body: MerchantIn):
    with SessionLocal() as s:
        data = body.model_dump()
        if not data.get("id"):
            data.pop("id")
        m = Merchant(**data)
        s.merge(m)
        s.commit()
        return {"id": m.id}


# ═══ 시장 · 매크로 ═══════════════════════════════════════════════
@router.get("/v1/market/state", tags=["market"])
def market_state():
    return runtime.rt.market.snapshot()


@router.post("/v1/market/refresh", tags=["market"])
async def market_refresh():
    from .workers import refresh_macro, refresh_news
    await refresh_macro()
    await refresh_news()
    return runtime.rt.market.snapshot()


@router.post("/v1/market/report", tags=["market"])
def market_report(values: dict[str, float]):
    """사람/외부 리포트 값 입력. LLM 값과 같은 검증기를 거친다."""
    return ReportValidator(runtime.rt.market).apply(values, source="manual")


class CoinEventIn(BaseModel):
    symbol: str
    unlock_pct_14d: float | None = None   # 14일 안 언락 (유통량 대비 %)
    staking_apr: float | None = None
    pool_imbalance: float | None = None   # 스테이블 풀 불균형 b_j (0~1)


@router.post("/v1/market/coin-events", tags=["market"])
def coin_events(items: list[CoinEventIn]):
    m = runtime.rt.market
    for it in items:
        sig = m.coin(it.symbol.upper())
        if it.unlock_pct_14d is not None:
            sig.unlock_pct = max(0.0, min(50.0, it.unlock_pct_14d))
        if it.staking_apr is not None:
            sig.staking_apr = max(0.0, min(0.5, it.staking_apr))
        if it.pool_imbalance is not None:
            sig.pool_imbalance = max(0.0, min(1.0, it.pool_imbalance))
    return {"ok": True}


@router.get("/v1/market/sell-preview", tags=["market"])
async def sell_preview(wallet_id: str, amount_krw: float):
    """지금 결제하면 무엇을 얼마나 팔지 미리보기 (지갑 잠금·체결 없음)."""
    rt = runtime.rt
    with SessionLocal() as s:
        w = s.get(Wallet, wallet_id)
        if not w:
            raise HTTPException(404)
        user = s.get(User, w.user_id)
        bal = await balances(w)
        syms = eligible_symbols(user, bal)
        caps = daily_caps(s, w.id, bal)
    hs = [Holding(sym, bal[sym], rt.market.price_krw(sym), caps[sym] * rt.market.price_krw(sym)) for sym in syms]
    ctx = SellContext(gamma0=settings.gamma0.get(user.risk_profile, 2.0), expected_spend_14d_krw=user.expected_spend_14d_krw)
    return rt.optimizer.plan(hs, amount_krw, ctx, set(syms)).to_dict()


# ═══ 구독 · 동의 · 사용량 ════════════════════════════════════════
def _usage_call(fn, *a, **kw):
    with SessionLocal() as s:
        try:
            out = fn(s, *a, **kw)
        except usage_svc.ConsentRequired as e:
            raise HTTPException(403, str(e)) from e
        s.commit()
        return out


class ConsentIn(BaseModel):
    tier: int = Field(..., ge=1, le=3)
    granted: bool


@router.post("/v1/users/{user_id}/consents", tags=["subscription"])
def consent(user_id: str, body: ConsentIn):
    _usage_call(usage_svc.set_consent, user_id, body.tier, body.granted)
    return {"ok": True}


class TxIn(BaseModel):
    merchant: str
    amount_krw: float
    at: datetime


@router.post("/v1/users/{user_id}/transactions", tags=["subscription"])
def add_tx(user_id: str, rows: list[TxIn]):
    return {"added": _usage_call(usage_svc.add_card_transactions, user_id, [r.model_dump() for r in rows])}


class ScreenTimeIn(BaseModel):
    month: str
    hours: float
    bundle_id: str | None = None
    service: str | None = None
    last_used_at: str | None = None


@router.post("/v1/users/{user_id}/usage/screen-time", tags=["subscription"])
def usage_screen_time(user_id: str, items: list[ScreenTimeIn]):
    return {"ingested": _usage_call(usage_svc.ingest_screen_time, user_id, [i.model_dump() for i in items])}


class FileIn(BaseModel):
    content: str


@router.post("/v1/users/{user_id}/usage/netflix-csv", tags=["subscription"])
def usage_netflix(user_id: str, body: FileIn):
    return _usage_call(usage_svc.ingest_netflix_csv, user_id, body.content)


@router.post("/v1/users/{user_id}/usage/youtube-takeout", tags=["subscription"])
def usage_youtube(user_id: str, body: FileIn):
    return _usage_call(usage_svc.ingest_youtube_takeout, user_id, body.content)


class TokenIn(BaseModel):
    access_token: str


@router.post("/v1/users/{user_id}/usage/spotify", tags=["subscription"])
async def usage_spotify(user_id: str, body: TokenIn):
    with SessionLocal() as s:
        try:
            out = await usage_svc.pull_spotify(s, user_id, body.access_token)
        except usage_svc.ConsentRequired as e:
            raise HTTPException(403, str(e)) from e
        s.commit()
        return out


@router.post("/v1/users/{user_id}/usage/google-drive", tags=["subscription"])
async def usage_gdrive(user_id: str, body: TokenIn):
    with SessionLocal() as s:
        try:
            gb = await usage_svc.pull_google_drive_storage(s, user_id, body.access_token)
        except usage_svc.ConsentRequired as e:
            raise HTTPException(403, str(e)) from e
        s.commit()
        return {"storage_gb": gb}


class StorageIn(BaseModel):
    service: str
    gb: float


@router.post("/v1/users/{user_id}/usage/storage", tags=["subscription"])
def usage_storage(user_id: str, body: StorageIn):
    src = "icloud" if body.service == "icloud" else "manual_storage"
    _usage_call(usage_svc.ingest_storage, user_id, body.service, body.gb, src)
    return {"ok": True}


class BenefitIn(BaseModel):
    service: str
    month: str
    krw: float | None = None   # 없으면 결제 내역으로 추정 (쿠팡)


@router.post("/v1/users/{user_id}/usage/benefit", tags=["subscription"])
def usage_benefit(user_id: str, body: BenefitIn):
    if body.krw is None and body.service == "coupang_wow":
        return {"estimated_krw": _usage_call(usage_svc.estimate_coupang_benefit, user_id, body.month)}
    _usage_call(usage_svc.ingest_benefit, user_id, body.service, body.month, body.krw or 0.0)
    return {"ok": True}


class SubIn(BaseModel):
    service: str
    plan: str
    share_count: int = 1
    next_billing: datetime | None = None


@router.post("/v1/users/{user_id}/subscriptions", tags=["subscription"])
def add_subscription(user_id: str, body: SubIn):
    from .algo.subscription import CATALOG
    if body.service not in CATALOG:
        raise HTTPException(422, f"service ∈ {list(CATALOG)}")
    with SessionLocal() as s:
        sub = s.query(Subscription).filter_by(user_id=user_id, service=body.service).one_or_none()
        if sub is None:
            sub = Subscription(user_id=user_id, service=body.service, plan=body.plan, source="manual")
            s.add(sub)
        sub.plan, sub.share_count, sub.next_billing = body.plan, body.share_count, body.next_billing
        s.commit()
        return {"id": sub.id}


@router.post("/v1/users/{user_id}/subscriptions/optimize", tags=["subscription"])
async def subs_optimize(user_id: str):
    from .services.subscriptions import run_for_user
    return await run_for_user(user_id)


@router.post("/v1/users/{user_id}/subscriptions/verify", tags=["subscription"])
def subs_verify(user_id: str):
    from .services.subscriptions import verify_changes
    return verify_changes(user_id)


@router.get("/v1/users/{user_id}/questions", tags=["subscription"])
def questions(user_id: str):
    with SessionLocal() as s:
        return [{"id": q.id, "kind": q.kind, "message": q.message, "proposed_plan": q.proposed_plan,
                 "delta_krw": q.delta_krw, "status": q.status}
                for q in s.query(SubQuestion).filter_by(user_id=user_id).order_by(SubQuestion.created_at.desc())]


class AnswerIn(BaseModel):
    answer: str = Field(..., pattern="^(keep|accept|later|usage)$")
    value: dict | None = None


@router.post("/v1/questions/{qid}/answer", tags=["subscription"])
def answer_q(qid: str, body: AnswerIn):
    from .services.subscriptions import answer
    try:
        return answer(qid, body.answer, body.value)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


# ═══ 시뮬레이션 도구 (MODE=sim 에서만) ═══════════════════════════
class FundIn(BaseModel):
    address: str
    symbol: str
    units: float


@router.post("/sim/fund", tags=["sim"])
def sim_fund(body: FundIn):
    if settings.mode != "sim":
        raise HTTPException(404)
    runtime.rt.chain.fund(body.address, body.symbol.upper(), body.units)
    for w_id, _ in list(runtime.rt.balance_cache.items()):
        invalidate(w_id)
    return {"ok": True}


class FailIn(BaseModel):
    symbol: str
    kinds: list[str]


@router.post("/sim/fail", tags=["sim"])
def sim_fail(body: FailIn):
    if settings.mode != "sim":
        raise HTTPException(404)
    runtime.rt.chain.inject_failure(body.symbol.upper(), *body.kinds)
    return {"ok": True}


@router.get("/v1/assets", tags=["market"])
def assets():
    return {s: {"kind": a.kind, "address": a.address, "stable": a.is_stable} for s, a in ASSETS.items()}
