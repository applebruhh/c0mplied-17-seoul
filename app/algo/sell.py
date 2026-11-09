"""매도 순서 최적화 (v5).

모든 자산(코인 + USDC·USDT)에 점수 S_i = −MC_i 를 매기고, 점수가 높은 자산부터
step(기본 1,000원)씩 채워 목표액을 만든다. 'USDC 먼저' 같은 고정 규칙은 없다.

코인     MC_i = μ_i + c_i + τ_i + x_i/D_i − γ·RC_i,  RC_i = (Σ(V−x))_i / P
스테이블 MC_j = μ_j + c_j + τ_j + x_j/D_j + ψ·1[U_after < L*]
  μ_j = y_j + r·(1−P_j)/P_j − π_j·ℓ
  c_j = c_swap + (1 − P_j)
  π_j = π0 + (π_max − π0)·clip(0.4·|P_j−1|/0.003 + 0.2·b_j + 0.25·g_iss + 0.15·g_reg, 0, 1)

모든 값은 '원화 1원을 팔 때 14일 기대손실 (소수)' 단위라 서로 비교할 수 있다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

from ..assets import ASSETS, Asset
from ..config import settings
from ..market.state import MarketState

INF = float("inf")


@dataclass
class Holding:
    symbol: str
    units: float
    price_krw: float
    cap_krw: float = INF               # 하루 매도 상한 남은 금액
    avg_cost_krw: float | None = None  # 세금 계산용 평단가 (없으면 τ=0)

    @property
    def value(self) -> float:
        return self.units * self.price_krw


@dataclass
class SellContext:
    gamma0: float = 2.0
    expected_spend_14d_krw: float = 0.0
    realized_gain_ytd_krw: float = 0.0
    today: date = field(default_factory=date.today)


@dataclass
class AssetDiag:
    symbol: str
    kind: str
    value_krw: float
    mu: float
    cost: float
    tax: float
    depth_krw: float
    mc0: float
    score0: float
    excluded: str | None = None
    components: dict = field(default_factory=dict)


@dataclass
class SellPlan:
    target_krw: float
    alloc_krw: dict[str, float]
    filled_krw: float
    shortfall_krw: float
    ranking: list[str]
    marginal_mc: float
    gamma: float
    cap_relaxed: bool
    diags: dict[str, AssetDiag]
    excluded: dict[str, str]
    l_star_krw: float

    def legs(self) -> list[tuple[str, float]]:
        return sorted(((s, x) for s, x in self.alloc_krw.items() if x > 0), key=lambda t: -t[1])

    def to_dict(self) -> dict:
        return {
            "target_krw": round(self.target_krw), "filled_krw": round(self.filled_krw),
            "shortfall_krw": round(self.shortfall_krw), "alloc_krw": {k: round(v) for k, v in self.alloc_krw.items() if v > 0},
            "ranking": self.ranking, "marginal_mc": self.marginal_mc, "gamma": self.gamma,
            "cap_relaxed": self.cap_relaxed, "excluded": self.excluded, "l_star_krw": round(self.l_star_krw),
            "assets": {k: {"mu": round(d.mu, 5), "cost": round(d.cost, 5), "tax": round(d.tax, 5),
                           "mc0": round(d.mc0, 5), "score0": round(d.score0, 5), "excluded": d.excluded,
                           "value_krw": round(d.value_krw), "components": d.components}
                       for k, d in self.diags.items()},
        }


class SellOptimizer:
    def __init__(self, state: MarketState, cfg=settings):
        self.m = state
        self.cfg = cfg

    # ── 공통 항 ────────────────────────────────────────────────
    def gamma(self, gamma0: float) -> float:
        g = max(self.m.r("g_geo"), self.m.r("g_reg"))
        return gamma0 * (1 + 1.5 * g + max(0.0, (self.m.dvol - 50) / 20))

    def tax_rate(self, h: Holding, ctx: SellContext, x: float) -> float:
        """2027-01-01 이후: 0.22·max(0, G·x − E) − 0.22·min(|G|x, max(0, Y−250만))·1[G<0], 1원당."""
        if ctx.today < date.fromisoformat(self.cfg.tax_start_date) or h.avg_cost_krw is None or x <= 0:
            return 0.0
        G = (h.price_krw - h.avg_cost_krw) / h.price_krw
        E = max(0.0, self.cfg.tax_deduction_krw - ctx.realized_gain_ytd_krw)
        t = self.cfg.tax_rate * max(0.0, G * x - E)
        if G < 0:
            t -= self.cfg.tax_rate * min(abs(G) * x, max(0.0, ctx.realized_gain_ytd_krw - self.cfg.tax_deduction_krw))
        return t / x

    def depth(self, sym: str, value: float) -> float:
        d = self.m.coin(sym).depth_krw
        if d > 0:
            return d
        a = ASSETS[sym]
        # 풀 깊이 미측정 시 보수적 기본값 (가격 1% 이동 기준, 원)
        return {"btc": 5e9, "eth": 3e9, "stable": 1e10}.get(a.kind, 1.5e8)

    def mu_coin(self, a: Asset) -> tuple[float, dict]:
        m = self.m
        sig = m.coin(a.symbol)
        t = a.t
        m_hat, q_hat, g_reg = m.m_hat(), m.q_hat(), m.r("g_reg")
        theme = sum(m.r(f"theme:{h}") for h in a.themes)
        stake = (sig.staking_apr if sig.staking_apr is not None else a.staking_apr) * 14 / 365
        comp = {
            "btc": t.b_btc * m_hat, "alt": t.b_alt * q_hat, "theme": theme,
            "reg": t.b_reg * self.cfg.reg_shock * g_reg,
            "trend": sig.T, "rho": sig.rho, "news": self.cfg.news_weight * m.r(f"news:{a.symbol}"),
            "unlock": -self.cfg.unlock_weight * sig.unlock_pct / 100, "lev": sig.L, "stake": stake,
        }
        return sum(comp.values()), {k: round(v, 5) for k, v in comp.items()}

    def depeg_prob(self, sym: str, P: float) -> tuple[float, float]:
        sig = self.m.coin(sym)
        Z = 0.4 * abs(P - 1) / 0.003 + 0.2 * sig.pool_imbalance + 0.25 * self.m.r(f"g_iss:{sym}") + 0.15 * self.m.r("g_reg")
        Z = max(0.0, min(1.0, Z))
        return self.cfg.pi0 + (self.cfg.pi_max - self.cfg.pi0) * Z, Z

    def mu_stable(self, a: Asset) -> tuple[float, float, dict]:
        P = self.m.price_usd(a.symbol)
        pi, Z = self.depeg_prob(a.symbol, P)
        sig = self.m.coin(a.symbol)
        y = (sig.staking_apr or 0.0) * 14 / 365
        mu = y + self.cfg.depeg_recovery * (1 - P) / P - pi * self.cfg.depeg_loss
        c = self.cfg.c_swap.get(a.symbol, 0.0005) + (1 - P)
        return mu, c, {"P": round(P, 5), "pi": round(pi, 5), "Z": round(Z, 4), "yield": round(y, 6)}

    def l_star(self, ctx: SellContext) -> float:
        return ctx.expected_spend_14d_krw * (1 + self.cfg.buffer_k * self.m.market_vol_14d())

    # ── 최적화 ────────────────────────────────────────────────
    def plan(self, holdings: list[Holding], target_krw: float, ctx: SellContext,
             allowed: set[str] | None = None, max_swap_assets: int | None = None) -> SellPlan:
        plan = self._greedy(holdings, target_krw, ctx, allowed, relax_cap=False)
        if plan.shortfall_krw > 0.5:
            relaxed = self._greedy(holdings, target_krw, ctx, allowed, relax_cap=True)
            if relaxed.filled_krw > plan.filled_krw:
                plan = relaxed
        k = max_swap_assets or self.cfg.max_swaps_per_payment
        swap_syms = [s for s, x in plan.legs() if self._is_swap(s)]
        if len(swap_syms) > k:
            keep = set(swap_syms[:k]) | {s for s in plan.alloc_krw if not self._is_swap(s)}
            restricted = self._greedy(holdings, target_krw, ctx, keep & (allowed or keep), relax_cap=plan.cap_relaxed)
            restricted.excluded.update({s: "max_swaps" for s in swap_syms[k:]})
            plan = restricted
        return plan

    def _is_swap(self, sym: str) -> bool:
        if sym == "USDC":
            return False
        if sym == "USDT" and self.cfg.treasury_accepts_usdt:
            return False
        return True

    def _greedy(self, holdings: list[Holding], target: float, ctx: SellContext,
                allowed: set[str] | None, relax_cap: bool) -> SellPlan:
        cfg, m = self.cfg, self.m
        gam = self.gamma(ctx.gamma0)
        hs = [h for h in holdings if h.units > 0 and h.symbol in ASSETS]
        syms = [h.symbol for h in hs]
        n = len(hs)
        V = [h.value for h in hs]
        P = sum(V) or 1.0
        x = [0.0] * n
        excluded: dict[str, str] = {}
        diags: dict[str, AssetDiag] = {}

        # 팩터 노출 (B) · 고유 변동성
        B = [(ASSETS[s].t.b_btc, ASSETS[s].t.b_alt) for s in syms]
        om = (m.btc_sigma_14d ** 2, m.alt_sigma_14d ** 2)
        eps2 = []
        for s in syms:
            a, sig = ASSETS[s], m.coin(s)
            se = sig.sigma_eps if sig.sigma_eps is not None else a.t.sigma_eps
            lev = 1.5 if (sig.L < 0 or sig.unlock_pct > 0) else 1.0
            eps2.append(0.0 if a.is_stable else (se * lev) ** 2)

        mu, cost, D, stable_extra = [0.0] * n, [0.0] * n, [0.0] * n, {}
        caps = []
        for i, h in enumerate(hs):
            a = ASSETS[h.symbol]
            D[i] = self.depth(h.symbol, V[i])
            if a.is_stable:
                mu[i], cost[i], stable_extra[i] = self.mu_stable(a)
            else:
                mu[i], comp = self.mu_coin(a)
                stable_extra[i] = comp
                cost[i] = a.fee
            cap = V[i] if (a.is_stable or relax_cap) else min(V[i], h.cap_krw)
            caps.append(cap)
            if allowed is not None and h.symbol not in allowed:
                excluded[h.symbol] = "not_allowed"
            elif a.is_stable and abs(m.price_usd(h.symbol) - 1) >= cfg.stable_guard:
                excluded[h.symbol] = "depeg_guard"

        l_star = self.l_star(ctx)
        stable_idx = [i for i, s in enumerate(syms) if ASSETS[s].is_stable]
        stable_total = sum(V[i] for i in stable_idx)

        def mc(i: int, xi_next: float) -> float:
            a = ASSETS[syms[i]]
            tax = self.tax_rate(hs[i], ctx, xi_next)
            slip = xi_next / D[i]
            if a.is_stable:
                u_after = stable_total - sum(x[j] for j in stable_idx) - (xi_next - x[i])
                buf = cfg.psi if u_after < l_star else 0.0
                return mu[i] + cost[i] + tax + slip + buf
            # RC_i = (Σ(V−x))_i / P,  Σ = B Ω Bᵀ + diag(ε²)
            f_btc = sum(B[j][0] * (V[j] - x[j]) for j in range(n))
            f_alt = sum(B[j][1] * (V[j] - x[j]) for j in range(n))
            rc = (B[i][0] * om[0] * f_btc + B[i][1] * om[1] * f_alt + eps2[i] * (V[i] - xi_next)) / P
            return mu[i] + cost[i] + tax + slip - gam * rc

        for i, s in enumerate(syms):
            m0 = mc(i, 0.0)
            diags[s] = AssetDiag(s, ASSETS[s].kind, V[i], mu[i], cost[i], self.tax_rate(hs[i], ctx, 1.0),
                                 D[i], m0, -m0, excluded.get(s), stable_extra[i])
        ranking = [s for s in sorted(syms, key=lambda s: diags[s].mc0) if s not in excluded]

        step = max(float(cfg.sell_step_krw), target / 3000)
        filled, last_mc = 0.0, 0.0
        while filled < target - 1e-6:
            amt = min(step, target - filled)
            best, best_mc = -1, INF
            for i in range(n):
                if syms[i] in excluded or x[i] + amt > caps[i] + 1e-6:
                    continue
                if not ASSETS[syms[i]].is_stable and (x[i] + amt) / D[i] > cfg.max_marginal_slippage:
                    continue
                v = mc(i, x[i] + amt)
                if v < best_mc:
                    best, best_mc = i, v
            if best < 0:
                # 남은 용량이 step 보다 작은 자산에서 마저 채움
                rem = [(caps[i] - x[i], i) for i in range(n) if syms[i] not in excluded and caps[i] - x[i] > 1e-6]
                if not rem:
                    break
                cap_left, best = max(rem)
                amt = min(amt, cap_left)
                best_mc = mc(best, x[best] + amt)
            x[best] += amt
            filled += amt
            last_mc = best_mc

        return SellPlan(target, dict(zip(syms, x)), filled, max(0.0, target - filled), ranking,
                        last_mc if math.isfinite(last_mc) else 0.0, gam, relax_cap, diags, excluded, l_star)

    # ── 구독 엔진용 λ ─────────────────────────────────────────
    def marginal_cost_for_spend(self, holdings: list[Holding], spend_krw: float, ctx: SellContext) -> float:
        """λ: 앞으로 낼 지출 spend 를 지금 자산으로 낸다고 할 때 마지막 1원의 한계비용 (0 이상)."""
        if spend_krw <= 0:
            plan = self._greedy(holdings, float(self.cfg.sell_step_krw), ctx, None, relax_cap=True)
        else:
            plan = self._greedy(holdings, spend_krw, ctx, None, relax_cap=True)
        return max(0.0, plan.marginal_mc)
