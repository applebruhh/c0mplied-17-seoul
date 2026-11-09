import asyncio
import logging

from app.adapters.base import EnginePaymentNotFound, PaymentAdapter
from app.models import AuthorizationRequest
from app.services import POSService

logger = logging.getLogger(__name__)


async def authorize(service: POSService, adapter: PaymentAdapter, command: AuthorizationRequest, timeout: float):
    try:
        result = await asyncio.wait_for(adapter.authorize(command), timeout)
        await asyncio.to_thread(service.apply_engine, result)
    except Exception as error:
        await asyncio.to_thread(service.record_error, str(command.payment_id), error)


async def reconcile_once(service: POSService, adapter: PaymentAdapter, timeout: float):
    await asyncio.to_thread(service.expire)
    # Claim unresolved commands after projecting any durable final responses locally.
    commands = await asyncio.to_thread(service.reconciliation_batch)
    for command in commands:
        try:
            try:
                result = await asyncio.wait_for(adapter.get_payment(command.checkout_id), timeout)
            except EnginePaymentNotFound:
                # This exception guarantees absence. Ordinary HTTP errors/timeouts
                # are uncertain outcomes and must never generate a new key.
                result = await asyncio.wait_for(adapter.authorize(command), timeout)
            await asyncio.to_thread(service.apply_engine, result)
        except Exception as error:
            await asyncio.to_thread(service.record_error, str(command.payment_id), error)


async def run_worker(service: POSService, adapter: PaymentAdapter, interval: float, timeout: float,
                     stop: asyncio.Event):
    while not stop.is_set():
        try:
            await reconcile_once(service, adapter, timeout)
        except Exception as error:
            logger.error("Reconciliation iteration failed: %s", type(error).__name__)
        try:
            await asyncio.wait_for(stop.wait(), interval)
        except TimeoutError:
            pass
