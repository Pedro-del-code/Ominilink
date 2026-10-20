"""Codec 'json-b64': JSON UTF-8; texto/HTML em UTF-8, imagens em Base64. Universal (qualquer navegador)."""
from __future__ import annotations

import base64
import binascii
import json

from .message import Message, bad, message_from_meta


class JsonB64Codec:
    name = "json-b64"
    binary = False

    def to_dict(self, msg: Message) -> dict:
        meta = msg.meta()
        if msg.kind in ("text", "html"):
            meta["encoding"], meta["body"] = "utf-8", msg.data.decode("utf-8")
        else:
            meta["encoding"], meta["body"] = "base64", base64.b64encode(msg.data).decode("ascii")
        return meta

    def encode(self, msg: Message) -> str:
        return json.dumps(self.to_dict(msg), ensure_ascii=False, separators=(",", ":"))

    def decode(self, frame: str | bytes, *, max_bytes: int) -> Message:
        if isinstance(frame, (bytes, bytearray)):
            try:
                frame = bytes(frame).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise bad("frame não é UTF-8") from exc
        if len(frame) > max_bytes * 3 + 8192:
            raise bad("frame excede o tamanho máximo")
        try:
            meta = json.loads(frame)
        except ValueError as exc:
            raise bad("JSON inválido") from exc
        if not isinstance(meta, dict):
            raise bad("cabeçalho não é um objeto JSON")
        body, encoding = meta.get("body"), meta.get("encoding")
        if not isinstance(body, str):
            raise bad("campo 'body' ausente")
        try:
            if encoding == "utf-8":
                data = body.encode("utf-8")
            elif encoding == "base64":
                if len(body) > (max_bytes * 4) // 3 + 8:
                    raise bad("corpo Base64 excede o tamanho máximo")
                data = base64.b64decode(body, validate=True)
            else:
                raise bad("campo 'encoding' deve ser 'utf-8' ou 'base64'")
        except (UnicodeEncodeError, binascii.Error) as exc:
            raise bad("corpo com codificação inválida") from exc
        return message_from_meta(meta, data, max_bytes)
