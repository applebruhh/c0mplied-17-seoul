"""실시간 시세.

live:
  - 코인 USD 가격 : Binance 웹소켓 miniTicker (XXXUSDT) × USDT/USD 페그
  - 스테이블 페그 : Kraken 공개 시세 USDTUSD, USDCUSD (10초 주기)
  - USD/KRW      : 두나무 환율 API (60초 주기). 원화 환산은 거래소 원화가(김치 프리미엄) 대신 환율을 쓴다.
                   회사가 받는 USDC 를 결국 환율 근처로 원화화하기 때문이다.
sim:
  - 시드 가격에서 작은 랜덤워크
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time

import httpx

from ..assets import ASSETS
from .state import MarketState

log = logging.getLogger("prices")

SIM_SEED_USD = {"BTC": 100_000.0, "ETH": 3_500.0, "LINK": 20.0, "AERO": 1.2, "SOL": 180.0,
                "ARB": 0.60, "DOGE": 0.20, "USDC": 1.0, "USDT": 1.0}
SIM_USDKRW = 1_400.0


def seed_sim(state: MarketState) -> None:
    for s, p in SIM_SEED_USD.items():
        state.set_price(s, p)
    state.set_usdkrw(SIM_USDKRW)


async def sim_price_loop(state: MarketState, period: float = 1.0) -> None:
    seed_sim(state)
    while True:
        await asyncio.sleep(period)
        for s, px in list(state.prices.items()):
            vol = 0.0 if ASSETS.get(s) and ASSETS[s].is_stable else 0.0008
            state.set_price(s, px.usd * (1 + random.gauss(0, vol)))
        # 스테이블은 페그 근처에서 흔들림
        for s in ("USDC", "USDT"):
            state.set_price(s, 1.0 + random.gauss(0, 0.0002))
        state.set_usdkrw(state.usdkrw.usd * (1 + random.gauss(0, 0.0001)))


# ── live ─────────────────────────────────────────────────────────
BINANCE_WS = "wss://stream.binance.com:9443/stream?streams="
KRAKEN = "https://api.kraken.com/0/public/Ticker?pair=USDTZUSD,USDCUSD"
DUNAMU_FX = "https://quotation-api-cdn.dunamu.com/v1/forex/recent?codes=FRX.KRWUSD"


async def fetch_fx(client: httpx.AsyncClient) -> float:
    r = await client.get(DUNAMU_FX, timeout=5)
    r.raise_for_status()
    return float(r.json()[0]["basePrice"])


async def fetch_pegs(client: httpx.AsyncClient) -> dict[str, float]:
    r = await client.get(KRAKEN, timeout=5)
    r.raise_for_status()
    res = r.json()["result"]
    out = {}
    for k, v in res.items():
        last = float(v["c"][0])
        if k.startswith("USDT"):
            out["USDT"] = last
        elif k.startswith("USDC"):
            out["USDC"] = last
    return out


async def peg_fx_loop(state: MarketState) -> None:
    async with httpx.AsyncClient() as client:
        n = 0
        while True:
            try:
                for s, p in (await fetch_pegs(client)).items():
                    state.set_price(s, p)
                if n % 6 == 0:
                    state.set_usdkrw(await fetch_fx(client))
            except Exception as e:  # noqa: BLE001
                log.warning("peg/fx fetch failed: %s", e)
            n += 1
            await asyncio.sleep(10)


async def binance_ws_loop(state: MarketState) -> None:
    import websockets

    syms = {a.binance_symbol.lower(): s for s, a in ASSETS.items() if a.binance_symbol}
    url = BINANCE_WS + "/".join(f"{b}@miniTicker" for b in syms)
    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                async for raw in ws:
                    d = json.loads(raw).get("data", {})
                    sym = syms.get(d.get("s", "").lower())
                    if sym:
                        usdt = state.prices.get("USDT")
                        peg = usdt.usd if usdt else 1.0
                        state.set_price(sym, float(d["c"]) * peg, d.get("E", time.time() * 1000) / 1000)
        except Exception as e:  # noqa: BLE001
            log.warning("binance ws reconnect: %s", e)
            await asyncio.sleep(2)
