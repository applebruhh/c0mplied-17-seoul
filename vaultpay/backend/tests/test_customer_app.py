import asyncio
from uuid import uuid4

import pytest

from conftest import running_app

pytestmark = pytest.mark.anyio
UID = "04:AA:BB:CC:DD:EE:01"


async def sign_up(client, name="김알렉스"):
    response = await client.post("/api/app/users", json={
        "name": name, "pay_assets": ["USDC", "ETH"], "risk_profile": "neutral",
        "delegation_agreed": True, "lock_notice_agreed": True,
    })
    assert response.status_code == 201, response.text
    return response.json()


async def deposit(client, customer_id, asset, amount):
    response = await client.post(f"/api/app/users/{customer_id}/deposits", json={"asset": asset, "amount_krw": amount},
                                 headers={"Idempotency-Key": str(uuid4())})
    assert response.status_code == 200, response.text
    return response.json()


async def test_sign_up_requires_both_consents(tmp_path):
    async with running_app(tmp_path / "pos.sqlite3") as (_, client):
        response = await client.post("/api/app/users", json={
            "name": "A", "pay_assets": ["USDC"], "delegation_agreed": True, "lock_notice_agreed": False})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "CONSENT_REQUIRED"


async def test_sign_up_deposit_and_preview(tmp_path):
    async with running_app(tmp_path / "pos.sqlite3") as (_, client):
        me = await sign_up(client)
        customer_id = me["profile"]["customer_id"]
        assert me["wallet"]["total_krw"] == 0 and me["cards"] == []
        me = await deposit(client, customer_id, "USDC", 14_000)
        assert me["wallet"]["total_krw"] == 14_000
        usdc = next(item for item in me["wallet"]["assets"] if item["asset"] == "USDC")
        assert usdc["quantity"] == "10.00"
        preview = (await client.get(f"/api/app/users/{customer_id}/sell-preview?amount_krw=4500")).json()
        assert preview["status"] == "OK" and preview["required_krw"] == 4635
        assert preview["legs"] == [{"asset": "USDC", "value_krw": 4635, "quantity": "3.31"}]
        too_much = (await client.get(f"/api/app/users/{customer_id}/sell-preview?amount_krw=20000")).json()
        assert too_much["status"] == "LIMIT_EXCEEDED"
        activity = (await client.get(f"/api/app/users/{customer_id}/activity")).json()["items"]
        assert activity[0]["kind"] == "DEPOSIT" and activity[0]["amount_krw"] == 14_000


async def test_terminal_enrollment_then_payment_settles_and_shows_deduction(tmp_path):
    async with running_app(tmp_path / "pos.sqlite3", stage_seconds=0.02) as (_, client):
        customer_id = (await sign_up(client))["profile"]["customer_id"]
        await deposit(client, customer_id, "USDC", 20_000)

        enrollment = (await client.post(f"/api/app/users/{customer_id}/card-enrollments",
                                        json={"terminal_id": "cafe-01"})).json()
        pending = (await client.get("/api/terminals/cafe-01/pending-enrollment")).json()["enrollment"]
        assert pending["enrollment_id"] == enrollment["enrollment_id"] and pending["customer_name"] == "김알렉스"
        done = (await client.post(f"/api/terminals/cafe-01/enrollments/{enrollment['enrollment_id']}/complete",
                                  json={"uid": UID})).json()
        assert done["status"] == "COMPLETED" and done["card_alias"] == "04AABBCCDDEE01"
        assert (await client.get("/api/terminals/cafe-01/pending-enrollment")).json()["enrollment"] is None
        cards = (await client.get(f"/api/app/users/{customer_id}")).json()["cards"]
        assert [card["card_alias"] for card in cards] == ["04AABBCCDDEE01"]

        created = (await client.post("/api/checkouts", json={
            "merchant_id": "cafe", "terminal_id": "cafe-01", "amount_krw": 4500})).json()
        tap = await client.post("/api/nfc/tap", json={
            "terminal_id": "cafe-01", "checkout_id": created["checkout_id"], "card_alias": "04AABBCCDDEE01",
            "client_event_id": str(uuid4())})
        assert tap.status_code == 200 and tap.json()["checkout"]["status"] == "AUTHORIZED"

        me = (await client.get(f"/api/app/users/{customer_id}")).json()
        assert me["wallet"]["locked"] is True
        blocked = await client.post(f"/api/app/users/{customer_id}/withdrawals", json={"asset": "USDC", "amount_krw": 100},
                                    headers={"Idempotency-Key": str(uuid4())})
        assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "WALLET_LOCKED"

        for _ in range(200):
            items = (await client.get(f"/api/app/users/{customer_id}/activity")).json()["items"]
            payment = next(item for item in items if item["kind"] == "PAYMENT")
            if payment["settlement_status"] == "COMPLETED":
                break
            await asyncio.sleep(0.02)
        assert payment["merchant"] == "BlockBucks 카페" and payment["amount_krw"] == 4500
        assert payment["sold"][0]["asset"] == "USDC" and payment["sold_krw"] == 4635
        assert payment["excess_returned_krw"] > 0
        me = (await client.get(f"/api/app/users/{customer_id}")).json()
        assert me["wallet"]["locked"] is False
        assert 20_000 - 4635 < me["wallet"]["total_krw"] < 20_000 - 4500

        overview = (await client.get("/api/ops/overview")).json()
        assert overview["authorized_total"] == 1 and overview["approved_today_krw"] == 4500
        assert any(wallet["customer_id"] == customer_id for wallet in overview["wallets"])


async def test_enrollment_rejects_bad_uid_and_expires_on_replace(tmp_path):
    async with running_app(tmp_path / "pos.sqlite3") as (_, client):
        customer_id = (await sign_up(client))["profile"]["customer_id"]
        first = (await client.post(f"/api/app/users/{customer_id}/card-enrollments", json={})).json()
        second = (await client.post(f"/api/app/users/{customer_id}/card-enrollments", json={})).json()
        assert (await client.get(f"/api/app/card-enrollments/{first['enrollment_id']}")).json()["status"] == "CANCELLED"
        bad = await client.post(f"/api/terminals/cafe-01/enrollments/{second['enrollment_id']}/complete",
                                json={"uid": "not-hex"})
        assert bad.status_code == 422
        manual = await client.post(f"/api/app/users/{customer_id}/cards", json={"uid": "04A1B2C3D4E5F6"})
        assert manual.status_code == 200 and manual.json()["card_token"]
        # The seeded demo UID moves to the customer who registered it last.
        accounts = (await client.get("/api/app/demo-accounts")).json()["items"]
        assert any(item["customer_id"] == customer_id for item in accounts)


async def test_pages_are_served_or_report_missing_build(tmp_path):
    async with running_app(tmp_path / "pos.sqlite3") as (_, client):
        for page in ("/", "/app", "/pos", "/terminal", "/ops"):
            assert (await client.get(page)).status_code in (200, 503)
