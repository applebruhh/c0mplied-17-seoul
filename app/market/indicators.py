"""차트 지표와 5단계 추세 판정 (v4 '코인 고유 요인' T_i, ρ_i).

판정 기준 (KST 09:00 = UTC 00:00 마감 일봉):
  A  이평선 배열 : 종가 > MA20 > MA60 → +1, 종가 < MA20 < MA60 → −1, 그 외 0
  S  MA60 기울기 : 10일 전 대비 MA60 변화율 > +1% → +1, < −1% → −1, 그 외 0
  W  스윙 구조   : 최근 두 스윙 고점·저점이 모두 높아짐 → +1, 모두 낮아짐 → −1, 그 외 0
  ADX14 ≥ 25     : 추세가 '강함'
  강한 상승(+2)  : A=+1, S=+1, W=+1, ADX≥25        → T = +4%
  상승(+1)       : A=+1 이고 (S=+1 또는 W=+1)       → T = +2%
  하락(−1)/강한 하락(−2) : 대칭                      → T = −2% / −4%
  나머지 횡보(0)                                    → T = 0
과열 보정 ρ: 강한 상승 + RSI14>75 + 종가>MA20+2·ATR14 → −2%, 강한 하락 + RSI14<25 + 종가<MA20−2·ATR14 → +2%
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class Candle:
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


def sma(xs: list[float], n: int) -> float:
    return sum(xs[-n:]) / n


def rsi(closes: list[float], n: int = 14) -> float:
    gains, losses = 0.0, 0.0
    # Wilder smoothing
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / n, losses / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def _tr(c: list[Candle], i: int) -> float:
    return max(c[i].high - c[i].low, abs(c[i].high - c[i - 1].close), abs(c[i].low - c[i - 1].close))


def atr(c: list[Candle], n: int = 14) -> float:
    trs = [_tr(c, i) for i in range(1, len(c))]
    a = sum(trs[:n]) / n
    for t in trs[n:]:
        a = (a * (n - 1) + t) / n
    return a


def adx(c: list[Candle], n: int = 14) -> float:
    pdm, ndm, trs = [], [], []
    for i in range(1, len(c)):
        up = c[i].high - c[i - 1].high
        dn = c[i - 1].low - c[i].low
        pdm.append(up if up > dn and up > 0 else 0.0)
        ndm.append(dn if dn > up and dn > 0 else 0.0)
        trs.append(_tr(c, i))

    def wilder(xs):
        s = sum(xs[:n])
        out = [s]
        for x in xs[n:]:
            s = s - s / n + x
            out.append(s)
        return out

    tr_s, p_s, n_s = wilder(trs), wilder(pdm), wilder(ndm)
    dxs = []
    for t, p, m in zip(tr_s, p_s, n_s):
        if t == 0:
            dxs.append(0.0)
            continue
        pdi, ndi = 100 * p / t, 100 * m / t
        dxs.append(0.0 if pdi + ndi == 0 else 100 * abs(pdi - ndi) / (pdi + ndi))
    if len(dxs) < n:
        return sum(dxs) / max(len(dxs), 1)
    a = sum(dxs[:n]) / n
    for d in dxs[n:]:
        a = (a * (n - 1) + d) / n
    return a


def swing_structure(c: list[Candle], k: int = 2) -> int:
    highs, lows = [], []
    for i in range(k, len(c) - k):
        win = c[i - k:i + k + 1]
        if c[i].high == max(x.high for x in win):
            highs.append(c[i].high)
        if c[i].low == min(x.low for x in win):
            lows.append(c[i].low)
    if len(highs) < 2 or len(lows) < 2:
        return 0
    if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
        return 1
    if highs[-1] < highs[-2] and lows[-1] < lows[-2]:
        return -1
    return 0


@dataclass
class TrendResult:
    state: int
    T: float
    rho: float
    detail: dict


def classify_trend(c: list[Candle]) -> TrendResult:
    if len(c) < 75:
        return TrendResult(0, 0.0, 0.0, {"reason": "insufficient_candles", "n": len(c)})
    closes = [x.close for x in c]
    close = closes[-1]
    ma20, ma60 = sma(closes, 20), sma(closes, 60)
    ma60_prev = sum(closes[-70:-10]) / 60
    A = 1 if close > ma20 > ma60 else (-1 if close < ma20 < ma60 else 0)
    slope = (ma60 - ma60_prev) / ma60_prev
    S = 1 if slope > 0.01 else (-1 if slope < -0.01 else 0)
    W = swing_structure(c[-60:])
    ax = adx(c[-100:] if len(c) >= 100 else c)
    r = rsi(closes[-100:] if len(closes) >= 100 else closes)
    at = atr(c[-100:] if len(c) >= 100 else c)

    if A == 1 and S == 1 and W == 1 and ax >= 25:
        st = 2
    elif A == 1 and (S == 1 or W == 1):
        st = 1
    elif A == -1 and S == -1 and W == -1 and ax >= 25:
        st = -2
    elif A == -1 and (S == -1 or W == -1):
        st = -1
    else:
        st = 0
    T = {2: 0.04, 1: 0.02, 0: 0.0, -1: -0.02, -2: -0.04}[st]
    rho = 0.0
    if st == 2 and r > 75 and close > ma20 + 2 * at:
        rho = -0.02
    elif st == -2 and r < 25 and close < ma20 - 2 * at:
        rho = 0.02
    return TrendResult(st, T, rho, {"A": A, "S": S, "W": W, "adx": round(ax, 2), "rsi": round(r, 2),
                                    "ma20": ma20, "ma60": ma60, "atr": at, "slope60": round(slope, 4)})


def log_returns(closes: list[float]) -> list[float]:
    return [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]


def regress_beta(y: list[float], x: list[float]) -> tuple[float, float]:
    """y = a + b x + e → (b, std(e))."""
    n = min(len(x), len(y))
    x, y = x[-n:], y[-n:]
    mx, my = sum(x) / n, sum(y) / n
    vx = sum((a - mx) ** 2 for a in x)
    b = 0.0 if vx == 0 else sum((a - mx) * (c - my) for a, c in zip(x, y)) / vx
    resid = [c - my - b * (a - mx) for a, c in zip(x, y)]
    se = math.sqrt(sum(e * e for e in resid) / max(n - 2, 1))
    return b, se
