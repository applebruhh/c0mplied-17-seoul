"""구독 최적화 실행 (월 1회 + 청구일 3일 전 점검). 결제 순간에는 구독 선택을 고정하고 매도량만 계산한다.

순서 (지침 알고리즘 4절): ① 입력 갱신 → ② λ → ③ 구독별 N, k* → ④ λ 재계산 → ⑤ 질문 대기열 → ⑥ 응답 반영
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .. import runtime
from ..algo.sell import Holding, SellContext
from ..algo.subscription import CATALOG, SubInput, detect_recurring, optimize
from ..config import settings
from ..db import SessionLocal
from ..models import CardTransaction, SubQuestion, Subscription, User, Wallet
from .common import alert, balances, decision
from .usage import has_consent, usage_summary


def _aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt and dt.tzinfo is None else dt


def sync_detected(s, user_id: str) -> list[Subscription]:
    if not has_consent(s, user_id, 1):
        return []
    txs = [(t.merchant_name, t.amount_krw, _aware(t.at)) for t in s.query(CardTransaction).filter_by(user_id=user_id)]
    out = []
    for d in detect_recurring(txs):
        sub = s.query(Subscription).filter_by(user_id=user_id, service=d["service"]).one_or_none()
        if sub is None:
            sub = Subscription(user_id=user_id, service=d["service"], plan=d["plan"], share_count=d["share_count"])
            s.add(sub)
        nb = d["last_charged"] + timedelta(days=d["interval_days"])
        sub.next_billing = nb
        out.append(sub)
    s.flush()
    return out


def _msg(service: str, kind: str, cur: str, best: str, delta: float, usage: dict, share: int) -> str:
    svc = CATALOG[service]
    name = svc.name
    if kind == "ask_usage":
        return f"{name}을(를) 요즘 얼마나 쓰세요? 사용 데이터가 없어 직접 여쭤봐요. (동의하면 자동으로 확인할 수 있어요)"
    saving = (svc.plan(cur).price - svc.plan(best).price) / max(share, 1)
    use = ""
    if "H" in usage:
        use = f"이번 달 {usage['H']:.1f}{'회' if svc.vtype == 'count' else '시간'} 사용하셨어요. "
    if "G" in usage:
        use = f"지금 {usage['G']:.0f}GB 쓰고 계세요. "
    if kind == "ask_idle":
        use = f"{name}을(를) 30일 넘게 안 쓰셨어요. "
    act = "잠시 멈추면" if best == "pause" else f"'{best}' 요금제로 바꾸면"
    return f"{use}{act} 월 {saving:,.0f}원 아낄 수 있어요 (체감 가치 포함 {delta:,.0f}원 이득). 바꿀까요?"


async def run_for_user(user_id: str) -> dict:
    rt = runtime.rt
    now = datetime.now(timezone.utc)
    with SessionLocal() as s:
        user = s.get(User, user_id)
        sync_detected(s, user_id)
        subs = s.query(Subscription).filter_by(user_id=user_id).all()
        w = s.query(Wallet).filter_by(user_id=user_id).first()

        # ① 입력
        inputs, usage_by_sub = [], {}
        for sub in subs:
            u = usage_summary(s, user_id, sub.service, now)
            usage_by_sub[sub.id] = u
            last = u.get("last_used_at") or _aware(sub.last_used_at)
            inputs.append(SubInput(
                sub_id=sub.id, service=sub.service, current_plan=sub.plan, share_count=sub.share_count,
                hours=u.get("H"), hours_ema=u.get("H_bar"), benefit_krw=u.get("B"), storage_gb=u.get("G"),
                days_to_billing=(_aware(sub.next_billing) - now).total_seconds() / 86400 if sub.next_billing else None,
                days_since_used=(now - last).days if last else None,
                v_override=sub.value_per_unit, sigma_override=sub.sigma,
                snoozed=bool(sub.snooze_until and _aware(sub.snooze_until) > now)))
        recent = [t for t in s.query(CardTransaction).filter_by(user_id=user_id) if (now - _aware(t.at)).days <= 30]
        sub_patterns = [p for sub in subs for p in CATALOG[sub.service].merchant_patterns]
        other_14d = sum(t.amount_krw for t in recent
                        if not any(p.upper() in t.merchant_name.upper() for p in sub_patterns)) * 14 / 30

        # ② λ(지출) — 지금 지갑으로 그 지출을 낸다고 할 때 마지막 1원의 매도 한계비용
        holdings: list[Holding] = []
        if w:
            bal = await balances(w)
            holdings = [Holding(sym, u_, rt.market.price_krw(sym)) for sym, u_ in bal.items()]
        ctx = SellContext(gamma0=settings.gamma0.get(user.risk_profile, 2.0))

        def lambda_fn(spend14: float) -> float:
            ctx.expected_spend_14d_krw = spend14
            return rt.optimizer.marginal_cost_for_spend(holdings, spend14, ctx) if holdings else 0.0

        # ③④ 구독 선택 ↔ λ 반복
        res = optimize(inputs, lambda_fn, other_14d, settings.sub_theta_krw, settings.sub_ask_days_before,
                       settings.sub_idle_days, settings.sub_lambda_iters, settings.sub_capacity_eps)
        spend_after = other_14d + sum(d.monthly_cost_best * 14 / 30 for d in res.decisions)
        user.expected_spend_14d_krw = spend_after       # 결제 버퍼 L* 입력

        # ⑤ 질문 대기열 (같은 구독에 열린 질문이 있으면 새로 안 만듦)
        created = []
        for d in res.decisions:
            if d.action == "none":
                continue
            if s.query(SubQuestion).filter_by(subscription_id=d.sub_id, status="pending").count():
                continue
            sub = s.get(Subscription, d.sub_id)
            q = SubQuestion(user_id=user_id, subscription_id=d.sub_id, kind=d.action,
                            proposed_plan=None if d.action == "ask_usage" else d.best, delta_krw=d.delta_krw,
                            message=_msg(d.service, d.action, d.current, d.best, d.delta_krw,
                                         usage_by_sub.get(d.sub_id, {}), sub.share_count))
            s.add(q)
            created.append(q)
        out = {"lambda": res.lam, "lambda_trace": res.lambda_trace, "iterations": res.iterations,
               "expected_spend_14d_krw": round(spend_after),
               "decisions": [d.__dict__ for d in res.decisions]}
        decision(s, "sub_opt", user_id, {"subs": [i.__dict__ for i in inputs], "other_spend_14d": other_14d,
                                         "theta": settings.sub_theta_krw}, out)
        s.commit()
        out["questions"] = [{"id": q.id, "kind": q.kind, "message": q.message, "proposed_plan": q.proposed_plan}
                            for q in created]
        return out


def answer(question_id: str, answer_: str, value: dict | None = None) -> dict:
    """answer: keep(유지) | accept(변경 수락) | later(나중에) | usage(직접 사용량 답변)."""
    now = datetime.now(timezone.utc)
    with SessionLocal() as s:
        q = s.get(SubQuestion, question_id)
        if q is None or q.status != "pending":
            raise ValueError("열린 질문이 아닙니다")
        sub = s.get(Subscription, q.subscription_id)
        svc = CATALOG[sub.service]
        q.status, q.answer = "answered", answer_
        if answer_ == "keep":
            base = sub.value_per_unit if sub.value_per_unit is not None else svc.v_default
            sub.value_per_unit = base * (1 + settings.sub_keep_bump)
            sub.snooze_until = now + timedelta(days=settings.sub_snooze_days)
        elif answer_ == "accept" and q.proposed_plan:
            # 실제 해지·변경은 사용자가 하거나 연동된 서비스에 한해 대신 한다. 다음 청구 뒤 결제가 바뀌었는지로 확인.
            sub.pending_change = q.proposed_plan
            nb = _aware(sub.next_billing) or now + timedelta(days=30)
            sub.pending_verify_after = nb + timedelta(days=3)
            alert(s, "info", f"{svc.name} 변경을 수락했어요. {svc.name} 앱/웹에서 직접 변경해 주세요. "
                  f"다음 청구 후 결제 금액으로 확인할게요.", user_id=q.user_id, audience="user")
        elif answer_ == "usage" and value:
            from .usage import upsert_usage
            month = now.strftime("%Y-%m")
            src = "manual_hours" if "hours" in value or "count" in value else "manual_storage"
            upsert_usage(s, q.user_id, sub.service, month, src, **{k: float(v) for k, v in value.items()
                                                                   if k in ("hours", "count", "storage_gb")})
        s.commit()
        return {"question": q.id, "status": q.status, "subscription": sub.id,
                "pending_change": sub.pending_change, "snooze_until": sub.snooze_until}


def verify_changes(user_id: str) -> list[dict]:
    """수락한 변경이 실제로 반영됐는지: 검증 시점 이후 그 서비스 결제 금액을 본다."""
    now = datetime.now(timezone.utc)
    out = []
    with SessionLocal() as s:
        for sub in s.query(Subscription).filter_by(user_id=user_id):
            if not sub.pending_change or not sub.pending_verify_after or _aware(sub.pending_verify_after) > now:
                continue
            svc = CATALOG[sub.service]
            since = _aware(sub.pending_verify_after) - timedelta(days=10)
            charges = [t.amount_krw for t in s.query(CardTransaction).filter_by(user_id=user_id)
                       if _aware(t.at) >= since and any(p.upper() in t.merchant_name.upper() for p in svc.merchant_patterns)]
            target = svc.plan(sub.pending_change).price / max(sub.share_count, 1)
            ok = (not charges and sub.pending_change == "pause") or any(abs(c - target) <= 0.1 * max(target, 1) for c in charges)
            if ok:
                sub.plan = sub.pending_change
                sub.pending_change = None
            else:
                alert(s, "info", f"{svc.name} 결제가 그대로 나갔어요 ({charges}). 변경이 아직 안 된 것 같아요.",
                      user_id=user_id, audience="user")
            out.append({"subscription": sub.id, "verified": ok, "charges": charges})
        s.commit()
    return out
