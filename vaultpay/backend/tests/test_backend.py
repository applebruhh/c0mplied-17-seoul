import asyncio
import json
from datetime import timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.adapters.mock import MockPaymentAdapter, utcnow
from app.db import Database
from app.models import AuthorizationRequest, EnginePayment, MerchantPayoutStatus, MockProjection, TapRequest
from app.streaming import stream_events
from app.tables import Checkout, DemoCard, Event, MockEngineRecord, Payment, TapEvent, Terminal
from conftest import running_app

pytestmark = pytest.mark.anyio


async def checkout(client, terminal="galaxy-01", **overrides):
    response = await client.post("/api/checkouts", json={
        "merchant_id": "merchant-demo", "terminal_id": terminal, "amount_krw": 4500, **overrides,
    })
    assert response.status_code == 201, response.text
    return response.json()


def tap_body(checkout_id=None, **overrides):
    body = {"terminal_id": "galaxy-01", "card_alias": "demo-alice", "client_event_id": str(uuid4())}
    if checkout_id:
        body["checkout_id"] = checkout_id
    return body | overrides


async def wait_for_payment(client, payment_id, predicate):
    async with asyncio.timeout(4):
        while True:
            response = await client.get(f"/api/ops/payments/{payment_id}")
            assert response.status_code == 200, response.text
            value = response.json()
            if predicate(value):
                return value
            await asyncio.sleep(0.01)


def parse_sse(text):
    result = []
    for block in text.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "data" in fields:
            result.append((fields["event"], int(fields["id"]), json.loads(fields["data"])))
    return result


async def test_checkout_creation_and_persistence(api):
    app, client = api
    created = await checkout(client)
    assert created["status"] == "WAITING_FOR_CARD"
    assert created["merchant_id"] == "merchant-demo"
    assert created["payment"] is None
    assert created["amount_krw"] == 4500
    assert created["expires_at"].endswith("Z")
    fetched = await client.get(f'/api/checkouts/{created["checkout_id"]}')
    assert fetched.json() == created
    active = await client.get("/api/terminals/galaxy-01/active-checkout")
    assert active.json()["checkout"] == created
    with app.state.database.transaction(write=False) as session:
        assert session.get(Checkout, created["checkout_id"]).amount_krw == 4500
    assert (await client.get("/api/v1/health")).json()["status"] == "ok"
    assert (await client.get("/api/capabilities")).json()["sse_enabled"]
    assert "/api/nfc/tap" in (await client.get("/openapi.json")).json()["paths"]


async def test_duplicate_active_checkout_and_creation_idempotency(api):
    _, client = api
    key = str(uuid4())
    body = {"merchant_id": "merchant-demo", "terminal_id": "galaxy-01", "amount_krw": 4500}
    first = await client.post("/api/checkouts", json=body, headers={"Idempotency-Key": key})
    retry = await client.post("/api/checkouts", json=body, headers={"Idempotency-Key": key})
    assert first.status_code == 201 and retry.status_code == 200
    assert first.json() == retry.json()
    conflict = await client.post("/api/checkouts", json=body | {"amount_krw": 5000}, headers={"Idempotency-Key": key})
    assert conflict.status_code == 409
    duplicate = await client.post("/api/checkouts", json=body)
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "ACTIVE_CHECKOUT_EXISTS"
    await checkout(client, "galaxy-02")


async def test_terminal_and_merchant_validation(api):
    app, client = api
    for body, code in [
        ({"merchant_id": "merchant-demo", "terminal_id": "unknown", "amount_krw": 1}, 404),
        ({"merchant_id": "another-merchant", "terminal_id": "galaxy-01", "amount_krw": 1}, 409),
        ({"merchant_id": "merchant-demo", "terminal_id": "galaxy-01", "amount_krw": True}, 422),
    ]:
        assert (await client.post("/api/checkouts", json=body)).status_code == code
    assert (await client.post("/api/nfc/tap", json=tap_body(terminal_id="unknown"))).status_code == 404
    with app.state.database.transaction() as session:
        session.get(Terminal, "galaxy-01").enabled = False
    assert (await client.post("/api/nfc/tap", json=tap_body())).status_code == 404


async def test_unknown_and_disabled_card_do_not_claim_checkout(api):
    app, client = api
    created = await checkout(client)
    response = await client.post("/api/nfc/tap", json=tap_body(card_alias="unknown"))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "UNKNOWN_CARD"
    with app.state.database.transaction() as session:
        session.get(DemoCard, "demo-alice").enabled = False
    assert (await client.post("/api/nfc/tap", json=tap_body())).status_code == 404
    state = (await client.get(f'/api/checkouts/{created["checkout_id"]}')).json()
    assert state["status"] == "WAITING_FOR_CARD" and state["payment"] is None


async def test_no_active_checkout(api):
    _, client = api
    response = await client.post("/api/nfc/tap", json=tap_body())
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "NO_ACTIVE_CHECKOUT"
    assert (await client.get("/api/ops/payments")).json()["total"] == 0


async def test_authorization_success_and_duplicate_taps(api):
    app, client = api
    created = await checkout(client)
    body = tap_body(created["checkout_id"])
    first = await client.post("/api/nfc/tap", json=body)
    assert first.status_code == 200, first.text
    result = first.json()
    payment = result["payment"]
    assert result["checkout"]["status"] == "AUTHORIZED"
    assert payment["idempotency_key"] == created["checkout_id"]
    assert payment["authorization_status"] == "AUTHORIZED"
    assert payment["asset_reservation_status"] == payment["prefunding_status"] == "RESERVED"
    assert payment["merchant_payout_status"] == "COMPLETED"
    assert payment["timestamps"]["asset_reserved_at"] <= payment["timestamps"]["merchant_paid_at"] <= payment["timestamps"]["authorized_at"]
    assert payment["source"] == "simulated" and payment["amount_krw"] == 4500
    repeated = (await client.post("/api/nfc/tap", json=body)).json()
    assert repeated["duplicate"] and repeated["payment"]["payment_id"] == payment["payment_id"]
    different_event = (await client.post("/api/nfc/tap", json=body | {"client_event_id": str(uuid4())})).json()
    assert different_event["duplicate"] and different_event["payment"]["payment_id"] == payment["payment_id"]
    conflict = await client.post("/api/nfc/tap", json=body | {"card_alias": "demo-declined"})
    assert conflict.status_code == 409
    assert (await client.get("/api/ops/payments")).json()["total"] == 1
    with app.state.database.transaction(write=False) as session:
        assert session.scalar(select(func.count()).select_from(TapEvent)) == 2
        assert session.scalar(select(func.count()).select_from(Payment)) == 1
    # Consecutive transactions cannot be consumed by a delayed tap bound to the old checkout.
    next_checkout = await checkout(client)
    replay = (await client.post("/api/nfc/tap", json=body)).json()
    assert replay["checkout"]["checkout_id"] == created["checkout_id"]
    assert (await client.get(f'/api/checkouts/{next_checkout["checkout_id"]}')).json()["status"] == "WAITING_FOR_CARD"


async def test_authorization_decline(api):
    _, client = api
    created = await checkout(client)
    response = await client.post("/api/nfc/tap", json=tap_body(created["checkout_id"], card_alias="demo-declined"))
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["checkout"]["status"] == "DECLINED"
    assert result["payment"]["authorization_status"] == "DECLINED"
    assert result["payment"]["settlement_status"] == "NOT_STARTED"
    assert result["payment"]["reason_code"] == "LIMIT_EXCEEDED"
    assert result["payment"]["merchant_payout_status"] == "NOT_STARTED"
    await checkout(client)


async def test_cancellation_and_expiration(api):
    app, client = api
    first = await checkout(client)
    path = f'/api/checkouts/{first["checkout_id"]}/cancel'
    assert (await client.post(path)).json()["status"] == "CANCELLED"
    assert (await client.post(path)).json()["status"] == "CANCELLED"
    assert (await client.post("/api/nfc/tap", json=tap_body(first["checkout_id"]))).status_code == 409
    second = await checkout(client)
    with app.state.database.transaction() as session:
        session.get(Checkout, second["checkout_id"]).expires_at = utcnow() - timedelta(seconds=1)
    response = await client.post("/api/nfc/tap", json=tap_body(second["checkout_id"]))
    assert response.status_code == 410
    assert (await client.get(f'/api/checkouts/{second["checkout_id"]}')).json()["status"] == "EXPIRED"
    assert (await client.get("/api/terminals/galaxy-01/active-checkout")).json()["checkout"] is None
    await checkout(client)


class CountingAdapter:
    source = "simulated"
    def __init__(self, *, timeout_after_accept=False):
        self.calls = 0
        self.timeout_after_accept = timeout_after_accept
    def attach(self, database):
        self.delegate = MockPaymentAdapter(database, stage_seconds=100)
    async def authorize(self, command):
        self.calls += 1
        await asyncio.sleep(0.05)
        result = await self.delegate.authorize(command)
        if self.timeout_after_accept:
            raise httpx.ReadTimeout("Engine accepted; response lost")
        return result
    async def get_payment(self, checkout_id):
        return await self.delegate.get_payment(checkout_id)


async def test_simultaneous_taps_authorize_once(tmp_path):
    adapter = CountingAdapter()
    async with running_app(tmp_path / "race.sqlite3", adapter=adapter) as (app, client):
        created = await checkout(client)
        body = tap_body(created["checkout_id"])
        responses = await asyncio.gather(*[
            client.post("/api/nfc/tap", json=body if i % 2 else body | {"client_event_id": str(uuid4())})
            for i in range(12)
        ])
        assert all(response.status_code in (200, 202) for response in responses), [r.text for r in responses]
        assert len({r.json()["payment"]["payment_id"] for r in responses}) == 1
        assert adapter.calls == 1
        with app.state.database.transaction(write=False) as session:
            assert session.scalar(select(func.count()).select_from(Payment)) == 1


async def test_concurrent_checkout_creation(api):
    _, client = api
    body = {"merchant_id": "merchant-demo", "terminal_id": "galaxy-01", "amount_krw": 1}
    results = await asyncio.gather(*[client.post("/api/checkouts", json=body) for _ in range(8)])
    assert sorted(r.status_code for r in results) == [201] + [409] * 7


async def test_two_application_instances_share_database_lock(tmp_path):
    path = tmp_path / "shared.sqlite3"
    adapter = CountingAdapter()
    async with running_app(path, adapter=adapter) as (_, first):
        created = await checkout(first)
        second_adapter = CountingAdapter()
        async with running_app(path, adapter=second_adapter) as (_, second):
            results = await asyncio.gather(
                first.post("/api/nfc/tap", json=tap_body(created["checkout_id"])),
                second.post("/api/nfc/tap", json=tap_body(created["checkout_id"])),
            )
            assert all(r.status_code in (200, 202) for r in results)
            assert len({r.json()["payment"]["payment_id"] for r in results}) == 1
            assert adapter.calls + second_adapter.calls == 1
            assert (await first.get("/api/ops/payments")).json()["total"] == 1


async def test_timeout_is_uncertain_then_reconciled_not_declined(tmp_path):
    adapter = CountingAdapter(timeout_after_accept=True)
    async with running_app(tmp_path / "timeout.sqlite3", adapter=adapter) as (_, client):
        created = await checkout(client)
        response = await client.post("/api/nfc/tap", json=tap_body(created["checkout_id"]))
        assert response.status_code == 202
        result = response.json()
        assert result["checkout"]["status"] == "AUTHORIZING"
        assert result["payment"]["last_error"] == "ReadTimeout"
        assert (await client.post(f'/api/checkouts/{created["checkout_id"]}/cancel')).status_code == 409
        final = await wait_for_payment(client, result["payment"]["payment_id"], lambda p: p["authorization_status"] == "AUTHORIZED")
        assert final["last_error"] is None and adapter.calls == 1


@pytest.mark.parametrize("alias,expected,failed_field", [
    ("demo-alice", "COMPLETED", None),
    ("demo-liquidation-fail", "RECOVERY_REQUIRED", "liquidation_status"),
    ("demo-reimbursement-fail", "RECOVERY_REQUIRED", "reimbursement_status"),
])
async def test_background_settlement_and_recovery(tmp_path, alias, expected, failed_field):
    async with running_app(tmp_path / "settle.sqlite3", stage_seconds=0.03) as (_, client):
        created = await checkout(client)
        response = await client.post("/api/nfc/tap", json=tap_body(created["checkout_id"], card_alias=alias))
        assert response.status_code == 200, response.text
        final = await wait_for_payment(client, response.json()["payment"]["payment_id"], lambda p: p["settlement_status"] == expected)
        assert final["authorization_status"] == "AUTHORIZED"
        assert final["prefunding_status"] == "RESERVED"
        assert final["merchant_payout_status"] == "COMPLETED"
        if failed_field:
            assert final[failed_field] == "FAILED"
            assert final["asset_reservation_status"] == "RESERVED"
            assert final["settled_at"] is None
        else:
            assert final["asset_reservation_status"] == "RELEASED"
            assert final["settlement_reference"].startswith("mock-receipt-")
            assert final["settled_at"] and final["timestamps"]["reimbursed_at"]
            assert final["timestamps"]["treasury_received_at"] <= final["timestamps"]["reimbursed_at"]
        state = (await client.get(f'/api/checkouts/{created["checkout_id"]}')).json()
        assert state["status"] == "AUTHORIZED"
        filtered = await client.get(f"/api/ops/payments?settlement_status={expected}")
        assert filtered.json()["total"] == 1


async def test_restart_recovers_claim_before_engine_submission(tmp_path):
    path = tmp_path / "restart.sqlite3"
    async with running_app(path) as (app, client):
        created = await checkout(client)
        payment_id, _ = await asyncio.to_thread(app.state.service.claim_tap, TapRequest(**tap_body(created["checkout_id"])))
    # Simulate the crashed owner's expired lease after shutdown, so the first
    # worker cannot submit the engine request during graceful shutdown.
    offline = Database(path)
    with offline.transaction() as session:
        session.get(Payment, payment_id).lease_until = utcnow() - timedelta(seconds=1)
    offline.close()
    async with running_app(path, stage_seconds=0.03) as (_, client):
        final = await wait_for_payment(client, payment_id, lambda p: p["settlement_status"] == "COMPLETED")
        assert final["authorization_status"] == "AUTHORIZED"
        assert final["idempotency_key"] == created["checkout_id"]


async def test_restart_continues_already_authorized_settlement(tmp_path):
    path = tmp_path / "restart-settle.sqlite3"
    async with running_app(path, stage_seconds=0.05) as (_, client):
        created = await checkout(client)
        result = (await client.post("/api/nfc/tap", json=tap_body(created["checkout_id"]))).json()
        payment_id = result["payment"]["payment_id"]
    async with running_app(path, stage_seconds=0.05) as (_, client):
        final = await wait_for_payment(client, payment_id, lambda p: p["settlement_status"] == "COMPLETED")
        assert final["authorization_status"] == "AUTHORIZED"


async def test_sse_replay_latest_snapshot_and_checkout_isolation(api):
    _, client = api
    first = await checkout(client)
    second = await checkout(client, "galaxy-02")
    path = f'/api/checkouts/{first["checkout_id"]}/events'
    initial = await client.get(path + "?follow=false")
    assert initial.headers["content-type"].startswith("text/event-stream")
    snapshots = parse_sse(initial.text)
    assert [event[0] for event in snapshots] == ["snapshot"]
    old_cursor = snapshots[0][1]
    await client.post("/api/nfc/tap", json=tap_body(first["checkout_id"]))
    replay = await client.get(path + "?follow=false&after=0", headers={"Last-Event-ID": str(old_cursor)})
    events = parse_sse(replay.text)
    assert events[-1][0] == "snapshot"
    assert events[-1][2]["checkout"]["status"] == "AUTHORIZED"
    replayed = events[:-1]
    assert all(event[1] > old_cursor for event in replayed)
    assert all(event[2]["checkout_id"] == first["checkout_id"] for event in replayed)
    assert "payment.authorization" in {event[0] for event in replayed}
    assert "reservation.status_changed" in {event[0] for event in replayed}
    all_events = parse_sse((await client.get("/api/ops/events?after=0&follow=false")).text)
    assert any(event[2].get("checkout_id") == second["checkout_id"] for event in all_events)
    assert (await client.get(path, headers={"Last-Event-ID": "bogus"})).status_code == 422
    future = parse_sse((await client.get(path + "?after=999999&follow=false")).text)
    assert future[0][0] == "resync" and future[-1][0] == "snapshot"
    assert (await client.get(f"/api/checkouts/{uuid4()}/events?follow=false")).status_code == 404


async def test_sse_follows_new_durable_events_and_disconnects(api):
    app, client = api
    created = await checkout(client)
    class Connected:
        async def is_disconnected(self):
            return False
    snapshot = app.state.service.get_checkout(created["checkout_id"])
    snapshot["checkout"] = snapshot["checkout"].model_dump(mode="json")
    stream = stream_events(Connected(), app.state.service, snapshot, created["checkout_id"], None, True)
    assert "event: snapshot" in await anext(stream)
    await client.post("/api/nfc/tap", json=tap_body(created["checkout_id"]))
    assert "event: checkout.status_changed" in await asyncio.wait_for(anext(stream), 2)
    await stream.aclose()
    class Disconnected:
        async def is_disconnected(self):
            return True
    stream = stream_events(Disconnected(), app.state.service, snapshot, created["checkout_id"], None, True)
    await anext(stream)
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


async def test_protected_reset_preserves_cursor_and_demo_registration(api):
    _, client = api
    created = await checkout(client)
    await client.post("/api/nfc/tap", json=tap_body(created["checkout_id"]))
    before = (await client.get("/api/ops/payments")).json()["latest_event_id"]
    assert (await client.post("/api/demo/reset")).status_code == 403
    assert (await client.post("/api/demo/reset", headers={"X-Demo-Reset-Token": "wrong"})).status_code == 403
    reset = await client.post("/api/demo/reset", headers={"X-Demo-Reset-Token": "test-reset"})
    assert reset.status_code == 200, reset.text
    assert reset.json()["latest_event_id"] > before
    assert (await client.get("/api/ops/payments")).json()["total"] == 0
    assert (await client.get(f'/api/checkouts/{created["checkout_id"]}')).status_code == 404
    await checkout(client)


async def test_reset_disabled_without_secret(tmp_path):
    async with running_app(tmp_path / "no-reset.sqlite3", reset_token=None) as (_, client):
        assert (await client.post("/api/demo/reset")).status_code == 503


async def test_database_constraints_protect_live_checkout_and_payment(api):
    app, client = api
    created = await checkout(client)
    with pytest.raises(IntegrityError):
        with app.state.database.transaction() as session:
            row = session.get(Checkout, created["checkout_id"])
            data = {column.name: getattr(row, column.name) for column in Checkout.__table__.columns}
            data.update(checkout_id=str(uuid4()), creation_key=str(uuid4()))
            session.add(Checkout(**data))
            session.flush()
    result = (await client.post("/api/nfc/tap", json=tap_body(created["checkout_id"]))).json()
    with pytest.raises(IntegrityError):
        with app.state.database.transaction() as session:
            row = session.get(Payment, result["payment"]["payment_id"])
            data = {column.name: getattr(row, column.name) for column in Payment.__table__.columns}
            data.update(payment_id=str(uuid4()), engine_payment_id=None)
            session.add(Payment(**data))
            session.flush()


async def test_engine_replay_does_not_regress_or_reverse_authorization(api):
    app, client = api
    created = await checkout(client)
    result = (await client.post("/api/nfc/tap", json=tap_body(created["checkout_id"]))).json()
    payment_id = result["payment"]["payment_id"]
    engine = await app.state.adapter.get_payment(UUID(created["checkout_id"]))
    before = app.state.service.payments()["latest_event_id"]
    app.state.service.apply_engine(engine)
    assert app.state.service.payments()["latest_event_id"] == before
    for mutation in ({"amount_krw": 1}, {"source": "external"}, {"checkout_id": str(uuid4())}):
        invalid = EnginePayment.model_validate(engine.model_dump(mode="json") | mutation | {"version": engine.version + 1})
        with pytest.raises(ValueError):
            app.state.service.apply_engine(invalid)
    declined = EnginePayment.model_validate(engine.model_dump(mode="json") | {
        "version": engine.version + 1, "authorization_status": "DECLINED", "settlement_status": "NOT_STARTED",
        "liquidation_status": "NOT_STARTED", "reimbursement_status": "NOT_STARTED",
    })
    with pytest.raises(ValueError, match="reverse"):
        app.state.service.apply_engine(declined)
    assert app.state.service.payment(payment_id).authorization_status == "AUTHORIZED"


async def test_mock_contract_rejects_changed_key_and_unsupported_assets(api):
    app, client = api
    created = await checkout(client)
    await client.post("/api/nfc/tap", json=tap_body(created["checkout_id"]))
    with app.state.database.transaction(write=False) as session:
        payment = session.scalar(select(Payment))
        command = app.state.service.command(session, payment)
    with pytest.raises(ValueError, match="different input"):
        await app.state.adapter.authorize(command.model_copy(update={"amount_krw": 1}))
    original = (await app.state.adapter.get_payment(command.checkout_id)).model_dump(mode="json")
    with pytest.raises(ValueError):
        EnginePayment.model_validate(original | {"assets": ["STOCK"]})
    with pytest.raises(ValueError):
        EnginePayment.model_validate(original | {"prefunding_status": "FAILED"})


async def test_pending_authorization_cannot_expire_or_be_reset(api):
    app, client = api
    created = await checkout(client)
    payment_id, _ = await asyncio.to_thread(app.state.service.claim_tap, TapRequest(**tap_body(created["checkout_id"])))
    with app.state.database.transaction() as session:
        session.get(Checkout, created["checkout_id"]).expires_at = utcnow() - timedelta(seconds=10)
    state = (await client.get(f'/api/checkouts/{created["checkout_id"]}')).json()
    assert state["status"] == "AUTHORIZING" and state["payment_id"] == payment_id
    reset = await client.post("/api/demo/reset", headers={"X-Demo-Reset-Token": "test-reset"})
    assert reset.status_code == 409
    assert (await client.post(f'/api/checkouts/{created["checkout_id"]}/cancel')).status_code == 409


async def test_financial_command_injection_is_rejected_by_http(api):
    _, client = api
    created = await checkout(client)
    for injected in ({"amount_krw": 1}, {"authorization_status": "AUTHORIZED"}, {"customer_id": "another"}):
        response = await client.post("/api/nfc/tap", json=tap_body(created["checkout_id"], **injected))
        assert response.status_code == 422
    assert (await client.get("/api/ops/payments")).json()["total"] == 0
    mismatch = await client.post("/api/nfc/tap", json=tap_body(created["checkout_id"], terminal_id="galaxy-02"))
    assert mismatch.status_code == 404
    assert (await client.get(f'/api/ops/payments/{uuid4()}')).status_code == 404


async def test_live_checkout_stream_receives_global_reset(api):
    app, client = api
    created = await checkout(client)
    snapshot = app.state.service.get_checkout(created["checkout_id"])
    old_cursor = snapshot["latest_event_id"]
    await client.post("/api/demo/reset", headers={"X-Demo-Reset-Token": "test-reset"})
    events = app.state.service.events(old_cursor, created["checkout_id"])
    assert [event.event_type for event in events] == ["demo.reset"]


async def test_financial_progress_cannot_regress_on_newer_version(api):
    app, client = api
    created = await checkout(client)
    await client.post("/api/nfc/tap", json=tap_body(created["checkout_id"]))
    original = (await app.state.adapter.get_payment(UUID(created["checkout_id"]))).model_dump(mode="json")
    processing = EnginePayment.model_validate(original | {"version": original["version"] + 1, "liquidation_status": "PROCESSING"})
    app.state.service.apply_engine(processing)
    stale_progress = EnginePayment.model_validate(original | {"version": original["version"] + 2})
    with pytest.raises(ValueError, match="regress"):
        app.state.service.apply_engine(stale_progress)
    changed_identity = EnginePayment.model_validate(original | {"engine_payment_id": "different", "version": original["version"] + 3})
    with pytest.raises(ValueError, match="identity"):
        app.state.service.apply_engine(changed_identity)


async def test_database_connection_pragmas_and_foreign_keys(api):
    app, _ = api
    from sqlalchemy import text
    with app.state.database.transaction(write=False) as session:
        assert session.scalar(text("PRAGMA foreign_keys")) == 1
        assert session.scalar(text("PRAGMA journal_mode")) == "wal"
    with pytest.raises(IntegrityError):
        with app.state.database.transaction() as session:
            session.add(DemoCard(card_alias="orphan", customer_id="missing", enabled=True))
            session.flush()


@pytest.mark.parametrize("payout", [None, "NOT_STARTED", "PENDING", "FAILED"])
async def test_new_approval_requires_explicit_completed_merchant_payout(tmp_path, payout):
    class IncompletePayoutAdapter(CountingAdapter):
        async def authorize(self, command):
            result = await super().authorize(command)
            # The orchestration boundary must revalidate even an unsafe adapter
            # model. None also represents a legacy response without this field.
            return result.model_copy(update={"merchant_payout_status": MerchantPayoutStatus(payout) if payout else None})
        async def get_payment(self, checkout_id):
            result = await super().get_payment(checkout_id)
            return result.model_copy(update={"merchant_payout_status": MerchantPayoutStatus(payout) if payout else None})

    async with running_app(tmp_path / "payout-gate.sqlite3", adapter=IncompletePayoutAdapter()) as (_, client):
        created = await checkout(client)
        response = await client.post("/api/nfc/tap", json=tap_body(created["checkout_id"]))
        assert response.status_code == 202, response.text
        state = response.json()
        assert state["checkout"]["status"] == "AUTHORIZING"
        assert state["payment"]["authorization_status"] == "PENDING"
        assert state["payment"]["authorized_at"] is None
        events = parse_sse((await client.get("/api/ops/events?after=0&follow=false")).text)
        assert not any(event[0] == "payment.authorization" for event in events)


async def test_payout_pending_is_distinct_from_reserved_prefunding(api):
    app, client = api
    created = await checkout(client)
    payment_id, command = await asyncio.to_thread(app.state.service.claim_tap, TapRequest(**tap_body(created["checkout_id"])))
    complete = (await app.state.adapter.authorize(command)).model_dump(mode="json")
    pending = EnginePayment.model_validate(complete | {
        "authorization_status": "PENDING", "merchant_payout_status": "PENDING",
        "liquidation_status": "NOT_STARTED", "settlement_status": "NOT_STARTED",
        "timestamps": {key: value for key, value in complete["timestamps"].items()
                       if key not in ("authorized_at", "merchant_paid_at")},
    })
    app.state.service.apply_engine(pending)
    state = (await client.get(f'/api/checkouts/{created["checkout_id"]}')).json()
    assert state["status"] == "AUTHORIZING"
    assert state["payment"]["prefunding_status"] == "RESERVED"
    assert state["payment"]["merchant_payout_status"] == "PENDING"
    app.state.service.apply_engine(EnginePayment.model_validate(complete | {"version": complete["version"] + 1}))
    approved = app.state.service.payment(payment_id)
    assert approved.authorization_status == "AUTHORIZED"
    assert approved.merchant_payout_status == "COMPLETED"
    with pytest.raises(ValueError, match="merchant payout"):
        app.state.service.apply_engine(EnginePayment.model_validate(complete | {
            "version": complete["version"] + 2, "merchant_payout_status": None,
        }))


async def test_failed_mock_payout_declines_and_explicitly_releases_wallet(api):
    app, client = api
    created = await checkout(client)
    body = tap_body(created["checkout_id"], card_alias="demo-payout-fail")
    response = await client.post("/api/nfc/tap", json=body)
    assert response.status_code == 200, response.text
    state = response.json()
    assert state["checkout"]["status"] == "DECLINED"
    payment = state["payment"]
    assert payment["merchant_payout_status"] == "FAILED"
    assert payment["prefunding_status"] == "RESERVED"
    assert payment["asset_reservation_status"] == "RELEASED"
    assert payment["hold_status"] == "RELEASED"
    assert payment["reason_code"] == "DEMO_MERCHANT_PAYOUT_FAILED"
    assert payment["liquidation_status"] == "NOT_STARTED"
    assert payment["settlement_status"] == "NOT_STARTED"
    assert "merchant_paid_at" not in payment["timestamps"]
    assert (await client.post("/api/nfc/tap", json=body)).json()["payment"] == payment
    engine = await app.state.adapter.get_payment(UUID(created["checkout_id"]))
    assert engine.liquidation_status == "NOT_STARTED"


async def test_duplicate_taps_reuse_hold_payout_and_liquidation_fixture(tmp_path):
    async with running_app(tmp_path / "effects.sqlite3", stage_seconds=0.03) as (app, client):
        created = await checkout(client)
        body = tap_body(created["checkout_id"])
        replies = await asyncio.gather(*[
            client.post("/api/nfc/tap", json=body | {"client_event_id": str(uuid4())}) for _ in range(6)
        ])
        assert all(reply.status_code in (200, 202) for reply in replies)
        payment_id = replies[0].json()["payment"]["payment_id"]
        complete = await wait_for_payment(client, payment_id, lambda p: p["settlement_status"] == "COMPLETED")
        replay = (await client.post("/api/nfc/tap", json=body)).json()["payment"]
        assert replay["payment_id"] == payment_id
        assert replay["timestamps"] == complete["timestamps"]
        assert replay["settlement_reference"] == complete["settlement_reference"]
        with app.state.database.transaction(write=False) as session:
            assert session.scalar(select(func.count()).select_from(MockEngineRecord)) == 1
            assert session.scalar(select(func.count()).select_from(Event).where(
                Event.event_type == "merchant_payout.status_changed"
            )) == 1
        record = await app.state.adapter.get_payment(UUID(created["checkout_id"]))
        assert record.merchant_payout_status == "COMPLETED"
        assert record.timestamps["merchant_paid_at"] <= record.timestamps["wallet_liquidation_requested_at"]


async def test_legacy_authorized_projection_remains_readable_without_inferred_payout(api):
    app, client = api
    created = await checkout(client)
    result = (await client.post("/api/nfc/tap", json=tap_body(created["checkout_id"]))).json()
    payment_id = result["payment"]["payment_id"]
    with app.state.database.transaction() as session:
        payment = session.get(Payment, payment_id)
        legacy = dict(payment.engine_detail)
        legacy.pop("merchant_payout_status")
        payment.engine_detail = legacy
        fixture = session.get(MockEngineRecord, created["checkout_id"])
        initial = dict(fixture.initial_result["projection"])
        initial.pop("merchant_payout_status")
        for field in MockProjection.model_fields:
            initial.pop(field, None)
        initial.pop("mock_events", None)
        fixture.initial_result = initial
    state = (await client.get(f'/api/checkouts/{created["checkout_id"]}')).json()
    assert state["status"] == "AUTHORIZED"
    assert state["payment"]["merchant_payout_status"] is None
    legacy = await app.state.adapter.get_payment(UUID(created["checkout_id"]))
    assert legacy.merchant_payout_status is None
    app.state.service.apply_engine(EnginePayment.model_validate(legacy.model_dump(mode="json") | {
        "version": legacy.version + 1, "liquidation_status": "PROCESSING",
    }))
    assert app.state.service.payment(payment_id).merchant_payout_status is None
