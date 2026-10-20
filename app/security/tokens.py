"""Credenciais de dispositivo: token = '<device_id>.<segredo>'; só o hash SHA-256 do segredo é guardado.

Os segredos têm 256 bits de entropia, portanto um hash rápido é adequado (não há ataque de força bruta
ou de dicionário viável); a comparação usa tempo constante.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets

from ..errors import AuthError

_TOKEN_RE = re.compile(r"^(dev_[0-9a-f]{16})\.([A-Za-z0-9_-]{32,64})\Z")


def new_device_id() -> str:
    return "dev_" + secrets.token_hex(8)


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("ascii")).hexdigest()


def issue_token(device_id: str) -> tuple[str, str]:
    """Retorna (token em claro — exibido uma única vez —, hash a persistir)."""
    secret = secrets.token_urlsafe(32)
    return f"{device_id}.{secret}", hash_secret(secret)


def parse_token(token: str) -> tuple[str, str]:
    match = _TOKEN_RE.match(token or "")
    if not match:
        raise AuthError("AUTH_INVALID")
    return match.group(1), match.group(2)


def verify_secret(secret: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_secret(secret), stored_hash or "")


def check_enrollment_key(provided: str | None, expected: str) -> bool:
    if not provided or not expected:
        return False
    return hmac.compare_digest(hashlib.sha256(provided.encode()).digest(), hashlib.sha256(expected.encode()).digest())
