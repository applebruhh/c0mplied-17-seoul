"""Server-side enrollment and lookup for external NFC card tokens."""
from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DemoIdentifier
from app.signing import card_token_from_uid
from app.tables import DemoCard, ExternalCard


def _uid_token(identity: str) -> str | None:
    try:
        return card_token_from_uid(identity)
    except ValueError:
        return None


def enroll_external_card(session: Session, *, card_alias: str, uid: str, customer_id: str) -> str:
    """Bind a locally registered customer to a token without changing any binding."""
    TypeAdapter(DemoIdentifier).validate_python(card_alias)
    token = card_token_from_uid(uid)
    alias_token = _uid_token(card_alias)
    if alias_token is not None and alias_token != token:
        raise ValueError("A UID-shaped alias must match the enrolled UID")

    card = session.get(DemoCard, card_alias)
    if card is not None and (not card.enabled or card.customer_id != customer_id):
        raise ValueError("Alias is disabled or belongs to another customer")
    binding = session.scalar(select(ExternalCard).where(ExternalCard.token == token))
    if binding is not None and binding.card_alias != card_alias:
        raise ValueError("Token is already enrolled to a different alias")
    existing = session.get(ExternalCard, card_alias)
    if existing is not None:
        if existing.token != token:
            raise ValueError("Alias is already enrolled to a different token")
        return token

    if card is None:
        session.add(DemoCard(card_alias=card_alias, customer_id=customer_id, enabled=True))
        session.flush()
    session.add(ExternalCard(card_alias=card_alias, token=token))
    session.flush()
    return token


def resolve_external_card(session: Session, identity: str) -> tuple[DemoCard, str] | None:
    binding = session.get(ExternalCard, identity)
    if binding is None:
        token = _uid_token(identity)
        if token is not None:
            binding = session.scalar(select(ExternalCard).where(ExternalCard.token == token))
    if binding is not None:
        card = session.get(DemoCard, binding.card_alias)
        if card is not None and card.enabled:
            return card, binding.token
    return None
