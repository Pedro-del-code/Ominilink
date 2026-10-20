import socket
import unittest

from app.discovery import UdpResponder, browse, classify, discover
from app.discovery.udp import MAX_REPLY, MIN_PROBE, PROBE_PREFIX, _parse_reply, build_probe


def raw_exchange(port, payload, timeout=0.5):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(payload, ("127.0.0.1", port))
        return sock.recvfrom(2048)[0]
    except socket.timeout:
        return None
    finally:
        sock.close()


class UdpDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.responder = UdpResponder("notebook-lab", "http://192.168.0.10:8000", port=0, host="127.0.0.1", multicast=False).start()
        self.addCleanup(self.responder.stop)
        self.port = self.responder.port

    def test_browse_finds_the_announced_service(self):
        found = browse(timeout=0.6, port=self.port, targets=[("127.0.0.1", self.port)])
        self.assertEqual([(s.name, s.url, s.role, s.address) for s in found],
                         [("notebook-lab", "http://192.168.0.10:8000", "coordinator", "127.0.0.1")])
        self.assertEqual(found[0].classification.status, "native")

    def test_browse_without_responders_returns_empty(self):
        dead = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); dead.bind(("127.0.0.1", 0)); port = dead.getsockname()[1]
        self.addCleanup(dead.close)
        self.assertEqual(browse(timeout=0.3, port=port, targets=[("127.0.0.1", port)]), [])

    def test_unsendable_targets_do_not_crash_the_browse(self):
        found = browse(timeout=0.3, port=self.port, targets=[("255.255.255.255", 1), ("127.0.0.1", self.port)])
        self.assertEqual(len(found), 1)

    def test_probe_is_padded_and_reply_is_never_larger_than_the_probe(self):
        probe = build_probe("ab12cd34ef56ab78")
        self.assertGreaterEqual(len(probe), MIN_PROBE)
        reply = raw_exchange(self.port, probe)
        self.assertIsNotNone(reply)
        self.assertLessEqual(len(reply), MAX_REPLY)
        self.assertLessEqual(len(reply), len(probe))                 # sem amplificação

    def test_invalid_probes_get_no_answer(self):
        valid = build_probe("ab12cd34ef56ab78")
        bad = {"curta": PROBE_PREFIX + b" ab12", "prefixo-errado": b"X" * 10 + valid[10:], "nonce-nao-hex": build_probe("zzzz!!")[:MIN_PROBE],
               "sem-nonce": PROBE_PREFIX.ljust(MIN_PROBE, b" "), "nonce-enorme": build_probe("a" * 100), "lixo": b"\x00\xff" * 200}
        for name, payload in bad.items():
            self.assertIsNone(raw_exchange(self.port, payload, 0.3), name)

    def test_per_source_rate_limit(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); sock.settimeout(0.4)
        self.addCleanup(sock.close)
        for _ in range(40):
            sock.sendto(build_probe("ab12cd34ef56ab78"), ("127.0.0.1", self.port))
        replies = 0
        try:
            while True:
                sock.recvfrom(2048); replies += 1
        except socket.timeout:
            pass
        self.assertTrue(1 <= replies <= 8, replies)

    def test_replies_from_strangers_are_validated(self):
        nonce = "ab12cd34ef56ab78"
        good = b'{"service":"_omnilink._tcp","v":1,"name":"ok","url":"http://10.0.0.5:8000","role":"receiver","nonce":"%s"}' % nonce.encode()
        self.assertEqual(_parse_reply(good, "10.0.0.5", nonce).role, "receiver")
        variants = {
            "nonce-errado": good.replace(nonce.encode(), b"0" * 16), "servico-errado": good.replace(b"_omnilink._tcp", b"_evil._tcp"),
            "versao": good.replace(b'"v":1', b'"v":2'), "url-javascript": good.replace(b"http://10.0.0.5:8000", b"javascript:alert(1)"),
            "url-com-credenciais": good.replace(b"http://10.0.0.5:8000", b"http://user:pw@evil.example"),
            "papel": good.replace(b'"receiver"', b'"admin"'), "nome-com-controle": good.replace(b'"ok"', b'"a\\u001b[2Jb"'),
            "nao-json": b"isto nao e json", "nao-objeto": b"[1,2]", "grande": good + b" " * 300,
        }
        for name, data in variants.items():
            self.assertIsNone(_parse_reply(data, "10.0.0.5", nonce), name)

    def test_constructor_validates_what_it_announces(self):
        for args in (("nome\x1b", "http://a.b:1", "coordinator"), ("n", "ftp://a.b", "coordinator"), ("n", "http://a.b", "admin"),
                     ("n", "http://" + "a" * 110, "coordinator"), ("n" * 60, "http://a.b", "coordinator")):
            with self.assertRaises(ValueError):
                UdpResponder(*args, port=0)

    def test_stop_is_idempotent_and_closes_the_socket(self):
        self.responder.stop(); self.responder.stop()
        self.assertIsNone(raw_exchange(self.port, build_probe("ab12cd34ef56ab78"), 0.3))


class CatalogAndManagerTests(unittest.TestCase):
    def test_classification_distinguishes_native_known_and_unknown(self):
        self.assertEqual(classify("_omnilink._tcp").status, "native")
        for service in ("_googlecast._tcp", "_airplay._tcp", "urn:dial-multiscreen-org:service:dial:1", "_matter._tcp"):
            self.assertEqual(classify(service).status, "known-unsupported", service)
        for service in ("_ipp._tcp", "_http._tcp"):
            self.assertEqual(classify(service).status, "informational", service)
        unknown = classify("_algo-novo._tcp")
        self.assertEqual((unknown.family, unknown.status), ("desconhecido", "unknown"))

    def test_discovery_report_explains_limits_honestly(self):
        class FakeClient:
            def devices(self):
                return [{"id": "dev_" + "1" * 16, "name": "sala", "online": True, "transports": ["sse"], "codecs": ["json-b64"], "roles": ["receiver"]}]
        report = discover(FakeClient())
        text = report.explain()
        self.assertIn("sala", text); self.assertIn("online", text)
        self.assertIn("mDNS", text)                         # deixa claro que mDNS/SSDP não são consultados
        self.assertEqual(discover(None).registered, [])

    def test_lan_discovery_through_the_manager(self):
        responder = UdpResponder("coord", "http://192.168.1.2:8000", port=0, host="127.0.0.1", multicast=False).start()
        self.addCleanup(responder.stop)
        report = discover(None, lan=True, lan_port=responder.port, lan_timeout=0.5, lan_targets=[("127.0.0.1", responder.port)])
        self.assertEqual(len(report.lan), 1)
        self.assertTrue(any("não são autenticados" in n for n in report.notes))
        self.assertIn("coord", report.explain())


if __name__ == "__main__":
    unittest.main()
