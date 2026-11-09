"""Service-controlled financial fixtures only: no custody, blockchain or bank calls."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN
import hashlib
import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.adapters.base import EnginePaymentNotFound
from app.adapters.mock_fixtures import ASSETS, RANKINGS, STABLECOINS, USDC_KRW, routes
from app.db import Database
from app.models import EnginePayment, AuthorizationRequest, MockEvent
from app.tables import MockCompanyCapacity, MockEngineEvent, MockEngineRecord, MockWallet, MockWalletOperation


def fingerprint(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class WalletBusyError(ValueError):
    pass


class MockPaymentAdapter:
    """Durable aggregate engine simulation. Only get_payment advances settlement.

    All wallet/engine state changes use the same short SQLite write transaction.
    No financial optimization or external wallet/DEX implementation is present.
    """
    source = "simulated"

    def __init__(self, database: Database, stage_seconds: float = 1, *,
                 margin=Decimal("0.03"), max_slippage=Decimal("0.005"), hold_ttl=300,
                 unknown_seconds=5, clock=utcnow):
        self.database, self.stage_seconds, self.clock = database, stage_seconds, clock
        self.margin, self.max_slippage = Decimal(str(margin)), Decimal(str(max_slippage))
        self.hold_ttl, self.unknown_seconds = hold_ttl, unknown_seconds
        if not (0 <= self.margin <= 1 and 0 <= self.max_slippage <= Decimal("0.005")
                and hold_ttl > 0 and stage_seconds > 0 and unknown_seconds > 0):
            raise ValueError("Invalid mock policy parameters")

    def _wallet(self, session, customer_id):
        wallet = session.scalar(select(MockWallet).where(MockWallet.customer_id == customer_id))
        if wallet is None:
            raise ValueError("No prepaid demo Embedded Wallet")
        return wallet

    def _day(self, wallet, now):
        day = now.date().isoformat()
        if not wallet.lock_checkout_id and wallet.sell_day != day:
            wallet.sell_day, wallet.daily_basis, wallet.daily_sold = day, dict(wallet.holdings), {}

    def _capacity(self, wallet, asset):
        value = wallet.holdings.get(asset, 0)
        if asset in STABLECOINS:
            return value
        cap = int(Decimal(wallet.daily_basis.get(asset, 0)) * Decimal("0.30"))
        return max(0, min(value, cap - wallet.daily_sold.get(asset, 0)))

    async def authorize(self, request: AuthorizationRequest) -> EnginePayment:
        return await asyncio.to_thread(self._authorize, request)

    def _authorize(self, request):
        data, key = request.model_dump(mode="json", exclude_none=True), request.idempotency_key
        with self.database.transaction() as session:
            record = session.get(MockEngineRecord, key)
            if record:
                if record.request_fingerprint != fingerprint(data):
                    raise ValueError("Engine key reused with different input")
                return self._pack(session, record)
            now = self.clock()
            wallet = self._wallet(session, request.customer_id)
            self._day(wallet, now)
            scenario = request.customer_id.removeprefix("customer-")
            required = int((Decimal(request.amount_krw) * (1 + self.margin)).to_integral_value(rounding=ROUND_CEILING))
            projection = EnginePayment(
                payment_id=request.payment_id, checkout_id=request.checkout_id,
                engine_payment_id=f"mock-{key}", amount_krw=request.amount_krw,
                authorization_status="PENDING", asset_reservation_status="NOT_STARTED",
                prefunding_status="NOT_STARTED", merchant_payout_status="NOT_STARTED",
                liquidation_status="NOT_STARTED", reimbursement_status="NOT_STARTED",
                settlement_status="NOT_STARTED", source="simulated", version=1,
                wallet_id=wallet.wallet_id, hold_status="NOT_LOCKED", required_krw=required,
                transaction_status="NOT_SUBMITTED", timestamps={"engine_received_at": now},
            ).model_dump(mode="json")
            projection["version"] = 0
            runtime = {
                "phase": "ready", "next_at": (now + timedelta(seconds=self.stage_seconds)).isoformat(),
                "stage_seconds": self.stage_seconds, "unknown_seconds": self.unknown_seconds,
                "margin": str(self.margin), "max_slippage": str(self.max_slippage),
                "hold_ttl": self.hold_ttl, "remaining_input": required, "proceeds_krw": 0,
                "routes": routes(scenario), "route_index": 0, "retry_index": 0,
                "submissions": [], "spendable_snapshot": dict(wallet.holdings),
                "daily_basis": dict(wallet.daily_basis), "sell_day": wallet.sell_day,
            }
            record = MockEngineRecord(checkout_id=key, request_fingerprint=fingerprint(data), request=data,
                                      initial_result={"projection": projection, "runtime": runtime},
                                      scenario=scenario, created_at=now)
            session.add(record)
            if wallet.lock_checkout_id:
                self._decline(session, record, wallet, "WALLET_BUSY", now, owns_lock=False)
                return self._pack(session, record)
            wallet.lock_checkout_id = key
            wallet.hold_expires_at = now + timedelta(seconds=self.hold_ttl)
            projection.update(hold_status="LOCKED", asset_reservation_status="PENDING",
                              hold_expires_at=wallet.hold_expires_at.isoformat())
            self._emit(session, record, "WALLET_LOCKED", now)
            capacities = [self._capacity(wallet, asset) for asset in
                          dict.fromkeys(route["asset"] for route in runtime["routes"])]
            if sum(wallet.holdings.values()) < required:
                self._decline(session, record, wallet, "LIMIT_EXCEEDED", now)
            elif sum([value for value in capacities if value > 0][:3]) < required:
                self._decline(session, record, wallet, "EXECUTION_LIQUIDITY_UNAVAILABLE", now)
            else:
                company = session.get(MockCompanyCapacity, "demo")
                if scenario == "prefund_fail" or company.available_krw < request.amount_krw:
                    projection["prefunding_status"] = "FAILED"
                    self._decline(session, record, wallet, "PREFUNDING_INSUFFICIENT", now)
                else:
                    company.available_krw -= request.amount_krw
                    projection.update(asset_reservation_status="RESERVED", prefunding_status="RESERVED")
                    projection["timestamps"].update(asset_reserved_at=now.isoformat(), prefunding_reserved_at=now.isoformat())
                    self._emit(session, record, "HOLD_PLACED", now)
                    if scenario == "payout_fail":
                        company.available_krw += request.amount_krw
                        projection["merchant_payout_status"] = "FAILED"
                        self._decline(session, record, wallet, "DEMO_MERCHANT_PAYOUT_FAILED", now)
                    elif scenario == "authorization_unknown":
                        projection.update(authorization_status="UNKNOWN", merchant_payout_status="PENDING",
                                          recovery_status="PENDING")
                        runtime.update(phase="authorization_unknown",
                                       next_at=(now + timedelta(seconds=self.unknown_seconds)).isoformat())
                        self._emit(session, record, "AUTHORIZATION_UNKNOWN", now)
                    else:
                        self._approved(session, record, now)
                        if scenario == "hold_expiry":
                            runtime["next_at"] = (wallet.hold_expires_at + timedelta(seconds=self.stage_seconds)).isoformat()
            record.initial_result = deepcopy(record.initial_result)
            flag_modified(record, "initial_result")
            return self._pack(session, record)

    def _approved(self, session, record, now):
        state = record.initial_result["projection"]
        state.update(authorization_status="AUTHORIZED", merchant_payout_status="COMPLETED",
                     liquidation_status="PENDING", settlement_status="PENDING", recovery_status="PENDING",
                     unpaid_exposure_krw=state["amount_krw"])
        state["timestamps"].update(merchant_paid_at=now.isoformat(), authorized_at=now.isoformat())
        record.initial_result["runtime"]["phase"] = "ready"
        self._emit(session, record, "MERCHANT_PAYOUT_COMPLETED", now)
        self._emit(session, record, "AUTHORIZED", now)

    def _decline(self, session, record, wallet, reason, now, owns_lock=True):
        state = record.initial_result["projection"]
        state.update(authorization_status="DECLINED", reason_code=reason)
        record.initial_result["runtime"]["phase"] = "done"
        self._emit(session, record, "AUTHORIZATION_DECLINED", now)
        if owns_lock:
            self._release(session, record, wallet, now)

    def _release(self, session, record, wallet, now, expired=False):
        if wallet.lock_checkout_id != record.checkout_id:
            raise RuntimeError("Wallet lock owner mismatch")
        wallet.lock_checkout_id, wallet.hold_expires_at = None, None
        state = record.initial_result["projection"]
        state.update(hold_status="EXPIRED" if expired else "RELEASED", asset_reservation_status="RELEASED")
        state["timestamps"]["wallet_released_at"] = now.isoformat()
        self._emit(session, record, "HOLD_EXPIRED" if expired else "HOLD_RELEASED", now)

    def _emit(self, session, record, kind, now):
        state, runtime = record.initial_result["projection"], record.initial_result["runtime"]
        state["version"] += 1
        state["mock_asset_metadata"] = {
            "fixture_version": "v5-demo-1", "rankings": deepcopy(RANKINGS),
            "spendable_snapshot_krw": runtime["spendable_snapshot"],
            "daily_basis_krw": runtime["daily_basis"], "sell_day": runtime["sell_day"],
            "margin": runtime["margin"], "max_slippage": runtime["max_slippage"],
            "max_submissions": 3, "max_retries_per_swap": 2,
            "submissions": deepcopy(runtime["submissions"]),
        }
        session.add(MockEngineEvent(
            event_id=f"{record.checkout_id}:{state['version']:06d}",
            payment_id=record.request["payment_id"], checkout_id=record.checkout_id,
            customer_id=record.request["customer_id"], wallet_id=state["wallet_id"],
            event_type=kind, timestamp=now,
            state={key: deepcopy(state[key]) for key in (
                "authorization_status", "hold_status", "asset_reservation_status", "prefunding_status",
                "merchant_payout_status", "liquidation_status", "reimbursement_status", "recovery_status",
                "transaction_status", "unpaid_exposure_krw", "loss_absorbed_krw",
            )}, mock_asset_metadata=deepcopy(state["mock_asset_metadata"]),
            recovery_outcome=state["recovery_status"],
        ))

    def _pack(self, session, record):
        if "projection" not in record.initial_result:
            return self._legacy_project(record)
        result = deepcopy(record.initial_result["projection"])
        events = session.scalars(select(MockEngineEvent).where(
            MockEngineEvent.checkout_id == record.checkout_id
        ).order_by(MockEngineEvent.event_id)).all()
        result["mock_events"] = [MockEvent(
            event_id=e.event_id, payment_id=e.payment_id, checkout_id=e.checkout_id,
            customer_id=e.customer_id, wallet_id=e.wallet_id, event_type=e.event_type,
            timestamp=aware(e.timestamp), state=e.state, mock_asset_metadata=e.mock_asset_metadata,
            recovery_outcome=e.recovery_outcome,
        ).model_dump(mode="json") for e in events]
        return EnginePayment.model_validate(result)

    async def get_payment(self, checkout_id: UUID) -> EnginePayment:
        return await asyncio.to_thread(self._get, str(checkout_id))

    def _get(self, key):
        with self.database.transaction() as session:
            record = session.get(MockEngineRecord, key)
            if record is None:
                raise EnginePaymentNotFound(key)
            if "projection" in record.initial_result:
                payload = deepcopy(record.initial_result)
                record.initial_result = payload
                self._advance(session, record, self.clock())
                record.initial_result = deepcopy(payload)
                flag_modified(record, "initial_result")
            return self._pack(session, record)

    def _advance(self, session, record, now):
        state, runtime = record.initial_result["projection"], record.initial_result["runtime"]
        if runtime["phase"] == "done":
            return
        wallet = self._wallet(session, record.request["customer_id"])
        if (state["authorization_status"] == "AUTHORIZED" and not runtime["submissions"]
                and now >= datetime.fromisoformat(state["hold_expires_at"])):
            self._failed(session, record, "HOLD_EXPIRED", now)
            self._loss(session, record, wallet, now, expired=True)
            return
        if now < datetime.fromisoformat(runtime["next_at"]):
            return
        runtime["next_at"] = (now + timedelta(seconds=runtime["stage_seconds"])).isoformat()
        phase = runtime["phase"]
        if phase == "authorization_unknown":
            self._approved(session, record, now)
        elif phase in ("ready", "ready_swap"):
            if phase == "ready":
                state["liquidation_status"] = "PROCESSING"
                state["timestamps"].update(wallet_liquidation_requested_at=now.isoformat(), liquidation_started_at=now.isoformat())
                self._emit(session, record, "LIQUIDATION_STARTED", now)
            self._submit(session, record, wallet, now)
        elif phase in ("submitted", "unknown"):
            last = runtime["submissions"][-1]
            route = runtime["routes"][runtime["route_index"]]
            if phase == "submitted" and route["outcome"] == "UNKNOWN":
                state["transaction_status"], last["status"] = "UNKNOWN", "UNKNOWN"
                runtime.update(phase="unknown", next_at=(now + timedelta(seconds=runtime["unknown_seconds"])).isoformat())
                self._emit(session, record, "TRANSACTION_UNKNOWN", now)
                return
            failed = route["outcome"] == "FAILED" or Decimal(route["slippage"]) > Decimal(runtime["max_slippage"])
            if failed:
                state["transaction_status"], last["status"] = "FAILED", "FAILED"
                last["reason"] = "SLIPPAGE_EXCEEDED" if Decimal(route["slippage"]) > Decimal(runtime["max_slippage"]) else "MOCK_SWAP_FAILED"
                self._emit(session, record, "DEX_SWAP_FAILED", now)
                if runtime["retry_index"] < min(2, route["max_retries"]):
                    runtime["retry_index"] += 1
                else:
                    runtime["route_index"] += 1
                    runtime["retry_index"] = 0
                if len(runtime["submissions"]) >= 3 or runtime["route_index"] >= len(runtime["routes"]):
                    self._failed(session, record, last["reason"], now)
                else:
                    runtime["phase"] = "ready_swap"
            else:
                state["transaction_status"], last["status"] = "CONFIRMED", "CONFIRMED"
                last["reconciled_at"] = now.isoformat()
                values, sold = dict(wallet.holdings), dict(wallet.daily_sold)
                asset, value = last["asset"], last["input_krw"]
                values[asset] -= value
                sold[asset] = sold.get(asset, 0) + value
                wallet.holdings, wallet.daily_sold = values, sold
                runtime["remaining_input"] -= value
                runtime["proceeds_krw"] += int((Decimal(value) * (1 - Decimal(route["slippage"]))).to_integral_value(rounding=ROUND_DOWN))
                self._emit(session, record, "DEX_SWAP_CONFIRMED", now)
                runtime["route_index"] += 1
                runtime["retry_index"] = 0
                if runtime["remaining_input"] <= 0:
                    if runtime["proceeds_krw"] < state["amount_krw"]:
                        self._failed(session, record, "INSUFFICIENT_SWAP_PROCEEDS", now)
                    else:
                        state.update(liquidation_status="COMPLETED", reimbursement_status="PENDING")
                        state["timestamps"]["liquidation_completed_at"] = now.isoformat()
                        runtime["phase"] = "treasury"
                        self._emit(session, record, "LIQUIDATION_COMPLETED", now)
                elif len(runtime["submissions"]) >= 3:
                    self._failed(session, record, "SWAP_BUDGET_EXHAUSTED", now)
                else:
                    runtime["phase"] = "ready_swap"
        elif phase == "treasury":
            if record.scenario == "reimbursement_fail":
                state["reimbursement_status"] = "FAILED"
                self._failed(session, record, "DEMO_REIMBURSEMENT_FAILED", now, liquidation_failed=False)
            else:
                state["timestamps"]["treasury_received_at"] = now.isoformat()
                self._emit(session, record, "USDC_RECEIVED_BY_TREASURY", now)
                self._credit_usdc(wallet, max(0, runtime["proceeds_krw"] - state["amount_krw"]))
                excess = max(0, runtime["proceeds_krw"] - state["amount_krw"])
                state.update(reimbursement_status="COMPLETED", recovery_status="REIMBURSED",
                             settlement_status="COMPLETED", unpaid_exposure_krw=0,
                             excess_returned_usdc=self._usdc(excess),
                             settlement_reference=f"mock-receipt-{record.checkout_id}")
                state["timestamps"].update(reimbursed_at=now.isoformat(), settled_at=now.isoformat(), released_at=now.isoformat())
                session.get(MockCompanyCapacity, "demo").available_krw += state["amount_krw"]
                self._emit(session, record, "REIMBURSEMENT_COMPLETED", now)
                self._release(session, record, wallet, now)
                runtime["phase"] = "done"
        elif phase == "loss_reconcile":
            self._loss(session, record, wallet, now)

    def _submit(self, session, record, wallet, now):
        state, runtime = record.initial_result["projection"], record.initial_result["runtime"]
        while runtime["route_index"] < len(runtime["routes"]):
            route = runtime["routes"][runtime["route_index"]]
            value = min(runtime["remaining_input"], self._capacity(wallet, route["asset"]))
            if value > 0:
                break
            runtime["route_index"] += 1
            runtime["retry_index"] = 0
        else:
            self._failed(session, record, "EXECUTION_LIQUIDITY_UNAVAILABLE", now)
            return
        if len(runtime["submissions"]) >= 3:
            self._failed(session, record, "SWAP_BUDGET_EXHAUSTED", now)
            return
        if route["asset"] not in state["assets"]:
            state["assets"].append(route["asset"])
        self._emit(session, record, "ASSET_SELECTED", now)
        runtime["submissions"].append({
            "submission_id": f"{record.checkout_id}:{len(runtime['submissions']) + 1}",
            "asset": route["asset"], "route_id": route["route_id"],
            "attempt": runtime["retry_index"] + 1, "input_krw": value,
            "max_slippage": runtime["max_slippage"], "quoted_slippage": route["slippage"],
            "submitted_at": now.isoformat(), "status": "PENDING",
        })
        state["transaction_status"] = "PENDING"
        runtime["phase"] = "submitted"
        self._emit(session, record, "DEX_SWAP_SUBMITTED", now)

    def _failed(self, session, record, reason, now, liquidation_failed=True):
        state, runtime = record.initial_result["projection"], record.initial_result["runtime"]
        state.update(settlement_status="RECOVERY_REQUIRED", recovery_status="PENDING", reason_code=reason,
                     unpaid_exposure_krw=state["amount_krw"])
        if liquidation_failed:
            state["liquidation_status"] = "FAILED"
        state["timestamps"]["recovery_required_at"] = now.isoformat()
        runtime["phase"] = "loss_reconcile"
        self._emit(session, record, "LIQUIDATION_FAILED" if liquidation_failed else "REIMBURSEMENT_FAILED", now)
        self._emit(session, record, "EXPOSURE_RECORDED", now)

    def _loss(self, session, record, wallet, now, expired=False):
        state, runtime = record.initial_result["projection"], record.initial_result["runtime"]
        if state["transaction_status"] in ("PENDING", "UNKNOWN"):
            raise ValueError("Cannot absorb loss or unlock an unresolved transaction")
        # Every submitted result is now definitively confirmed/failed. This demo
        # explicitly writes off the company payment, never reverses authorization.
        self._credit_usdc(wallet, runtime["proceeds_krw"])
        state.update(recovery_status="LOSS_ABSORBED", loss_absorbed_krw=state["amount_krw"])
        state["timestamps"]["loss_absorbed_at"] = now.isoformat()
        self._emit(session, record, "LOSS_ABSORBED", now)
        self._release(session, record, wallet, now, expired=expired)
        runtime["phase"] = "done"

    @staticmethod
    def _usdc(value):
        return str((Decimal(value) / USDC_KRW).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN))

    @staticmethod
    def _credit_usdc(wallet, value):
        balances = dict(wallet.holdings)
        balances["USDC"] = balances.get("USDC", 0) + value
        wallet.holdings = balances

    def wallet_snapshot(self, customer_id):
        with self.database.transaction(write=False) as session:
            wallet = self._wallet(session, customer_id)
            return {"wallet_id": wallet.wallet_id, "holdings_krw": dict(wallet.holdings),
                    "lock_checkout_id": wallet.lock_checkout_id,
                    "daily_basis_krw": dict(wallet.daily_basis), "daily_sold_krw": dict(wallet.daily_sold)}

    def deposit(self, customer_id, asset, value_krw, operation_id):
        if type(value_krw) is not int or value_krw <= 0:
            raise ValueError("Positive integer mock deposit value required")
        return self._operation(customer_id, asset, value_krw, operation_id)

    def withdraw(self, customer_id, asset, value_krw, operation_id):
        if type(value_krw) is not int or value_krw <= 0:
            raise ValueError("Positive integer mock deposit value required")
        return self._operation(customer_id, asset, -value_krw, operation_id)

    spend = withdraw

    def _operation(self, customer_id, asset, delta, operation_id):
        if asset not in ASSETS or type(delta) is not int or delta == 0 or not operation_id:
            raise ValueError("Invalid mock asset/operation")
        digest = fingerprint({"customer_id": customer_id, "asset": asset, "delta_krw": delta})
        with self.database.transaction() as session:
            previous = session.get(MockWalletOperation, operation_id)
            if previous:
                if previous.fingerprint != digest:
                    raise ValueError("Operation key reused with different input")
                return previous.result
            wallet = self._wallet(session, customer_id)
            if delta < 0 and wallet.lock_checkout_id:
                raise WalletBusyError("WALLET_BUSY")
            balances = dict(wallet.holdings)
            if balances.get(asset, 0) + delta < 0:
                raise ValueError("Insufficient deposited mock balance")
            balances[asset] = balances.get(asset, 0) + delta
            wallet.holdings = balances
            result = {"wallet_id": wallet.wallet_id, "holdings_krw": balances,
                      "lock_checkout_id": wallet.lock_checkout_id}
            session.add(MockWalletOperation(operation_id=operation_id, wallet_id=wallet.wallet_id,
                                            fingerprint=digest, result=result))
            return result
    def _legacy_project(self, record: MockEngineRecord) -> EnginePayment:
        result = dict(record.initial_result)
        if result["authorization_status"] == "DECLINED":
            return EnginePayment.model_validate(result)
        timestamps = dict(result["timestamps"])
        started = aware(record.created_at)
        elapsed = (utcnow() - started).total_seconds()
        stage = min(3, int(elapsed / self.stage_seconds))
        if stage >= 1:
            result.update(version=2, liquidation_status="PROCESSING")
            timestamps["wallet_liquidation_requested_at"] = started + timedelta(seconds=self.stage_seconds)
            timestamps["liquidation_started_at"] = started + timedelta(seconds=self.stage_seconds)
        if stage >= 2:
            if record.scenario == "liquidation_fail":
                result.update(version=3, liquidation_status="FAILED", settlement_status="RECOVERY_REQUIRED",
                              reason_code="DEMO_LIQUIDATION_FAILED")
                timestamps["recovery_required_at"] = started + timedelta(seconds=2 * self.stage_seconds)
                result["timestamps"] = timestamps
                return EnginePayment.model_validate(result)
            result.update(version=3, liquidation_status="COMPLETED", reimbursement_status="PENDING")
            timestamps["liquidation_completed_at"] = started + timedelta(seconds=2 * self.stage_seconds)
            timestamps["treasury_received_at"] = timestamps["liquidation_completed_at"]
        if stage >= 3:
            if record.scenario == "reimbursement_fail":
                result.update(version=4, reimbursement_status="FAILED", settlement_status="RECOVERY_REQUIRED",
                              reason_code="DEMO_REIMBURSEMENT_FAILED")
                timestamps["recovery_required_at"] = started + timedelta(seconds=3 * self.stage_seconds)
            else:
                result.update(version=4, reimbursement_status="COMPLETED", settlement_status="COMPLETED",
                              asset_reservation_status="RELEASED", settlement_reference=f"mock-receipt-{record.checkout_id}")
                timestamps["reimbursed_at"] = started + timedelta(seconds=3 * self.stage_seconds)
                timestamps["released_at"] = timestamps["reimbursed_at"]
                timestamps["settled_at"] = timestamps["reimbursed_at"]
        result["timestamps"] = timestamps
        return EnginePayment.model_validate(result)
