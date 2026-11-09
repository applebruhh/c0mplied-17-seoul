"""뉴스 → 점수 (LLM). LLM 은 숫자만 만들고, 결정은 고정 공식이 한다.

흐름: RSS 헤드라인 수집 → Claude 에 JSON 스키마로 점수 요청 → ReportValidator → MarketState
출력 키 (모두 validator.SPECS 범위 안이어야 함):
  nasdaq_14d, dxy_14d, q_hat (소수, 예: −0.03 = −3%)
  g_geo, g_reg, g_iss:USDC, g_iss:USDT (0~1)
  theme:<l1|l2|defi|meme|…> (소수), news:<SYMBOL> (−1~+1)
"""
from __future__ import annotations

import json
import logging
import re
import xml.etree.ElementTree as ET

import httpx

from ..assets import ASSETS
from ..config import settings
from .state import MarketState
from .validator import ReportValidator

log = logging.getLogger("news")

FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://feeds.content.dowjones.io/public/rss/mw_topstories",
]
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

PROMPT = """너는 크립토 결제 엔진의 '보고서' 단계다. 아래 최근 헤드라인만 근거로 숫자를 낸다.
결정은 하지 말고, 지정된 키의 숫자와 근거만 JSON 으로 반환한다. 근거가 없으면 그 키를 빼라.

키와 범위:
- nasdaq_14d: 나스닥 14일 수익률 전망 (−0.08~0.08)
- dxy_14d: 달러인덱스 14일 전망 (−0.04~0.04)
- q_hat: 알트코인이 BTC 대비 14일간 더 오를 정도 (−0.10~0.10)
- g_geo: 지정학 리스크 강도 (0~1), g_reg: 크립토 규제 악재 강도 (0~1)
- g_iss:USDC, g_iss:USDT: 발행사 이슈 강도 (0~1)
- theme:<l1|l2|defi|meme|ai|gaming>: 테마 상대 전망 (−0.10~0.10)
- news:<심볼>: 코인별 뉴스 감성 (−1~1). 심볼 후보: {symbols}

반환 형식 (JSON 만):
{{"values": {{"키": 숫자, ...}}, "evidence": {{"키": "근거 헤드라인 요약"}}}}

헤드라인:
{headlines}
"""


async def fetch_headlines(client: httpx.AsyncClient, limit: int = 60) -> list[str]:
    out: list[str] = []
    for url in FEEDS:
        try:
            r = await client.get(url, timeout=8, follow_redirects=True)
            r.raise_for_status()
            root = ET.fromstring(r.content)
            for item in root.iter("item"):
                t = item.findtext("title")
                if t:
                    out.append(t.strip())
        except Exception as e:  # noqa: BLE001
            log.warning("feed %s failed: %s", url, e)
    return out[:limit]


def parse_llm_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("JSON 없음")
    data = json.loads(m.group(0))
    vals = data.get("values", {})
    if not isinstance(vals, dict):
        raise ValueError("values 형식 오류")
    return {"values": vals, "evidence": data.get("evidence", {})}


async def score_with_llm(client: httpx.AsyncClient, headlines: list[str]) -> dict:
    prompt = PROMPT.format(symbols=", ".join(s for s, a in ASSETS.items()),
                           headlines="\n".join(f"- {h}" for h in headlines))
    r = await client.post(ANTHROPIC_URL, timeout=60, headers={
        "x-api-key": settings.anthropic_api_key, "anthropic-version": "2023-06-01",
        "content-type": "application/json"},
        json={"model": settings.llm_model, "max_tokens": 1500,
              "messages": [{"role": "user", "content": prompt}]})
    r.raise_for_status()
    text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
    return parse_llm_json(text)


async def refresh_news(state: MarketState) -> dict:
    if not settings.anthropic_api_key:
        return {"skipped": "ANTHROPIC_API_KEY 없음"}
    async with httpx.AsyncClient() as client:
        heads = await fetch_headlines(client)
        if not heads:
            return {"skipped": "헤드라인 없음"}
        scored = await score_with_llm(client, heads)
    result = ReportValidator(state).apply(scored["values"], source="llm")
    result["evidence"] = scored["evidence"]
    result["n_headlines"] = len(heads)
    return result
