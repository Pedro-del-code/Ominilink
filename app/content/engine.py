"""Motor de conteúdo: valida o que chega, descreve para a negociação e adapta ao receptor."""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field

from ..capabilities.models import KIND_MIMES, ContentDescriptor, DeviceCapabilities
from ..codec.message import Message
from ..errors import OmniLinkError, TooLarge, Unprocessable, ValidationFailure
from .html_sanitizer import html_to_text, sanitize_html
from .images import ImageInfo, fit_image, inspect_image
from .text import normalize_text


@dataclass(frozen=True)
class ContentLimits:
    max_text_chars: int = 20_000
    max_html_bytes: int = 200_000
    max_image_bytes: int = 2_000_000
    max_image_pixels: int = 8_000_000


@dataclass
class Prepared:
    kind: str
    mime: str
    data: bytes
    descriptor: ContentDescriptor
    notes: list = field(default_factory=list)


def prepare_incoming(kind: str, *, encoding: str, body: str, mime: str | None, limits: ContentLimits) -> Prepared:
    """Valida e normaliza o conteúdo bruto recebido pela API."""
    if kind == "text":
        if encoding != "utf-8":
            raise ValidationFailure("INVALID_REQUEST", reason="Texto deve usar encoding 'utf-8'.")
        text = normalize_text(body, limits.max_text_chars)
        data = text.encode("utf-8")
        return Prepared("text", "text/plain", data, ContentDescriptor("text", "text/plain", len(data), chars=len(text)))
    if kind == "html":
        if encoding != "utf-8":
            raise ValidationFailure("INVALID_REQUEST", reason="HTML deve usar encoding 'utf-8'.")
        clean, changed = sanitize_html(body, limits.max_html_bytes)
        data = clean.encode("utf-8")
        return Prepared("html", "text/html", data, ContentDescriptor("html", "text/html", len(data)),
                        ["html_sanitized"] if changed else [])
    if kind == "image":
        if encoding != "base64":
            raise ValidationFailure("INVALID_REQUEST", reason="Imagem deve usar encoding 'base64'.")
        if not isinstance(body, str) or len(body) > (limits.max_image_bytes * 4) // 3 + 8:
            raise TooLarge("SIZE_LIMIT_EXCEEDED", size=len(body) if isinstance(body, str) else 0,
                           limit=limits.max_image_bytes, unit="bytes (Base64 decodificado)")
        try:
            data = base64.b64decode(body, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValidationFailure("INVALID_REQUEST", reason="Base64 inválido.") from exc
        info = inspect_image(data, max_bytes=limits.max_image_bytes, max_pixels=limits.max_image_pixels)
        if mime and mime != info.mime:
            raise OmniLinkError("INVALID_IMAGE", reason=f"MIME declarado ({mime}) difere do conteúdo ({info.mime})")
        return Prepared("image", info.mime, data, ContentDescriptor("image", info.mime, len(data), info.width, info.height))
    raise ValidationFailure("INVALID_REQUEST", reason="Campo 'kind' deve ser 'text', 'image' ou 'html'.")


def apply_adaptations(msg: Message, adaptations, limits: ContentLimits) -> None:
    """Executa o plano de adaptação sobre a mensagem (modifica-a no lugar)."""
    for step in adaptations:
        if step.op == "html_to_text":
            text = html_to_text(msg.data.decode("utf-8"))
            if not text:
                raise OmniLinkError("ADAPTATION_FAILED", reason="o HTML não contém texto exibível")
            msg.data, msg.kind, msg.mime = text.encode("utf-8"), "text", "text/plain"
            msg.adapted.append("html_to_text")
        elif step.op == "fit_image":
            p = step.params
            info = inspect_image(msg.data, max_bytes=limits.max_image_bytes, max_pixels=limits.max_image_pixels)
            data, new_info, ops = fit_image(msg.data, info, mimes=tuple(p["mimes"]), max_width=p.get("max_width"),
                                            max_height=p.get("max_height"), max_bytes=p["max_bytes"])
            msg.data, msg.mime = data, new_info.mime
            msg.adapted.extend(ops)
        else:
            raise OmniLinkError("ADAPTATION_FAILED", reason=f"operação desconhecida: {step.op}")
        msg.refresh_hash()


def verify_for_receiver(msg: Message, caps: DeviceCapabilities, *, max_pixels: int = 8_000_000) -> ImageInfo | None:
    """Confere se a mensagem final cabe nas capacidades do receptor (usado no coordenador e no próprio receptor)."""
    cap = caps.content.get(msg.kind)
    if cap is None or msg.mime not in (cap.mimes or KIND_MIMES[msg.kind]):
        raise Unprocessable("CONTENT_UNSUPPORTED", kind=msg.kind)
    if len(msg.data) > cap.max_bytes:
        raise TooLarge("SIZE_LIMIT_EXCEEDED", size=len(msg.data), limit=cap.max_bytes, unit="bytes")
    if msg.kind == "text":
        chars = len(msg.data.decode("utf-8"))
        if cap.max_chars and chars > cap.max_chars:
            raise TooLarge("SIZE_LIMIT_EXCEEDED", size=chars, limit=cap.max_chars, unit="caracteres")
    if msg.kind == "image":
        info = inspect_image(msg.data, max_bytes=cap.max_bytes, max_pixels=max_pixels)
        if (cap.max_width and info.width > cap.max_width) or (cap.max_height and info.height > cap.max_height):
            raise OmniLinkError("ADAPTATION_FAILED", reason="dimensões da imagem excedem o limite do receptor")
        return info
    return None
