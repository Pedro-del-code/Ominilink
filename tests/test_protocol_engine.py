import json
import unittest

from app.capabilities import ContentCap, ContentDescriptor, DeviceCapabilities
from app.capabilities.profiles import (browser_receiver, constrained_receiver, legacy_receiver, python_receiver,
                                       python_sender)
from app.errors import ValidationFailure
from app.protocols import (CodecSpec, NegotiationContext, TransportSpec, default_registry, negotiate)

TEXT = ContentDescriptor("text", "text/plain", 20, chars=20)
HTML = ContentDescriptor("html", "text/html", 500)
SMALL_IMG = ContentDescriptor("image", "image/png", 10_000, 64, 48)
BIG_IMG = ContentDescriptor("image", "image/png", 900_000, 640, 480)
SENDER = python_sender()


def codes(result, severity=None):
    return [d.code for d in result.diagnostics if severity is None or d.severity == severity]


class CapabilityDocumentTests(unittest.TestCase):
    def test_roundtrip_of_all_profiles(self):
        for factory in (python_receiver, python_sender, browser_receiver, constrained_receiver, legacy_receiver):
            caps = factory()
            self.assertEqual(DeviceCapabilities.from_dict(json.loads(json.dumps(caps.to_dict()))), caps)

    def test_strict_validation_rejects_bad_documents(self):
        good = python_receiver().to_dict()
        bad_docs = [
            [], {"name": ""}, {**good, "roles": ["admin"]}, {**good, "roles": []},
            {**good, "protocol_versions": []}, {**good, "protocol_versions": ["1"]},
            {**good, "transports": ["a b"]}, {**good, "transports": ["x"] * 40},
            {**good, "content": {"video": {"max_bytes": 1}}},
            {**good, "content": {"text": {"max_bytes": -1}}},
            {**good, "content": {"image": {"max_bytes": 10, "mimes": ["image/svg+xml"]}}},
            {**good, "name": "x" * 100}, {**good, "name": "ok\x00bad"[:0] or 5},
        ]
        for doc in bad_docs:
            with self.subTest(doc=str(doc)[:60]), self.assertRaises(ValidationFailure):
                DeviceCapabilities.from_dict(doc)

    def test_unknown_top_level_keys_are_ignored_for_forward_compatibility(self):
        doc = python_receiver().to_dict(); doc["futuro"] = {"x": 1}
        self.assertEqual(DeviceCapabilities.from_dict(doc), python_receiver())


class SelectionTests(unittest.TestCase):
    def pick(self, receiver, content, **ctx):
        res = negotiate(SENDER, receiver, content, NegotiationContext(**ctx))
        self.assertTrue(res.compatible, codes(res))
        return res.selection.transport, res.selection.codec

    def test_small_payloads_prefer_low_latency_transport(self):
        self.assertEqual(self.pick(python_receiver(), TEXT), ("sse", "json-b64"))

    def test_large_payloads_prefer_efficient_codec(self):
        self.assertEqual(self.pick(python_receiver(), ContentDescriptor("image", "image/png", 1_500_000, 640, 480)),
                         ("http-longpoll", "binary-frame"))

    def test_browser_without_binary_codec_stays_on_json(self):
        self.assertEqual(self.pick(browser_receiver(), ContentDescriptor("image", "image/png", 1_500_000, 640, 480)),
                         ("sse", "json-b64"))

    def test_active_session_wins_over_static_preference(self):
        # sem sessão ativa, texto curto vai por SSE; com long-poll ativo o receptor só será alcançado por ele
        # (e, dentro do long-poll, o codec binário é o mais eficiente que ambos entendem)
        self.assertEqual(self.pick(python_receiver(), TEXT), ("sse", "json-b64"))
        self.assertEqual(self.pick(python_receiver(), TEXT, active_transports=frozenset({"http-longpoll"})),
                         ("http-longpoll", "binary-frame"))
        self.assertEqual(self.pick(browser_receiver(), TEXT, active_transports=frozenset({"http-longpoll"})),
                         ("http-longpoll", "json-b64"))

    def test_binary_codec_is_never_paired_with_sse(self):
        res = negotiate(SENDER, python_receiver(), BIG_IMG)
        self.assertTrue(all(not (c.get("transport") == "sse" and c.get("codec") == "binary-frame") for c in res.considered if "score" in c))
        self.assertIn("CODEC_NEEDS_BINARY_TRANSPORT", codes(res, "info"))

    def test_result_is_json_serializable(self):
        json.dumps(negotiate(SENDER, python_receiver(), BIG_IMG).to_dict())


class AdaptationTests(unittest.TestCase):
    def test_image_is_adapted_for_constrained_receiver(self):
        res = negotiate(SENDER, constrained_receiver(), BIG_IMG)
        self.assertTrue(res.compatible)
        step = res.selection.adaptations[0]
        self.assertEqual(step.op, "fit_image")
        self.assertEqual(set(step.params["reasons"]), {"transcode", "downscale", "shrink"})
        self.assertIn("ADAPTATION_APPLIED", codes(res))

    def test_adaptation_can_be_forbidden(self):
        res = negotiate(SENDER, constrained_receiver(), BIG_IMG, NegotiationContext(allow_adaptation=False))
        self.assertFalse(res.compatible)
        self.assertIn("CONTENT_NEEDS_ADAPTATION", codes(res, "error"))

    def test_html_falls_back_to_text_when_only_text_is_supported(self):
        res = negotiate(SENDER, constrained_receiver(), HTML)
        self.assertEqual([a.op for a in res.selection.adaptations], ["html_to_text"])

    def test_unsupported_content_has_alternative(self):
        res = negotiate(SENDER, DeviceCapabilities("so-texto", content={"text": ContentCap(1000)}, transports=("sse",),
                                                   codecs=("json-b64",)), SMALL_IMG)
        self.assertFalse(res.compatible)
        self.assertIn("CONTENT_UNSUPPORTED", codes(res, "error"))
        self.assertTrue(res.alternatives)

    def test_text_over_receiver_limit(self):
        res = negotiate(SENDER, constrained_receiver(), ContentDescriptor("text", "text/plain", 3000, chars=3000))
        self.assertIn("SIZE_LIMIT_EXCEEDED", codes(res, "error"))

    def test_image_already_compatible_needs_no_adaptation(self):
        res = negotiate(SENDER, python_receiver(), SMALL_IMG)
        self.assertEqual(res.selection.adaptations, [])


class IncompatibilityTests(unittest.TestCase):
    def test_legacy_device_gets_a_full_explanation(self):
        res = negotiate(SENDER, legacy_receiver(), TEXT)
        self.assertFalse(res.compatible)
        self.assertIsNone(res.selection)
        self.assertTrue({"PROTOCOL_VERSION_MISMATCH", "NO_COMMON_TRANSPORT"} <= set(codes(res, "error")))
        self.assertIn("TRANSPORT_NOT_IMPLEMENTED", codes(res, "warning"))
        self.assertTrue(res.alternatives)
        self.assertTrue(any(c.get("rejected") == "TRANSPORT_NOT_IMPLEMENTED" for c in res.considered))

    def test_role_mismatch(self):
        res = negotiate(python_receiver(), python_receiver(), TEXT)  # "emissor" só recebe
        self.assertIn("ROLE_MISMATCH", codes(res, "error"))
        res = negotiate(SENDER, python_sender(), TEXT)
        self.assertIn("ROLE_MISMATCH", codes(res, "error"))

    def test_no_transport_advertised_and_unknown_transport(self):
        caps = DeviceCapabilities("sem-transporte", content={"text": ContentCap(1000)}, codecs=("json-b64",))
        self.assertIn("NO_TRANSPORT_ADVERTISED", codes(negotiate(SENDER, caps, TEXT), "error"))
        caps = DeviceCapabilities("misto", content={"text": ContentCap(1000)}, codecs=("json-b64",),
                                  transports=("pombo-correio", "http-longpoll"))
        res = negotiate(SENDER, caps, TEXT)
        self.assertTrue(res.compatible)
        self.assertEqual(res.selection.transport, "http-longpoll")
        self.assertIn("TRANSPORT_UNKNOWN", codes(res, "warning"))

    def test_no_common_codec(self):
        caps = DeviceCapabilities("cbor-only", content={"text": ContentCap(1000)}, transports=("sse",), codecs=("cbor",))
        res = negotiate(SENDER, caps, TEXT)
        self.assertIn("NO_COMMON_CODEC", codes(res, "error"))
        self.assertIn("CODEC_UNKNOWN", codes(res, "warning"))

    def test_tls_policy(self):
        insecure = negotiate(SENDER, python_receiver(), TEXT, NegotiationContext(require_secure=True, channel_secure=False))
        self.assertFalse(insecure.compatible)
        self.assertIn("TLS_REQUIRED", codes(insecure, "error"))
        secure = negotiate(SENDER, python_receiver(), TEXT, NegotiationContext(require_secure=True, channel_secure=True))
        self.assertTrue(secure.compatible)

    def test_version_mismatch_alone(self):
        caps = python_receiver(); caps.protocol_versions = (3,)
        res = negotiate(SENDER, caps, TEXT)
        self.assertFalse(res.compatible)
        self.assertEqual(codes(res, "error"), ["PROTOCOL_VERSION_MISMATCH"])


class ExtensibilityTests(unittest.TestCase):
    def test_new_transport_and_codec_plug_in_without_changing_the_engine(self):
        registry = default_registry()
        registry.register_transport(TransportSpec("ws", score=90, supports_binary=True, implemented=True))
        registry.register_codec(CodecSpec("cbor", score=95, binary=True))
        receiver = DeviceCapabilities("futuro", content={"text": ContentCap(1000)}, transports=("ws", "sse"),
                                      codecs=("cbor", "json-b64"))
        res = negotiate(SENDER, receiver, TEXT, registry=registry)
        self.assertTrue(res.compatible)
        self.assertEqual((res.selection.transport, res.selection.codec), ("ws", "cbor"))
        # sem o plugin, o mesmo receptor cai no que existe:
        res = negotiate(SENDER, receiver, TEXT)
        self.assertEqual((res.selection.transport, res.selection.codec), ("sse", "json-b64"))


if __name__ == "__main__":
    unittest.main()
