"""Deterministic, durable mock responses. No asset selection or funds are executed."""
import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import UUID

from app.adapters.base import EnginePaymentNotFound
from app.db import Database
from app.models import AuthorizationRequest, EnginePayment
from app.tables import MockEngineRecord


def fingerprint(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class MockPaymentAdapter:
    """Scripted engine orchestration, including its internal wallet operations.

    A single durable checkout-keyed fixture represents the simulated hold,
    payout and liquidation instruction. Replays only read that fixture.
    """
    source = "simulated"

    def __init__(self, database: Database, stage_seconds: float = 1):
        self.database = database
        self.stage_seconds = stage_seconds

    async def authorize(self, request: AuthorizationRequest) -> EnginePayment:
        return await asyncio.to_thread(self._authorize, request)

    def _authorize(self, request: AuthorizationRequest) -> EnginePayment:
        data = request.model_dump(mode="json")
        key = request.idempotency_key
        digest = fingerprint(data)
        with self.database.transaction() as session:
            record = session.get(MockEngineRecord, key)
            if record:
                if record.request_fingerprint != digest:
                    raise ValueError("Engine key reused with different input")
                return self._project(record)
            now = utcnow()
            scenario = request.customer_id.removeprefix("customer-")
            hold_failed = scenario == "decline"
            payout_failed = scenario == "payout_fail"
            declined = hold_failed or payout_failed
            timestamps = {"engine_received_at": now}
            if not hold_failed:
                timestamps.update(asset_reserved_at=now, prefunding_reserved_at=now)
            if payout_failed:
                timestamps["merchant_payout_failed_at"] = now
            elif not declined:
                # Ordered simulated engine conditions, with no real transfers:
                # wallet hold -> company capacity -> merchant payout -> approval.
                timestamps.update(merchant_paid_at=now, authorized_at=now)
            result = EnginePayment(
                payment_id=request.payment_id, checkout_id=request.checkout_id,
                engine_payment_id=f"mock-{key}", amount_krw=request.amount_krw,
                authorization_status="DECLINED" if declined else "AUTHORIZED",
                asset_reservation_status="FAILED" if hold_failed else "RESERVED",
                prefunding_status="NOT_STARTED" if hold_failed else "RESERVED",
                merchant_payout_status="NOT_STARTED" if hold_failed else "FAILED" if payout_failed else "COMPLETED",
                liquidation_status="NOT_STARTED" if declined else "PENDING",
                reimbursement_status="NOT_STARTED",
                settlement_status="NOT_STARTED" if declined else "PENDING",
                source="simulated", version=1, assets=[] if hold_failed else ["USDC"],
                reason_code="DEMO_SPENDING_LIMIT" if hold_failed else "DEMO_MERCHANT_PAYOUT_FAILED" if payout_failed else None,
                timestamps=timestamps,
            )
            session.add(MockEngineRecord(
                checkout_id=key, request_fingerprint=digest, request=data,
                initial_result=result.model_dump(mode="json"), scenario=scenario, created_at=now,
            ))
            return result

    async def get_payment(self, checkout_id: UUID) -> EnginePayment:
        return await asyncio.to_thread(self._get, str(checkout_id))

    def _get(self, key: str) -> EnginePayment:
        with self.database.transaction(write=False) as session:
            record = session.get(MockEngineRecord, key)
            if record is None:
                raise EnginePaymentNotFound(key)
            return self._project(record)

    def _project(self, record: MockEngineRecord) -> EnginePayment:
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
