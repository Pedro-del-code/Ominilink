"""Perfil de produção (como no Render): HTTPS obrigatório atrás do proxy, logs JSON sem segredos, falha rápida."""
import contextlib
import importlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

from app.capabilities.profiles import python_receiver, python_sender
from app.config import ConfigError

KEY = "chave-de-producao-0123456789"


def load_wsgi(env):
    sys.modules.pop("wsgi", None)
    buf = io.StringIO()
    with mock.patch.dict(os.environ, env, clear=False), contextlib.redirect_stdout(buf):
        module = importlib.import_module("wsgi")
    return module.app, buf


class ProductionProfileTests(unittest.TestCase):
    def tearDown(self):
        sys.modules.pop("wsgi", None)

    def env(self, **extra):
        return {"OMNILINK_ENV": "production", "OMNILINK_ENROLLMENT_KEY": KEY, "RENDER": "true", "OMNILINK_LOG_LEVEL": "INFO", **extra}

    def test_fails_fast_without_a_strong_enrollment_key(self):
        sys.modules.pop("wsgi", None)
        env = {k: v for k, v in os.environ.items() if not k.startswith("OMNILINK_")}
        env.update({"OMNILINK_ENV": "production"})
        with mock.patch.dict(os.environ, env, clear=True), self.assertRaises(ConfigError):
            importlib.import_module("wsgi")

    def test_https_is_enforced_behind_the_proxy_and_health_checks_still_work(self):
        app, _ = load_wsgi(self.env())
        client = app.test_client()
        self.assertEqual(client.get("/health").status_code, 200)                    # o health check do Render chega por HTTP interno
        self.assertEqual(client.get("/v1/devices").status_code, 426)
        hdr = {"X-Enrollment-Key": KEY, "X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.9"}
        resp = client.post("/v1/devices", json={"capabilities": python_receiver().to_dict()}, headers=hdr)
        self.assertEqual(resp.status_code, 201)
        self.assertIn("max-age", resp.headers["Strict-Transport-Security"])

    def test_json_logs_never_contain_credentials(self):
        app, buf = load_wsgi(self.env())
        client = app.test_client()
        H = {"X-Forwarded-Proto": "https"}
        sender = client.post("/v1/devices", json={"capabilities": python_sender().to_dict()}, headers={**H, "X-Enrollment-Key": KEY}).get_json()
        receiver = client.post("/v1/devices", json={"capabilities": python_receiver().to_dict()}, headers={**H, "X-Enrollment-Key": KEY}).get_json()
        client.put(f"/v1/grants/{sender['device_id']}", json={"kinds": ["text"]}, headers={**H, "Authorization": "Bearer " + receiver["token"]})
        client.post("/v1/messages", json={"to": receiver["device_id"], "kind": "text", "body": "texto-privado-abc", "encoding": "utf-8"},
                    headers={**H, "Authorization": "Bearer " + sender["token"]})
        client.get("/v1/devices", headers={**H, "Authorization": "Bearer dev_0123456789abcdef." + "z" * 43})   # falha de autenticação
        output = buf.getvalue()
        lines = [l for l in output.splitlines() if l.strip()]
        self.assertTrue(lines)
        for line in lines:
            json.loads(line)                                                       # cada linha é JSON válido
        for secret in (KEY, sender["token"], receiver["token"], sender["token"].split(".")[1], receiver["token"].split(".")[1],
                       "texto-privado-abc", "z" * 43):
            self.assertNotIn(secret, output)
        self.assertIn("device.registered", output); self.assertIn("auth.failed", output)

    def test_ephemeral_key_notice_is_only_for_development(self):
        _, buf = load_wsgi({"OMNILINK_ENV": "development", "OMNILINK_ENROLLMENT_KEY": ""})
        self.assertIn("EFÊMERA", buf.getvalue())
        _, buf = load_wsgi(self.env())
        self.assertNotIn("EFÊMERA", buf.getvalue())

    def test_database_url_selects_the_sql_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, _ = load_wsgi(self.env(DATABASE_URL="sqlite:///" + os.path.join(tmp, "p.db")))
            body = app.test_client().get("/health").get_json()
            self.assertEqual((body["store"], body["persistent"]), ("sqlite", True))


class DevServerEntrypointTests(unittest.TestCase):
    """O comando documentado no README (`python -m app.api`) precisa realmente subir o servidor."""

    def test_python_dash_m_app_api_serves_health_and_the_receiver_page(self):
        import socket
        import subprocess
        import time
        import urllib.request

        sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
        env = {**os.environ, "OMNILINK_ENV": "development", "OMNILINK_ENROLLMENT_KEY": "chave-de-teste-123456"}
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        proc = subprocess.Popen([sys.executable, "-m", "app.api", "--port", str(port)], cwd=root, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            body = None
            for _ in range(60):
                try:
                    body = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1))
                    break
                except Exception:
                    time.sleep(0.2)
            self.assertIsNotNone(body, "servidor não subiu")
            self.assertEqual(body["status"], "ok")
            page = urllib.request.urlopen(f"http://127.0.0.1:{port}/receiver", timeout=2)
            self.assertIn("OmniLink", page.read().decode())
            self.assertIn("script-src 'self'", page.headers["Content-Security-Policy"])
        finally:
            proc.terminate()
            out = proc.communicate(timeout=5)[0]
        self.assertIn("receptor web", out)
        self.assertNotIn("EFÊMERA", out)                       # a chave veio do ambiente


if __name__ == "__main__":
    unittest.main()
