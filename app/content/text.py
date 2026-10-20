"""Preparação de texto: normalização Unicode e remoção de caracteres de controle."""
from __future__ import annotations

import unicodedata

from ..errors import TooLarge, ValidationFailure


def normalize_text(value: str, max_chars: int) -> str:
    if not isinstance(value, str):
        raise ValidationFailure("INVALID_REQUEST", reason="O texto deve ser uma string.")
    text = unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(ch for ch in text if ch in "\n\t" or not unicodedata.category(ch).startswith("C"))
    if not text.strip():
        raise ValidationFailure("INVALID_REQUEST", reason="O texto está vazio.")
    if len(text) > max_chars:
        raise TooLarge("SIZE_LIMIT_EXCEEDED", size=len(text), limit=max_chars, unit="caracteres")
    return text
