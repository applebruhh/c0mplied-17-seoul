"""LLM 보고서 검증기.

LLM 은 뉴스를 0~1 점수나 %로 바꾸는 일만 한다. 그 값은 여기서 범위와 한 번에 바뀌는 폭을
확인한 뒤에만 MarketState 에 들어간다 (v4 '실시간 갱신과 보정' 표).
- 범위 밖 → 거부
- 변화폭 초과 → 거부 (근거가 충분하면 다음 주기에 다시 들어온다)
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from .state import DecayingValue, MarketState


@dataclass(frozen=True)
class Spec:
    lo: float
    hi: float
    max_step: float
    half_life_h: float


SPECS: dict[str, Spec] = {
    "nasdaq_14d": Spec(-0.08, 0.08, 0.04, 48),
    "dxy_14d":    Spec(-0.04, 0.04, 0.02, 48),
    "q_hat":      Spec(-0.10, 0.10, 0.04, 48),
    "g_geo":      Spec(0.0, 1.0, 0.4, 72),
    "g_reg":      Spec(0.0, 1.0, 0.4, 72),
    "g_iss:USDC": Spec(0.0, 1.0, 0.4, 72),
    "g_iss:USDT": Spec(0.0, 1.0, 0.4, 72),
}
THEME_SPEC = Spec(-0.10, 0.10, 0.05, 48)
NEWS_SPEC = Spec(-1.0, 1.0, 0.5, 48)
UNLOCK_SPEC = Spec(0.0, 50.0, 50.0, 24 * 14)   # 일정 데이터 (% of supply)


def spec_for(key: str) -> Spec | None:
    if key in SPECS:
        return SPECS[key]
    if key.startswith("theme:"):
        return THEME_SPEC
    if key.startswith("news:"):
        return NEWS_SPEC
    return None


class ReportValidator:
    def __init__(self, state: MarketState):
        self.state = state

    def apply(self, values: dict[str, float], source: str = "llm") -> dict:
        accepted, rejected = {}, {}
        now = time.time()
        with self.state.lock:
            for key, raw in values.items():
                spec = spec_for(key)
                if spec is None:
                    rejected[key] = "unknown_key"
                    continue
                try:
                    v = float(raw)
                except (TypeError, ValueError):
                    rejected[key] = "not_a_number"
                    continue
                if v != v or not (spec.lo <= v <= spec.hi):
                    rejected[key] = f"out_of_range[{spec.lo},{spec.hi}]"
                    continue
                prev = self.state.report.get(key)
                prev_v = prev.current(now) if prev else 0.0
                if abs(v - prev_v) > spec.max_step + 1e-12:
                    rejected[key] = f"step_too_large({prev_v:+.4f}->{v:+.4f}, max {spec.max_step})"
                    continue
                self.state.report[key] = DecayingValue(v, now, spec.half_life_h)
                accepted[key] = v
            self.state.report_meta = {"source": source, "at": now, "accepted": len(accepted), "rejected": len(rejected)}
        return {"accepted": accepted, "rejected": rejected}
