"""실시간 시장 상태 (메모리). 결제 승인 경로는 네트워크를 타지 않고 여기 캐시만 읽는다.

값의 출처
- 피드(데이터): 가격, 환율, 펀딩비, 미결제약정, DVOL, ETF 순유입, 스테이블 공급, 풀 깊이, 캔들 → 추세
- 보고서(LLM): 나스닥·달러 전망, 알트 강도, 테마, 지정학·규제·발행사 점수, 코인 뉴스
  → 반드시 validator.ReportValidator 를 거쳐야 여기에 들어온다.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field


@dataclass
class Px:
    usd: float
    ts: float


@dataclass
class DecayingValue:
    """보고서 값: 반감기에 따라 0(중립)으로 줄어든다."""
    value: float = 0.0
    set_at: float = 0.0
    half_life_h: float = 48.0

    def current(self, now: float | None = None) -> float:
        if self.set_at == 0:
            return self.value
        dt_h = ((now or time.time()) - self.set_at) / 3600
        return self.value * math.pow(0.5, dt_h / self.half_life_h)


@dataclass
class CoinSignals:
    trend_state: int = 0              # +2 강한상승 … −2 강한하락
    T: float = 0.0                    # 추세 프리미엄 (소수)
    rho: float = 0.0                  # 과열·투매 되돌림
    funding_annual: float = 0.0       # 연환산 펀딩비
    oi_at_30d_high: bool = False
    L: float = 0.0                    # 레버리지 과열 항
    unlock_pct: float = 0.0           # 14일 안 언락 (유통량 대비 %)
    staking_apr: float | None = None
    depth_krw: float = 0.0            # D_i: 평균 슬리피지 = x / (2D)
    sigma_eps: float | None = None    # 회귀로 추정된 고유 변동성 (없으면 기본값)
    pool_imbalance: float = 0.0       # 스테이블 b_j
    updated_at: float = 0.0


@dataclass
class MarketState:
    prices: dict[str, Px] = field(default_factory=dict)
    usdkrw: Px = field(default_factory=lambda: Px(0.0, 0.0))
    coins: dict[str, CoinSignals] = field(default_factory=dict)

    # 피드 값
    z_etf: float = 0.0                # BTC 현물 ETF 5일 순유입 z
    sc_hat: float = 0.0               # 스테이블 시총 14일 증가율 (소수)
    dvol: float = 50.0
    btc_sigma_14d: float = 0.09       # Ω 의 BTC 팩터 14일 변동성
    alt_sigma_14d: float = 0.06       # Ω 의 ALT 팩터 14일 변동성
    nasdaq_r14: float = 0.0
    dxy_r14: float = 0.0
    alt_vs_btc_r14: float = 0.0

    # 보고서 값 (검증 통과분만)
    report: dict[str, DecayingValue] = field(default_factory=dict)
    report_meta: dict = field(default_factory=dict)

    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    # ── 가격 ───────────────────────────────────────────────────
    def set_price(self, sym: str, usd: float, ts: float | None = None) -> None:
        with self.lock:
            self.prices[sym] = Px(usd, ts or time.time())

    def set_usdkrw(self, rate: float, ts: float | None = None) -> None:
        with self.lock:
            self.usdkrw = Px(rate, ts or time.time())

    def price_usd(self, sym: str, max_age: float | None = None) -> float:
        p = self.prices.get(sym)
        if p is None:
            raise KeyError(sym)
        if max_age is not None and time.time() - p.ts > max_age:
            raise TimeoutError(f"{sym} 시세가 {time.time() - p.ts:.0f}초 지났음")
        return p.usd

    def price_krw(self, sym: str, max_age: float | None = None) -> float:
        fx = self.fx(max_age)
        return self.price_usd(sym, max_age) * fx

    def fx(self, max_age: float | None = None) -> float:
        if self.usdkrw.usd <= 0:
            raise KeyError("USDKRW")
        if max_age is not None and time.time() - self.usdkrw.ts > max(max_age, 300):
            raise TimeoutError("USDKRW 환율이 오래됨")
        return self.usdkrw.usd

    def coin(self, sym: str) -> CoinSignals:
        with self.lock:
            return self.coins.setdefault(sym, CoinSignals())

    # ── 보고서 값 조회 ─────────────────────────────────────────
    def r(self, key: str, default: float = 0.0) -> float:
        dv = self.report.get(key)
        return dv.current() if dv else default

    # ── 파생 지표 ──────────────────────────────────────────────
    def m_hat(self) -> float:
        """BTC 14일 전망 (v4): clip(1.5N̂ − 2.0DXŶ + 2%·z_ETF + 1.0SĈ − 5%·g_geo, ±20%)."""
        n_hat = self.r("nasdaq_14d", 0.3 * self.nasdaq_r14)
        dxy_hat = self.r("dxy_14d", 0.3 * self.dxy_r14)
        v = 1.5 * n_hat - 2.0 * dxy_hat + 0.02 * self.z_etf + 1.0 * self.sc_hat - 0.05 * self.r("g_geo")
        return max(-0.20, min(0.20, v))

    def q_hat(self) -> float:
        return self.r("q_hat", max(-0.10, min(0.10, 0.3 * self.alt_vs_btc_r14)))

    def market_vol_14d(self) -> float:
        return (self.dvol / 100.0) * math.sqrt(14 / 365)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "usdkrw": self.usdkrw.usd,
                "prices_usd": {k: round(v.usd, 8) for k, v in self.prices.items()},
                "m_hat": round(self.m_hat(), 5),
                "q_hat": round(self.q_hat(), 5),
                "z_etf": self.z_etf, "sc_hat": self.sc_hat, "dvol": self.dvol,
                "nasdaq_r14": self.nasdaq_r14, "dxy_r14": self.dxy_r14, "alt_vs_btc_r14": self.alt_vs_btc_r14,
                "report": {k: round(v.current(), 5) for k, v in self.report.items()},
                "report_meta": self.report_meta,
                "coins": {k: {kk: vv for kk, vv in vars(v).items()} for k, v in self.coins.items()},
            }


market = MarketState()
