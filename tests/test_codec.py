import json
import struct
import unittest
import zlib

from app.codec import (BinaryFrameCodec, JsonB64Codec, Message, available_codecs, get_codec, new_message_id,
                       register_codec)
from app.codec import _CODECS
from app.codec.binary_codec import MAGIC
from app.errors import OmniLinkError, TooLarge, ValidationFailure
from tests.helpers import DEV_A, DEV_B, image_bytes, noise_png

MAX = 2_000_000


def make_msg(kind="image", mime="image/png", data=None, **kw):
    return Message(new_message_id(), DEV_A, DEV_B, kind, mime, data if data is not None else image_bytes(), **kw)


class RoundTripTests(unittest.TestCase):
    def check(self, codec_name, msg):
        codec = get_codec(codec_name)
        out = codec.decode(codec.encode(msg), max_bytes=MAX)
        self.assertEqual((out.id, out.sender, out.recipient, out.kind, out.mime), (msg.id, msg.sender, msg.recipient, msg.kind, msg.mime))
        self.assertEqual((out.data, out.sha256, out.filename, out.adapted), (msg.data, msg.sha256, msg.filename, msg.adapted))

    def test_image_roundtrip_both_codecs(self):
        for name in ("json-b64", "binary-frame"):
            with self.subTest(codec=name):
                self.check(name, make_msg(filename="foto.png", adapted=["fit_image"]))

    def test_unicode_text_roundtrip_both_codecs(self):
        text = "Olá, ação! 你好 🙂\nsegunda linha\ttab".encode("utf-8")
        for name in ("json-b64", "binary-frame"):
            with self.subTest(codec=name):
                self.check(name, make_msg("text", "text/plain", text))

    def test_html_roundtrip(self):
        self.check("binary-frame", make_msg("html", "text/html", b"<p>ol\xc3\xa1</p>"))

    def test_json_is_text_and_binary_is_bytes(self):
        msg = make_msg()
        self.assertIsInstance(JsonB64Codec().encode(msg), str)
        self.assertIsInstance(BinaryFrameCodec().encode(msg), bytes)

    def test_binary_avoids_base64_overhead(self):
        msg = make_msg(data=noise_png())
        j, b = len(JsonB64Codec().encode(msg)), len(BinaryFrameCodec().encode(msg))
        self.assertLess(b, j * 0.8, f"binário={b} json={j}")

    def test_binary_frame_layout(self):
        msg = make_msg()
        frame = BinaryFrameCodec().encode(msg)
        magic, version, flags, reserved, hlen, plen = struct.unpack_from(">4sBBHII", frame)
        self.assertEqual((magic, version, flags, reserved, plen), (MAGIC, 1, 0, 0, len(msg.data)))
        self.assertEqual(len(frame), 16 + hlen + plen)
        self.assertEqual(frame[16 + hlen:], msg.data)
        self.assertEqual(json.loads(frame[16:16 + hlen])["encoding"], "binary")


class BinaryRejectionTests(unittest.TestCase):
    def setUp(self):
        self.codec = BinaryFrameCodec()
        self.msg = make_msg()
        self.frame = self.codec.encode(self.msg)

    def rejects(self, frame, code="MALFORMED_MESSAGE", max_bytes=MAX):
        with self.assertRaises(OmniLinkError) as ctx:
            self.codec.decode(frame, max_bytes=max_bytes)
        self.assertEqual(ctx.exception.code, code)

    def test_truncated_and_empty(self):
        self.rejects(b"")
        self.rejects(self.frame[:10])
        self.rejects(self.frame[:-1])

    def test_trailing_bytes(self):
        self.rejects(self.frame + b"x")

    def test_bad_magic_version_and_reserved(self):
        self.rejects(b"XXXX" + self.frame[4:])
        self.rejects(self.frame[:4] + bytes([9]) + self.frame[5:])
        self.rejects(self.frame[:5] + bytes([1]) + self.frame[6:])
        self.rejects(self.frame[:6] + b"\x00\x01" + self.frame[8:])

    def test_header_length_limits(self):
        zero = bytearray(self.frame); zero[8:12] = (0).to_bytes(4, "big")
        self.rejects(bytes(zero))
        huge = bytearray(self.frame); huge[8:12] = (10**6).to_bytes(4, "big")
        self.rejects(bytes(huge))

    def test_payload_over_limit(self):
        with self.assertRaises(TooLarge):
            self.codec.decode(self.frame, max_bytes=10)

    def test_corrupted_payload_detected_by_hash(self):
        flipped = self.frame[:-1] + bytes([self.frame[-1] ^ 0x01])
        self.rejects(flipped, "INTEGRITY_FAILED")

    def test_str_frame_rejected(self):
        self.rejects("texto")

    def test_header_not_json_or_wrong_encoding(self):
        header = b"not json"
        frame = struct.pack(">4sBBHII", MAGIC, 1, 0, 0, len(header), 0) + header
        self.rejects(frame)
        meta = self.msg.meta(); meta["encoding"] = "base64"
        header = json.dumps(meta).encode()
        self.rejects(struct.pack(">4sBBHII", MAGIC, 1, 0, 0, len(header), len(self.msg.data)) + header + self.msg.data)


class JsonRejectionTests(unittest.TestCase):
    def setUp(self):
        self.codec = JsonB64Codec()
        self.msg = make_msg()
        self.doc = json.loads(self.codec.encode(self.msg))

    def rejects(self, frame, code="MALFORMED_MESSAGE"):
        with self.assertRaises(OmniLinkError) as ctx:
            self.codec.decode(frame if isinstance(frame, (str, bytes)) else json.dumps(frame), max_bytes=MAX)
        self.assertEqual(ctx.exception.code, code)

    def mutated(self, **changes):
        doc = dict(self.doc); doc.update(changes)
        return doc

    def test_invalid_json_and_non_object(self):
        self.rejects("{não é json")
        self.rejects("[1, 2, 3]")
        self.rejects(b"\xff\xfe\x00")

    def test_missing_or_bad_fields(self):
        doc = dict(self.doc); del doc["body"]
        self.rejects(doc)
        self.rejects(self.mutated(type="ack"))
        self.rejects(self.mutated(v=2))
        self.rejects(self.mutated(id="x"))
        self.rejects(self.mutated(kind="video"))
        self.rejects(self.mutated(mime="image/svg+xml"))
        self.rejects(self.mutated(encoding="hex"))
        self.rejects(self.mutated(ts="agora"))
        self.rejects(self.mutated(adapted=["a"] * 20))

    def test_base64_must_be_strict(self):
        self.rejects(self.mutated(body="@@@@"))
        self.rejects(self.mutated(body=self.doc["body"][:-1]))

    def test_integrity_checks(self):
        self.rejects(self.mutated(sha256="0" * 64), "INTEGRITY_FAILED")
        self.rejects(self.mutated(size=self.doc["size"] + 1), "INTEGRITY_FAILED")
        self.rejects(self.mutated(sha256="ZZ"), "INTEGRITY_FAILED")

    def test_lone_surrogate_in_text_is_rejected(self):
        text = Message(new_message_id(), DEV_A, DEV_B, "text", "text/plain", b"ok")
        doc = json.loads(self.codec.encode(text)); doc["body"] = "x"
        self.rejects(json.dumps(doc).replace('"x"', '"\\ud800"'))

    def test_oversized_frame(self):
        with self.assertRaises(OmniLinkError):
            self.codec.decode("{" + " " * (MAX * 3 + 9000) + "}", max_bytes=MAX)


class RegistryTests(unittest.TestCase):
    def test_builtin_codecs_and_unknown(self):
        self.assertTrue({"json-b64", "binary-frame"} <= set(available_codecs()))
        with self.assertRaises(ValidationFailure):
            get_codec("nao-existe")

    def test_plugin_codec_without_touching_the_core(self):
        class ZlibCodec:
            """Plugin de exemplo: JSON comprimido com zlib (com proteção contra bomba de descompressão)."""
            name, binary = "json-zlib", True

            def encode(self, msg):
                return zlib.compress(JsonB64Codec().encode(msg).encode("utf-8"))

            def decode(self, frame, *, max_bytes):
                d = zlib.decompressobj()
                raw = d.decompress(frame, max_bytes * 3 + 8192)
                if d.unconsumed_tail:
                    raise ValidationFailure("MALFORMED_MESSAGE", reason="descompressão excede o limite")
                return JsonB64Codec().decode(raw, max_bytes=max_bytes)

        register_codec(ZlibCodec())
        try:
            msg = make_msg()
            out = get_codec("json-zlib").decode(get_codec("json-zlib").encode(msg), max_bytes=MAX)
            self.assertEqual(out.data, msg.data)
            with self.assertRaises(ValidationFailure):  # bomba de descompressão
                get_codec("json-zlib").decode(zlib.compress(b" " * 50_000_000), max_bytes=1000)
        finally:
            _CODECS.pop("json-zlib", None)


if __name__ == "__main__":
    unittest.main()
