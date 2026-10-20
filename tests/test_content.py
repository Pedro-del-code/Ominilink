import base64
import io
import unittest
from html.parser import HTMLParser

from PIL import Image

from app.capabilities import ContentCap, DeviceCapabilities
from app.codec import Message, new_message_id
from app.content import (ContentLimits, apply_adaptations, fit_image, html_to_text, inspect_image, prepare_incoming,
                         sanitize_html, sniff_mime, verify_for_receiver, wrap_document)
from app.errors import OmniLinkError, TooLarge, ValidationFailure
from app.protocols import Adaptation
from tests.helpers import DEV_A, DEV_B, image_bytes, noise_png

LIM = ContentLimits()


ALLOWED_ATTRS = {"href", "rel", "target", "title", "lang", "dir", "src", "alt", "width", "height", "colspan", "rowspan", "start"}


class _Structure(HTMLParser):
    """Lê a saída do sanitizador como um navegador leria: coleta tags e nomes de atributos."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags, self.attrs = [], []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs.extend((tag, name, value) for name, value in attrs)


class HtmlSanitizerTests(unittest.TestCase):
    def clean(self, src):
        return sanitize_html(src)[0]

    def assert_structurally_safe(self, out):
        from app.content.html_sanitizer import ALLOWED_TAGS, SAFE_SCHEMES
        parsed = _Structure()
        parsed.feed(out)
        self.assertTrue(set(parsed.tags) <= ALLOWED_TAGS, parsed.tags)
        for tag, name, value in parsed.attrs:
            self.assertIn(name, ALLOWED_ATTRS, f"atributo inesperado {name}={value!r} em <{tag}>")
            if name == "href":
                self.assertTrue(value.startswith("#") or value.split(":", 1)[0].lower() in SAFE_SCHEMES, value)
            if name == "src":
                self.assertTrue(value.startswith("data:image/"), value)

    def test_attack_vectors_are_neutralized(self):
        vectors = {
            "script": "<p>a</p><script>alert(1)</script>",
            "onerror": "<img src=x onerror=alert(1)>",
            "js-url": '<a href="javascript:alert(1)">x</a>',
            "js-url-obfuscated": '<a href="  JaVa\n\tScRiPt:alert(1)">x</a>',
            "data-url-link": '<a href="data:text/html;base64,PHNjcmlwdD4=">x</a>',
            "vbscript": '<a href="vbscript:msgbox(1)">x</a>',
            "iframe": '<iframe src="https://evil.example"></iframe>',
            "svg": "<svg onload=alert(1)><script>1</script></svg>",
            "math": "<math><mi xlink:href=javascript:alert(1)>x</mi></math>",
            "style-tag": "<style>@import 'https://evil.example/x.css'</style>",
            "style-attr": '<div style="background:url(javascript:alert(1))">x</div>',
            "form": '<form action="https://evil.example"><input name=a><button>ok</button></form>',
            "object": "<object data=x></object><embed src=x>",
            "meta-refresh": '<meta http-equiv="refresh" content="0;url=https://evil.example">',
            "base": '<base href="https://evil.example/">',
            "event-handlers": '<p onclick="a()" onmouseover="b()" onfocus="c()">x</p>',
            "template": "<template><script>1</script></template>",
            "comment-tricks": "<!--[if IE]><script>1</script><![endif]--><p>x</p>",
            "unclosed-script": "<p>ok</p><script>alert(1)",
            "attr-breakout": '<a href="https://ok.example/&quot; onclick=&quot;x()">x</a>',
            "nested-script-in-svg": "<svg><g><script>alert(1)</script></g></svg>",
            "protocol-relative": '<a href="//evil.example/x">x</a>',
        }
        forbidden = ("<script", "javascript:", "vbscript:", "onerror", "onclick", "onload", "onmouseover", "<iframe",
                     "<style", "<form", "<object", "<embed", "<svg", "<math", "<meta", "<base", "style=", "evil.example/x")
        for name, src in vectors.items():
            with self.subTest(vector=name):
                raw = self.clean(src)
                self.assert_structurally_safe(raw)      # verificação estrutural (como um navegador interpretaria)
                if name == "attr-breakout":             # o texto 'onclick' existe, mas só DENTRO do valor escapado do href
                    self.assertNotIn(' onclick', raw)
                    continue
                for token in forbidden:
                    self.assertNotIn(token, raw.lower(), f"{name}: {raw}")

    def test_legitimate_markup_is_preserved(self):
        out = self.clean('<h1>Título</h1><p>Olá <b>mundo</b> &amp; <i>todos</i></p><ul><li>um</li></ul>'
                         '<a href="https://ok.example/p?a=1&b=2" title="t">link</a><table><tr><td colspan=2>c</td></tr></table>')
        for expected in ("<h1>Título</h1>", "<b>mundo</b>", "&amp;", "<li>um</li>", 'href="https://ok.example/p?a=1&amp;b=2"',
                         'colspan="2"', "<table>"):
            self.assertIn(expected, out)
        self.assertIn('rel="noopener noreferrer nofollow"', out)

    def test_text_is_always_escaped(self):
        out = self.clean("<p>1 < 2 && 3 > 2 <3 </p>")
        self.assertNotIn("< 2", out)
        self.assertIn("&lt;", out)

    def test_misnested_and_unclosed_tags_are_rebalanced(self):
        self.assertEqual(self.clean("<b><i>x</b></i>"), "<b><i>x</i></b>")
        self.assertEqual(self.clean("<p><b>aberto"), "<p><b>aberto</b></p>")

    def test_images_only_as_data_uris(self):
        png = base64.b64encode(image_bytes()).decode()
        self.assertIn("<img", self.clean(f'<img src="data:image/png;base64,{png}" alt="ok" width="10">'))
        self.assertNotIn("<img", self.clean('<img src="https://tracker.example/p.gif">'))
        self.assertNotIn("<img", self.clean('<img src="data:image/svg+xml;base64,PHN2Zz4=">'))

    def test_removal_flag_and_limits(self):
        self.assertTrue(sanitize_html("<script>1</script>x")[1])
        self.assertFalse(sanitize_html("<p>limpo</p>")[1])
        with self.assertRaises(TooLarge):
            sanitize_html("x" * 1000, max_bytes=100)

    def test_deep_nesting_is_bounded(self):
        out = self.clean("<div>" * 500 + "x" + "</div>" * 500)
        self.assertLessEqual(out.count("<div>"), 40)

    def test_html_to_text(self):
        text = html_to_text("<h1>Título</h1><p>Olá <b>você</b></p><ul><li>a</li><li>b</li></ul><script>x()</script>")
        self.assertIn("Título", text); self.assertIn("Olá você", text); self.assertIn("- a", text)
        self.assertNotIn("x()", text)

    def test_wrapped_document_carries_restrictive_csp(self):
        doc = wrap_document("<p>x</p>", "t<>")
        self.assertIn("default-src 'none'", doc)
        self.assertNotIn("<title>t<>", doc)


class IncomingContentTests(unittest.TestCase):
    def prep(self, kind, body, encoding="utf-8", mime=None, limits=LIM):
        return prepare_incoming(kind, encoding=encoding, body=body, mime=mime, limits=limits)

    def test_text_is_normalized_and_stripped_of_controls(self):
        p = self.prep("text", "a\x1b[31mvermelho\x00\r\nfim")
        text = p.data.decode()
        self.assertNotIn("\x1b", text); self.assertNotIn("\x00", text); self.assertNotIn("\r", text)
        self.assertEqual(p.descriptor.chars, len(text))

    def test_text_validation(self):
        with self.assertRaises(ValidationFailure): self.prep("text", "   ")
        with self.assertRaises(TooLarge): self.prep("text", "x" * 50, limits=ContentLimits(max_text_chars=10))
        with self.assertRaises(ValidationFailure): self.prep("text", "ok", encoding="base64")
        with self.assertRaises(ValidationFailure): self.prep("text", 123)
        with self.assertRaises(ValidationFailure): self.prep("video", "x")

    def test_html_notes_when_sanitized(self):
        self.assertEqual(self.prep("html", "<p>ok</p><script>1</script>").notes, ["html_sanitized"])
        self.assertEqual(self.prep("html", "<p>ok</p>").notes, [])

    def test_valid_images_in_all_formats(self):
        for fmt, mime in (("PNG", "image/png"), ("JPEG", "image/jpeg"), ("GIF", "image/gif"), ("WEBP", "image/webp")):
            with self.subTest(fmt=fmt):
                p = self.prep("image", base64.b64encode(image_bytes(fmt)).decode(), "base64")
                self.assertEqual((p.mime, p.descriptor.width, p.descriptor.height), (mime, 64, 48))

    def test_invalid_images_are_rejected(self):
        good = image_bytes()
        cases = {
            "not-an-image": b"isto nao e uma imagem",
            "truncated": good[:40],
            "corrupted-body": good[:60] + b"\x00" * 30 + good[90:],
            "svg": b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
            "trailing-data-png": good + b"<script>alert(1)</script>",
            "gif-header-only": b"GIF89a" + b"\x00" * 20,
            "empty": b"",
        }
        for name, data in cases.items():
            with self.subTest(case=name), self.assertRaises(OmniLinkError) as ctx:
                self.prep("image", base64.b64encode(data).decode(), "base64")
            self.assertIn(ctx.exception.code, ("INVALID_IMAGE", "INVALID_REQUEST"))

    def test_declared_mime_must_match_content(self):
        with self.assertRaises(OmniLinkError):
            self.prep("image", base64.b64encode(image_bytes("PNG")).decode(), "base64", mime="image/jpeg")

    def test_size_dimension_and_base64_limits(self):
        with self.assertRaises(TooLarge):
            self.prep("image", base64.b64encode(noise_png()).decode(), "base64", limits=ContentLimits(max_image_bytes=1000))
        with self.assertRaises(OmniLinkError) as ctx:  # bomba de descompressão: dimensões enormes, arquivo minúsculo
            big = image_bytes("PNG", (6000, 6000), (1, 1, 1))
            self.prep("image", base64.b64encode(big).decode(), "base64", limits=ContentLimits(max_image_pixels=1_000_000, max_image_bytes=10_000_000))
        self.assertEqual(ctx.exception.code, "INVALID_IMAGE")
        with self.assertRaises(ValidationFailure): self.prep("image", "@@@não-base64@@@", "base64")
        with self.assertRaises(ValidationFailure): self.prep("image", "abc", "utf-8")

    def test_sniffing(self):
        self.assertEqual(sniff_mime(image_bytes("JPEG")), "image/jpeg")
        self.assertIsNone(sniff_mime(b"MZ\x90\x00"))


class ImageAdaptationTests(unittest.TestCase):
    def test_fit_image_downscales_transcodes_and_shrinks(self):
        data = noise_png((400, 300))
        info = inspect_image(data, max_bytes=5_000_000, max_pixels=8_000_000)
        out, new, ops = fit_image(data, info, mimes=("image/jpeg",), max_width=160, max_height=120, max_bytes=20_000)
        self.assertEqual(new.mime, "image/jpeg")
        self.assertLessEqual((new.width, new.height), (160, 120))
        self.assertLessEqual(len(out), 20_000)
        self.assertEqual(inspect_image(out, max_bytes=20_000, max_pixels=10**6).mime, "image/jpeg")
        self.assertTrue(ops[0].startswith("fit_image:image/png->image/jpeg"))

    def test_fit_image_keeps_format_when_possible_and_handles_alpha(self):
        data = image_bytes("PNG", (300, 200), (10, 20, 30, 128), "RGBA")
        info = inspect_image(data, max_bytes=5_000_000, max_pixels=8_000_000)
        _, new, _ = fit_image(data, info, mimes=("image/png", "image/jpeg"), max_width=100, max_height=100, max_bytes=100_000)
        self.assertEqual(new.mime, "image/png")
        _, new, _ = fit_image(data, info, mimes=("image/jpeg",), max_width=None, max_height=None, max_bytes=100_000)
        self.assertEqual(new.mime, "image/jpeg")

    def test_fit_image_fails_clearly_when_impossible(self):
        data = noise_png((400, 300))
        info = inspect_image(data, max_bytes=5_000_000, max_pixels=8_000_000)
        with self.assertRaises(OmniLinkError) as ctx:
            fit_image(data, info, mimes=("image/jpeg",), max_width=400, max_height=300, max_bytes=5)
        self.assertEqual(ctx.exception.code, "ADAPTATION_FAILED")

    def test_apply_adaptations_updates_message_and_hash(self):
        msg = Message(new_message_id(), DEV_A, DEV_B, "html", "text/html", b"<h1>Oi</h1><p>mundo</p>")
        old = msg.sha256
        apply_adaptations(msg, [Adaptation("html_to_text")], LIM)
        self.assertEqual((msg.kind, msg.mime), ("text", "text/plain"))
        self.assertNotEqual(msg.sha256, old)
        self.assertIn("html_to_text", msg.adapted)


class ReceiverVerificationTests(unittest.TestCase):
    caps = DeviceCapabilities("r", content={"text": ContentCap(20, max_chars=10), "image": ContentCap(5000, ("image/png",), 100, 100)},
                              transports=("sse",), codecs=("json-b64",))

    def msg(self, kind, mime, data):
        return Message(new_message_id(), DEV_A, DEV_B, kind, mime, data)

    def test_accepts_conforming_content(self):
        verify_for_receiver(self.msg("text", "text/plain", b"ok"), self.caps)
        verify_for_receiver(self.msg("image", "image/png", image_bytes("PNG", (50, 50))), self.caps)

    def test_rejects_non_conforming_content(self):
        for m in (self.msg("html", "text/html", b"<p>x</p>"), self.msg("text", "text/plain", b"x" * 30),
                  self.msg("text", "text/plain", "ação ação ação".encode()), self.msg("image", "image/jpeg", image_bytes("JPEG")),
                  self.msg("image", "image/png", image_bytes("PNG", (200, 200))), self.msg("image", "image/png", b"\x89PNG\r\n\x1a\nlixo")):
            with self.subTest(kind=m.kind, size=len(m.data)), self.assertRaises(OmniLinkError):
                verify_for_receiver(m, self.caps)


if __name__ == "__main__":
    unittest.main()
