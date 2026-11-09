import asyncio
import hashlib
import hmac
import json
from pathlib import Path
import subprocess
import sys
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from app.adapters.base import EnginePaymentNotFound
from app.adapters.external import EngineOutcomeUnknown, ExternalPaymentAdapter
from app.cards import enroll_external_card
from app.contracts import ExternalAuthorizeRequest, ExternalAuthorizeResponse
from app.db import Database
from app.main import create_app
from app.config import Settings
from app.models import AuthorizationRequest, AuthorizationResult, EnginePayment
from app.signing import card_token_from_uid, normalize_uid, sign_raw_json
from app.tables import AuthorizationSnapshot, Checkout, Event, MockEngineRecord, Payment
from test_backend import wait_for_payment

TOKEN = "91ea67e855c23f69148746d73bf9c5d290227c0c4ab762f3fcc84713512d892b"
SECRET = "contract-test-secret"  # Public deterministic test vector, not a credential.
RAW = (b'{"amount_krw":4500,"card_token":"91ea67e855c23f69148746d73bf9c5d290227c0c4ab762f3fcc84713512d892b",'
       b'"checkout_id":"00000000-0000-0000-0000-000000000001","merchant_id":"cafe"}')
SIGNATURE = "3379048687feba1393a1abfd08c93ae781f31b380e018cd3549f2d2d02d10176"
ROOT = Path(__file__).resolve().parents[2]


def command(**overrides):
    data = dict(payment_id=uuid4(), checkout_id=uuid4(), merchant_id="cafe", terminal_id="cafe-01",
                customer_id="customer-success", amount_krw=4500, card_token=TOKEN)
    return AuthorizationRequest(**(data | overrides))


def reply(status="AUTHORIZED", reason=None, **overrides):
    return {"payment_id": "sb-engine-payment-1", "status": status, "reason": reason,
            "latency_ms": 3, "idempotent_replay": False} | overrides


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / "external.sqlite3")
    db.initialize()
    yield db
    db.close()


@pytest.mark.parametrize("uid", ["04:a1:b2:c3:d4:e5:f6", "04-A1-B2-C3-D4-E5-F6", " 04 a1\tb2\nc3 d4 e5 f6 ", "04A1B2C3D4E5F6"])
def test_uid_token_matches_team_vector_and_preserves_byte_order(uid):
    assert normalize_uid(uid) == "04A1B2C3D4E5F6"
    assert card_token_from_uid(uid) == TOKEN
    assert card_token_from_uid(uid) != hashlib.sha256(bytes.fromhex(normalize_uid(uid))).hexdigest()
    assert card_token_from_uid("f6:e5:d4:c3:b2:a1:04") != TOKEN


@pytest.mark.parametrize("uid", ["", "0", "0x04", "04:zz", "ＡＢ", "04/ab"])
def test_invalid_uids_are_rejected(uid):
    with pytest.raises(ValueError):
        card_token_from_uid(uid)


def test_hmac_matches_rfc4231_and_frozen_exact_wire_vector():
    assert sign_raw_json(b"what do ya want for nothing?", "Jefe") == "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"
    wire = ExternalAuthorizeRequest(checkout_id=UUID(int=1), amount_krw=4500, merchant_id="cafe", card_token=TOKEN)
    assert wire.raw_json() == RAW
    assert sign_raw_json(RAW, SECRET) == SIGNATURE
    assert sign_raw_json(RAW + b"\n", SECRET) != SIGNATURE
    assert sign_raw_json(b"", SECRET) == hmac.new(SECRET.encode(), b"", hashlib.sha256).hexdigest()


@pytest.mark.parametrize("bad", [{"amount_krw": True}, {"amount_krw": "4500"}, {"card_token": TOKEN.upper()},
                                  {"currency": "KRW"}, {"payment_id": "not-on-wire"}])
def test_external_request_is_exactly_four_fields(bad):
    with pytest.raises(ValidationError):
        ExternalAuthorizeRequest.model_validate({"checkout_id": str(uuid4()), "amount_krw": 4500,
                                                "merchant_id": "cafe", "card_token": TOKEN} | bad)


@pytest.mark.parametrize("bad", [reply(reason="WALLET_BUSY"), reply("DECLINED"), reply("DECLINED", "SOMETHING_ELSE"),
                                  reply(latency_ms=True), reply(idempotent_replay="false"), reply(unexpected=True)])
def test_external_response_rejects_contract_mismatches(bad):
    with pytest.raises(ValidationError):
        ExternalAuthorizeResponse.model_validate(bad)


@pytest.mark.anyio
async def test_signed_transport_sends_the_exact_raw_body_and_common_response(database, monkeypatch):
    monkeypatch.setenv("SB_POS_HMAC_SECRET", SECRET)
    calls = []
    def handler(request):
        calls.append(request)
        assert request.method == "POST" and request.url.path == "/v1/payments/authorize"
        assert request.content == RAW
        assert request.headers["X-SB-Signature"] == SIGNATURE
        assert request.headers["Content-Type"] == "application/json"
        assert set(json.loads(request.content)) == {"checkout_id", "amount_krw", "merchant_id", "card_token"}
        return httpx.Response(200, json=reply())
    async with httpx.AsyncClient(base_url="https://engine.test", transport=httpx.MockTransport(handler)) as client:
        adapter = ExternalPaymentAdapter(client, database)
        request = command(checkout_id=UUID(int=1))
        result = await adapter.authorize(request)
        assert isinstance(result, AuthorizationResult) and not isinstance(result, EnginePayment)
        assert result.payment_id == request.payment_id and result.engine_payment_id == "sb-engine-payment-1"
        assert result.authorization_status == "AUTHORIZED" and not result.financial_status_reported
        assert result.latency_ms == 3 and result.source == "external"
        assert result == await adapter.authorize(request)
        assert len(calls) == 1
        with database.transaction(write=False) as session:
            assert session.get(AuthorizationSnapshot, str(request.checkout_id)).wire_body == RAW


@pytest.mark.anyio
@pytest.mark.parametrize("reason", ["LIMIT_EXCEEDED", "WALLET_BUSY", "UNKNOWN_WALLET", "PRICE_UNAVAILABLE"])
async def test_all_final_external_decline_reasons_are_preserved(database, reason):
    async with httpx.AsyncClient(base_url="https://engine.test", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=reply("DECLINED", reason))
    )) as client:
        result = await ExternalPaymentAdapter(client, database, secret=SECRET).authorize(command())
        assert result.authorization_status == "DECLINED" and result.reason_code == reason


@pytest.mark.anyio
async def test_missing_secret_token_or_wrong_base_url_never_dispatch(database, monkeypatch):
    monkeypatch.delenv("SB_POS_HMAC_SECRET", raising=False)
    calls = []
    async with httpx.AsyncClient(base_url="https://engine.test", transport=httpx.MockTransport(
        lambda request: calls.append(request) or httpx.Response(200, json=reply())
    )) as client:
        with pytest.raises(ValueError, match="SB_POS_HMAC_SECRET"):
            ExternalPaymentAdapter(client, database)
        with pytest.raises(ValueError, match="card token"):
            await ExternalPaymentAdapter(client, database, secret=SECRET).authorize(command(card_token=None))
        assert calls == []
    async with httpx.AsyncClient(base_url="https://engine.test/prefix/") as client:
        with pytest.raises(ValueError, match="origin"):
            ExternalPaymentAdapter(client, database, secret=SECRET)


@pytest.mark.anyio
@pytest.mark.parametrize("change", [{"amount_krw": 4501}, {"merchant_id": "another"}, {"card_token": "a" * 64},
                                    {"payment_id": UUID(int=2)}, {"customer_id": "another"}])
async def test_payload_is_immutable_across_adapter_restarts(database, change):
    calls = []
    async with httpx.AsyncClient(base_url="https://engine.test", transport=httpx.MockTransport(
        lambda request: calls.append(request) or httpx.Response(200, json=reply())
    )) as client:
        request = command()
        await ExternalPaymentAdapter(client, database, secret=SECRET).authorize(request)
        restarted = ExternalPaymentAdapter(client, database, secret=SECRET)
        with pytest.raises(ValueError, match="immutable"):
            await restarted.authorize(request.model_copy(update=change))
        assert len(calls) == 1


@pytest.mark.anyio
async def test_timeout_retry_looks_up_same_checkout_and_never_resubmits(database):
    calls = []
    def handler(request):
        calls.append(request)
        if request.method == "POST":
            raise httpx.ReadTimeout("Response lost", request=request)
        assert request.url.path == f"/v1/payments/{checkout_id}/pos"
        assert request.content == b""
        assert request.headers["X-SB-Signature"] == sign_raw_json(request.content, SECRET)
        return httpx.Response(200, json=reply(idempotent_replay=True))
    async with httpx.AsyncClient(base_url="https://engine.test", transport=httpx.MockTransport(handler)) as client:
        request = command()
        checkout_id = request.checkout_id
        with pytest.raises(EngineOutcomeUnknown):
            await ExternalPaymentAdapter(client, database, secret=SECRET).authorize(request)
        result = await ExternalPaymentAdapter(client, database, secret=SECRET).authorize(request)
        assert result.authorization_status == "AUTHORIZED" and result.idempotent_replay
        assert [r.method for r in calls] == ["POST", "GET"]


@pytest.mark.anyio
@pytest.mark.parametrize("response", [httpx.Response(404), httpx.Response(500), httpx.Response(200, json={"payment": reply()}),
                                      httpx.Response(200, content=b"invalid json"), httpx.Response(307, headers={"Location": "https://other.test"})])
async def test_uncertain_or_unspecified_responses_do_not_become_declines_or_absence(database, response):
    calls = []
    async with httpx.AsyncClient(base_url="https://engine.test", transport=httpx.MockTransport(
        lambda request: calls.append(request) or response
    )) as client:
        request = command()
        adapter = ExternalPaymentAdapter(client, database, secret=SECRET)
        for _ in range(2):
            with pytest.raises(EngineOutcomeUnknown) as error:
                await adapter.authorize(request)
            assert not isinstance(error.value, EnginePaymentNotFound)
        assert [r.method for r in calls] == ["POST", "GET"]


@pytest.mark.anyio
async def test_parallel_authorize_calls_claim_one_initial_post(database):
    posted, finish_post = asyncio.Event(), asyncio.Event()
    calls = []
    async def handler(request):
        calls.append(request.method)
        if request.method == "POST":
            posted.set()
            await finish_post.wait()
            return httpx.Response(200, json=reply())
        return httpx.Response(200, json=reply("UNKNOWN"))
    async with httpx.AsyncClient(base_url="https://engine.test", transport=httpx.MockTransport(handler)) as client:
        request = command()
        first = asyncio.create_task(ExternalPaymentAdapter(client, database, secret=SECRET).authorize(request))
        await posted.wait()
        second = await ExternalPaymentAdapter(client, database, secret=SECRET).authorize(request)
        finish_post.set()
        assert (await first).authorization_status == "AUTHORIZED"
        assert second.authorization_status == "UNKNOWN"
        assert calls == ["POST", "GET"]


@pytest.mark.anyio
async def test_lookup_deduplicates_versions_and_rejects_reversal_or_changed_identity(database):
    responses = [reply(), reply(idempotent_replay=True, latency_ms=1), reply("DECLINED", "WALLET_BUSY"), reply(payment_id="another-id")]
    async with httpx.AsyncClient(base_url="https://engine.test", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=responses.pop(0))
    )) as client:
        adapter, request = ExternalPaymentAdapter(client, database, secret=SECRET), command()
        first = await adapter.authorize(request)
        assert (await adapter.get_payment(request.checkout_id)).version == first.version
        with pytest.raises(EngineOutcomeUnknown, match="reversed"):
            await adapter.get_payment(request.checkout_id)
        with pytest.raises(EngineOutcomeUnknown, match="correlation"):
            await adapter.get_payment(request.checkout_id)


@pytest.mark.anyio
async def test_pos_external_bridge_preserves_ids_and_does_not_invent_financial_state(tmp_path):
    path = tmp_path / "bridge.sqlite3"
    database = Database(path)
    seen = []
    def handler(request):
        seen.append(request)
        body = json.loads(request.content)
        assert body["merchant_id"] == "cafe" and body["card_token"] == TOKEN
        return httpx.Response(200, json=reply())
    async with httpx.AsyncClient(base_url="https://engine.test", transport=httpx.MockTransport(handler)) as remote:
        adapter = ExternalPaymentAdapter(remote, database, secret=SECRET)
        app = create_app(settings=Settings(database_path=path), payment_adapter=adapter)
        async with app.router.lifespan_context(app):
            with app.state.database.transaction() as session:
                enroll_external_card(
                    session, card_alias="04A1B2C3D4E5F6", uid="04A1B2C3D4E5F6", customer_id="customer-success",
                )
            async with httpx.AsyncClient(base_url="http://pos", transport=httpx.ASGITransport(app=app)) as client:
                created = await client.post("/api/checkouts", json={"merchant_id": "cafe", "terminal_id": "cafe-01", "amount_krw": 4500})
                assert created.status_code == 201, created.text
                checkout_id = created.json()["checkout_id"]
                body = {"terminal_id": "cafe-01", "card_alias": "04A1B2C3D4E5F6", "checkout_id": checkout_id, "client_event_id": str(uuid4())}
                response = await client.post("/api/nfc/tap", json=body)
                assert response.status_code == 200, response.text
                payment = response.json()["payment"]
                assert response.json()["checkout"]["status"] == "AUTHORIZED"
                assert payment["payment_id"] != "sb-engine-payment-1" and payment["engine_payment_id"] == "sb-engine-payment-1"
                assert not payment["financial_status_reported"]
                assert payment["asset_reservation_status"] == payment["prefunding_status"] == "PENDING"
                assert payment["merchant_payout_status"] is None and payment["hold_status"] is None
                assert payment["settlement_status"] == "NOT_STARTED"
                repeat = await client.post("/api/nfc/tap", json=body)
                assert repeat.json()["payment"]["payment_id"] == payment["payment_id"] and len(seen) == 1
                with app.state.database.transaction(write=False) as session:
                    assert session.scalar(select(func.count()).select_from(MockEngineRecord)) == 0
                    command_snapshot = app.state.service.command(session, session.get(Payment, payment["payment_id"]))
                    assert command_snapshot.card_token == TOKEN
    database.close()


@pytest.mark.anyio
async def test_pos_timeout_reconciles_via_get_with_original_payload(tmp_path):
    path = tmp_path / "timeout-bridge.sqlite3"
    database, calls = Database(path), []
    def handler(request):
        calls.append(request)
        if request.method == "POST":
            raise httpx.ReadTimeout("Unknown result", request=request)
        return httpx.Response(200, json=reply())
    async with httpx.AsyncClient(base_url="https://engine.test", transport=httpx.MockTransport(handler)) as remote:
        app = create_app(settings=Settings(database_path=path, worker_interval=0.01, ops_token="contract-ops-test"),
                         payment_adapter=ExternalPaymentAdapter(remote, database, secret=SECRET))
        async with app.router.lifespan_context(app):
            with app.state.database.transaction() as session:
                enroll_external_card(
                    session, card_alias="04A1B2C3D4E5F6", uid="04A1B2C3D4E5F6", customer_id="customer-success",
                )
            async with httpx.AsyncClient(base_url="http://pos", transport=httpx.ASGITransport(app=app)) as client:
                created = (await client.post("/api/checkouts", json={"merchant_id": "cafe", "terminal_id": "cafe-01", "amount_krw": 4500})).json()
                response = await client.post("/api/nfc/tap", json={"terminal_id": "cafe-01", "card_alias": "04A1B2C3D4E5F6",
                                                                 "checkout_id": created["checkout_id"], "client_event_id": str(uuid4())})
                assert response.status_code == 202 and response.json()["payment"]["authorization_status"] == "UNKNOWN"
                payment_id = response.json()["payment"]["payment_id"]
                client.headers["Authorization"] = "Bearer contract-ops-test"
                final = await wait_for_payment(client, payment_id, lambda p: p["authorization_status"] == "AUTHORIZED")
                assert not final["financial_status_reported"]
                assert [r.method for r in calls] == ["POST", "GET"]
    database.close()


@pytest.mark.anyio
async def test_mock_implements_the_same_authorization_result_interface(api):
    app, _ = api
    request = command(merchant_id="merchant-demo", terminal_id="galaxy-01", card_token=None)
    result = await app.state.adapter.authorize(request)
    assert isinstance(result, AuthorizationResult) and result.financial_status_reported
    assert result.authorization_status == "AUTHORIZED" and isinstance(result, EnginePayment)


def test_frozen_artifacts_have_no_contract_drift():
    completed = subprocess.run([sys.executable, str(ROOT / "scripts/freeze_contracts.py"), "--check"], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.anyio
async def test_observed_authorization_and_sparse_lookup_keep_engine_metadata(database):
    request = command()
    correlated = {"checkout_id": str(request.checkout_id), "amount_krw": 4500.0}

    def handler(call):
        if call.method == "POST":
            return httpx.Response(200, json=reply() | correlated)
        return httpx.Response(200, json=correlated | {"status": "AUTHORIZED", "reason": None})

    async with httpx.AsyncClient(base_url="https://engine.test", transport=httpx.MockTransport(handler)) as remote:
        adapter = ExternalPaymentAdapter(remote, database, secret=SECRET)
        authorized = await adapter.authorize(request)
        assert authorized.engine_payment_id == "sb-engine-payment-1"
        assert await adapter.get_payment(request.checkout_id) == authorized


@pytest.mark.anyio
async def test_observed_sparse_lookup_reconciles_without_inventing_metadata(database):
    request = command()
    calls = []

    def handler(call):
        calls.append(call.method)
        if call.method == "POST":
            raise httpx.ReadTimeout("Lost authorization response", request=call)
        return httpx.Response(200, json={
            "checkout_id": str(request.checkout_id), "amount_krw": 4500.0,
            "status": "DECLINED", "reason": "WALLET_BUSY",
        })

    async with httpx.AsyncClient(base_url="https://engine.test", transport=httpx.MockTransport(handler)) as remote:
        adapter = ExternalPaymentAdapter(remote, database, secret=SECRET)
        with pytest.raises(EngineOutcomeUnknown):
            await adapter.authorize(request)
        result = await adapter.get_payment(request.checkout_id)
        assert result.authorization_status == "DECLINED" and result.reason_code == "WALLET_BUSY"
        assert result.engine_payment_id is result.latency_ms is result.idempotent_replay is None
        assert await adapter.authorize(request) == result
        assert calls == ["POST", "GET"]


@pytest.mark.anyio
@pytest.mark.parametrize("mismatch", [{"amount_krw": 4501}, {"checkout_id": str(UUID(int=1))}])
async def test_correlated_engine_responses_must_match_original_request(database, mismatch):
    request = command()
    correlated = {"checkout_id": str(request.checkout_id), "amount_krw": 4500.0} | mismatch

    def handler(call):
        data = reply() if call.method == "POST" else {"status": "AUTHORIZED", "reason": None}
        return httpx.Response(200, json=data | correlated)

    async with httpx.AsyncClient(base_url="https://engine.test", transport=httpx.MockTransport(handler)) as remote:
        adapter = ExternalPaymentAdapter(remote, database, secret=SECRET)
        with pytest.raises(EngineOutcomeUnknown):
            await adapter.authorize(request)
        with pytest.raises(EngineOutcomeUnknown):
            await adapter.get_payment(request.checkout_id)
