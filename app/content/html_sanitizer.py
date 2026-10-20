"""Sanitização de HTML por lista de permissão (reconstrói o documento a partir de tokens).

Estratégia: nada do HTML original é copiado — cada texto é escapado e só tags/atributos permitidos são
emitidos. É uma camada de defesa; a outra é a apresentação em iframe com sandbox e CSP restritiva.
"""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

from ..errors import TooLarge, ValidationFailure

ALLOWED_TAGS = frozenset({
    "a", "abbr", "b", "blockquote", "br", "caption", "code", "dd", "del", "details", "div", "dl", "dt", "em",
    "figcaption", "figure", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img", "ins", "kbd", "li", "mark", "ol",
    "p", "pre", "q", "s", "samp", "small", "span", "strong", "sub", "summary", "sup", "table", "tbody", "td",
    "tfoot", "th", "thead", "tr", "u", "ul"})
VOID_TAGS = frozenset({"br", "hr", "img"})
DROP_WITH_CONTENT = frozenset({
    "script", "style", "iframe", "frame", "frameset", "object", "applet", "noscript", "template", "svg", "math",
    "form", "textarea", "select", "option", "optgroup", "button", "head", "title", "audio", "video", "canvas",
    "dialog", "xmp", "plaintext", "noembed", "noframes", "portal"})
DROP_VOID = frozenset({"meta", "link", "base", "input", "source", "track", "embed"})
BLOCK_TAGS = frozenset({"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "blockquote", "pre", "ul", "ol",
                        "table", "hr", "br", "dt", "dd", "figure", "figcaption", "details", "summary", "caption"})
GLOBAL_ATTRS = frozenset({"title", "lang", "dir"})
TAG_ATTRS = {"a": {"href"}, "img": {"src", "alt", "width", "height"}, "td": {"colspan", "rowspan"},
             "th": {"colspan", "rowspan"}, "ol": {"start"}}
SAFE_SCHEMES = frozenset({"http", "https", "mailto"})
MAX_DEPTH = 40

_DATA_IMG = re.compile(r"^data:image/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/=]+\Z", re.I)
_DIGITS = re.compile(r"^[0-9]{1,4}\Z")
_LANG = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,3}\Z")
_CTRL = re.compile(r"[\x00-\x20\x7f-\x9f]")
_TEXT_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

CSP = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"


def _clean_attr(tag: str, name: str, value: str) -> str | None:
    if name == "href":
        url = _CTRL.sub("", value)
        if not url or len(url) > 2000:
            return None
        if url.startswith("#"):
            return url
        try:
            parts = urlsplit(url)
        except ValueError:
            return None
        return url if parts.scheme.lower() in SAFE_SCHEMES else None
    if name == "src":
        value = value.strip()
        return value if len(value) <= 400_000 and _DATA_IMG.match(value) else None
    if name in ("width", "height", "colspan", "rowspan", "start"):
        return value if _DIGITS.match(value.strip()) else None
    if name in ("title", "alt"):
        return _TEXT_CTRL.sub("", value)[:300]
    if name == "lang":
        return value if _LANG.match(value) else None
    if name == "dir":
        return value if value in ("ltr", "rtl", "auto") else None
    return None


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[str] = []
        self.skip_tag: str | None = None
        self.skip_depth = 0
        self.removed = 0

    def _attrs(self, tag: str, attrs: list) -> str | None:
        allowed = GLOBAL_ATTRS | TAG_ATTRS.get(tag, set())
        parts, seen = [], set()
        for raw_name, raw_value in attrs:
            name = (raw_name or "").lower()
            if name in seen:
                continue
            seen.add(name)
            cleaned = _clean_attr(tag, name, raw_value or "") if name in allowed else None
            if cleaned is None:
                self.removed += 1
                continue
            parts.append(f' {name}="{html.escape(cleaned, quote=True)}"')
        if tag == "img" and not any(p.startswith(" src=") for p in parts):
            return None
        if tag == "a":
            parts.append(' rel="noopener noreferrer nofollow" target="_blank"')
        return "".join(parts)

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self.skip_tag:
            if tag == self.skip_tag:
                self.skip_depth += 1
            return
        if tag in DROP_VOID:
            self.removed += 1
            return
        if tag in DROP_WITH_CONTENT:
            self.removed += 1
            self.skip_tag, self.skip_depth = tag, 1
            return
        if tag not in ALLOWED_TAGS or len(self.stack) >= MAX_DEPTH:
            self.removed += 1  # tag desconhecida: descarta a tag e mantém o texto interno
            return
        rendered = self._attrs(tag, attrs)
        if rendered is None:
            self.removed += 1
            return
        self.out.append(f"<{tag}{rendered}>")
        if tag not in VOID_TAGS:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.skip_tag:
            if tag == self.skip_tag:
                self.skip_depth -= 1
                if self.skip_depth == 0:
                    self.skip_tag = None
            return
        if tag in VOID_TAGS or tag not in self.stack:
            return
        while self.stack:
            top = self.stack.pop()
            self.out.append(f"</{top}>")
            if top == tag:
                break

    def handle_data(self, data):
        if not self.skip_tag:
            self.out.append(html.escape(_TEXT_CTRL.sub("", data), quote=False))


def sanitize_html(src: str, max_bytes: int | None = None) -> tuple[str, bool]:
    """Retorna (HTML seguro, houve remoções?)."""
    if not isinstance(src, str):
        raise ValidationFailure("INVALID_REQUEST", reason="O HTML deve ser uma string.")
    if max_bytes and len(src.encode("utf-8", "replace")) > max_bytes:
        raise TooLarge("SIZE_LIMIT_EXCEEDED", size=len(src.encode("utf-8", "replace")), limit=max_bytes, unit="bytes")
    parser = _Sanitizer()
    try:
        parser.feed(src)
        parser.close()
    except Exception as exc:  # o parser da stdlib é tolerante, mas nunca confiamos em entrada externa
        raise ValidationFailure("MALFORMED_MESSAGE", reason="HTML não pôde ser analisado") from exc
    while parser.stack:
        parser.out.append(f"</{parser.stack.pop()}>")
    return "".join(parser.out), parser.removed > 0


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_tag: str | None = None
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self.skip_tag:
            if tag == self.skip_tag:
                self.skip_depth += 1
            return
        if tag in DROP_WITH_CONTENT:
            self.skip_tag, self.skip_depth = tag, 1
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.skip_tag:
            if tag == self.skip_tag:
                self.skip_depth -= 1
                if self.skip_depth == 0:
                    self.skip_tag = None
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip_tag:
            self.parts.append(_TEXT_CTRL.sub("", data))


def html_to_text(src: str) -> str:
    """Converte HTML em texto simples (adaptação para receptores que só exibem texto)."""
    extractor = _TextExtractor()
    try:
        extractor.feed(src)
        extractor.close()
    except Exception as exc:
        raise ValidationFailure("MALFORMED_MESSAGE", reason="HTML não pôde ser analisado") from exc
    text = "".join(extractor.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def wrap_document(fragment: str, title: str = "OmniLink") -> str:
    """Documento HTML completo com CSP restritiva (para salvar em disco ou usar em srcdoc de iframe sandbox)."""
    return ("<!doctype html><html lang=\"pt-BR\"><head><meta charset=\"utf-8\">"
            f"<meta http-equiv=\"Content-Security-Policy\" content=\"{CSP}\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>{html.escape(title)}</title>"
            "<style>body{font:16px/1.55 system-ui,sans-serif;margin:16px;max-width:70ch}"
            "img{max-width:100%;height:auto}table{border-collapse:collapse}td,th{border:1px solid #888;padding:4px 8px}"
            "pre{white-space:pre-wrap}</style></head><body>" + fragment + "</body></html>")
