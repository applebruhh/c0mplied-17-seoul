from typing import Protocol
from uuid import UUID

from app.models import AuthorizationRequest, EnginePayment


class PaymentAdapter(Protocol):
    """Engine boundary. Exceptions are unresolved outcomes, never declines."""

    async def authorize(self, request: AuthorizationRequest) -> EnginePayment: ...

    async def get_payment(self, payment_id: UUID) -> EnginePayment: ...
