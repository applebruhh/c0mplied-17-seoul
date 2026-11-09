import logging
import httpx
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, delete, func, or_, select

from app.adapters.mock import aware, fingerprint, utcnow
from app.db import Database, seed_mock_wallets
from app.models import (
    AuthorizationRequest, AuthorizationStatus as Auth, CheckoutCreate, CheckoutRead,
    CheckoutStatus as CS, EnginePayment, EventRead, MerchantPayoutStatus, MockProjection,
    PaymentRead, SettlementStatus as SS,
    TapRequest,
)
from app.tables import (Checkout, DemoCard, Event, MockCompanyCapacity, MockEngineEvent,
                        MockEngineRecord, MockWallet, MockWalletOperation, Payment, TapEvent, Terminal)

logger = logging.getLogger(__name__)
ACTIVE = (CS.WAITING_FOR_CARD, CS.AUTHORIZING)
FINANCIAL_FIELDS = (
    "authorization_status", "asset_reservation_status", "prefunding_status",
    "liquidation_status", "reimbursement_status", "settlement_status",
)


class APIError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


def cursor(session) -> int:
    return session.scalar(select(func.max(Event.event_id))) or 0


def payment_read(session, payment: Payment) -> PaymentRead:
    checkout = session.get(Checkout, payment.checkout_id)
    return PaymentRead(
        payment_id=payment.payment_id, checkout_id=payment.checkout_id,
        idempotency_key=payment.idempotency_key, merchant_id=checkout.merchant_id,
        terminal_id=checkout.terminal_id, customer_id=payment.customer_id,
        amount_krw=checkout.amount_krw,
        **{field: getattr(payment, field) for field in FINANCIAL_FIELDS},
        merchant_payout_status=payment.engine_detail.get("merchant_payout_status"),
        **{field: payment.engine_detail[field] for field in MockProjection.model_fields if field in payment.engine_detail},
        source=payment.source, engine_payment_id=payment.engine_payment_id,
        engine_version=payment.engine_version, reason_code=payment.reason_code,
        last_error=payment.last_error, assets=payment.engine_detail.get("assets", []),
        timestamps=payment.engine_detail.get("timestamps", {}),
        settlement_reference=payment.engine_detail.get("settlement_reference"),
        created_at=aware(payment.created_at), updated_at=aware(payment.updated_at),
        authorized_at=aware(payment.authorized_at) if payment.authorized_at else None,
        settled_at=aware(payment.settled_at) if payment.settled_at else None,
    )


def checkout_read(session, checkout: Checkout) -> CheckoutRead:
    payment = session.scalar(select(Payment).where(Payment.checkout_id == checkout.checkout_id))
    return CheckoutRead(
        checkout_id=checkout.checkout_id, merchant_id=checkout.merchant_id,
        terminal_id=checkout.terminal_id, amount_krw=checkout.amount_krw,
        status=checkout.status, created_at=aware(checkout.created_at),
        expires_at=aware(checkout.expires_at), updated_at=aware(checkout.updated_at),
        payment_id=payment.payment_id if payment else None,
        payment=payment_read(session, payment) if payment else None,
    )


def append_event(session, kind: str, checkout: Checkout | None, payment: Payment | None = None,
                 payload: dict | None = None):
    session.flush()
    if payload is None:
        payload = {"checkout": checkout_read(session, checkout).model_dump(mode="json")}
    session.add(Event(
        event_type=kind, checkout_id=checkout.checkout_id if checkout else None,
        payment_id=payment.payment_id if payment else None,
        source=payment.source if payment else "simulated", occurred_at=utcnow(), payload=payload,
    ))
    session.flush()


class POSService:
    def __init__(self, database: Database, source: str = "simulated", lease_seconds: float = 10):
        self.db, self.source, self.lease_seconds = database, source, lease_seconds

    def expire(self):
        now = utcnow()
        with self.db.transaction() as session:
            for checkout in session.scalars(select(Checkout).where(
                Checkout.status == CS.WAITING_FOR_CARD, Checkout.expires_at <= now
            )).all():
                checkout.status, checkout.updated_at = CS.EXPIRED, now
                append_event(session, "checkout.status_changed", checkout)

    @staticmethod
    def terminal(session, terminal_id: str):
        terminal = session.get(Terminal, terminal_id)
        if terminal is None or not terminal.enabled:
            raise APIError(404, "UNKNOWN_TERMINAL", "Terminal is not registered or enabled")
        return terminal

    def create_checkout(self, body: CheckoutCreate, creation_key: str | None):
        self.expire()
        digest = fingerprint(body.model_dump(mode="json"))
        with self.db.transaction() as session:
            terminal = self.terminal(session, body.terminal_id)
            if terminal.merchant_id != body.merchant_id:
                raise APIError(409, "MERCHANT_MISMATCH", "Terminal does not belong to this merchant")
            if creation_key:
                prior = session.scalar(select(Checkout).where(Checkout.creation_key == creation_key))
                if prior:
                    if prior.creation_fingerprint != digest:
                        raise APIError(409, "IDEMPOTENCY_CONFLICT", "Creation key was used with different input")
                    return checkout_read(session, prior), False
            active = session.scalar(select(Checkout).where(
                Checkout.terminal_id == body.terminal_id, Checkout.status.in_(ACTIVE)
            ))
            if active:
                raise APIError(409, "ACTIVE_CHECKOUT_EXISTS", "Terminal already has an active checkout")
            now = utcnow()
            checkout = Checkout(
                checkout_id=str(uuid4()), merchant_id=body.merchant_id,
                terminal_id=body.terminal_id, amount_krw=body.amount_krw,
                currency="KRW", status=CS.WAITING_FOR_CARD,
                creation_key=creation_key or str(uuid4()), creation_fingerprint=digest,
                created_at=now, updated_at=now,
                expires_at=now + timedelta(seconds=body.expires_in_seconds),
            )
            session.add(checkout)
            append_event(session, "checkout.created", checkout)
            return checkout_read(session, checkout), True

    def get_checkout(self, checkout_id: str):
        self.expire()
        with self.db.transaction(write=False) as session:
            checkout = session.get(Checkout, checkout_id)
            if checkout is None:
                raise APIError(404, "CHECKOUT_NOT_FOUND", "Checkout does not exist")
            return {"checkout": checkout_read(session, checkout), "latest_event_id": cursor(session)}

    def active_checkout(self, terminal_id: str):
        self.expire()
        with self.db.transaction(write=False) as session:
            self.terminal(session, terminal_id)
            checkout = session.scalar(select(Checkout).where(
                Checkout.terminal_id == terminal_id, Checkout.status.in_(ACTIVE)
            ))
            return {"checkout": checkout_read(session, checkout) if checkout else None,
                    "latest_event_id": cursor(session)}

    def cancel(self, checkout_id: str):
        self.expire()
        with self.db.transaction() as session:
            checkout = session.get(Checkout, checkout_id)
            if checkout is None:
                raise APIError(404, "CHECKOUT_NOT_FOUND", "Checkout does not exist")
            if checkout.status == CS.CANCELLED:
                return checkout_read(session, checkout)
            if checkout.status != CS.WAITING_FOR_CARD:
                raise APIError(409, "INVALID_CHECKOUT_STATE", "Only a waiting checkout can be cancelled")
            checkout.status, checkout.updated_at = CS.CANCELLED, utcnow()
            append_event(session, "checkout.status_changed", checkout)
            return checkout_read(session, checkout)

    @staticmethod
    def command(session, payment: Payment) -> AuthorizationRequest:
        checkout = session.get(Checkout, payment.checkout_id)
        return AuthorizationRequest(
            payment_id=payment.payment_id, checkout_id=checkout.checkout_id,
            merchant_id=checkout.merchant_id, terminal_id=checkout.terminal_id,
            customer_id=payment.customer_id, amount_krw=checkout.amount_krw,
        )

    def claim_tap(self, body: TapRequest):
        """Returns (payment ID, owns initial engine submission). Database is the lock."""
        self.expire()
        digest = fingerprint(body.model_dump(mode="json"))
        with self.db.transaction() as session:
            self.terminal(session, body.terminal_id)
            prior = session.get(TapEvent, str(body.client_event_id))
            if prior:
                if prior.payload_fingerprint != digest:
                    raise APIError(409, "IDEMPOTENCY_CONFLICT", "Event ID was used with different input")
                return prior.payment_id, None
            card = session.get(DemoCard, body.card_alias)
            if card is None or not card.enabled:
                raise APIError(404, "UNKNOWN_CARD", "Demo card is not registered or enabled")
            if body.checkout_id:
                checkout = session.get(Checkout, str(body.checkout_id))
                if checkout is None or checkout.terminal_id != body.terminal_id:
                    raise APIError(404, "CHECKOUT_NOT_FOUND", "Checkout does not belong to this terminal")
            else:
                checkout = session.scalar(select(Checkout).where(
                    Checkout.terminal_id == body.terminal_id, Checkout.status.in_(ACTIVE)
                ))
                if checkout is None:
                    raise APIError(409, "NO_ACTIVE_CHECKOUT", "Terminal has no active checkout")
            payment = session.scalar(select(Payment).where(Payment.checkout_id == checkout.checkout_id))
            if payment:
                if payment.customer_id != card.customer_id:
                    raise APIError(409, "CHECKOUT_ALREADY_CLAIMED", "Checkout is associated with another customer")
                session.add(TapEvent(
                    client_event_id=str(body.client_event_id), payload_fingerprint=digest,
                    checkout_id=checkout.checkout_id, payment_id=payment.payment_id, created_at=utcnow(),
                ))
                return payment.payment_id, None
            if checkout.status == CS.EXPIRED or aware(checkout.expires_at) <= utcnow():
                raise APIError(410, "CHECKOUT_EXPIRED", "Checkout has expired")
            if checkout.status != CS.WAITING_FOR_CARD:
                raise APIError(409, "NO_ACTIVE_CHECKOUT", "Checkout is not waiting for a card")
            now = utcnow()
            checkout.status, checkout.updated_at = CS.AUTHORIZING, now
            payment = Payment(
                payment_id=str(uuid4()), checkout_id=checkout.checkout_id,
                idempotency_key=checkout.checkout_id, customer_id=card.customer_id,
                authorization_status="PENDING", asset_reservation_status="PENDING",
                prefunding_status="PENDING", liquidation_status="NOT_STARTED",
                reimbursement_status="NOT_STARTED", settlement_status="NOT_STARTED",
                source=self.source, engine_version=0, engine_detail={},
                created_at=now, updated_at=now,
                lease_until=now + timedelta(seconds=self.lease_seconds),
            )
            session.add(payment)
            session.flush()
            session.add(TapEvent(
                client_event_id=str(body.client_event_id), payload_fingerprint=digest,
                checkout_id=checkout.checkout_id, payment_id=payment.payment_id, created_at=now,
            ))
            append_event(session, "checkout.status_changed", checkout, payment)
            append_event(session, "reservation.status_changed", checkout, payment)
            append_event(session, "prefunding.status_changed", checkout, payment)
            return payment.payment_id, self.command(session, payment)

    def tap_result(self, payment_id: str, duplicate: bool):
        with self.db.transaction(write=False) as session:
            payment = session.get(Payment, payment_id)
            if payment is None:
                raise APIError(404, "PAYMENT_NOT_FOUND", "Payment does not exist")
            checkout = session.get(Checkout, payment.checkout_id)
            return {"checkout": checkout_read(session, checkout), "payment": payment_read(session, payment),
                    "duplicate": duplicate, "latest_event_id": cursor(session)}

    def apply_engine(self, result: EnginePayment):
        # Revalidate even if a test/integration adapter constructs a model unsafely.
        result = EnginePayment.model_validate(result.model_dump(mode="json"))
        with self.db.transaction() as session:
            payment = session.get(Payment, str(result.payment_id))
            if payment is None:  # A protected reset may have removed settled fixtures.
                return
            checkout = session.get(Checkout, payment.checkout_id)
            if (str(result.checkout_id) != checkout.checkout_id or result.amount_krw != checkout.amount_krw
                    or result.source != payment.source):
                raise ValueError("Engine payment correlation, amount or source mismatch")
            if payment.engine_payment_id and payment.engine_payment_id != result.engine_payment_id:
                raise ValueError("Engine payment identity cannot change")
            if result.version <= payment.engine_version:
                # An unchanged poll still completes the worker's lease. Leaving
                # the lease held would stall subsequent settlement polling.
                payment.lease_until = utcnow()
                return
            if (payment.authorization_status != Auth.PENDING
                    and payment.authorization_status != Auth.UNKNOWN
                    and result.authorization_status != payment.authorization_status):
                raise ValueError("Engine cannot reverse a final authorization")
            if (result.authorization_status == Auth.AUTHORIZED
                    and payment.authorization_status != Auth.AUTHORIZED
                    and result.merchant_payout_status != MerchantPayoutStatus.COMPLETED):
                raise ValueError("New authorization requires explicit completed merchant payout")
            prior_payout = payment.engine_detail.get("merchant_payout_status")
            if (prior_payout == MerchantPayoutStatus.COMPLETED
                    and result.merchant_payout_status != MerchantPayoutStatus.COMPLETED):
                raise ValueError("Engine cannot regress a completed merchant payout")
            if payment.settlement_status == SS.COMPLETED:
                return
            if (payment.settlement_status == SS.RECOVERY_REQUIRED
                    and payment.engine_detail.get("recovery_status") != "PENDING"):
                return  # Preserve terminal legacy recovery and reconciled demo loss.
            # A newer upstream version must not regress completed stages.
            for field in ("liquidation_status", "reimbursement_status"):
                if getattr(payment, field) == "COMPLETED" and getattr(result, field) != "COMPLETED":
                    raise ValueError("Engine cannot regress a completed stage")
                rank = {"NOT_STARTED": 0, "PENDING": 1, "PROCESSING": 2, "COMPLETED": 3, "FAILED": 3}
                if rank[getattr(result, field)] < rank[getattr(payment, field)]:
                    raise ValueError("Engine cannot regress financial progress")
            old_checkout_status = checkout.status
            prior_event_ids = {event["event_id"] for event in payment.engine_detail.get("mock_events", [])}
            payout_changed = prior_payout != result.merchant_payout_status
            changed = {field for field in FINANCIAL_FIELDS if getattr(payment, field) != getattr(result, field)}
            for field in FINANCIAL_FIELDS:
                setattr(payment, field, getattr(result, field))
            now = utcnow()
            payment.engine_version = result.version
            payment.engine_payment_id = result.engine_payment_id
            payment.engine_detail = result.model_dump(mode="json")
            payment.reason_code, payment.last_error = result.reason_code, None
            payment.updated_at = now
            payment.lease_until = now
            if result.authorization_status == Auth.AUTHORIZED:
                checkout.status = CS.AUTHORIZED
                payment.authorized_at = result.timestamps.get("authorized_at") or payment.authorized_at or now
            elif result.authorization_status == Auth.DECLINED:
                checkout.status = CS.DECLINED
            if result.settlement_status == SS.COMPLETED:
                payment.settled_at = result.timestamps.get("settled_at") or now
            if old_checkout_status != checkout.status:
                checkout.updated_at = now
                append_event(session, "checkout.status_changed", checkout, payment)
            kinds = {
                "authorization_status": "payment.authorization",
                "asset_reservation_status": "reservation.status_changed",
                "prefunding_status": "prefunding.status_changed",
                "liquidation_status": "liquidation.status_changed",
                "reimbursement_status": "reimbursement.status_changed",
                "settlement_status": "settlement.status_changed",
            }
            for field in FINANCIAL_FIELDS:
                if field in changed:
                    append_event(session, kinds[field], checkout, payment)
            for event in result.mock_events:
                if (event.payment_id != payment.payment_id or event.checkout_id != payment.checkout_id
                        or event.customer_id != payment.customer_id or event.wallet_id != result.wallet_id):
                    raise ValueError("Mock event correlation mismatch")
                if event.event_id not in prior_event_ids:
                    append_event(session, event.event_type, checkout, payment, payload={
                        "checkout": checkout_read(session, checkout).model_dump(mode="json"),
                        "mock_engine_event": event.model_dump(mode="json"),
                    })
            if payout_changed:
                append_event(session, "merchant_payout.status_changed", checkout, payment)
            if not changed and not payout_changed:
                append_event(session, "payment.updated", checkout, payment)

    def record_error(self, payment_id: str, error: Exception):
        # Do not persist exception messages: they can contain URLs, credentials or payloads.
        with self.db.transaction() as session:
            payment = session.get(Payment, payment_id)
            if payment:
                error_code = type(error).__name__
                changed = payment.last_error != error_code
                payment.last_error, payment.updated_at = error_code, utcnow()
                if payment.authorization_status in (Auth.PENDING, Auth.UNKNOWN) and isinstance(error, (TimeoutError, httpx.TransportError)):
                    payment.authorization_status = Auth.UNKNOWN
                payment.lease_until = utcnow() + timedelta(seconds=1)
                if changed:
                    append_event(session, "payment.reconciliation_pending",
                                 session.get(Checkout, payment.checkout_id), payment)

    def reconciliation_batch(self, limit: int = 20):
        now = utcnow()
        with self.db.transaction() as session:
            payments = session.scalars(select(Payment).where(
                Payment.lease_until <= now,
                or_(Payment.authorization_status.in_((Auth.PENDING, Auth.UNKNOWN)),
                    Payment.settlement_status == SS.PENDING,
                    and_(Payment.settlement_status == SS.RECOVERY_REQUIRED,
                         Payment.engine_detail["recovery_status"].as_string() == "PENDING")),
            ).order_by(Payment.created_at).limit(limit)).all()
            commands = []
            for payment in payments:
                payment.lease_until = now + timedelta(seconds=self.lease_seconds)
                commands.append(self.command(session, payment))
            return commands

    def payment(self, payment_id: str):
        with self.db.transaction(write=False) as session:
            payment = session.get(Payment, payment_id)
            if payment is None:
                raise APIError(404, "PAYMENT_NOT_FOUND", "Payment does not exist")
            return payment_read(session, payment)

    def payments(self, limit: int = 50, offset: int = 0, settlement_status: SS | None = None):
        with self.db.transaction(write=False) as session:
            query = select(Payment)
            count = select(func.count()).select_from(Payment)
            if settlement_status:
                query = query.where(Payment.settlement_status == settlement_status)
                count = count.where(Payment.settlement_status == settlement_status)
            rows = session.scalars(query.order_by(Payment.created_at.desc(), Payment.payment_id.desc())
                                   .limit(limit).offset(offset)).all()
            return {"items": [payment_read(session, row) for row in rows],
                    "total": session.scalar(count), "limit": limit, "offset": offset,
                    "latest_event_id": cursor(session)}

    def events(self, after: int, checkout_id: str | None = None, through: int | None = None):
        with self.db.transaction(write=False) as session:
            query = select(Event).where(Event.event_id > after)
            if checkout_id:
                query = query.where(or_(Event.checkout_id == checkout_id, Event.event_type == "demo.reset"))
            if through is not None:
                query = query.where(Event.event_id <= through)
            rows = session.scalars(query.order_by(Event.event_id).limit(100)).all()
            return [EventRead(
                event_id=row.event_id, event_type=row.event_type, checkout_id=row.checkout_id,
                payment_id=row.payment_id, source=row.source, occurred_at=aware(row.occurred_at), payload=row.payload,
            ) for row in rows]

    def reset(self):
        if self.source != "simulated":
            raise APIError(409, "RESET_DISABLED", "Demo reset is available only with the mock adapter")
        with self.db.transaction() as session:
            if session.scalar(select(Checkout.checkout_id).where(Checkout.status == CS.AUTHORIZING).limit(1)):
                raise APIError(409, "AUTHORIZATION_IN_PROGRESS", "Resolve pending authorization before resetting")
            if session.scalar(select(MockEngineRecord.checkout_id).where(
                MockEngineRecord.initial_result["projection"]["transaction_status"].as_string().in_(("PENDING", "UNKNOWN"))
            ).limit(1)):
                raise APIError(409, "TRANSACTION_UNRESOLVED", "Reconcile pending mock transactions before resetting")
            for table in (Event, TapEvent, Payment, Checkout, MockEngineEvent, MockEngineRecord,
                          MockWalletOperation, MockWallet, MockCompanyCapacity):
                session.execute(delete(table))
            seed_mock_wallets(session)
            # sqlite_autoincrement preserves the event cursor across a reset.
            append_event(session, "demo.reset", None, payload={"reset": True})
            return {"reset": True, "latest_event_id": cursor(session)}
