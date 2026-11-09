from contextlib import asynccontextmanager
import json
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from app.adapters.external import ExternalPaymentAdapter
from app.adapters.mock import MockPaymentAdapter
from app.cards import enroll_external_card
from app.config import Settings
from app.contracts import ExternalCardRegistration
from app.main import create_app
from test_backend import wait_for_payment
from test_external_contract import SECRET, TOKEN, reply

UID = "04:a1:b2:c3:d4:e5:f6"
ALIAS = "shortcut-card"
OPS_TOKEN = "local-ops-test-token"
OPS_HEADERS = {"Authorization": f"Bearer {OPS_TOKEN}"}


@asynccontextmanager
async def external_app(path, handler, *, ops_token=OPS_TOKEN):
    app = create_app(
        settings=Settings(
            database_path=path, payment_engine_mode="external", payment_engine_base_url="https://engine.test",
            sb_pos_hmac_secret=SECRET, ops_token=ops_token, worker_interval=0.01,
        ),
        engine_transport=httpx.MockTransport(handler),
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://pos") as client:
            yield app, client


def enroll(app, **overrides):
    fields = {"card_alias": ALIAS, "uid": UID, "customer_id": "customer-success"} | overrides
    with app.state.database.transaction() as session:
        return enroll_external_card(session, **fields)


async def checkout_and_tap(client, identity):
    checkout = await client.post("/api/checkouts", json={
        "merchant_id": "cafe", "terminal_id": "cafe-01", "amount_krw": 4500,
    })
    assert checkout.status_code == 201, checkout.text
    tap = {"terminal_id": "cafe-01", "checkout_id": checkout.json()["checkout_id"],
           "client_event_id": str(uuid4()), "card_alias": identity}
    return tap, await client.post("/api/nfc/tap", json=tap)


def test_modes_and_external_config_are_explicit(monkeypatch):
    for name in ("PAYMENT_ENGINE_MODE", "PAYMENT_ENGINE_BASE_URL", "SB_POS_HMAC_SECRET"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings.from_env()
    assert isinstance(create_app(settings=settings).state.adapter, MockPaymentAdapter)

    monkeypatch.setenv("PAYMENT_ENGINE_MODE", "external")
    monkeypatch.setenv("PAYMENT_ENGINE_BASE_URL", "https://engine.test")
    with pytest.raises(ValidationError, match="SB_POS_HMAC_SECRET"):
        Settings.from_env()
    monkeypatch.setenv("SB_POS_HMAC_SECRET", SECRET)
    settings = Settings.from_env()
    assert settings.payment_engine_mode == "external" and SECRET not in repr(settings)

    monkeypatch.setenv("PAYMENT_ENGINE_BASE_URL", "https://user:password@engine.test")
    with pytest.raises(ValidationError, match="origin") as error:
        Settings.from_env()
    assert "password" not in str(error.value) and SECRET not in str(error.value)


@pytest.mark.anyio
@pytest.mark.parametrize("identity", [UID, "04-a1-b2-c3-d4-e5-f6", "04 A1\tB2 C3 D4 E5 F6", ALIAS])
async def test_registered_uid_and_shortcut_resolve_to_same_authorization(tmp_path, identity):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=reply())

    async with external_app(tmp_path / "pos.sqlite3", handler) as (app, client):
        assert isinstance(app.state.adapter, ExternalPaymentAdapter)
        assert not (await client.get("/api/capabilities")).json()["demo_only"]
        assert enroll(app) == enroll(app) == TOKEN
        tap, response = await checkout_and_tap(client, identity)
        assert response.status_code == 200, response.text
        assert response.json()["checkout"]["status"] == "AUTHORIZED"
        assert len(calls) == 1
        assert json.loads(calls[0].content) == {
            "checkout_id": tap["checkout_id"], "merchant_id": "cafe", "amount_krw": 4500, "card_token": TOKEN,
        }


@pytest.mark.anyio
@pytest.mark.parametrize("identity", ["demo-alice", "not-registered", UID])
async def test_unenrolled_card_does_not_claim_checkout_or_call_engine(tmp_path, identity):
    def handler(request):
        pytest.fail(f"Unexpected {request.method} to {request.url.path}")

    async with external_app(tmp_path / "pos.sqlite3", handler) as (_, client):
        tap, response = await checkout_and_tap(client, identity)
        assert response.status_code == 404
        checkout = await client.get(f"/api/checkouts/{tap['checkout_id']}")
        assert checkout.json()["status"] == "WAITING_FOR_CARD"


@pytest.mark.anyio
async def test_enrollment_rejects_conflicts_and_registration_payload_uses_token(tmp_path):
    def handler(request):
        pytest.fail("Local enrollment must never call the remote server")

    async with external_app(tmp_path / "pos.sqlite3", handler) as (app, _):
        assert enroll(app) == TOKEN
        with pytest.raises(ValueError, match="different token"):
            enroll(app, uid="04:a1:b2:c3:d4:e5:f7")
        with pytest.raises(ValueError, match="different alias"):
            enroll(app, card_alias="other-shortcut")
        with pytest.raises(ValueError, match="another customer"):
            enroll(app, customer_id="customer-limit")
        payload = ExternalCardRegistration(token=TOKEN, wallet_id="fixture-wallet")
        assert payload.model_dump(mode="json") == {"token": TOKEN, "wallet_id": "fixture-wallet"}
        with pytest.raises(ValidationError):
            ExternalCardRegistration.model_validate({"card_token": TOKEN, "wallet_id": "fixture-wallet"})


@pytest.mark.anyio
async def test_worker_retries_only_unsent_post_with_original_wire_bytes(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.PoolTimeout("Could not acquire a connection", request=request)
        return httpx.Response(200, json=reply())

    async with external_app(tmp_path / "pos.sqlite3", handler) as (app, client):
        enroll(app)
        _, response = await checkout_and_tap(client, ALIAS)
        assert response.status_code == 202
        payment = response.json()["payment"]
        assert payment["authorization_status"] == "UNKNOWN"
        client.headers.update(OPS_HEADERS)
        await wait_for_payment(client, payment["payment_id"], lambda p: p["authorization_status"] == "AUTHORIZED")
        first, second = calls
        assert first.method == second.method == "POST"
        assert first.content == second.content
        assert first.headers["X-SB-Signature"] == second.headers["X-SB-Signature"]
        assert json.loads(second.content)["checkout_id"] == payment["checkout_id"]


@pytest.mark.anyio
async def test_ops_proxy_is_read_only_and_preserves_frozen_local_projection(tmp_path):
    # An opaque stub value checks transport only. No upstream shape is implied.
    fixture = ["local HTTP stub", {"fixture": True}]
    calls = []

    def handler(request):
        assert request.method == "GET"
        assert "Authorization" not in request.headers and "X-SB-Signature" not in request.headers
        calls.append((request.url.path, dict(request.url.params)))
        return httpx.Response(200, json=fixture)

    async with external_app(tmp_path / "pos.sqlite3", handler) as (_, client):
        base = "/api/ops"
        cases = [
            ("/engine/payments", "/ops/payments", {"limit": "50"}),
            ("/engine/payments?limit=3", "/ops/payments", {"limit": "3"}),
            ("/engine/payments/stub-id", "/ops/payments/stub-id", {}),
            ("/alerts?audience=internal", "/ops/alerts", {"limit": "100", "audience": "internal"}),
            ("/decisions?kind=stub&ref_id=a%26b&limit=2", "/ops/decisions",
             {"kind": "stub", "ref_id": "a&b", "limit": "2"}),
            ("/decisions", "/ops/decisions", {"limit": "20"}),
        ]
        for local, _, _ in cases:
            assert (await client.get(base + local)).status_code == 403
        assert (await client.get(base + "/events?follow=false")).status_code == 403
        assert (await client.get(base + "/payments")).status_code == 403
        assert (await client.get(base + f"/payments/{uuid4()}")).status_code == 403
        assert calls == []

        for local, _, _ in cases:
            response = await client.get(base + local, headers=OPS_HEADERS)
            assert response.json() == fixture
            assert response.headers["Cache-Control"] == "no-store"
        assert calls == [(path, query) for _, path, query in cases]
        projection = await client.get(base + "/payments", headers=OPS_HEADERS)
        assert projection.json()["items"] == [] and projection.json()["total"] == 0
        assert (await client.get(base + "/alerts?limit=101", headers=OPS_HEADERS)).status_code == 422
        assert len(calls) == len(cases)


@pytest.mark.anyio
async def test_ops_errors_are_generic_and_missing_token_disables_routes(tmp_path):
    async with external_app(tmp_path / "configured.sqlite3", lambda _: httpx.Response(
        503, content=b"private upstream details",
    )) as (_, client):
        response = await client.get("/api/ops/alerts", headers=OPS_HEADERS)
        assert response.status_code == 502 and "private upstream details" not in response.text

    def unexpected(request):
        pytest.fail(f"Unexpected proxy request: {request.url.path}")

    async with external_app(tmp_path / "disabled.sqlite3", unexpected, ops_token=None) as (_, client):
        assert (await client.get("/api/ops/alerts", headers=OPS_HEADERS)).status_code == 503
        assert (await client.get("/api/ops/payments")).status_code == 503
        assert (await client.get("/api/health")).status_code == 200
