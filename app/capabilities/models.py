"""Modelo das capacidades anunciadas por um dispositivo (documento JSON versionado).

O documento é a base da negociação: o receptor declara o que consegue receber e apresentar; o coordenador
nunca presume capacidades que não foram anunciadas.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..errors import ValidationFailure
from ..security.validation import bounded_int, clean_label, string_list

KINDS = ("text", "image", "html")
ROLES = ("sender", "receiver")
IMAGE_MIMES = ("image/png", "image/jpeg", "image/gif", "image/webp")
KIND_MIMES = {"text": ("text/plain",), "html": ("text/html",), "image": IMAGE_MIMES}
SCHEMA_VERSION = 1
_MIME_RE = re.compile(r"^[a-z]+/[a-z0-9.+-]+\Z")


def _bad(reason: str) -> ValidationFailure:
    return ValidationFailure("INVALID_REQUEST", reason=reason)


@dataclass(frozen=True)
class ContentDescriptor:
    """Descrição de um conteúdo a enviar (usada na negociação, sem carregar o corpo)."""
    kind: str
    mime: str
    size: int
    width: int | None = None
    height: int | None = None
    chars: int | None = None


@dataclass(frozen=True)
class ContentCap:
    max_bytes: int
    mimes: tuple[str, ...] = ()
    max_width: int | None = None
    max_height: int | None = None
    max_chars: int | None = None

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"max_bytes": self.max_bytes}
        if self.mimes:
            out["mimes"] = list(self.mimes)
        for key in ("max_width", "max_height", "max_chars"):
            if getattr(self, key) is not None:
                out[key] = getattr(self, key)
        return out

    @classmethod
    def from_dict(cls, kind: str, data: Any) -> "ContentCap":
        if not isinstance(data, dict):
            raise _bad(f"content.{kind} deve ser um objeto.")
        max_bytes = bounded_int(data.get("max_bytes"), f"content.{kind}.max_bytes", 1, 100_000_000)
        mimes = string_list(data.get("mimes", list(KIND_MIMES[kind])), f"content.{kind}.mimes", pattern=_MIME_RE)
        if not mimes or any(m not in KIND_MIMES[kind] for m in mimes):
            raise _bad(f"content.{kind}.mimes contém tipo não suportado para '{kind}'.")
        opt = {}
        for key, hi in (("max_width", 20000), ("max_height", 20000), ("max_chars", 10_000_000)):
            if data.get(key) is not None:
                opt[key] = bounded_int(data[key], f"content.{kind}.{key}", 1, hi)
        return cls(max_bytes=max_bytes, mimes=mimes, **opt)


@dataclass
class DeviceCapabilities:
    name: str
    platform: str = "unknown"
    roles: tuple[str, ...] = ("receiver",)
    protocol_versions: tuple[int, ...] = (1,)
    transports: tuple[str, ...] = ()
    codecs: tuple[str, ...] = ()
    content: dict[str, ContentCap] = field(default_factory=dict)
    features: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "schema": SCHEMA_VERSION,
            "name": self.name,
            "platform": self.platform,
            "roles": list(self.roles),
            "protocol_versions": list(self.protocol_versions),
            "transports": list(self.transports),
            "codecs": list(self.codecs),
            "content": {k: v.to_dict() for k, v in self.content.items()},
            "features": list(self.features),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "DeviceCapabilities":
        if not isinstance(data, dict):
            raise _bad("O documento de capacidades deve ser um objeto JSON.")
        roles = string_list(data.get("roles", ["receiver"]), "roles")
        if not roles or any(r not in ROLES for r in roles):
            raise _bad("Campo 'roles' deve conter 'sender' e/ou 'receiver'.")
        raw_versions = data.get("protocol_versions", [1])
        if not isinstance(raw_versions, list) or not raw_versions or len(raw_versions) > 8:
            raise _bad("Campo 'protocol_versions' deve ser uma lista de 1 a 8 inteiros.")
        versions = tuple(sorted({bounded_int(v, "protocol_versions", 1, 99) for v in raw_versions}))
        raw_content = data.get("content") or {}
        if not isinstance(raw_content, dict):
            raise _bad("Campo 'content' deve ser um objeto.")
        content: dict[str, ContentCap] = {}
        for kind, cap in raw_content.items():
            if kind not in KINDS:
                raise _bad(f"Tipo de conteúdo desconhecido em 'content': {str(kind)[:20]!r}.")
            content[kind] = ContentCap.from_dict(kind, cap)
        return cls(
            name=clean_label(data.get("name"), "name"),
            platform=clean_label(data.get("platform", "unknown"), "platform"),
            roles=roles,
            protocol_versions=versions,
            transports=string_list(data.get("transports"), "transports"),
            codecs=string_list(data.get("codecs"), "codecs"),
            content=content,
            features=string_list(data.get("features"), "features"),
        )
