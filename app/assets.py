"""자산 레지스트리.

- 지갑의 모든 코인(USDC·USDT 포함)은 같은 '자산'으로 취급한다 (지침 0절).
- 스테이블 여부는 매도 점수 공식을 고르는 데만 쓴다 (지침 2절, 알고리즘 2절).
- 체인 주소는 Base 메인넷 기준. 실서비스 전 반드시 공식 문서로 다시 확인할 것.
  address=None 인 자산은 시뮬레이션 전용이다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

NATIVE = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"


@dataclass(frozen=True)
class AssetType:
    b_btc: float
    b_alt: float
    b_reg: float
    sigma_eps: float          # 14일 고유 변동성 (소수)


# v4 문서 '노출도 기본값' 표 + 계산 예시의 σε
TYPES: dict[str, AssetType] = {
    "btc":    AssetType(1.0, 0.0, 0.1, 0.00),
    "eth":    AssetType(1.2, 0.5, 0.4, 0.05),
    "l1":     AssetType(1.5, 1.0, 0.5, 0.08),
    "defi":   AssetType(1.4, 1.0, 0.8, 0.09),
    "l2":     AssetType(1.6, 1.0, 0.8, 0.12),
    "meme":   AssetType(1.8, 1.2, 0.3, 0.14),
    "stable": AssetType(0.0, 0.0, 0.0, 0.00),
}


@dataclass(frozen=True)
class Asset:
    symbol: str
    kind: str                         # TYPES 키
    decimals: int
    address: str | None               # 체인 토큰 주소 (None = sim 전용)
    upbit_market: str | None          # 원화 시세 (KRW-XXX)
    binance_symbol: str | None        # 캔들·펀딩비 (XXXUSDT)
    themes: tuple[str, ...] = field(default_factory=tuple)
    staking_apr: float = 0.0          # Y_i (유동성 스테이킹 토큰 등)
    fee: float = 0.003                # c_i 기본값 (풀 수수료 + 스프레드)

    @property
    def is_stable(self) -> bool:
        return self.kind == "stable"

    @property
    def t(self) -> AssetType:
        return TYPES[self.kind]


ASSETS: dict[str, Asset] = {a.symbol: a for a in [
    Asset("BTC",  "btc",   8,  "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf", "KRW-BTC",  "BTCUSDT",  (), 0, 0.001),   # cbBTC
    Asset("ETH",  "eth",   18, NATIVE,                                        "KRW-ETH",  "ETHUSDT",  ("l1",), 0, 0.001),
    Asset("LINK", "defi",  18, "0x88Fb150BDc53A65fe94Dea0c9BA0a6dAf8C6e196", "KRW-LINK", "LINKUSDT", ("defi", "oracle"), 0, 0.004),
    Asset("AERO", "defi",  18, "0x940181a94A35A4569E4529A3CDfB74e38FD98631", None,       "AEROUSDT", ("defi", "dex"), 0, 0.005),
    Asset("USDC", "stable", 6, "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "KRW-USDC", None,       ()),
    Asset("USDT", "stable", 6, "0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2", "KRW-USDT", None,       ()),
    # 시뮬레이션 전용 (v4 계산 예시 포트폴리오 재현용)
    Asset("SOL",  "l1",    9,  None, "KRW-SOL",  "SOLUSDT",  ("l1",), 0.07, 0.002),
    Asset("ARB",  "l2",    18, None, "KRW-ARB",  "ARBUSDT",  ("l2",), 0, 0.005),
    Asset("DOGE", "meme",  8,  None, "KRW-DOGE", "DOGEUSDT", ("meme",), 0, 0.006),
]}

STABLES = [s for s, a in ASSETS.items() if a.is_stable]


def get(symbol: str) -> Asset:
    try:
        return ASSETS[symbol.upper()]
    except KeyError as e:  # pragma: no cover
        raise ValueError(f"지원하지 않는 자산: {symbol}") from e
