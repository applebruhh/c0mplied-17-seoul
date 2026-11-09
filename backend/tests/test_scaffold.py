"""Phase 1 contract checks updated for the implemented Phase 2 contract."""
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.models import CheckoutCreate, TapRequest


def test_tap_cannot_override_amount_or_authorization():
    with pytest.raises(ValidationError):
        TapRequest(terminal_id="galaxy-01", card_alias="demo-alice", client_event_id=uuid4(),
                   amount_krw=1, authorization_status="AUTHORIZED")


@pytest.mark.parametrize("amount", [0, -1, 1.5, True, "4500", 10_000_001])
def test_amount_must_be_bounded_integer(amount):
    with pytest.raises(ValidationError):
        CheckoutCreate(merchant_id="merchant-demo", terminal_id="galaxy-01", amount_krw=amount)
