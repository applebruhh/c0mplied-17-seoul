"""체인 어댑터 인터페이스: 임베디드 지갑 잔고 · DEX 견적/스왑 · 토큰 송금 · 영수증."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class WalletRef:
    address: str
    provider: str = "sim"           # sim|local|privy
    provider_wallet_id: str = ""


@dataclass
class Quote:
    sell_symbol: str
    buy_symbol: str
    sell_units: float
    buy_units: float                # 예상 수령량
    min_buy_units: float            # 슬리피지 한도 반영 최소 수령량
    price_impact: float             # 시장가 대비 손실 비율 (수수료+슬리피지)
    raw: dict = field(default_factory=dict)


@dataclass
class TxResult:
    tx_hash: str


@dataclass
class Receipt:
    success: bool
    received: dict[str, float] = field(default_factory=dict)   # 지갑이 받은 토큰 (swap out)
    error: str | None = None


class SwapError(Exception):
    """견적 실패·유동성 부족·슬리피지 초과 등. retryable=False 면 대체 코인으로 넘어간다."""

    def __init__(self, msg: str, retryable: bool = True):
        super().__init__(msg)
        self.retryable = retryable


class ChainAdapter(Protocol):
    async def balances(self, wallet: WalletRef) -> dict[str, float]: ...
    async def quote(self, wallet: WalletRef, sell: str, buy: str, sell_units: float, slippage: float) -> Quote: ...
    async def swap(self, wallet: WalletRef, q: Quote) -> TxResult: ...
    async def transfer(self, wallet: WalletRef, symbol: str, units: float, to: str) -> TxResult: ...
    async def wait(self, tx: TxResult, wallet: WalletRef, out_symbol: str | None = None, timeout: float = 60) -> Receipt: ...
    async def depth_krw(self, symbol: str, price_usd: float, usdkrw: float) -> float: ...
