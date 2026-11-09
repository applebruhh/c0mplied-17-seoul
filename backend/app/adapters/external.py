from uuid import UUID

from app.models import AuthorizationRequest, EnginePayment


class ExternalPaymentAdapter:
    """Deliberately disabled until the external team agrees the integration contract.

    Future implementation uses HTTPX with checkout_id as Idempotency-Key,
    authenticated requests and same-key lookup. No real financial calls occur.
    """
    source = "external"

    async def authorize(self, request: AuthorizationRequest) -> EnginePayment:
        raise NotImplementedError("External Payment Engine integration is not configured")

    async def get_payment(self, checkout_id: UUID) -> EnginePayment:
        raise NotImplementedError("External Payment Engine integration is not configured")
