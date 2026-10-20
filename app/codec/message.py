"""Mensagem canônica (em memória) e validação estrita dos metadados vindos do fio."""
from __future__ import annotations

import hashlib
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from ..capabilities.models import KIND_MIMES, KINDS
from ..errors import OmniLinkError, TooLarge, ValidationFailure
from ..security.validation import DEVICE_ID_RE, MESSAGE_ID_RE, clean_filename

PROTOCOL_VERSION = 1
_SHA_RE = re.compile(r"^[0-9a-f]{64}\Z")


def new_message_id() -> str:
    return "msg_" + secrets.token_hex(12)


@dataclass
class Message:
    id: str
    sender: str
    recipient: str
    kind: str
    mime: str
    data: bytes
    filename: str | None = None
    created_at: float = field(default_factory=time.time)
    sha256: str = ""
    adapted: list = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.sha256:
            self.refresh_hash()

    def refresh_hash(self) -> None:
        self.sha256 = hashlib.sha256(self.data).hexdigest()

    def meta(self) -> dict:
        return {"type": "message", "v": PROTOCOL_VERSION, "id": self.id, "from": self.sender, "to": self.recipient,
                "ts": round(self.created_at, 3), "kind": self.kind, "mime": self.mime, "filename": self.filename,
                "size": len(self.data), "sha256": self.sha256, "adapted": list(self.adapted)}


def bad(reason: str) -> ValidationFailure:
    return ValidationFailure("MALFORMED_MESSAGE", reason=reason)


def message_from_meta(meta: Any, data: bytes, max_bytes: int) -> Message:
    """Reconstrói e valida uma Message; qualquer inconsistência vira erro de diagnóstico."""
    if not isinstance(meta, dict):
        raise bad("cabeçalho não é um objeto JSON")
    if meta.get("type") != "message":
        raise bad("campo 'type' deve ser 'message'")
    if meta.get("v") != PROTOCOL_VERSION or isinstance(meta.get("v"), bool):
        raise bad(f"versão de protocolo não suportada: {str(meta.get('v'))[:10]}")

    def field_of(key: str, typ: type) -> Any:
        value = meta.get(key)
        if not isinstance(value, typ) or isinstance(value, bool):
            raise bad(f"campo '{key}' ausente ou com tipo inválido")
        return value

    mid, sender, recipient = field_of("id", str), field_of("from", str), field_of("to", str)
    if not MESSAGE_ID_RE.match(mid) or not DEVICE_ID_RE.match(sender) or not DEVICE_ID_RE.match(recipient):
        raise bad("identificador com formato inválido")
    kind, mime = field_of("kind", str), field_of("mime", str)
    if kind not in KINDS or mime not in KIND_MIMES[kind]:
        raise bad("tipo de conteúdo ou MIME não permitido")
    ts = meta.get("ts")
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        raise bad("campo 'ts' inválido")
    if len(data) > max_bytes:
        raise TooLarge("PAYLOAD_TOO_LARGE", limit=max_bytes)
    if field_of("size", int) != len(data):
        raise OmniLinkError("INTEGRITY_FAILED", reason="tamanho declarado difere do conteúdo recebido")
    digest = field_of("sha256", str)
    if not _SHA_RE.match(digest) or digest != hashlib.sha256(data).hexdigest():
        raise OmniLinkError("INTEGRITY_FAILED", reason="hash SHA-256 não confere")
    if kind in ("text", "html"):
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise bad("conteúdo textual não é UTF-8 válido") from exc
    adapted = meta.get("adapted", [])
    if not isinstance(adapted, list) or len(adapted) > 8 or any(not isinstance(a, str) or len(a) > 40 for a in adapted):
        raise bad("campo 'adapted' inválido")
    try:
        filename = clean_filename(meta.get("filename"))
    except ValidationFailure as exc:
        raise bad("campo 'filename' inválido") from exc
    return Message(id=mid, sender=sender, recipient=recipient, kind=kind, mime=mime, data=data, filename=filename,
                   created_at=float(ts), sha256=digest, adapted=list(adapted))
