import math
from datetime import datetime, timedelta, timezone

from app.algo.sell import Holding, SellContext, SellOptimizer
from app.algo.subscription import SubInput, decide, detect_recurring, optimize
from app.market import prices
from app.market.indicators import Candle, classify_trend
from app.market.state import DecayingValue, MarketState
from app.market.validator import ReportValidator


def _state():
    st = MarketState()
    prices.seed_sim(st)
    return st


# ── 매도 ──────────────────────────────────────────────────────
def test_stable_competes_by_score_not_fixed_order():
    st = _state()
    opt = SellOptimizer(st)
    # ARB 가 포트폴리오의 작은 비중일 때 (크면 위험 항 −γ·RC 때문에 상승 추세여도 먼저 팔린다)
    hs = [Holding("USDC", 10_000, st.price_krw("USDC")), Holding("ARB", 1000, st.price_krw("ARB"), cap_krw=1e12)]
    # ARB 강한 하락 + 언락 → μ 가 크게 음수 → USDC 보다 먼저 팔린다
    c = st.coin("ARB")
    c.T, c.unlock_pct = -0.04, 2.0
    p = opt.plan(hs, 50_000, SellContext())
    assert p.ranking[0] == "ARB" and p.alloc_krw["ARB"] > 0
    # 반대로 ARB 강한 상승이면 USDC 가 먼저
    c.T, c.unlock_pct = 0.04, 0.0
    p = opt.plan(hs, 50_000, SellContext())
    assert p.ranking[0] == "USDC" and p.alloc_krw["USDC"] == 50_000


def test_buffer_psi_limits_stable_drawdown():
    st = _state()
    opt = SellOptimizer(st)
    st.coin("BTC").T = 0.02                             # BTC 상승 추세 → MC ≈ 2% (USDC 0.02% 와 USDC+ψ 3% 사이)
    hs = [Holding("USDC", 1000, st.price_krw("USDC")), Holding("BTC", 0.001, st.price_krw("BTC"), cap_krw=1e12)]
    # USDC 140만원, 14일 지출 120만원 → L* ≈ 131.5만원. USDC 는 L* 선(약 8.5만원)까지만 먼저, 나머지는 BTC
    ctx = SellContext(expected_spend_14d_krw=1_200_000)
    p = opt.plan(hs, 100_000, ctx)
    l_star = opt.l_star(ctx)
    assert abs(p.alloc_krw["USDC"] - (1_400_000 - l_star)) <= 1000
    assert p.alloc_krw["BTC"] > 0


def test_daily_cap_and_relax():
    st = _state()
    opt = SellOptimizer(st)
    v = st.price_krw("ETH")
    hs = [Holding("ETH", 1, v, cap_krw=0.3 * v)]
    p = opt.plan(hs, 0.2 * v, SellContext())
    assert not p.cap_relaxed and abs(p.filled_krw - 0.2 * v) < 1
    p = opt.plan(hs, 0.5 * v, SellContext())          # 상한 넘는 결제 → 상한 풀어 채움 (승인은 이미 됨)
    assert p.cap_relaxed and abs(p.filled_krw - 0.5 * v) < 1


def test_max_three_swap_assets():
    st = _state()
    opt = SellOptimizer(st)
    hs = [Holding(s, 1e9 / st.price_krw(s), st.price_krw(s), cap_krw=3e5) for s in ("BTC", "ETH", "LINK", "SOL", "ARB", "DOGE")]
    p = opt.plan(hs, 1_200_000, SellContext())
    swaps = [s for s, x in p.alloc_krw.items() if x > 0]
    assert len(swaps) <= 3


def test_gamma_formula():
    st = _state()
    st.dvol = 70
    st.report["g_reg"] = DecayingValue(0.1, 0)
    assert math.isclose(SellOptimizer(st).gamma(2.0), 2 * (1 + 0.15 + 1.0))


def test_depeg_probability():
    st = _state()
    st.set_price("USDT", 0.997)
    pi, Z = SellOptimizer(st).depeg_prob("USDT", 0.997)
    assert math.isclose(Z, 0.4, rel_tol=1e-6)
    assert math.isclose(pi, 0.0005 + (0.05 - 0.0005) * 0.4)


def test_m_hat_clip():
    st = _state()
    st.report["nasdaq_14d"] = DecayingValue(0.08, 0)
    st.z_etf, st.sc_hat = 5, 0.05
    assert st.m_hat() == 0.20


# ── 검증기 ────────────────────────────────────────────────────
def test_validator_range_and_step():
    st = _state()
    v = ReportValidator(st)
    r = v.apply({"g_geo": 1.5, "q_hat": 0.03, "news:BTC": 0.9, "bogus": 1})
    assert "g_geo" in r["rejected"] and "bogus" in r["rejected"]
    assert r["accepted"]["q_hat"] == 0.03
    assert "news:BTC" in r["rejected"]                     # 0 → 0.9 는 한 번에 ±0.5 초과
    r = v.apply({"q_hat": 0.09})
    assert "q_hat" in r["rejected"]                        # 0.03 → 0.09 는 ±4%p 초과


def test_decay_half_life():
    dv = DecayingValue(1.0, 1000.0, 48)
    assert math.isclose(dv.current(1000 + 48 * 3600), 0.5)


# ── 추세 ──────────────────────────────────────────────────────
def _candles(drift, n=120, noise=0.01):
    import random
    random.seed(1)
    out, p = [], 100.0
    for _ in range(n):
        o = p
        p = p * (1 + drift + random.gauss(0, noise))
        out.append(Candle(o, max(o, p) * 1.005, min(o, p) * 0.995, p))
    return out


def test_trend_strong_up_and_down():
    up = classify_trend(_candles(0.01))
    dn = classify_trend(_candles(-0.01))
    assert up.state >= 1 and up.T > 0
    assert dn.state <= -1 and dn.T < 0
    assert classify_trend(_candles(0.0, n=30)).detail["reason"] == "insufficient_candles"


# ── 구독 ──────────────────────────────────────────────────────
def test_low_usage_netflix_suggests_cheaper_plan():
    s = SubInput("s1", "netflix", "premium", hours=1.0, hours_ema=1.5, days_to_billing=2)
    d = decide(s, lam=0.0, theta=2000, ask_days=3, idle_days=30)
    assert d.best in ("ad_standard", "pause") and d.action == "ask_change"


def test_not_in_billing_window_no_question():
    s = SubInput("s1", "netflix", "premium", hours=1.0, hours_ema=1.5, days_to_billing=10)
    assert decide(s, 0.0, 2000, 3, 30).action == "none"


def test_heavy_user_keeps_plan():
    s = SubInput("s1", "netflix", "standard", hours=40, hours_ema=40, days_to_billing=2)
    assert decide(s, 0.0, 2000, 3, 30).best == "standard"


def test_no_data_asks_user():
    s = SubInput("s1", "spotify", "individual", days_to_billing=2)
    assert decide(s, 0.0, 2000, 3, 30).action == "ask_usage"


def test_capacity_picks_cheapest_fit():
    s = SubInput("s1", "icloud", "2tb", storage_gb=120, days_to_billing=1)
    d = decide(s, 0.0, 2000, 3, 30)
    assert d.best == "200gb" and d.action == "ask_change"


def test_lambda_iteration_converges():
    subs = [SubInput("a", "netflix", "premium", hours=0.5, hours_ema=0.5, days_to_billing=1),
            SubInput("b", "spotify", "individual", hours=30, hours_ema=30, days_to_billing=1)]
    calls = []

    def lam(spend):
        calls.append(spend)
        return 0.02 + spend / 1e7
    res = optimize(subs, lam, other_spend_14d=200_000, theta=2000, ask_days=3, idle_days=30)
    assert res.iterations <= 3 and len(res.lambda_trace) >= 2
    assert res.lambda_trace[-1] <= res.lambda_trace[0]       # 구독 정리 → 지출 감소 → λ 감소


def test_detect_recurring():
    now = datetime.now(timezone.utc)
    txs = [("NETFLIX.COM", 13500, now - timedelta(days=60)), ("NETFLIX.COM", 13500, now - timedelta(days=30)),
           ("스타벅스", 5000, now - timedelta(days=3))]
    out = detect_recurring(txs)
    assert out and out[0]["service"] == "netflix" and out[0]["plan"] == "standard"
