from uuid import UUID

from app.models import AuthorizationRequest, EnginePayment


class MockPaymentAdapter:
    """In-memory contract fixture; no actual reservations or financial service.

    Deliberately has no timed settlement simulation yet. A new instance loses
    its fixtures; restart-safe reconciliation belongs to the next milestone.
    """

    def __init__(self) -> None:
        self._requests: dict[UUID, AuthorizationRequest] = {}
        self._payments: dict[UUID, EnginePayment] = {}

    async def authorize(self, request: AuthorizationRequest) -> EnginePayment:
        previous = self._requests.get(request.payment_id)
        if previous is not None and previous != request:
            raise ValueError("Payment key reused with a different request")
        if request.payment_id not in self._payments:
            self._requests[request.payment_id] = request.model_copy(deep=True)
            self._payments[request.payment_id] = EnginePayment(
                payment_id=request.payment_id,
                engine_payment_id=f"mock-{request.payment_id}",
                amount_krw=request.amount_krw,
                authorization_status="AUTHORIZED",
                settlement_status="PENDING",
                asset_reservation_status="RESERVED",
                krw_reservation_status="RESERVED",
                source="simulated",
                assets=["USDC"],
            )
        return self._payments[request.payment_id].model_copy(deep=True)

    async def get_payment(self, payment_id: UUID) -> EnginePayment:
        return self._payments[payment_id].model_copy(deep=True)
