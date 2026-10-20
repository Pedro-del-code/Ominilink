import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.client import cli
from app.client.api import OmniLinkClient
from tests.helpers import ENROLL_KEY, ServerThread, image_bytes, make_settings


class CliCase(unittest.TestCase):
    def setUp(self):
        self.server = ServerThread(make_settings()).__enter__()
        self.addCleanup(self.server.__exit__)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def register(self, name, profile):
        path = self.dir / f"{name}.json"
        with mock.patch.dict(os.environ, {"OMNILINK_ENROLLMENT_KEY": ENROLL_KEY}):
            code, out = self.run_cli("register", "--server", self.server.url, "--name", name, "--profile", profile, "--out", str(path))
        self.assertEqual(code, 0, out)
        return path, json.loads(path.read_text())

    def cli(self, path, *argv):
        return self.run_cli(argv[0], "-c", str(path), "--retries", "2", *argv[1:])


class RegistrationTests(CliCase):
    def test_credentials_are_private_and_the_token_is_never_printed(self):
        path = self.dir / "r.json"
        with mock.patch.dict(os.environ, {"OMNILINK_ENROLLMENT_KEY": ENROLL_KEY}):
            code, out = self.run_cli("register", "--server", self.server.url, "--name", "sala", "--profile", "python-receiver", "--out", str(path))
        creds = json.loads(path.read_text())
        self.assertEqual(code, 0)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertNotIn(creds["token"], out); self.assertNotIn(ENROLL_KEY, out)
        self.assertRegex(creds["device_id"], r"^dev_[0-9a-f]{16}$")
        self.assertIn(creds["device_id"], out)

    def test_tilde_in_the_credentials_path_is_expanded(self):
        self.assertEqual(cli._creds_path("~/x/y.json"), Path.home() / "x" / "y.json")

    def test_wrong_enrollment_key_fails_cleanly(self):
        with mock.patch.dict(os.environ, {"OMNILINK_ENROLLMENT_KEY": "chave-errada-errada"}):
            code, out = self.run_cli("register", "--server", self.server.url, "--name", "x", "--out", str(self.dir / "x.json"))
        self.assertEqual(code, 1)
        self.assertIn("ENROLLMENT_DENIED", out)
        self.assertFalse((self.dir / "x.json").exists())

    def test_missing_credentials_file_is_explained(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli("devices", "-c", str(self.dir / "nao-existe.json"))
        self.assertIn("register", str(ctx.exception))

    def test_unreachable_server_returns_a_connection_error_code(self):
        path, creds = self.register("r", "python-receiver")
        creds["server"] = "http://127.0.0.1:9"
        path.write_text(json.dumps(creds))
        code, out = self.cli(path, "devices", "--max-delay", "0.01")
        self.assertEqual(code, 4)
        self.assertIn("Falha de conexão", out)


class WorkflowTests(CliCase):
    def test_discover_negotiate_send_and_grant_flow(self):
        sender, sc = self.register("emissor", "python-sender")
        receiver, rc = self.register("receptor", "python-receiver")
        _, text = self.cli(sender, "devices")
        self.assertIn("receptor", text); self.assertIn("offline", text)

        img = self.dir / "foto.png"; img.write_bytes(image_bytes("PNG", (320, 200)))
        code, text = self.cli(sender, "negotiate", "--to", rc["device_id"], "--kind", "image", "--file", str(img))
        self.assertEqual(code, 0); self.assertIn("Compatível", text); self.assertIn("transporte=", text)

        code, text = self.cli(sender, "send", "--to", rc["device_id"], "text", "olá")
        self.assertEqual(code, 3)
        self.assertIn("Aguardando autorização", text); self.assertIn(sc["device_id"], text)

        code, text = self.cli(receiver, "grant", "list")
        self.assertIn("Pedidos pendentes", text); self.assertIn(sc["device_id"], text)
        code, _ = self.cli(receiver, "grant", "allow", sc["device_id"], "--kinds", "text", "html")
        self.assertEqual(code, 0)

        code, text = self.cli(sender, "send", "--to", rc["device_id"], "text", "olá", "--wait", "0.3")
        self.assertIn("Aceita: msg_", text)
        self.assertEqual(code, 4)                                    # ninguém recebeu ainda: a fila guarda
        self.assertIn("Status final: queued", text); self.assertIn("RECEIVER_OFFLINE", text)

        page = self.dir / "p.html"; page.write_text("<p>oi</p><script>x()</script>")
        code, text = self.cli(sender, "send", "--to", rc["device_id"], "html", str(page))
        self.assertEqual(code, 0); self.assertIn("Aceita", text)

        code, text = self.cli(receiver, "grant", "revoke", sc["device_id"])
        self.assertIn("Revogada", text)
        self.assertEqual(self.cli(sender, "send", "--to", rc["device_id"], "text", "x")[0], 3)

    def test_adaptation_and_incompatibility_are_reported(self):
        sender, sc = self.register("emissor", "python-sender")
        small, small_c = self.register("painel", "constrained-receiver")
        legacy, legacy_c = self.register("antigo", "legacy-receiver")
        for rc in (small, legacy):
            self.cli(rc, "grant", "allow", sc["device_id"], "--kinds", "text", "image")
        img = self.dir / "grande.png"; img.write_bytes(image_bytes("PNG", (800, 600)))
        code, text = self.cli(sender, "send", "--to", small_c["device_id"], "image", str(img))
        self.assertEqual(code, 0); self.assertIn("ADAPTATION_APPLIED", text); self.assertIn("fit_image", text)
        code, text = self.cli(sender, "send", "--to", small_c["device_id"], "image", str(img), "--no-adapt")
        self.assertEqual(code, 2); self.assertIn("CONTENT_NEEDS_ADAPTATION", text)
        code, text = self.cli(sender, "send", "--to", legacy_c["device_id"], "text", "alô")
        self.assertEqual(code, 2)
        for expected in ("INCOMPATÍVEL", "PROTOCOL_VERSION_MISMATCH", "NO_COMMON_TRANSPORT", "Alternativas propostas", "sugestão"):
            self.assertIn(expected, text)

    def test_grant_decisions_and_self_revocation(self):
        sender, sc = self.register("emissor", "python-sender")
        receiver, rc = self.register("receptor", "python-receiver")
        self.cli(sender, "send", "--to", rc["device_id"], "text", "x")
        request_id = OmniLinkClient(rc["server"], rc["token"]).grants()["requests"][0]["id"]
        code, text = self.cli(receiver, "grant", "deny", request_id)
        self.assertEqual(code, 0); self.assertIn("denied", text)
        self.assertEqual(self.run_cli("grant", "-c", str(receiver), "approve")[0], 1)       # falta o id do pedido
        code, _ = self.cli(receiver, "self-revoke")
        self.assertEqual(code, 0)
        code, text = self.cli(receiver, "devices")
        self.assertEqual(code, 1); self.assertIn("DEVICE_REVOKED", text)

    def test_explain_service_and_discover(self):
        code, text = self.run_cli("explain-service", "_googlecast._tcp")
        self.assertIn("Google Cast", text); self.assertIn("known-unsupported", text)
        self.assertIn("desconhecido", self.run_cli("explain-service", "_xyz._tcp")[1])
        receiver, _ = self.register("tv-sala", "browser-receiver")
        code, text = self.cli(receiver, "discover")
        self.assertIn("tv-sala", text); self.assertIn("mDNS", text)


class ReceiveCommandTests(CliCase):
    """`receive` bloqueia; aqui verificamos apenas as escolhas (transporte/codec/política) feitas pela CLI."""

    def launch(self, profile, *extra):
        path, creds = self.register("rx", profile)
        captured = {}

        class FakeRunner:
            def __init__(self, client, service, **kw):
                captured.update(kw); captured["policy"] = service.grant_policy
            def run(self):
                captured["ran"] = True

        with mock.patch.object(cli, "ReceiverRunner", FakeRunner):
            code, out = self.cli(path, "receive", "--inbox", str(self.dir / "inbox"), *extra)
        self.assertEqual(code, 0); self.assertTrue(captured["ran"])
        return captured, out

    def test_defaults_follow_the_device_capabilities(self):
        cap, _ = self.launch("python-receiver")
        self.assertEqual((cap["transport"], cap["codec"]), ("sse", "json-b64"))
        cap, _ = self.launch("constrained-receiver")
        self.assertEqual((cap["transport"], cap["codec"]), ("http-longpoll", "json-b64"))
        cap, _ = self.launch("python-receiver", "--transport", "http-longpoll")
        self.assertEqual((cap["transport"], cap["codec"]), ("http-longpoll", "binary-frame"))

    def test_authorization_policy_flags(self):
        cap, out = self.launch("python-receiver", "--allow", "dev_" + "1" * 16)
        self.assertEqual(cap["policy"].allowed_senders, frozenset({"dev_" + "1" * 16})); self.assertFalse(cap["policy"].auto_accept_all)
        self.assertNotIn("ATENÇÃO", out)
        cap, out = self.launch("python-receiver", "--auto-accept-all")
        self.assertTrue(cap["policy"].auto_accept_all); self.assertIn("ATENÇÃO", out)


if __name__ == "__main__":
    unittest.main()
