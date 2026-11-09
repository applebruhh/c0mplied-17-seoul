"""SB Payment Engine authorization transport; no wallet/DEX/financial operations."""
import asyncio
import os
from uuid import UUID

import httpx
from pydantic import SecretStr, ValidationError

from app.adapters.base import EnginePaymentNotFound
from app.config import engine_origin
from app.contracts import ExternalAuthorizeRequest, ExternalAuthorizeResponse
from app.db import Database
from app.models import AuthorizationRequest, AuthorizationResult
from app.signing import sign_raw_json
from app.tables import AuthorizationSnapshot


class EngineOutcomeUnknown(RuntimeError):
    """Transport, missing lookup, or response-contract failure; never a decline."""


class ExternalPaymentAdapter:
    """Caller owns AsyncClient lifecycle, base URL and bounded timeouts.

    Only /v1/payments/authorize and /v1/payments/{checkout_id}/pos are used.
    """
    source = "external"

    def __init__(self, client: httpx.AsyncClient, database: Database, *, secret: str | None = None):
        value = secret if secret is not None else os.getenv("SB_POS_HMAC_SECRET", "")
        if not value:
            raise ValueError("SB_POS_HMAC_SECRET is required")
        engine_origin(client.base_url)
        self._client, self._database, self._secret = client, database, SecretStr(value)

    def _prepare(self, request):
        # Do not resolve/re-hash a token on retry. Freeze the original selected
        # card and amount along with exactly the JSON bytes that will be signed.
        if request.card_token is None:
            raise ValueError("Registered UID card token is required for external authorization")
        command = request.model_dump(mode="json")
        with self._database.transaction() as session:
            snapshot = session.get(AuthorizationSnapshot, str(request.checkout_id))
            if snapshot is None:
                snapshot = AuthorizationSnapshot(checkout_id=str(request.checkout_id),
                    payment_id=str(request.payment_id), request=command, submitted=False, response_revision=0)
                session.add(snapshot)
            elif AuthorizationRequest.model_validate(snapshot.request) != request:
                raise ValueError("Checkout authorization payload is immutable")
            if snapshot.wire_body is None:
                snapshot.wire_body = ExternalAuthorizeRequest(
                    checkout_id=request.checkout_id, amount_krw=request.amount_krw,
                    merchant_id=request.merchant_id, card_token=request.card_token,
                ).raw_json()
            first_submission = not snapshot.submitted
            snapshot.submitted = True  # Commit ownership before the first network call.
            cached = snapshot.engine_response
            return snapshot.wire_body, first_submission, cached, snapshot.response_revision

    @staticmethod
    def _normalized(request, response, revision):
        return AuthorizationResult(
            payment_id=request.payment_id, checkout_id=request.checkout_id,
            engine_payment_id=response.payment_id, amount_krw=request.amount_krw,
            authorization_status=response.status, source="external", version=revision,
            reason_code=response.reason, latency_ms=response.latency_ms,
            idempotent_replay=response.idempotent_replay, financial_status_reported=False,
        )

    def _request(self, checkout_id):
        with self._database.transaction(write=False) as session:
            snapshot = session.get(AuthorizationSnapshot, str(checkout_id))
            if snapshot is None:
                # Lack of local metadata does not prove absence at the engine.
                raise EngineOutcomeUnknown("No persisted authorization context for lookup")
            request = AuthorizationRequest.model_validate(snapshot.request)
            if request.card_token is not None and not snapshot.submitted:
                # The local ownership claim was never made, or HTTPX proved it
                # never acquired a connection. Either way no POST was sent.
                raise EnginePaymentNotFound
            return request

    def _allow_unsent_retry(self, checkout_id):
        with self._database.transaction() as session:
            snapshot = session.get(AuthorizationSnapshot, str(checkout_id))
            if snapshot is not None and snapshot.engine_response is None:
                snapshot.submitted = False

    def _record_response(self, request, response):
        with self._database.transaction() as session:
            snapshot = session.get(AuthorizationSnapshot, str(request.checkout_id))
            if snapshot is None:
                raise EngineOutcomeUnknown("Authorization context disappeared")
            if snapshot.engine_response:
                previous = ExternalAuthorizeResponse.model_validate(snapshot.engine_response)
                if previous.payment_id != response.payment_id:
                    raise EngineOutcomeUnknown("Engine payment correlation changed")
                if previous.status in ("AUTHORIZED", "DECLINED") and previous.status != response.status:
                    raise EngineOutcomeUnknown("Engine reversed a final authorization")
                changed = (previous.status, previous.reason) != (response.status, response.reason)
            else:
                changed = True
            snapshot.response_revision += int(changed)
            snapshot.engine_response = response.model_dump(mode="json")
            return self._normalized(request, response, snapshot.response_revision)

    def _parse(self, response):
        try:
            response.raise_for_status()
            return ExternalAuthorizeResponse.model_validate(response.json())
        except (httpx.HTTPStatusError, ValidationError, ValueError) as error:
            # 404 lookup absence/HTTP business-error envelopes were not specified;
            # do not translate them into a definitive decline or automatic retry.
            raise EngineOutcomeUnknown("Unresolved Payment Engine response") from error

    async def authorize(self, request: AuthorizationRequest) -> AuthorizationResult:
        wire, first, cached, revision = await asyncio.to_thread(self._prepare, request)
        if cached is not None and cached["status"] in ("AUTHORIZED", "DECLINED"):
            return self._normalized(request, ExternalAuthorizeResponse.model_validate(cached), revision)
        if not first:
            return await self.get_payment(request.checkout_id)
        signature = sign_raw_json(wire, self._secret.get_secret_value())
        try:
            response = await self._client.post("/v1/payments/authorize", content=wire,
                headers={"Content-Type": "application/json", "X-SB-Signature": signature},
                follow_redirects=False)
        except httpx.PoolTimeout as error:
            await asyncio.to_thread(self._allow_unsent_retry, request.checkout_id)
            raise EngineOutcomeUnknown("No connection acquired; authorization can be retried") from error
        except httpx.TransportError as error:
            raise EngineOutcomeUnknown("Uncertain authorization submission; use status lookup") from error
        parsed = self._parse(response)
        if parsed.status not in ("AUTHORIZED", "DECLINED"):
            raise EngineOutcomeUnknown("Authorization did not return a final decision")
        return await asyncio.to_thread(self._record_response, request, parsed)

    async def get_payment(self, checkout_id: UUID) -> AuthorizationResult:
        request = await asyncio.to_thread(self._request, checkout_id)
        try:
            # GET has an empty raw body; sign b"", not a synthetic JSON object.
            response = await self._client.get(f"/v1/payments/{checkout_id}/pos", headers={
                "X-SB-Signature": sign_raw_json(b"", self._secret.get_secret_value())}, follow_redirects=False)
        except httpx.TransportError as error:
            raise EngineOutcomeUnknown("Uncertain Payment Engine lookup") from error
        parsed = self._parse(response)
        return await asyncio.to_thread(self._record_response, request, parsed)
