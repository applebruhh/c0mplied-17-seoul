import asyncio
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
import sqlite3
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.adapters.mock import MockPaymentAdapter, WalletBusyError, utcnow
from app.db import Database
from app.models import AuthorizationRequest, AuthorizationStatus
from app.tables import (Checkout, Customer, DemoCard, Event, Merchant, MockCompanyCapacity,
                        MockEngineEvent, MockEngineRecord, Payment, TapEvent, Terminal)
from conftest import running_app
from test_backend import checkout, tap_body, wait_for_payment

pytestmark = pytest.mark.anyio


class Clock:
    def __init__(self):
        self.value = utcnow()
    def __call__(self):
        return self.value
    def advance(self, seconds=1.01):
        self.value += timedelta(seconds=seconds)


@pytest.fixture
def engine(tmp_path):
    database = Database(tmp_path / "engine.sqlite3")
    database.initialize()
    clock = Clock()
    adapter = MockPaymentAdapter(database, clock=clock)
    yield adapter, clock, database
    database.close()


def command(scenario="success", amount=4500):
    return AuthorizationRequest(payment_id=uuid4(), checkout_id=uuid4(), merchant_id="merchant-demo",
                                terminal_id="galaxy-01", customer_id=f"customer-{scenario}", amount_krw=amount)


async def finish(adapter, clock, checkout_id):
    for _ in range(30):
        clock.advance()
        result = await adapter.get_payment(checkout_id)
        if result.recovery_status in ("REIMBURSED", "LOSS_ABSORBED"):
            return result
    raise AssertionError(result.model_dump(mode="json"))


async def test_prepaid_wallet_hold_snapshot_and_excess_usdc(engine):
    adapter, clock, db = engine
    request = command()
    result = await adapter.authorize(request)
    assert result.authorization_status == "AUTHORIZED" and result.required_krw == 4635
    assert result.hold_status == "LOCKED" and result.liquidation_status == "PENDING"
    assert not result.mock_asset_metadata["submissions"]
    assert [event.event_type for event in result.mock_events][:2] == ["WALLET_LOCKED", "HOLD_PLACED"]
    assert adapter.wallet_snapshot(request.customer_id)["lock_checkout_id"] == str(request.checkout_id)
    for operation in (adapter.withdraw, adapter.spend):
        with pytest.raises(WalletBusyError):
            operation(request.customer_id, "USDC", 1, str(uuid4()))
    adapter.deposit(request.customer_id, "USDC", 700, "incoming")
    assert adapter.wallet_snapshot(request.customer_id)["lock_checkout_id"] == str(request.checkout_id)
    final = await finish(adapter, clock, request.checkout_id)
    assert final.recovery_status == "REIMBURSED" and final.hold_status == "RELEASED"
    assert final.unpaid_exposure_krw == 0
    assert final.excess_returned_usdc == "0.09285714"
    snapshot = adapter.wallet_snapshot(request.customer_id)
    assert snapshot["holdings_krw"]["USDC"] == 20000 + 700 + 130
    assert snapshot["holdings_krw"]["BTC"] == 30000 - 4635
    assert snapshot["daily_sold_krw"]["BTC"] == 4635
    assert final.assets == ["BTC"]  # Stablecoins are not automatically sold first.
    with db.transaction(write=False) as session:
        assert session.get(MockCompanyCapacity, "demo").available_krw == 1_000_000
        assert session.scalar(select(func.count()).select_from(MockEngineEvent)) == len(final.mock_events)
    for event in final.mock_events:
        assert event.checkout_id == str(request.checkout_id) and event.payment_id == str(request.payment_id)
        assert event.customer_id == request.customer_id and event.wallet_id == final.wallet_id
        assert event.state and event.mock_asset_metadata and event.timestamp


@pytest.mark.parametrize("scenario,reason", [
    ("wallet_busy", "WALLET_BUSY"), ("decline", "LIMIT_EXCEEDED"),
    ("execution_fail", "EXECUTION_LIQUIDITY_UNAVAILABLE"), ("prefund_fail", "PREFUNDING_INSUFFICIENT"),
])
async def test_distinct_decline_reasons_and_lock_cleanup(engine, scenario, reason):
    adapter, _, _ = engine
    request = command(scenario)
    result = await adapter.authorize(request)
    assert result.authorization_status == "DECLINED" and result.reason_code == reason
    assert result.liquidation_status == "NOT_STARTED"
    snapshot = adapter.wallet_snapshot(request.customer_id)
    if scenario == "wallet_busy":
        assert snapshot["lock_checkout_id"] == "fixture-busy-owner"
        assert result.hold_status == "NOT_LOCKED"
    else:
        assert snapshot["lock_checkout_id"] is None and result.hold_status == "RELEASED"
    if scenario == "execution_fail":
        assert sum(snapshot["holdings_krw"].values()) >= result.required_krw
    if scenario == "prefund_fail":
        assert result.prefunding_status == "FAILED"


async def test_actual_mock_company_capacity_check(engine):
    adapter, _, db = engine
    with db.transaction() as session:
        session.get(MockCompanyCapacity, "demo").available_krw = 0
    result = await adapter.authorize(command())
    assert result.reason_code == "PREFUNDING_INSUFFICIENT"
    assert adapter.wallet_snapshot("customer-success")["lock_checkout_id"] is None


async def test_wallet_lock_is_atomic_across_independent_database_instances(engine):
    adapter, clock, db = engine
    other_db = Database(db.path)
    other = MockPaymentAdapter(other_db, clock=clock)
    first, second = command(), command()
    try:
        results = await asyncio.gather(adapter.authorize(first), other.authorize(second))
        assert sorted(r.authorization_status for r in results) == ["AUTHORIZED", "DECLINED"]
        assert [r.reason_code for r in results if r.authorization_status == "DECLINED"] == ["WALLET_BUSY"]
        winner = next(r for r in results if r.authorization_status == "AUTHORIZED")
        assert adapter.wallet_snapshot("customer-success")["lock_checkout_id"] == str(winner.checkout_id)
        with db.transaction(write=False) as session:
            assert session.scalar(select(func.count()).select_from(MockEngineEvent).where(MockEngineEvent.event_type == "HOLD_PLACED")) == 1
            assert session.get(MockCompanyCapacity, "demo").available_krw == 995500
    finally:
        other_db.close()


async def test_same_wallet_concurrent_pos_checkouts(api):
    app, client = api
    first = await checkout(client)
    second = await checkout(client, "galaxy-02")
    responses = await asyncio.gather(
        client.post("/api/nfc/tap", json=tap_body(first["checkout_id"])),
        client.post("/api/nfc/tap", json=tap_body(second["checkout_id"], terminal_id="galaxy-02")),
    )
    states = [response.json() for response in responses]
    assert sorted(state["checkout"]["status"] for state in states) == ["AUTHORIZED", "DECLINED"]
    assert next(state for state in states if state["checkout"]["status"] == "DECLINED")["payment"]["reason_code"] == "WALLET_BUSY"
    assert len({state["payment"]["payment_id"] for state in states}) == 2
    with app.state.database.transaction(write=False) as session:
        assert session.scalar(select(func.count()).select_from(MockEngineEvent).where(MockEngineEvent.event_type == "HOLD_PLACED")) == 1


async def test_daily_nonstable_cap_is_shared_by_consecutive_payments(engine):
    adapter, clock, _ = engine
    first = command(amount=8000)
    await adapter.authorize(first)
    await finish(adapter, clock, first.checkout_id)
    second = command()
    await adapter.authorize(second)
    final = await finish(adapter, clock, second.checkout_id)
    assert final.recovery_status == "REIMBURSED"
    snapshot = adapter.wallet_snapshot("customer-success")
    assert snapshot["daily_sold_krw"]["BTC"] == 9000  # 30% of initial 30,000.
    assert snapshot["daily_sold_krw"]["ETH"] == 3875
    assert final.assets == ["BTC", "ETH"]
    assert len(final.mock_asset_metadata["submissions"]) == 2


async def test_stablecoin_has_no_percentage_cap_and_usdt_is_supported(engine):
    adapter, clock, _ = engine
    request = command("usdt", amount=60000)
    result = await adapter.authorize(request)
    assert result.authorization_status == "AUTHORIZED"
    final = await finish(adapter, clock, request.checkout_id)
    assert final.recovery_status == "REIMBURSED" and final.assets == ["USDT"]
    assert adapter.wallet_snapshot(request.customer_id)["daily_sold_krw"]["USDT"] == 61800
    with pytest.raises(ValueError):
        adapter.deposit(request.customer_id, "STOCK", 100, "invalid")


@pytest.mark.parametrize("scenario,expected_attempts", [("slippage_fail", [1, 2, 3]), ("liquidation_fail", [1, 1, 2])])
async def test_retry_fallback_limits_and_loss_only_after_reconciliation(engine, scenario, expected_attempts):
    adapter, clock, db = engine
    request = command(scenario)
    await adapter.authorize(request)
    for _ in range(20):
        clock.advance()
        state = await adapter.get_payment(request.checkout_id)
        if state.liquidation_status == "FAILED":
            break
    assert state.authorization_status == "AUTHORIZED" and state.recovery_status == "PENDING"
    assert state.hold_status == "LOCKED" and state.unpaid_exposure_krw == 4500
    assert adapter.wallet_snapshot(request.customer_id)["lock_checkout_id"] == str(request.checkout_id)
    attempts = state.mock_asset_metadata["submissions"]
    assert [a["attempt"] for a in attempts] == expected_attempts
    assert len(attempts) == 3 and all(Decimal(a["max_slippage"]) <= Decimal("0.005") for a in attempts)
    final = await finish(adapter, clock, request.checkout_id)
    assert final.recovery_status == "LOSS_ABSORBED" and final.hold_status == "RELEASED"
    assert final.asset_reservation_status == "RELEASED" and final.authorization_status == "AUTHORIZED"
    assert final.unpaid_exposure_krw == final.loss_absorbed_krw == 4500
    assert final.settlement_status == "RECOVERY_REQUIRED"
    assert adapter.wallet_snapshot(request.customer_id)["lock_checkout_id"] is None
    assert adapter.wallet_snapshot(request.customer_id)["holdings_krw"] == state.mock_asset_metadata["spendable_snapshot_krw"]
    names = [event.event_type for event in final.mock_events]
    assert names.index("EXPOSURE_RECORDED") < names.index("LOSS_ABSORBED") < names.index("HOLD_RELEASED")
    with db.transaction(write=False) as session:
        assert session.get(MockCompanyCapacity, "demo").available_krw == 995500


async def test_alternate_asset_fixture_does_not_force_stablecoin_first(engine):
    adapter, clock, _ = engine
    request = command("alternate")
    await adapter.authorize(request)
    final = await finish(adapter, clock, request.checkout_id)
    assert final.recovery_status == "REIMBURSED"
    assert final.assets == ["BTC", "ETH"]
    assert [a["status"] for a in final.mock_asset_metadata["submissions"]] == ["FAILED", "CONFIRMED"]
    assert adapter.wallet_snapshot(request.customer_id)["daily_sold_krw"] == {"ETH": 4635}


async def test_hold_ttl_expiry_before_submission_records_loss_and_releases(engine):
    _, clock, db = engine
    adapter = MockPaymentAdapter(db, hold_ttl=60, clock=clock)
    request = command("hold_expiry")
    initial = await adapter.authorize(request)
    clock.advance(61)
    expired = await adapter.get_payment(request.checkout_id)
    assert (expired.hold_expires_at - initial.timestamps["engine_received_at"]).total_seconds() == 60
    assert expired.authorization_status == "AUTHORIZED" and expired.hold_status == "EXPIRED"
    assert expired.reason_code == "HOLD_EXPIRED" and expired.recovery_status == "LOSS_ABSORBED"
    assert expired.unpaid_exposure_krw == 4500 and expired.loss_absorbed_krw == 4500
    assert not expired.mock_asset_metadata["submissions"]
    assert adapter.wallet_snapshot(request.customer_id)["lock_checkout_id"] is None
    assert "HOLD_EXPIRED" in [event.event_type for event in expired.mock_events]


async def test_pending_unknown_swap_keeps_lock_past_ttl_until_resolved(engine):
    _, clock, db = engine
    adapter = MockPaymentAdapter(db, hold_ttl=2, unknown_seconds=10, clock=clock)
    request = command("unknown")
    await adapter.authorize(request)
    clock.advance()
    pending = await adapter.get_payment(request.checkout_id)
    assert pending.transaction_status == "PENDING"
    clock.advance()
    unknown = await adapter.get_payment(request.checkout_id)
    assert unknown.transaction_status == "UNKNOWN" and unknown.hold_status == "LOCKED"
    clock.advance(8)
    unresolved = await adapter.get_payment(request.checkout_id)
    assert unresolved.transaction_status == "UNKNOWN"
    assert unresolved.hold_status == "LOCKED" and unresolved.hold_expires_at < clock()
    adapter.deposit(request.customer_id, "USDC", 100, "during-unknown")
    with pytest.raises(WalletBusyError):
        adapter.withdraw(request.customer_id, "USDT", 1, "blocked")
    clock.advance(3)
    resolved = await adapter.get_payment(request.checkout_id)
    assert resolved.transaction_status == "CONFIRMED" and resolved.hold_status == "LOCKED"
    final = await finish(adapter, clock, request.checkout_id)
    assert final.recovery_status == "REIMBURSED" and final.hold_status == "RELEASED"
    assert final.loss_absorbed_krw == 0


async def test_authorization_unknown_does_not_release_before_merchant_outcome(engine):
    _, clock, db = engine
    adapter = MockPaymentAdapter(db, hold_ttl=2, unknown_seconds=10, clock=clock)
    request = command("authorization_unknown")
    result = await adapter.authorize(request)
    assert result.authorization_status == "UNKNOWN" and result.hold_status == "LOCKED"
    clock.advance(3)
    assert (await adapter.get_payment(request.checkout_id)).authorization_status == "UNKNOWN"
    clock.advance(8)
    approved = await adapter.get_payment(request.checkout_id)
    assert approved.authorization_status == "AUTHORIZED"
    final = await adapter.get_payment(request.checkout_id)
    assert final.hold_status == "EXPIRED" and final.loss_absorbed_krw == 4500


async def test_duplicate_authorize_and_repeated_settlement_are_side_effect_free(engine):
    adapter, clock, db = engine
    request = command()
    replies = await asyncio.gather(*[adapter.authorize(request) for _ in range(8)])
    assert len({r.version for r in replies}) == 1
    with pytest.raises(ValueError, match="different input"):
        await adapter.authorize(request.model_copy(update={"amount_krw": 4501}))
    final = await finish(adapter, clock, request.checkout_id)
    before = adapter.wallet_snapshot(request.customer_id)
    replay = await adapter.authorize(request)
    repeated = await adapter.get_payment(request.checkout_id)
    assert final == replay == repeated
    assert before == adapter.wallet_snapshot(request.customer_id)
    with db.transaction(write=False) as session:
        assert session.scalar(select(func.count()).select_from(MockEngineRecord)) == 1
        assert session.scalar(select(func.count()).select_from(MockEngineEvent)) == len(final.mock_events)
    second = command()
    await adapter.authorize(second)
    await adapter.get_payment(request.checkout_id)
    assert adapter.wallet_snapshot(request.customer_id)["lock_checkout_id"] == str(second.checkout_id)


async def test_mock_operation_keys_are_idempotent_and_deposits_do_not_unlock(engine):
    adapter, _, _ = engine
    first = adapter.deposit("customer-success", "USDC", 100, "deposit-1")
    assert first == adapter.deposit("customer-success", "USDC", 100, "deposit-1")
    with pytest.raises(ValueError):
        adapter.deposit("customer-success", "USDC", 101, "deposit-1")
    request = command()
    await adapter.authorize(request)
    result = adapter.deposit("customer-success", "ETH", 200, "deposit-2")
    assert result["lock_checkout_id"] == str(request.checkout_id)
    with pytest.raises(ValueError):
        adapter.deposit("customer-success", "ETH", -1, "negative")


async def test_restart_keeps_unknown_transaction_and_policy_snapshot(engine):
    _, clock, db = engine
    adapter = MockPaymentAdapter(db, hold_ttl=2, unknown_seconds=10, clock=clock)
    request = command("unknown")
    await adapter.authorize(request)
    clock.advance()
    await adapter.get_payment(request.checkout_id)
    clock.advance()
    original = await adapter.get_payment(request.checkout_id)
    restarted = MockPaymentAdapter(db, hold_ttl=999, unknown_seconds=1, clock=clock)
    clock.advance(5)
    state = await restarted.get_payment(request.checkout_id)
    assert state.transaction_status == "UNKNOWN" and state.hold_status == "LOCKED"
    assert state.hold_expires_at == original.hold_expires_at
    clock.advance(6)
    await restarted.get_payment(request.checkout_id)
    assert (await finish(restarted, clock, request.checkout_id)).recovery_status == "REIMBURSED"


async def test_pos_unknown_auth_and_reset_protection(tmp_path):
    async with running_app(tmp_path / "unknown.sqlite3", stage_seconds=0.02) as (app, client):
        created = await checkout(client)
        response = await client.post("/api/nfc/tap", json=tap_body(created["checkout_id"], card_alias="demo-authorization-unknown"))
        assert response.status_code == 202
        assert response.json()["payment"]["authorization_status"] == "UNKNOWN"
        assert response.json()["checkout"]["status"] == "AUTHORIZING"
        assert (await client.post("/api/demo/reset", headers={"X-Demo-Reset-Token": "test-reset"})).status_code == 409


async def test_pos_loss_reconciliation_and_mock_events_are_durable(tmp_path):
    async with running_app(tmp_path / "loss.sqlite3", stage_seconds=0.01) as (app, client):
        created = await checkout(client)
        response = await client.post("/api/nfc/tap", json=tap_body(created["checkout_id"], card_alias="demo-liquidation-fail"))
        payment_id = response.json()["payment"]["payment_id"]
        final = await wait_for_payment(client, payment_id, lambda p: p["recovery_status"] == "LOSS_ABSORBED")
        assert final["authorization_status"] == "AUTHORIZED" and final["hold_status"] == "RELEASED"
        events = app.state.service.events(0, created["checkout_id"])
        logged = [e for e in events if "mock_engine_event" in e.payload]
        assert "LOSS_ABSORBED" in [e.event_type for e in logged]
        assert all(e.payload["mock_engine_event"]["wallet_id"] == final["wallet_id"] for e in logged)
        count = app.state.service.payments()["latest_event_id"]
        app.state.service.apply_engine(await app.state.adapter.get_payment(UUID(created["checkout_id"])))
        assert app.state.service.payments()["latest_event_id"] == count


async def test_phase2_database_upgrade_preserves_payments_taps_and_events(tmp_path):
    path = tmp_path / "phase2.sqlite3"
    with sqlite3.connect(path) as raw:
        raw.executescript((Path(__file__).parent / "fixtures/phase2-schema.sql").read_text())
    db = Database(path)
    now, checkout_id, payment_id, event_id = utcnow(), str(uuid4()), str(uuid4()), str(uuid4())
    with db.sessions.begin() as session:
        session.add(Merchant(merchant_id="merchant-demo", display_name="Existing merchant"))
        session.add(Customer(customer_id="customer-success", display_name="Existing customer"))
        session.flush()
        session.add(Terminal(terminal_id="galaxy-01", merchant_id="merchant-demo", enabled=True))
        session.flush()
        session.add(Checkout(checkout_id=checkout_id, merchant_id="merchant-demo", terminal_id="galaxy-01",
                             amount_krw=4500, status="AUTHORIZING", creation_key=str(uuid4()),
                             creation_fingerprint="old", created_at=now, updated_at=now, expires_at=now+timedelta(minutes=5)))
        session.flush()
        session.add(Payment(payment_id=payment_id, checkout_id=checkout_id, idempotency_key=checkout_id,
                            customer_id="customer-success", authorization_status="PENDING",
                            asset_reservation_status="PENDING", prefunding_status="PENDING",
                            liquidation_status="NOT_STARTED", reimbursement_status="NOT_STARTED", settlement_status="NOT_STARTED",
                            source="simulated", lease_until=now, created_at=now, updated_at=now))
        session.flush()
        session.add(TapEvent(client_event_id=event_id, payload_fingerprint="old", checkout_id=checkout_id,
                             payment_id=payment_id, created_at=now))
        session.add(Event(event_type="checkout.created", checkout_id=checkout_id, payment_id=payment_id,
                          source="simulated", occurred_at=now, payload={"existing": True}))
    db.initialize()
    db.initialize()  # Upgrade is idempotent.
    with db.transaction() as session:
        payment = session.get(Payment, payment_id)
        assert payment.checkout_id == checkout_id
        payment.authorization_status = AuthorizationStatus.UNKNOWN
        assert session.get(TapEvent, event_id).payment_id == payment_id
        assert session.scalar(select(Event)).payload == {"existing": True}
        assert session.get(Merchant, "merchant-demo").display_name == "Existing merchant"
    with db.transaction(write=False) as session:
        assert session.get(Payment, payment_id).authorization_status == "UNKNOWN"
    db.close()


async def test_three_distinct_legs_share_one_global_submission_budget(engine):
    adapter, clock, _ = engine
    request = command(amount=25000)
    assert (await adapter.authorize(request)).authorization_status == "AUTHORIZED"
    final = await finish(adapter, clock, request.checkout_id)
    assert final.recovery_status == "REIMBURSED"
    assert [leg["asset"] for leg in final.mock_asset_metadata["submissions"]] == ["BTC", "ETH", "USDC"]
    assert len(final.mock_asset_metadata["submissions"]) == 3
    snapshot = adapter.wallet_snapshot(request.customer_id)
    assert snapshot["daily_sold_krw"]["BTC"] == 9000
    assert snapshot["daily_sold_krw"]["ETH"] == 12000


async def test_reimbursement_failure_absorbs_loss_after_known_failed_delivery(engine):
    adapter, clock, _ = engine
    request = command("reimbursement_fail")
    await adapter.authorize(request)
    final = await finish(adapter, clock, request.checkout_id)
    assert final.authorization_status == "AUTHORIZED" and final.reimbursement_status == "FAILED"
    assert final.recovery_status == "LOSS_ABSORBED" and final.hold_status == "RELEASED"
    assert final.unpaid_exposure_krw == final.loss_absorbed_krw == 4500
    assert "treasury_received_at" not in final.timestamps
    assert adapter.wallet_snapshot(request.customer_id)["holdings_krw"]["USDC"] == 20000 + 4630


async def test_demo_reset_refuses_unknown_swap_then_succeeds_after_reconciliation(tmp_path):
    from app.config import Settings
    from app.main import create_app
    import httpx
    app = create_app(settings=Settings(database_path=tmp_path / "reset-unknown.sqlite3",
                                      demo_reset_token="test-reset", mock_stage_seconds=0.01,
                                      mock_unknown_seconds=0.2, worker_interval=0.01))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            created = await checkout(client)
            result = (await client.post("/api/nfc/tap", json=tap_body(created["checkout_id"], card_alias="demo-unknown"))).json()
            payment_id = result["payment"]["payment_id"]
            await wait_for_payment(client, payment_id, lambda p: p["transaction_status"] == "UNKNOWN")
            response = await client.post("/api/demo/reset", headers={"X-Demo-Reset-Token": "test-reset"})
            assert response.status_code == 409 and response.json()["error"]["code"] == "TRANSACTION_UNRESOLVED"
            await wait_for_payment(client, payment_id, lambda p: p["recovery_status"] == "REIMBURSED")
            assert (await client.post("/api/demo/reset", headers={"X-Demo-Reset-Token": "test-reset"})).status_code == 200


async def test_configurable_demo_values_and_ceil_margin(engine):
    from app.config import Settings
    _, clock, db = engine
    config = Settings(mock_hold_ttl=60, mock_margin="0.05", mock_max_slippage="0.003")
    adapter = MockPaymentAdapter(db, hold_ttl=config.mock_hold_ttl, margin=config.mock_margin,
                                 max_slippage=config.mock_max_slippage, clock=clock)
    result = await adapter.authorize(command(amount=4501))
    assert result.required_krw == 4727
    assert (result.hold_expires_at - clock()).total_seconds() == 60
    with pytest.raises(ValueError):
        Settings(mock_max_slippage="0.006")
