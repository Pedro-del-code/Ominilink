"""Registro de codecs de mensagem. Novos codecs são plugados com register_codec()."""
from __future__ import annotations

from ..errors import ValidationFailure
from .binary_codec import BinaryFrameCodec
from .json_codec import JsonB64Codec
from .message import PROTOCOL_VERSION, Message, new_message_id

_CODECS: dict = {}


def register_codec(codec) -> None:
    _CODECS[codec.name] = codec


def get_codec(name: str):
    try:
        return _CODECS[name]
    except KeyError:
        raise ValidationFailure("CODEC_UNKNOWN", name=str(name)[:40]) from None


def available_codecs() -> list:
    return sorted(_CODECS)


register_codec(JsonB64Codec())
register_codec(BinaryFrameCodec())

__all__ = ["PROTOCOL_VERSION", "Message", "new_message_id", "register_codec", "get_codec", "available_codecs",
           "JsonB64Codec", "BinaryFrameCodec"]
