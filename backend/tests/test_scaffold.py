import asyncio
import sqlite3
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.adapters.external import ExternalPaymentAdapter
from app.adapters.mock import MockPaymentAdapter
from app.db import connect, initialize
from app.main import create_app
from app.models import AuthorizationRequest, CheckoutCreate, TapRequest


def request():
    return AuthorizationRequest(
        payment_id=uuid4(), checkout_id=uuid4(), customer_id="demo-alice", amount_krw=4500
    )


def test_startup_initializes_schema_without_enabling_payments(tmp_path):
    path = tmp_path / "demo.sqlite3"
    with TestClient(create_app(path)) as client:
        assert client.get("/api/v1/health").json()["status"] == "ok"
        assert client.get("/api/v1/capabilities").json()["payment_routes_enabled"] is False
        assert client.post("/api/v1/taps", json={}).status_code == 404
    with connect(path) as db:
        assert db.execute("SELECT count(*) FROM payments").fetchone()[0] == 0


@pytest.mark.parametrize("amount", [0, -1, 1.5, True, "4500", 10_000_001])
def test_amount_must_be_bounded_integer(amount):
    with pytest.raises(ValidationError):
        CheckoutCreate(terminal_id="galaxy-01", amount_krw=amount)


def test_tap_cannot_override_amount_or_outcome():
    with pytest.raises(ValidationError):
        TapRequest(
            terminal_id="galaxy-01", checkout_id=uuid4(), card_id="demo-alice",
            amount_krw=1, authorization_status="AUTHORIZED",
        )


def test_mock_is_explicitly_simulated_and_idempotent():
    async def run():
        adapter = MockPaymentAdapter()
        command = request()
        first = await adapter.authorize(command)
        assert first.source == "simulated"
        assert first.settlement_status == "PENDING"
        assert first == await adapter.authorize(command)
        assert first == await adapter.get_payment(command.payment_id)
        first.assets.clear()
        assert (await adapter.get_payment(command.payment_id)).assets == ["USDC"]
        with pytest.raises(ValueError, match="different request"):
            await adapter.authorize(command.model_copy(update={"amount_krw": 5000}))
    asyncio.run(run())


def test_external_contract_uses_persisted_key_and_preserves_recovery():
    async def run():
        command = request()
        projection = (await MockPaymentAdapter().authorize(command)).model_dump(mode="json")
        projection.update(source="external", settlement_status="RECOVERY_REQUIRED")

        def handler(incoming):
            if incoming.method == "POST":
                assert incoming.headers["Idempotency-Key"] == str(command.payment_id)
            return httpx.Response(200, json=projection)

        async with httpx.AsyncClient(
            base_url="https://engine.example/v1/", transport=httpx.MockTransport(handler)
        ) as client:
            adapter = ExternalPaymentAdapter(client)
            result = await adapter.authorize(command)
            assert result.authorization_status == "AUTHORIZED"
            assert result.settlement_status == "RECOVERY_REQUIRED"
            assert result.asset_reservation_status == "RESERVED"
            assert await adapter.get_payment(command.payment_id) == result
    asyncio.run(run())


@pytest.mark.parametrize("mutation", [
    {"amount_krw": 1}, {"payment_id": str(uuid4())},
    {"assets": ["STOCK"]}, {"source": "simulated"},
    {"krw_reservation_status": "NONE"},
])
def test_external_rejects_invalid_engine_results(mutation):
    async def run():
        command = request()
        projection = (await MockPaymentAdapter().authorize(command)).model_dump(mode="json")
        projection.update(source="external")
        projection.update(mutation)
        async with httpx.AsyncClient(
            base_url="https://engine.example/",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=projection)),
        ) as client:
            with pytest.raises(ValueError):
                await ExternalPaymentAdapter(client).authorize(command)
    asyncio.run(run())


def test_engine_timeout_does_not_become_decline():
    async def run():
        def handler(incoming):
            raise httpx.ReadTimeout("Unknown engine outcome", request=incoming)
        async with httpx.AsyncClient(
            base_url="https://engine.example/", transport=httpx.MockTransport(handler)
        ) as client:
            with pytest.raises(httpx.ReadTimeout):
                await ExternalPaymentAdapter(client).authorize(request())
    asyncio.run(run())


def test_schema_blocks_second_live_checkout_and_enforces_foreign_keys(tmp_path):
    path = tmp_path / "demo.sqlite3"
    initialize(path)
    initialize(path)
    with connect(path) as db:
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        db.execute("INSERT INTO terminals VALUES ('galaxy-01', 'Galaxy')")
        statement = """INSERT INTO checkouts
            (id, terminal_id, creation_key, creation_fingerprint, amount_krw,
             status, created_at, expires_at, updated_at)
            VALUES (?, 'galaxy-01', ?, 'hash', 4500, 'WAITING', ?, ?, ?)"""
        timestamp = "2026-11-09T01:00:00Z"
        db.execute(statement, ("first", "key-1", timestamp, timestamp, timestamp))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(statement, ("second", "key-2", timestamp, timestamp, timestamp))
        db.execute("UPDATE checkouts SET status = 'APPROVED' WHERE id = 'first'")
        db.execute(statement, ("second", "key-2", timestamp, timestamp, timestamp))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO demo_cards VALUES ('bad', 'missing', 1)")
