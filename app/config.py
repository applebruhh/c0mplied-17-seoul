"""엔진 설정. 모든 값은 환경변수로 덮어쓸 수 있다 (접두사 SB_).

지침 5·6절의 '제안값(팀 합의 필요)'은 여기 한 곳에 모았다.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.getenv(f"SB_{name}", default)


def _f(name: str, default: float) -> float:
    return float(_env(name, str(default)))


def _i(name: str, default: int) -> int:
    return int(_env(name, str(default)))


def _b(name: str, default: bool) -> bool:
    return _env(name, "1" if default else "0").lower() in ("1", "true", "yes")


@dataclass
class Settings:
    # ── 실행 모드 ───────────────────────────────────────────────
    # sim  : 체인·DEX·은행·가격 모두 시뮬레이션 (데모/테스트)
    # live : 실제 RPC, 0x 스왑, 임베디드 지갑, 은행 API, 거래소 시세
    mode: str = field(default_factory=lambda: _env("MODE", "sim"))
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", "sqlite:///./stockbucks.db"))

    # ── 결제 흐름 파라미터 (지침 결제흐름 5절) ─────────────────
    margin: float = field(default_factory=lambda: _f("MARGIN", 0.03))
    max_slippage: float = field(default_factory=lambda: _f("MAX_SLIPPAGE", 0.005))
    hold_ttl_sec: int = field(default_factory=lambda: _i("HOLD_TTL_SEC", 300))
    swap_retries: int = field(default_factory=lambda: _i("SWAP_RETRIES", 2))
    max_swaps_per_payment: int = field(default_factory=lambda: _i("MAX_SWAPS", 3))
    price_max_age_sec: float = field(default_factory=lambda: _f("PRICE_MAX_AGE_SEC", 30))

    # ── 매도 알고리즘 v5 (지침 알고리즘 6절) ───────────────────
    horizon_days: int = 14
    sell_step_krw: int = field(default_factory=lambda: _i("SELL_STEP_KRW", 1000))
    daily_sell_cap: float = field(default_factory=lambda: _f("DAILY_SELL_CAP", 0.30))
    max_marginal_slippage: float = 0.01          # v4: 한계 슬리피지 1% 넘으면 후보 제외
    gamma0: dict = field(default_factory=lambda: {"conservative": 3.0, "neutral": 2.0, "aggressive": 1.0})
    reg_shock: float = -0.08                     # 규제 충격 −8%·g_reg
    news_weight: float = 0.05                    # 5%·n_i
    unlock_weight: float = 0.5                   # −0.5·U_i
    tax_start_date: str = field(default_factory=lambda: _env("TAX_START_DATE", "2029-01-01"))
    tax_rate: float = 0.22
    tax_deduction_krw: float = 2_500_000

    # 스테이블 (지침 알고리즘 2절 시작값)
    pi0: float = 0.0005
    pi_max: float = 0.05
    depeg_loss: float = 0.04                     # ℓ
    depeg_recovery: float = 0.5                  # r
    psi: float = 0.03
    c_swap: dict = field(default_factory=lambda: {"USDC": 0.0002, "USDT": 0.0005})
    stable_guard: float = 0.02                   # |P−1| ≥ 2% → 결제 매도 제외
    buffer_k: float = 1.0                        # L* = 14일 지출 × (1 + k·시장변동성)

    # 구독 (지침 알고리즘 3·6절)
    sub_theta_krw: float = 2000
    sub_ask_days_before: int = 3
    sub_idle_days: int = 30
    sub_snooze_days: int = 90
    sub_capacity_eps: float = 0.10
    sub_lambda_iters: int = 3
    sub_keep_bump: float = 0.2                   # "유지" 응답 시 v_s 20% 상향

    # ── 체인 / 지갑 / DEX ──────────────────────────────────────
    chain_id: int = field(default_factory=lambda: _i("CHAIN_ID", 8453))      # Base
    rpc_url: str = field(default_factory=lambda: _env("RPC_URL", "https://mainnet.base.org"))
    treasury_address: str = field(default_factory=lambda: _env("TREASURY_ADDRESS", "0xTREASURY"))
    treasury_accepts_usdt: bool = field(default_factory=lambda: _b("TREASURY_ACCEPTS_USDT", True))
    wallet_provider: str = field(default_factory=lambda: _env("WALLET_PROVIDER", "sim"))  # sim|local|privy
    privy_app_id: str = field(default_factory=lambda: _env("PRIVY_APP_ID", ""))
    privy_app_secret: str = field(default_factory=lambda: _env("PRIVY_APP_SECRET", ""))
    privy_auth_key: str = field(default_factory=lambda: _env("PRIVY_AUTHORIZATION_KEY", ""))
    zeroex_api_key: str = field(default_factory=lambda: _env("ZEROEX_API_KEY", ""))

    # ── 원화 송금 (가맹점 정산) ────────────────────────────────
    bank_provider: str = field(default_factory=lambda: _env("BANK_PROVIDER", "sim"))  # sim|openbanking
    openbanking_base: str = field(default_factory=lambda: _env("OPENBANKING_BASE", "https://testapi.openbanking.or.kr"))
    openbanking_token: str = field(default_factory=lambda: _env("OPENBANKING_TOKEN", ""))
    openbanking_client_code: str = field(default_factory=lambda: _env("OPENBANKING_CLIENT_CODE", ""))
    company_fintech_num: str = field(default_factory=lambda: _env("COMPANY_FINTECH_NUM", ""))

    # ── 시세 / 매크로 / 뉴스 ───────────────────────────────────
    price_source: str = field(default_factory=lambda: _env("PRICE_SOURCE", "sim"))  # sim|upbit
    fred_api_key: str = field(default_factory=lambda: _env("FRED_API_KEY", ""))
    sosovalue_api_key: str = field(default_factory=lambda: _env("SOSOVALUE_API_KEY", ""))
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY", ""))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", "claude-sonnet-5-5"))
    macro_refresh_sec: int = field(default_factory=lambda: _i("MACRO_REFRESH_SEC", 600))
    news_refresh_sec: int = field(default_factory=lambda: _i("NEWS_REFRESH_SEC", 1800))

    # ── 외부 연동 (구독 사용량) ────────────────────────────────
    spotify_client_id: str = field(default_factory=lambda: _env("SPOTIFY_CLIENT_ID", ""))
    spotify_client_secret: str = field(default_factory=lambda: _env("SPOTIFY_CLIENT_SECRET", ""))

    # ── POS Backend ↔ 엔진 인증 ────────────────────────────────
    pos_hmac_secret: str = field(default_factory=lambda: _env("POS_HMAC_SECRET", "dev-secret"))
    require_hmac: bool = field(default_factory=lambda: _b("REQUIRE_HMAC", False))
    start_workers: bool = field(default_factory=lambda: _b("START_WORKERS", True))


settings = Settings()

DECISION_FORMULA_VERSION = "sell-v5.0/sub-v5.0"
