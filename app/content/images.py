"""Validação e adaptação de imagens (PNG, JPEG, GIF, WebP) com Pillow.

Segurança: o formato é identificado pelos bytes mágicos e conferido com o decodificador; as dimensões são
checadas ANTES da decodificação (proteção contra bombas de descompressão); a imagem é decodificada por
completo para detectar arquivos corrompidos ou truncados.
"""
from __future__ import annotations

import io
import time
from dataclasses import dataclass

from PIL import Image

from ..errors import OmniLinkError, TooLarge

MIME_BY_FORMAT = {"PNG": "image/png", "JPEG": "image/jpeg", "GIF": "image/gif", "WEBP": "image/webp"}
_PIL_FORMAT = {v: k for k, v in MIME_BY_FORMAT.items()}
_LOSSY_QUALITIES = (88, 75, 60, 45, 30)
_SCALES = (1.0, 0.85, 0.7, 0.55, 0.4, 0.3, 0.2, 0.1)


@dataclass(frozen=True)
class ImageInfo:
    mime: str
    width: int
    height: int
    size: int


def sniff_mime(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _invalid(reason: str) -> OmniLinkError:
    return OmniLinkError("INVALID_IMAGE", reason=reason)


def _has_trailing_data(data: bytes, mime: str) -> bool:
    """Detecta bytes anexados após o fim lógico da imagem (técnica comum de arquivos poliglotas).

    JPEG fica de fora: câmeras gravam legitimamente dados extras (MPF/miniaturas) após o primeiro EOI.
    """
    if mime == "image/png":
        return not data.endswith(b"IEND\xaeB`\x82")
    if mime == "image/gif":
        return not data.endswith(b";")
    if mime == "image/webp":
        return int.from_bytes(data[4:8], "little") + 8 != len(data)
    return False


def inspect_image(data: bytes, *, max_bytes: int, max_pixels: int) -> ImageInfo:
    if len(data) > max_bytes:
        raise TooLarge("SIZE_LIMIT_EXCEEDED", size=len(data), limit=max_bytes, unit="bytes")
    mime = sniff_mime(data)
    if mime is None:
        raise _invalid("formato não reconhecido (permitidos: PNG, JPEG, GIF, WebP)")
    if _has_trailing_data(data, mime):
        raise _invalid("há dados extras após o fim da imagem")
    try:
        with Image.open(io.BytesIO(data)) as im:
            width, height = im.size
            if width < 1 or height < 1 or width * height > max_pixels:
                raise _invalid(f"dimensões fora do limite ({width}x{height}; máximo {max_pixels} pixels)")
            if MIME_BY_FORMAT.get(im.format or "") != mime:
                raise _invalid("o conteúdo não corresponde ao formato declarado pelos bytes iniciais")
            im.load()  # decodificação completa: detecta corrupção/truncamento
    except OmniLinkError:
        raise
    except Exception as exc:  # Pillow lança tipos variados para dados malformados
        raise _invalid(f"arquivo corrompido ({type(exc).__name__})") from exc
    return ImageInfo(mime, width, height, len(data))


def _has_alpha(im: Image.Image) -> bool:
    return im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)


def _encode(im: Image.Image, mime: str, quality: int | None) -> bytes:
    buf = io.BytesIO()
    alpha = _has_alpha(im)
    if mime == "image/jpeg":
        rgba = im.convert("RGBA")
        base = Image.new("RGB", rgba.size, (255, 255, 255))
        base.paste(rgba, mask=rgba.split()[3])
        base.save(buf, "JPEG", quality=quality or 85, optimize=True)
    elif mime == "image/webp":
        im.convert("RGBA" if alpha else "RGB").save(buf, "WEBP", quality=quality or 85, method=4)
    elif mime == "image/png":
        im.convert("RGBA" if alpha else "RGB").save(buf, "PNG", optimize=True)
    else:
        im.convert("RGB").convert("P", palette=Image.Palette.ADAPTIVE).save(buf, "GIF")
    return buf.getvalue()


def fit_image(data: bytes, info: ImageInfo, *, mimes, max_width, max_height, max_bytes,
              budget_seconds: float = 8.0) -> tuple[bytes, ImageInfo, list]:
    """Redimensiona/recodifica até caber nos limites do receptor. Falha com ADAPTATION_FAILED se impossível."""
    order = [info.mime] if info.mime in mimes else []
    order += [m for m in ("image/jpeg", "image/webp", "image/png", "image/gif") if m in mimes and m not in order]
    if not order:
        raise OmniLinkError("ADAPTATION_FAILED", reason="o receptor não aceita formato de imagem produzível")
    with Image.open(io.BytesIO(data)) as im0:
        animated = bool(getattr(im0, "is_animated", False))
        im0.load()
        base = im0.copy()
    notes = ["animation_dropped"] if animated else []
    deadline = time.monotonic() + budget_seconds
    for target in order:
        qualities = _LOSSY_QUALITIES if target in ("image/jpeg", "image/webp") else (None,)
        for scale in _SCALES:
            tw = max(1, int(min(base.width, max_width or base.width) * scale))
            th = max(1, int(min(base.height, max_height or base.height) * scale))
            candidate = base.copy()
            candidate.thumbnail((tw, th), Image.Resampling.LANCZOS)
            for quality in qualities:
                if time.monotonic() > deadline:
                    raise OmniLinkError("ADAPTATION_FAILED", reason="tempo limite excedido ao adaptar a imagem")
                out = _encode(candidate, target, quality)
                if len(out) <= max_bytes:
                    ops = [f"fit_image:{info.mime}->{target}@{candidate.width}x{candidate.height}"] + notes
                    return out, ImageInfo(target, candidate.width, candidate.height, len(out)), ops
    raise OmniLinkError("ADAPTATION_FAILED", reason=f"não foi possível reduzir a imagem para {max_bytes} bytes")
