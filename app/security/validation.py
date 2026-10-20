"""Validação rigorosa de entradas: identificadores, rótulos, nomes de arquivo e tipos básicos."""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from ..errors import ValidationFailure

DEVICE_ID_RE = re.compile(r"^dev_[0-9a-f]{16}\Z")
MESSAGE_ID_RE = re.compile(r"^msg_[0-9a-f]{24}\Z")
REQUEST_ID_RE = re.compile(r"^grq_[0-9a-f]{16}\Z")
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}\Z")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._+-]{0,39}\Z")


def _fail(reason: str) -> ValidationFailure:
    return ValidationFailure("INVALID_REQUEST", reason=reason)


def check_id(value: Any, pattern: re.Pattern, field: str) -> str:
    if not isinstance(value, str) or not pattern.match(value):
        raise _fail(f"Campo '{field}' inválido.")
    return value


def check_device_id(value: Any, field: str = "device_id") -> str:
    return check_id(value, DEVICE_ID_RE, field)


def check_message_id(value: Any) -> str:
    return check_id(value, MESSAGE_ID_RE, "message_id")


def check_request_id(value: Any) -> str:
    return check_id(value, REQUEST_ID_RE, "request_id")


def clean_label(value: Any, field: str, max_len: int = 64) -> str:
    """Texto curto exibível: NFC, sem caracteres de controle, tamanho limitado."""
    if not isinstance(value, str):
        raise _fail(f"Campo '{field}' deve ser texto.")
    text = unicodedata.normalize("NFC", value)
    text = "".join(ch for ch in text if not unicodedata.category(ch).startswith("C")).strip()
    if not text or len(text) > max_len:
        raise _fail(f"Campo '{field}' deve ter de 1 a {max_len} caracteres.")
    return text


def clean_filename(value: Any) -> str | None:
    """Nome de arquivo apenas informativo: remove caminhos e caracteres incomuns."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise _fail("Campo 'filename' deve ser texto.")
    base = re.split(r"[\\/]", value)[-1]
    base = re.sub(r"[^A-Za-z0-9._ -]", "_", base).strip(" .")[:100]
    return base or None


def string_list(value: Any, field: str, *, max_items: int = 16, pattern: re.Pattern = NAME_RE) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or len(value) > max_items:
        raise _fail(f"Campo '{field}' deve ser uma lista com até {max_items} itens.")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not pattern.match(item):
            raise _fail(f"Item inválido em '{field}'.")
        if item not in out:
            out.append(item)
    return tuple(out)


def bounded_int(value: Any, field: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise _fail(f"Campo '{field}' deve ser inteiro entre {lo} e {hi}.")
    return value
