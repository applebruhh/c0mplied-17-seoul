"""구독 사용량 수집 (동의 3단계, 지침 알고리즘 5절).

1단계(필수) 결제 내역·반복 결제 감지   → card_transactions
2단계(선택) 서비스 사용시간             → screen_time(iOS 앱 DeviceActivity 집계), spotify_api,
                                          netflix_csv(시청 기록 내려받기), youtube_takeout(시청 기록 JSON)
3단계(선택) 혜택 금액·저장 용량          → benefits(manual/쿠팡 주문 추정), google_drive(storageQuota), icloud(manual)

동의 없는 데이터는 받지 않고, 동의를 철회하면 해당 단계 데이터를 지운다.
추정치(estimated=True)는 기록에 남겨 사용자에게 보여줄 때 구분한다.
"""
from __future__ import annotations

import csv
import io
import json
from collections import defaultdict
from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session

from ..models import CardTransaction, Consent, UsageRecord

TIER_OF_SOURCE = {"card": 1, "screen_time": 2, "spotify_api": 2, "netflix_csv": 2, "youtube_takeout": 2,
                  "manual_hours": 2, "benefits": 3, "google_drive": 3, "icloud": 3, "manual_storage": 3}
HOUR_SOURCES = {"screen_time", "spotify_api", "netflix_csv", "youtube_takeout", "manual_hours"}
TIER3_SOURCES = {"benefits", "google_drive", "icloud", "manual_storage"}

# iOS DeviceActivity 번들 ID → 서비스 키 (UI 앱이 이 키로 집계해서 올림)
BUNDLE_TO_SERVICE = {
    "com.netflix.Netflix": "netflix", "net.cj.tving": "tving", "com.google.ios.youtube": "youtube_premium",
    "com.spotify.client": "spotify", "com.iwilab.KakaoTalk": "kakao_emoticon",
}


class ConsentRequired(Exception):
    pass


def set_consent(s: Session, user_id: str, tier: int, granted: bool) -> None:
    row = s.query(Consent).filter_by(user_id=user_id, tier=tier).one_or_none()
    if row is None:
        row = Consent(user_id=user_id, tier=tier)
        s.add(row)
    row.granted, row.at = granted, datetime.now(timezone.utc)
    if not granted:
        if tier == 1:
            s.query(CardTransaction).filter_by(user_id=user_id).delete()
        srcs = HOUR_SOURCES if tier == 2 else TIER3_SOURCES if tier == 3 else set()
        if srcs:
            s.query(UsageRecord).filter(UsageRecord.user_id == user_id, UsageRecord.source.in_(srcs)).delete()


def has_consent(s: Session, user_id: str, tier: int) -> bool:
    row = s.query(Consent).filter_by(user_id=user_id, tier=tier).one_or_none()
    return bool(row and row.granted)


def require(s: Session, user_id: str, source: str) -> None:
    tier = TIER_OF_SOURCE[source]
    if not has_consent(s, user_id, tier):
        raise ConsentRequired(f"{tier}단계 동의가 필요합니다 ({source})")


def upsert_usage(s: Session, user_id: str, service: str, month: str, source: str, **vals) -> UsageRecord:
    require(s, user_id, source)
    r = s.query(UsageRecord).filter_by(user_id=user_id, service=service, month=month, source=source).one_or_none()
    if r is None:
        r = UsageRecord(user_id=user_id, service=service, month=month, source=source)
        s.add(r)
    for k, v in vals.items():
        setattr(r, k, v)
    return r


def add_card_transactions(s: Session, user_id: str, rows: list[dict]) -> int:
    require(s, user_id, "card")
    for r in rows:
        at = r["at"] if isinstance(r["at"], datetime) else datetime.fromisoformat(r["at"])
        s.add(CardTransaction(user_id=user_id, merchant_name=r["merchant"], amount_krw=float(r["amount_krw"]), at=at))
    return len(rows)


# ── 2단계: 사용시간 ────────────────────────────────────────────
def ingest_screen_time(s: Session, user_id: str, items: list[dict]) -> int:
    """iOS 앱이 보낸 월별 집계: [{bundle_id|service, month: 'YYYY-MM', hours, last_used_at?}]"""
    n = 0
    for it in items:
        svc = it.get("service") or BUNDLE_TO_SERVICE.get(it.get("bundle_id", ""))
        if not svc:
            continue
        last = it.get("last_used_at")
        upsert_usage(s, user_id, svc, it["month"], "screen_time", hours=float(it["hours"]),
                     last_used_at=datetime.fromisoformat(last) if last else None)
        n += 1
    return n


def _parse_duration(v: str) -> float:
    parts = [int(x) for x in v.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, sec = parts[-3:]
    return h + m / 60 + sec / 3600


def ingest_netflix_csv(s: Session, user_id: str, content: str, est_hours_per_title: float = 0.75) -> dict:
    """넷플릭스 '시청 기록' CSV.
    - 개인정보 내려받기(ViewingActivity.csv): Start Time, Duration 열 → 실제 시간
    - 계정 페이지 시청 기록(NetflixViewingHistory.csv): Title, Date 만 → 편당 추정 시간"""
    reader = csv.DictReader(io.StringIO(content.lstrip("﻿")))
    hours, last = defaultdict(float), None
    estimated = False
    for row in reader:
        if "Start Time" in row and row.get("Duration"):
            at = datetime.fromisoformat(row["Start Time"].replace(" ", "T")).replace(tzinfo=timezone.utc)
            hours[at.strftime("%Y-%m")] += _parse_duration(row["Duration"])
        elif "Date" in row:
            raw = row["Date"].strip()
            at = None
            for fmt in ("%m/%d/%y", "%Y. %m. %d.", "%y. %m. %d.", "%Y-%m-%d", "%d/%m/%Y"):
                try:
                    at = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
                    break
                except ValueError:
                    continue
            if at is None:
                continue
            hours[at.strftime("%Y-%m")] += est_hours_per_title
            estimated = True
        else:
            continue
        last = max(last, at) if last else at
    for month, h in hours.items():
        upsert_usage(s, user_id, "netflix", month, "netflix_csv", hours=round(h, 2), last_used_at=last)
    return {"months": dict(hours), "estimated": estimated, "last_used_at": last.isoformat() if last else None}


def ingest_youtube_takeout(s: Session, user_id: str, content: str, avg_minutes: float = 8.0) -> dict:
    """Google 테이크아웃 watch-history.json: [{title, titleUrl, time}] → 월별 시청 수 × 평균 길이 (추정)."""
    data = json.loads(content)
    counts, last = defaultdict(int), None
    for it in data:
        if "titleUrl" not in it or "time" not in it:
            continue
        at = datetime.fromisoformat(it["time"].replace("Z", "+00:00"))
        counts[at.strftime("%Y-%m")] += 1
        last = max(last, at) if last else at
    for month, c in counts.items():
        upsert_usage(s, user_id, "youtube_premium", month, "youtube_takeout",
                     count=c, hours=round(c * avg_minutes / 60, 2), last_used_at=last)
    return {"months": dict(counts), "estimated": True}


async def pull_spotify(s: Session, user_id: str, access_token: str) -> dict:
    """Spotify Web API 최근 재생 50곡 → 이번 달 재생시간에 누적 (1시간마다 호출하면 빠짐없이 쌓인다)."""
    require(s, user_id, "spotify_api")
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get("https://api.spotify.com/v1/me/player/recently-played", params={"limit": 50},
                        headers={"Authorization": f"Bearer {access_token}"})
        r.raise_for_status()
        items = r.json().get("items", [])
    by_month, last = defaultdict(float), None
    rec_cache: dict[str, UsageRecord] = {}
    for it in items:
        at = datetime.fromisoformat(it["played_at"].replace("Z", "+00:00"))
        month = at.strftime("%Y-%m")
        rec = rec_cache.get(month) or s.query(UsageRecord).filter_by(user_id=user_id, service="spotify",
                                                                     month=month, source="spotify_api").one_or_none()
        if rec is not None:
            rec_cache[month] = rec
        if rec and rec.last_used_at and at <= _aware(rec.last_used_at):
            continue                                   # 이미 센 곡
        by_month[month] += it["track"]["duration_ms"] / 3_600_000
        last = max(last, at) if last else at
    for month, h in by_month.items():
        rec = rec_cache.get(month)
        prev = rec.hours if rec and rec.hours else 0.0
        upsert_usage(s, user_id, "spotify", month, "spotify_api", hours=round(prev + h, 3), last_used_at=last)
    return {"added_hours": dict(by_month)}


# ── 3단계: 혜택·용량 ───────────────────────────────────────────
async def pull_google_drive_storage(s: Session, user_id: str, access_token: str) -> float:
    require(s, user_id, "google_drive")
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get("https://www.googleapis.com/drive/v3/about", params={"fields": "storageQuota"},
                        headers={"Authorization": f"Bearer {access_token}"})
        r.raise_for_status()
        gb = int(r.json()["storageQuota"]["usage"]) / 1024 ** 3
    upsert_usage(s, user_id, "google_one", datetime.now(timezone.utc).strftime("%Y-%m"), "google_drive", storage_gb=round(gb, 2))
    return gb


def ingest_storage(s: Session, user_id: str, service: str, gb: float, source: str = "icloud") -> None:
    upsert_usage(s, user_id, service, datetime.now(timezone.utc).strftime("%Y-%m"), source, storage_gb=gb)


def ingest_benefit(s: Session, user_id: str, service: str, month: str, krw: float) -> None:
    upsert_usage(s, user_id, service, month, "benefits", benefit_krw=krw)


def estimate_coupang_benefit(s: Session, user_id: str, month: str, delivery_fee: float = 3000) -> float:
    """쿠팡 와우 혜택 추정: 그 달 쿠팡 결제 건수 × 무료배송 절감액 (가정값)."""
    n = sum(1 for t in s.query(CardTransaction).filter_by(user_id=user_id)
            if "쿠팡" in t.merchant_name or "COUPANG" in t.merchant_name.upper()
            if t.at.strftime("%Y-%m") == month and t.amount_krw > 10000)
    krw = n * delivery_fee
    if has_consent(s, user_id, 3):
        ingest_benefit(s, user_id, "coupang_wow", month, krw)
    return krw


def _aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt and dt.tzinfo is None else dt


def usage_summary(s: Session, user_id: str, service: str, now: datetime) -> dict:
    """H_s(이번 달), H̄_s(최근 3개월 지수평균, α=0.5), B_s, G_s, 마지막 사용일."""
    rows = s.query(UsageRecord).filter_by(user_id=user_id, service=service).all()
    if not rows:
        return {}
    by_month: dict[str, dict] = defaultdict(dict)
    last = None
    for r in rows:
        m = by_month[r.month]
        for k in ("hours", "count", "benefit_krw", "storage_gb"):
            v = getattr(r, k)
            if v is not None:
                m[k] = max(m.get(k, 0.0), v)          # 같은 달 여러 출처면 큰 값 (중복 합산 방지)
        if r.last_used_at:
            last = max(last, _aware(r.last_used_at)) if last else _aware(r.last_used_at)
    months = sorted(by_month)
    cur = now.strftime("%Y-%m")
    out: dict = {"last_used_at": last}
    key = "count" if service == "kakao_emoticon" else "hours"
    series = [by_month[m].get(key) for m in months[-3:] if by_month[m].get(key) is not None]
    if series:
        ema = series[0]
        for v in series[1:]:
            ema = 0.5 * v + 0.5 * ema
        out["H_bar"] = ema
        out["H"] = by_month.get(cur, {}).get(key, series[-1])
    for k, name in (("benefit_krw", "B"), ("storage_gb", "G")):
        vals = [by_month[m][k] for m in months if k in by_month[m]]
        if vals:
            out[name] = vals[-1]
    return out
