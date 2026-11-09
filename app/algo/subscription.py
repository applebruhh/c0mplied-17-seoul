"""구독 최적화 (v5, 월 1회, 결제와 별도).

N_{s,k} = V_{s,k} − (1+λ)·P_{s,k}/m_s − κ_{s,k}·1[k ≠ 현재]
  시간형·횟수형 V = v_s · U_s · q_{s,k},  U_s = 0.5·H_s + 0.5·H̄_s
  혜택형        V = B_s · q_{s,k}
  용량형        V = M_s  (요금제 용량 ≥ (1+ε)·G_s) 아니면 −∞
  정지 요금제   q_{s,정지} = σ_s
질문: Δ_s = N_{s,k*} − N_{s,현재} ≥ θ 이고 다음 청구일까지 3일 이하. 30일 넘게 안 쓴 서비스는 먼저 묻는다.
λ 와 구독 선택은 결과가 안 바뀔 때까지 최대 3회 반복한다.

⚠️ CATALOG 의 요금·v_s·σ_s·q·κ 는 설명용 가정값이다. 실제 요금으로 바꿔 넣고 팀이 정할 것.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

NEG_INF = float("-inf")


@dataclass(frozen=True)
class Plan:
    key: str
    price: float          # 월 요금 (원)
    q: float = 1.0        # 가치 비율 (시간형·혜택형)
    capacity_gb: float = 0.0
    kappa: float = 0.0    # 이 요금제로 바꿀 때 비용 (위약금 등)


@dataclass(frozen=True)
class Service:
    key: str
    name: str
    vtype: str            # time|count|benefit|capacity
    v_default: float      # v_s: 사용 1단위(시간·회) 가치 (원)
    sigma_default: float  # σ_s: 정지했을 때 남는 가치 비율
    plans: tuple[Plan, ...]
    merchant_patterns: tuple[str, ...] = ()
    big_m: float = 1e6    # 용량형 M_s

    def plan(self, key: str) -> Plan:
        return next(p for p in self.plans if p.key == key)


def _pause(capacity_gb: float = 0.0) -> Plan:
    return Plan("pause", 0.0, q=0.0, capacity_gb=capacity_gb)


CATALOG: dict[str, Service] = {s.key: s for s in [
    Service("netflix", "넷플릭스", "time", 2500, 0.3, (
        Plan("ad_standard", 5500, 0.85), Plan("standard", 13500, 1.0), Plan("premium", 17000, 1.02), _pause()),
        ("NETFLIX",)),
    Service("tving", "티빙", "time", 2000, 0.4, (
        Plan("ad", 5500, 0.85), Plan("basic", 9500, 0.95), Plan("standard", 13500, 1.0), Plan("premium", 17000, 1.03), _pause()),
        ("TVING", "티빙")),
    Service("youtube_premium", "유튜브 프리미엄", "time", 900, 0.5, (
        Plan("lite", 8500, 0.8), Plan("premium", 14900, 1.0), _pause()),
        ("YOUTUBE", "GOOGLE*YOUTUBE")),
    Service("spotify", "스포티파이", "time", 600, 0.6, (
        Plan("individual", 11990, 1.0), _pause()),
        ("SPOTIFY",)),
    Service("kakao_emoticon", "카카오 이모티콘 플러스", "count", 60, 0.2, (
        Plan("plus", 3900, 1.0), _pause()),
        ("KAKAO", "이모티콘")),
    Service("coupang_wow", "쿠팡 와우", "benefit", 1.0, 0.0, (
        Plan("wow", 7890, 1.0), _pause()),
        ("COUPANG", "쿠팡")),
    Service("naver_plus", "네이버플러스 멤버십", "benefit", 1.0, 0.0, (
        Plan("plus", 4900, 1.0), _pause()),
        ("NAVER", "네이버")),
    Service("icloud", "iCloud+", "capacity", 1.0, 0.0, (
        Plan("50gb", 1100, capacity_gb=50), Plan("200gb", 4400, capacity_gb=200),
        Plan("2tb", 14000, capacity_gb=2000), _pause(capacity_gb=5)),
        ("APPLE.COM/BILL", "ICLOUD")),
    Service("google_one", "Google One", "capacity", 1.0, 0.0, (
        Plan("100gb", 2400, capacity_gb=100), Plan("200gb", 3700, capacity_gb=200),
        Plan("2tb", 11900, capacity_gb=2000), _pause(capacity_gb=15)),
        ("GOOGLE*ONE", "GOOGLE ONE")),
]}


@dataclass
class SubInput:
    sub_id: str
    service: str
    current_plan: str
    share_count: int = 1
    hours: float | None = None            # H_s (이번 달)  — count 형이면 사용 횟수
    hours_ema: float | None = None        # H̄_s (최근 3개월 지수평균)
    benefit_krw: float | None = None      # B_s
    storage_gb: float | None = None       # G_s
    days_to_billing: float | None = None  # D_s
    days_since_used: float | None = None
    v_override: float | None = None
    sigma_override: float | None = None
    snoozed: bool = False


@dataclass
class SubDecision:
    sub_id: str
    service: str
    current: str
    best: str
    delta_krw: float
    scores: dict[str, float]
    action: str            # none|ask_change|ask_idle|ask_usage
    reason: str = ""
    monthly_cost_best: float = 0.0
    monthly_cost_current: float = 0.0


def usage_value(svc: Service, s: SubInput, plan: Plan) -> float | None:
    v = s.v_override if s.v_override is not None else svc.v_default
    sigma = s.sigma_override if s.sigma_override is not None else svc.sigma_default
    q = sigma if plan.key == "pause" else plan.q
    if svc.vtype in ("time", "count"):
        if s.hours is None and s.hours_ema is None:
            return None
        H = s.hours if s.hours is not None else s.hours_ema
        Hb = s.hours_ema if s.hours_ema is not None else s.hours
        return v * (0.5 * H + 0.5 * Hb) * q
    if svc.vtype == "benefit":
        if s.benefit_krw is None:
            return None
        return s.benefit_krw * q
    if svc.vtype == "capacity":
        if s.storage_gb is None:
            return None
        return svc.big_m if plan.capacity_gb >= (1 + EPS[0]) * s.storage_gb else NEG_INF
    raise ValueError(svc.vtype)


EPS = [0.10]   # 용량 여유 ε (config 에서 덮어씀)


def net_benefit(svc: Service, s: SubInput, plan: Plan, lam: float) -> float | None:
    V = usage_value(svc, s, plan)
    if V is None:
        return None
    switch = plan.kappa if plan.key != s.current_plan else 0.0
    return V - (1 + lam) * plan.price / max(s.share_count, 1) - switch


def decide(s: SubInput, lam: float, theta: float, ask_days: int, idle_days: int) -> SubDecision:
    svc = CATALOG[s.service]
    cur_plan = svc.plan(s.current_plan)
    scores: dict[str, float] = {}
    for p in svc.plans:
        n = net_benefit(svc, s, p, lam)
        if n is not None:
            scores[p.key] = n
    if not scores:
        return SubDecision(s.sub_id, s.service, s.current_plan, s.current_plan, 0.0, {}, "ask_usage",
                           "사용 데이터 없음 → 사용자에게 직접 묻고, 답이 없으면 보류", cur_plan.price, cur_plan.price)
    best = max(scores, key=lambda k: scores[k])
    delta = scores[best] - scores.get(s.current_plan, NEG_INF)
    in_window = s.days_to_billing is not None and s.days_to_billing <= ask_days
    idle = s.days_since_used is not None and s.days_since_used > idle_days
    action, reason = "none", ""
    if s.snoozed:
        reason = "유지 응답 후 90일 유예 중"
    elif best != s.current_plan and in_window and idle:
        action, reason = "ask_idle", f"{s.days_since_used:.0f}일째 사용 없음"
    elif best != s.current_plan and in_window and delta >= theta:
        action, reason = "ask_change", f"Δ={delta:,.0f}원 ≥ θ"
    elif best != s.current_plan:
        reason = "청구일 3일 전 아님" if not in_window else f"Δ={delta:,.0f}원 < θ"
    clean = {k: (round(v) if v != NEG_INF else None) for k, v in scores.items()}
    return SubDecision(s.sub_id, s.service, s.current_plan, best, round(delta) if delta != float("inf") else 0,
                       clean, action, reason, svc.plan(best).price / max(s.share_count, 1),
                       cur_plan.price / max(s.share_count, 1))


@dataclass
class OptimizeResult:
    lam: float
    iterations: int
    decisions: list[SubDecision]
    lambda_trace: list[float] = field(default_factory=list)


def optimize(subs: list[SubInput], lambda_fn: Callable[[float], float], other_spend_14d: float,
             theta: float, ask_days: int, idle_days: int, max_iter: int = 3, eps: float = 0.10) -> OptimizeResult:
    """λ ↔ 구독 선택을 고정점까지 반복. lambda_fn(14일 지출) → λ."""
    EPS[0] = eps

    def spend14(choice: dict[str, str]) -> float:
        tot = other_spend_14d
        for s in subs:
            p = CATALOG[s.service].plan(choice[s.sub_id])
            tot += p.price / max(s.share_count, 1) * 14 / 30
        return tot

    choice = {s.sub_id: s.current_plan for s in subs}
    lam = lambda_fn(spend14(choice))
    trace = [lam]
    decisions: list[SubDecision] = []
    it = 0
    for it in range(1, max_iter + 1):
        decisions = [decide(s, lam, theta, ask_days, idle_days) for s in subs]
        new_choice = {d.sub_id: d.best for d in decisions}
        lam_new = lambda_fn(spend14(new_choice))
        trace.append(lam_new)
        stable = new_choice == choice and abs(lam_new - lam) < 1e-6
        choice, lam = new_choice, lam_new
        if stable:
            break
    decisions = [decide(s, lam, theta, ask_days, idle_days) for s in subs]
    # 30일 넘게 안 쓴 서비스 질문을 먼저
    decisions.sort(key=lambda d: (d.action != "ask_idle", d.action == "none", -d.delta_krw))
    return OptimizeResult(lam, it, decisions, trace)


def detect_recurring(txs: list[tuple[str, float, datetime]], min_count: int = 2) -> list[dict]:
    """결제 내역에서 반복 결제 감지: 가맹점 패턴이 카탈로그와 맞고, 25~35일 간격, 금액 ±10%."""
    out = []
    for svc in CATALOG.values():
        hits = sorted([(amt, at) for name, amt, at in txs
                       if any(p.upper() in name.upper() for p in svc.merchant_patterns)], key=lambda t: t[1])
        if len(hits) < min_count:
            continue
        gaps = [(hits[i][1] - hits[i - 1][1]).days for i in range(1, len(hits))]
        amts = [h[0] for h in hits]
        last_amt = amts[-1]
        if all(25 <= g <= 35 for g in gaps[-(min_count - 1):]) and all(abs(a - last_amt) <= 0.1 * last_amt for a in amts[-min_count:]):
            plan = min(svc.plans, key=lambda p: abs(p.price - last_amt) if p.key != "pause" else 1e18)
            shares = max(1, round(plan.price / last_amt)) if last_amt < plan.price * 0.8 else 1
            last_at = hits[-1][1]
            if last_at.tzinfo is None:
                last_at = last_at.replace(tzinfo=timezone.utc)
            out.append({"service": svc.key, "plan": plan.key, "amount": last_amt, "share_count": shares,
                        "last_charged": last_at, "interval_days": gaps[-1]})
    return out
