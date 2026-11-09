"""시뮬레이션 체인: 지갑 잔고 장부 + 코인/USDC 상수곱 풀 + 블록 지연 + 실패 주입.

풀 가격은 견적 시점의 시장가로 맞추고, 풀 크기(reserve)로 슬리피지를 만든다.
상수곱 풀에서 x 만큼 팔 때 평균 슬리피지 ≈ x / R  →  엔진의 D(평균 슬리피지 = x/2D) 는 R/2.
"""
from __future__ import annotations

import asyncio
import itertools
from collections import defaultdict

from ..assets import ASSETS
from ..market.state import MarketState
from .base import Quote, Receipt, SwapError, TxResult, WalletRef

DEFAULT_RESERVE_USD = {"BTC": 8e6, "ETH": 5e6, "LINK": 3e5, "AERO": 2e5, "SOL": 1.5e6,
                       "ARB": 1.5e5, "DOGE": 2e5, "USDT": 2e7, "USDC": 2e7}
POOL_FEE = {"BTC": 0.0005, "ETH": 0.0005, "USDT": 0.0001}


class SimChain:
    def __init__(self, state: MarketState, block_time: float = 0.05):
        self.state = state
        self.block_time = block_time
        self.ledger: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        self.reserve_usd = dict(DEFAULT_RESERVE_USD)
        self.fail_plan: dict[str, list[str]] = defaultdict(list)   # sym → ["quote"|"revert"|"slippage"|"noliq"]
        self.pending: dict[str, Receipt] = {}
        self._ids = itertools.count(1)

    # 테스트/데모 도구
    def fund(self, address: str, symbol: str, units: float) -> None:
        self.ledger[address.lower()][symbol] += units

    def inject_failure(self, symbol: str, *kinds: str) -> None:
        self.fail_plan[symbol].extend(kinds)

    def _pop_fail(self, symbol: str, stage: str) -> str | None:
        plan = self.fail_plan.get(symbol)
        if plan and plan[0] in ({"quote", "noliq"} if stage == "quote" else {"revert", "slippage"}):
            return plan.pop(0)
        return None

    # ChainAdapter
    async def balances(self, wallet: WalletRef) -> dict[str, float]:
        return {k: v for k, v in self.ledger[wallet.address.lower()].items() if v > 1e-12}

    def _amm_out(self, sell: str, buy: str, units: float) -> tuple[float, float]:
        p_sell, p_buy = self.state.price_usd(sell), self.state.price_usd(buy)
        R = self.reserve_usd.get(sell, 1e5)
        x_usd = units * p_sell
        fee = POOL_FEE.get(sell, 0.003)
        out_usd = R * (x_usd * (1 - fee)) / (R + x_usd * (1 - fee))   # 상수곱 (가격 기준 정규화)
        return out_usd / p_buy, 1 - out_usd / x_usd if x_usd else 0.0

    async def quote(self, wallet: WalletRef, sell: str, buy: str, sell_units: float, slippage: float) -> Quote:
        f = self._pop_fail(sell, "quote")
        if f == "noliq":
            raise SwapError(f"{sell} 유동성 부족", retryable=False)
        if f == "quote":
            raise SwapError(f"{sell} 견적 실패 (일시)")
        bal = self.ledger[wallet.address.lower()][sell]
        if bal + 1e-12 < sell_units:
            raise SwapError(f"{sell} 잔고 부족 {bal} < {sell_units}", retryable=False)
        out, impact = self._amm_out(sell, buy, sell_units)
        return Quote(sell, buy, sell_units, out, out * (1 - slippage), impact)

    async def swap(self, wallet: WalletRef, q: Quote) -> TxResult:
        h = f"0xsim{next(self._ids):060x}"
        f = self._pop_fail(q.sell_symbol, "swap")
        addr = wallet.address.lower()
        if f == "revert":
            self.pending[h] = Receipt(False, error="execution reverted")
            return TxResult(h)
        out, _ = self._amm_out(q.sell_symbol, q.buy_symbol, q.sell_units)
        if f == "slippage":
            out = q.min_buy_units * 0.99
        if out < q.min_buy_units:
            self.pending[h] = Receipt(False, error="slippage exceeded (minBuyAmount)")
            return TxResult(h)
        self.ledger[addr][q.sell_symbol] -= q.sell_units
        self.ledger[addr][q.buy_symbol] += out
        self.pending[h] = Receipt(True, {q.buy_symbol: out})
        return TxResult(h)

    async def transfer(self, wallet: WalletRef, symbol: str, units: float, to: str) -> TxResult:
        h = f"0xsim{next(self._ids):060x}"
        src = self.ledger[wallet.address.lower()]
        if src[symbol] + 1e-9 < units:
            self.pending[h] = Receipt(False, error="insufficient balance")
            return TxResult(h)
        src[symbol] -= units
        self.ledger[to.lower()][symbol] += units
        self.pending[h] = Receipt(True, {})
        return TxResult(h)

    async def wait(self, tx: TxResult, wallet: WalletRef, out_symbol: str | None = None, timeout: float = 60) -> Receipt:
        await asyncio.sleep(self.block_time)
        return self.pending.pop(tx.tx_hash, Receipt(False, error="unknown tx"))

    async def depth_krw(self, symbol: str, price_usd: float, usdkrw: float) -> float:
        if symbol == "USDC":
            return 1e12
        return self.reserve_usd.get(symbol, 1e5) * usdkrw / 2
