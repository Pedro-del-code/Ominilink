"""Codec 'binary-frame': cabeçalho fixo de 16 bytes + cabeçalho JSON + payload bruto (sem Base64).

Layout (big-endian):
    0   4  assinatura  b"OLNK"
    4   1  versão do frame (1)
    5   1  flags (reservado; deve ser 0)
    6   2  reservado (deve ser 0)
    8   4  tamanho do cabeçalho JSON (<= 16 KiB)
    12  4  tamanho do payload
    16  N  cabeçalho JSON UTF-8 (metadados, com "encoding": "binary")
    16+N M payload bruto
"""
from __future__ import annotations

import json
import struct

from ..errors import TooLarge
from .message import Message, bad, message_from_meta

MAGIC = b"OLNK"
FRAME_VERSION = 1
_HEADER = struct.Struct(">4sBBHII")
MAX_HEADER = 16 * 1024


class BinaryFrameCodec:
    name = "binary-frame"
    binary = True

    def encode(self, msg: Message) -> bytes:
        meta = msg.meta()
        meta["encoding"] = "binary"
        header = json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return _HEADER.pack(MAGIC, FRAME_VERSION, 0, 0, len(header), len(msg.data)) + header + msg.data

    def decode(self, frame: bytes | str, *, max_bytes: int) -> Message:
        if not isinstance(frame, (bytes, bytearray)):
            raise bad("frame binário esperado")
        frame = bytes(frame)
        if len(frame) < _HEADER.size:
            raise bad("frame truncado (menor que o cabeçalho fixo)")
        magic, version, flags, reserved, hlen, plen = _HEADER.unpack_from(frame)
        if magic != MAGIC:
            raise bad("assinatura do frame inválida")
        if version != FRAME_VERSION:
            raise bad(f"versão de frame não suportada: {version}")
        if flags != 0 or reserved != 0:
            raise bad("bits reservados devem ser zero")
        if not 0 < hlen <= MAX_HEADER:
            raise bad("tamanho de cabeçalho inválido")
        if plen > max_bytes:
            raise TooLarge("PAYLOAD_TOO_LARGE", limit=max_bytes)
        if len(frame) != _HEADER.size + hlen + plen:
            raise bad("tamanho declarado difere do recebido")
        try:
            meta = json.loads(frame[_HEADER.size:_HEADER.size + hlen].decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise bad("cabeçalho JSON inválido") from exc
        if not isinstance(meta, dict) or meta.get("encoding") != "binary":
            raise bad("campo 'encoding' deve ser 'binary'")
        return message_from_meta(meta, frame[_HEADER.size + hlen:], max_bytes)
