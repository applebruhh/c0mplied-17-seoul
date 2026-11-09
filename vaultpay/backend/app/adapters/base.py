from typing import Literal, Protocol
from uuid import UUID

from app.models import AuthorizationRequest, AuthorizationResult


class EnginePaymentNotFound(Exception):
    """Engine can definitively confirm the key has not been submitted."""


class PaymentAdapter(Protocol):
    """POS-to-Payment-Engine boundary, never a wallet or DEX interface.

    The engine internally coordinates the Embedded Wallet hold, company
    capacity and merchant payout before approval, then wallet liquidation,
    treasury reimbursement and release. All effects use the checkout key.
    """
    source: Literal["simulated", "external"]

    async def authorize(self, request: AuthorizationRequest) -> AuthorizationResult: ...

    async def get_payment(self, checkout_id: UUID) -> AuthorizationResult: ...
