"""실시간 피드 파서 오프라인 테스트 (네트워크 없이, 공개 API 응답 형식으로)."""
import asyncio

import httpx

from app.market import macro, news_llm, prices
from app.market.state import MarketState


def _mock(routes):
    def handler(req: httpx.Request):
        for key, body in routes.items():
            if key in str(req.url):
                return httpx.Response(200, json=body)
        return httpx.Response(404, json={})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_kraken_peg_and_fx_parsers():
    async def go():
        async with _mock({"kraken": {"result": {"USDTZUSD": {"c": ["0.99970", "1"]}, "USDCUSD": {"c": ["1.0001", "1"]}}},
                          "dunamu": [{"basePrice": 1391.5}]}) as c:
            return await prices.fetch_pegs(c), await prices.fetch_fx(c)
    pegs, fx = asyncio.run(go())
    assert pegs == {"USDT": 0.9997, "USDC": 1.0001} and fx == 1391.5


def test_binance_klines_funding_and_trend():
    rows = []
    p = 100.0
    for i in range(201):
        o, p = p, p * 1.01
        rows.append([i, str(o), str(p * 1.005), str(o * 0.995), str(p), "1", 0, "0", 0, "0", "0", "0"])
    async def go():
        async with _mock({"klines": rows,
                          "premiumIndex": {"lastFundingRate": "0.0003"},
                          "openInterestHist": [{"sumOpenInterestValue": str(v)} for v in range(1, 31)]}) as c:
            ks = await macro.klines(c, "BTCUSDT")
            f = await macro.funding_oi(c, "BTCUSDT")
            return ks, f
    ks, (annual, oi_high) = asyncio.run(go())
    assert len(ks) == 200                               # 진행 중 봉 제외
    assert abs(annual - 0.0003 * 3 * 365) < 1e-9 and oi_high
    assert macro.leverage_term(annual, oi_high) == -0.015
    st = MarketState()
    macro.apply_candles(st, {"BTC": ks, "ETH": ks})
    assert st.coin("BTC").T > 0


def test_llama_and_deribit_parsers():
    async def go():
        rows = [{"totalCirculatingUSD": {"peggedUSD": 1e11 * (1 + i / 1000)}} for i in range(30)]
        async with _mock({"stablecoincharts": rows,
                          "volatility_index": {"result": {"data": [[0, 50, 55, 45, 52.5]]}}}) as c:
            return await macro.stable_supply_growth(c), await macro.dvol(c)
    g, dv = asyncio.run(go())
    assert abs(g - (1.029 / 1.015 - 1)) < 1e-9 and dv == 52.5


def test_llm_json_parse():
    out = news_llm.parse_llm_json('설명 {"values": {"g_reg": 0.3, "news:ETH": -0.2}, "evidence": {"g_reg": "SEC"}} 끝')
    assert out["values"]["g_reg"] == 0.3
