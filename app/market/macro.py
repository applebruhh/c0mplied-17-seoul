"""실시간 매크로·코인 신호 수집 (피드 값). 보고서(LLM) 값은 news_llm.py 가 따로 넣는다.

| 항목                 | 출처                                                  | 주기 |
| 나스닥 14일 수익률   | FRED NASDAQCOM (FRED_API_KEY)                         | 10분 |
| 달러인덱스 14일      | FRED DTWEXBGS                                         | 10분 |
| BTC 현물 ETF 순유입  | SoSoValue (SOSOVALUE_API_KEY) → z(최근 5일 합)        | 10분 |
| 스테이블 시총 14일   | DefiLlama stablecoincharts/all                        | 10분 |
| DVOL                 | Deribit get_volatility_index_data                     | 10분 |
| 펀딩비 · 미결제약정  | Binance Futures premiumIndex, openInterestHist        | 10분 |
| 일봉 → 추세·σε·Ω     | Binance klines 1d (200개)                             | 10분 (값은 일봉 마감 때만 바뀜) |

각 fetch 는 독립적으로 실패할 수 있고, 실패하면 이전 값을 그대로 둔다.
"""
from __future__ import annotations

import asyncio
import logging
import math
import statistics
import time
from datetime import datetime, timedelta, timezone

import httpx

from ..assets import ASSETS
from ..config import settings
from .indicators import Candle, classify_trend, log_returns, regress_beta
from .state import MarketState

log = logging.getLogger("macro")

FRED = "https://api.stlouisfed.org/fred/series/observations"
LLAMA_STABLES = "https://stablecoins.llama.fi/stablecoincharts/all"
DERIBIT_DVOL = "https://www.deribit.com/api/v2/public/get_volatility_index_data"
BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
BINANCE_PREMIUM = "https://fapi.binance.com/fapi/v1/premiumIndex"
BINANCE_OI_HIST = "https://fapi.binance.com/futures/data/openInterestHist"
SOSO_ETF = "https://api.sosovalue.xyz/openapi/v2/etf/historicalInflowChart"


async def fred_r14(client: httpx.AsyncClient, series: str) -> float | None:
    if not settings.fred_api_key:
        return None
    start = (datetime.now(timezone.utc) - timedelta(days=40)).date().isoformat()
    r = await client.get(FRED, params={"series_id": series, "api_key": settings.fred_api_key,
                                       "file_type": "json", "observation_start": start}, timeout=8)
    r.raise_for_status()
    obs = [(o["date"], float(o["value"])) for o in r.json()["observations"] if o["value"] not in (".", "")]
    if len(obs) < 11:
        return None
    last_date = datetime.fromisoformat(obs[-1][0])
    base = next((v for d, v in reversed(obs) if (last_date - datetime.fromisoformat(d)).days >= 14), obs[0][1])
    return obs[-1][1] / base - 1


async def stable_supply_growth(client: httpx.AsyncClient) -> float:
    r = await client.get(LLAMA_STABLES, timeout=10)
    r.raise_for_status()
    rows = r.json()
    cur = rows[-1]["totalCirculatingUSD"]["peggedUSD"]
    prev = rows[-15]["totalCirculatingUSD"]["peggedUSD"]
    return cur / prev - 1


async def dvol(client: httpx.AsyncClient) -> float:
    end = int(time.time() * 1000)
    r = await client.get(DERIBIT_DVOL, params={"currency": "BTC", "resolution": "3600",
                                               "start_timestamp": end - 6 * 3600_000, "end_timestamp": end}, timeout=8)
    r.raise_for_status()
    return float(r.json()["result"]["data"][-1][4])


async def etf_z(client: httpx.AsyncClient) -> float | None:
    """⚠️ SoSoValue 응답 형식은 계약 시 문서로 다시 확인할 것."""
    if not settings.sosovalue_api_key:
        return None
    r = await client.post(SOSO_ETF, json={"type": "us-btc-spot"},
                          headers={"x-soso-api-key": settings.sosovalue_api_key}, timeout=8)
    r.raise_for_status()
    rows = sorted(r.json()["data"], key=lambda x: x["date"])
    flows = [float(x["totalNetInflow"]) for x in rows][-65:]
    sums = [sum(flows[i:i + 5]) for i in range(len(flows) - 4)]
    if len(sums) < 10:
        return None
    mu, sd = statistics.mean(sums[:-1]), statistics.pstdev(sums[:-1]) or 1.0
    return (sums[-1] - mu) / sd


async def klines(client: httpx.AsyncClient, symbol: str, limit: int = 200) -> list[Candle]:
    r = await client.get(BINANCE_KLINES, params={"symbol": symbol, "interval": "1d", "limit": limit}, timeout=8)
    r.raise_for_status()
    # 마지막 봉은 진행 중이므로 제외 → UTC 00:00(KST 09:00) 마감 일봉만 사용
    return [Candle(float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])) for k in r.json()[:-1]]


async def funding_oi(client: httpx.AsyncClient, symbol: str) -> tuple[float, bool]:
    r = await client.get(BINANCE_PREMIUM, params={"symbol": symbol}, timeout=5)
    r.raise_for_status()
    annual = float(r.json()["lastFundingRate"]) * 3 * 365
    r2 = await client.get(BINANCE_OI_HIST, params={"symbol": symbol, "period": "1d", "limit": 30}, timeout=5)
    r2.raise_for_status()
    oi = [float(x["sumOpenInterestValue"]) for x in r2.json()]
    return annual, bool(oi) and oi[-1] >= max(oi)


def leverage_term(funding_annual: float, oi_high: bool) -> float:
    """v4 L_i: 펀딩비 > 30% 이고 OI 30일 최고 → −1.5%, 펀딩비 < −20% → +1%."""
    if funding_annual > 0.30 and oi_high:
        return -0.015
    if funding_annual < -0.20:
        return 0.01
    return 0.0


def apply_candles(state: MarketState, series: dict[str, list[Candle]]) -> None:
    """추세(T, ρ), 고유 변동성 σε, 팩터 변동성 Ω 를 일봉으로 갱신."""
    btc = series.get("BTC")
    if not btc or len(btc) < 75:
        return
    btc_r = log_returns([c.close for c in btc])
    state.btc_sigma_14d = statistics.pstdev(btc_r[-90:]) * math.sqrt(14)
    alt_rel: list[list[float]] = []
    for sym, cs in series.items():
        tr = classify_trend(cs)
        sig = state.coin(sym)
        sig.trend_state, sig.T, sig.rho = tr.state, tr.T, tr.rho
        if sym != "BTC" and len(cs) >= 75:
            r = log_returns([c.close for c in cs])
            _, se = regress_beta(r[-180:], btc_r[-180:])
            sig.sigma_eps = se * math.sqrt(14)
            n = min(len(r), len(btc_r))
            alt_rel.append([a - b for a, b in zip(r[-n:], btc_r[-n:])])
        sig.updated_at = time.time()
    if alt_rel:
        n = min(len(x) for x in alt_rel)
        idx = [statistics.mean(x[-n:][i] for x in alt_rel) for i in range(n)]
        state.alt_sigma_14d = statistics.pstdev(idx[-90:]) * math.sqrt(14)
        state.alt_vs_btc_r14 = math.exp(sum(idx[-14:])) - 1


async def refresh_all(state: MarketState) -> dict:
    """모든 피드를 병렬로 가져와 상태에 반영. 결과 요약을 돌려준다 (decision_log 기록용)."""
    errors: dict[str, str] = {}
    async with httpx.AsyncClient() as client:
        async def safe(name, coro):
            try:
                return await coro
            except Exception as e:  # noqa: BLE001
                errors[name] = repr(e)[:200]
                return None

        coin_syms = {s: a.binance_symbol for s, a in ASSETS.items() if a.binance_symbol}
        tasks = {
            "nasdaq": safe("nasdaq", fred_r14(client, "NASDAQCOM")),
            "dxy": safe("dxy", fred_r14(client, "DTWEXBGS")),
            "sc": safe("sc", stable_supply_growth(client)),
            "dvol": safe("dvol", dvol(client)),
            "etf": safe("etf", etf_z(client)),
            **{f"k:{s}": safe(f"k:{s}", klines(client, b)) for s, b in coin_syms.items()},
            **{f"f:{s}": safe(f"f:{s}", funding_oi(client, b)) for s, b in coin_syms.items()},
        }
        res = dict(zip(tasks.keys(), await asyncio.gather(*tasks.values())))

    with state.lock:
        if res["nasdaq"] is not None:
            state.nasdaq_r14 = res["nasdaq"]
        if res["dxy"] is not None:
            state.dxy_r14 = res["dxy"]
        if res["sc"] is not None:
            state.sc_hat = res["sc"]
        if res["dvol"] is not None:
            state.dvol = res["dvol"]
        if res["etf"] is not None:
            state.z_etf = res["etf"]
        for s in coin_syms:
            f = res.get(f"f:{s}")
            if f:
                sig = state.coin(s)
                sig.funding_annual, sig.oi_at_30d_high = f
                sig.L = leverage_term(*f)
        apply_candles(state, {s: res[f"k:{s}"] for s in coin_syms if res.get(f"k:{s}")})
    return {"errors": errors, "m_hat": state.m_hat(), "q_hat": state.q_hat(), "dvol": state.dvol}
