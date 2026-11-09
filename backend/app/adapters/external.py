from uuid import UUID

import httpx

from app.models import AuthorizationRequest, EnginePayment


class ExternalPaymentAdapter:
    """Proposed engine contract, not enabled by the application scaffold.

    Caller owns the AsyncClient lifecycle and configures base_url, bearer
    authentication and timeouts. HTTP/validation errors must be reconciled by
    the orchestration layer using the original key, never treated as declines.
    """

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    def _parse(self, response: httpx.Response, payment_id: UUID) -> EnginePayment:
        response.raise_for_status()
        payment = EnginePayment.model_validate(response.json())
        if payment.payment_id != payment_id or payment.source != "external":
            raise ValueError("Engine response correlation or provenance mismatch")
        return payment

    async def authorize(self, request: AuthorizationRequest) -> EnginePayment:
        response = await self._client.post(
            "authorizations",
            json=request.model_dump(mode="json"),
            headers={"Idempotency-Key": str(request.payment_id)},
        )
        payment = self._parse(response, request.payment_id)
        if payment.amount_krw != request.amount_krw:
            raise ValueError("Engine response amount mismatch")
        return payment

    async def get_payment(self, payment_id: UUID) -> EnginePayment:
        response = await self._client.get(f"authorizations/by-key/{payment_id}")
        return self._parse(response, payment_id)
