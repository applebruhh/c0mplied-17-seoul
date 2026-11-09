import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app import runtime
from app.config import settings
from app.main import app


def _client():
    return TestClient(app)


def test_api_end_to_end_payment_and_subscription():
    c = _client()
    with c:
        u = c.post("/v1/users", json={"name": "민지", "risk_profile": "conservative", "pay_assets": [],
                                      "delegation_agreed": True, "lock_notice_agreed": True}).json()["id"]
        w = c.post(f"/v1/users/{u}/wallets", json={"address": "0xabc"}).json()["id"]
        assert c.post("/v1/cards", json={"token": "nfc-1", "wallet_id": w}).status_code == 200
        c.post("/v1/merchants", json={"id": "m9", "name": "빵집", "bank_code": "004", "account_num": "1", "account_holder": "빵집"})
        for sym, units in {"ETH": 1, "LINK": 100, "USDT": 200}.items():
            c.post("/sim/fund", json={"address": "0xabc", "symbol": sym, "units": units})

        # POS → 승인
        r = c.post("/v1/payments/authorize", json={"checkout_id": "ck-1", "amount_krw": 12_000, "merchant_id": "m9",
                                                   "card_token": "nfc-1"}).json()
        assert r["status"] == "AUTHORIZED"
        pos = c.get("/v1/payments/ck-1/pos").json()
        assert set(pos) == {"checkout_id", "status", "reason", "amount_krw"}       # POS 엔 승인 결과만
        for _ in range(100):
            v = c.get(f"/ops/payments/{r['payment_id']}").json()
            if v["axes"]["hold"] != "PLACED":
                break
            time.sleep(0.02)
        assert v["axes"] == {"authorization": "AUTHORIZED", "decline_reason": None, "hold": "RELEASED",
                             "liquidation": "COMPLETED", "recovery": "RECOVERED"}
        assert c.get("/ops/decisions", params={"kind": "sell_plan", "ref_id": r["payment_id"]}).json()

        # 구독: 동의 없으면 거절
        assert c.post(f"/v1/users/{u}/transactions", json=[]).status_code == 403
        for tier in (1, 2):
            c.post(f"/v1/users/{u}/consents", json={"tier": tier, "granted": True})
        now = datetime.now(timezone.utc)
        txs = [{"merchant": "NETFLIX.COM", "amount_krw": 17000, "at": (now - timedelta(days=d)).isoformat()} for d in (58, 28)]
        assert c.post(f"/v1/users/{u}/transactions", json=txs).json()["added"] == 2
        csv = "Title,Date\n" + "\n".join(f"쇼 {i},{(now - timedelta(days=35 + i)).strftime('%m/%d/%y')}" for i in range(2))
        assert c.post(f"/v1/users/{u}/usage/netflix-csv", json={"content": csv}).json()["estimated"]
        out = c.post(f"/v1/users/{u}/subscriptions/optimize").json()
        d = out["decisions"][0]
        assert d["service"] == "netflix" and d["best"] != "premium"
        qs = c.get(f"/v1/users/{u}/questions").json()
        assert qs and qs[0]["kind"] in ("ask_idle", "ask_change")
        a = c.post(f"/v1/questions/{qs[0]['id']}/answer", json={"answer": "keep"}).json()
        assert a["snooze_until"]
        out2 = c.post(f"/v1/users/{u}/subscriptions/optimize").json()
        assert out2["decisions"][0]["action"] == "none"        # 90일 유예


def test_hmac_required(monkeypatch):
    monkeypatch.setattr(settings, "require_hmac", True)
    c = _client()
    body = json.dumps({"checkout_id": "x", "amount_krw": 1, "merchant_id": "m", "wallet_id": "nope"}).encode()
    assert c.post("/v1/payments/authorize", content=body, headers={"content-type": "application/json"}).status_code == 401
    sig = hmac.new(settings.pos_hmac_secret.encode(), body, hashlib.sha256).hexdigest()
    r = c.post("/v1/payments/authorize", content=body, headers={"content-type": "application/json", "X-SB-Signature": sig})
    assert r.status_code == 200 and r.json()["reason"] == "UNKNOWN_WALLET"


def test_authorize_latency_budget(setup_user):
    import asyncio

    from app.services.authorization import AuthRequest, authorize
    lat = []

    async def go():
        for i in range(20):
            t = time.perf_counter()
            await authorize(AuthRequest(f"lat-{i}", 5_000, "m1", card_token="card1"))
            lat.append((time.perf_counter() - t) * 1000)
            await runtime.rt.drain()
    asyncio.new_event_loop().run_until_complete(go())
    lat.sort()
    print(f"authorize p50={lat[10]:.1f}ms p95={lat[18]:.1f}ms")
    assert lat[18] < 1000
