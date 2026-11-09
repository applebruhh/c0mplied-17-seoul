"""Frozen SB wire signing/token rules. Secrets never enter a client contract."""
import hashlib
import hmac
import re


def normalize_uid(uid: str) -> str:
    normalized = "".join(char for char in uid if char not in ":-" and not char.isspace()).upper()
    if not normalized or len(normalized) % 2 or not re.fullmatch(r"[0-9A-F]+", normalized):
        raise ValueError("UID must contain complete hexadecimal bytes")
    return normalized


def card_token_from_uid(uid: str) -> str:
    return hashlib.sha256(normalize_uid(uid).encode("ascii")).hexdigest()


def sign_raw_json(body: bytes, secret: str) -> str:
    if not isinstance(body, bytes) or not secret:
        raise ValueError("Exact body bytes and a nonempty signing secret are required")
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
