"""Executa, como processos separados, exatamente os comandos documentados no README."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

from tests.helpers import image_bytes

ROOT = Path(__file__).resolve().parents[1]
KEY = "chave-do-teste-do-readme-123"


class ReadmeFlowTests(unittest.TestCase):
    def run_module(self, *args, timeout=30):
        return subprocess.run([sys.executable, "-m", *args], cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=timeout)

    def test_documented_commands_work_end_to_end(self):
        sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup); tmp = Path(tmp.name)
        self.env = {**os.environ, "OMNILINK_ENV": "development", "OMNILINK_ENROLLMENT_KEY": KEY, "PYTHONUNBUFFERED": "1",
                    "OMNILINK_DATABASE_URL": f"sqlite:///{tmp / 'omnilink.db'}", "OMNILINK_LOG_LEVEL": "CRITICAL"}
        url = f"http://127.0.0.1:{port}"
        server = subprocess.Popen([sys.executable, "-m", "app.api", "--port", str(port)], cwd=ROOT, env=self.env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (server.terminate(), server.wait(5)))
        for _ in range(60):
            try:
                if json.load(urllib.request.urlopen(url + "/health", timeout=1))["persistent"]:
                    break
            except Exception:
                time.sleep(0.2)
        else:
            self.fail("servidor não subiu")

        creds = {}
        for name, profile in (("rx", "python-receiver"), ("tx", "python-sender")):
            out = self.run_module("app.client", "register", "--server", url, "--name", f"notebook-{name}", "--profile", profile,
                                  "--out", str(tmp / f"{name}.json"))
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            creds[name] = json.loads((tmp / f"{name}.json").read_text())
            self.assertNotIn(creds[name]["token"], out.stdout)

        listing = self.run_module("app.client", "devices", "-c", str(tmp / "tx.json"))
        self.assertIn("notebook-rx", listing.stdout)

        inbox = tmp / "inbox"
        receiver = subprocess.Popen([sys.executable, "-u", "-m", "app.client", "receive", "-c", str(tmp / "rx.json"),
                                     "--allow", creds["tx"]["device_id"], "--inbox", str(inbox)],
                                    cwd=ROOT, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(lambda: (receiver.terminate(), receiver.wait(5)))
        time.sleep(1.0)

        (tmp / "foto.png").write_bytes(image_bytes("PNG", (320, 200)))
        (tmp / "pagina.html").write_text("<h1>Oi</h1><script>roubar()</script><p>texto</p>", encoding="utf-8")
        for what, value in (("text", "Olá do README"), ("image", str(tmp / "foto.png")), ("html", str(tmp / "pagina.html"))):
            for attempt in range(30):          # 1º envio de cada tipo: NOT_AUTHORIZED até o receptor aprovar (documentado no README)
                out = self.run_module("app.client", "send", "-c", str(tmp / "tx.json"), "--to", creds["rx"]["device_id"],
                                      what, value, "--wait", "10")
                if out.returncode != 3:
                    break
                time.sleep(0.3)
            self.assertEqual(out.returncode, 0, f"{what}: {out.stdout}{out.stderr}")
            self.assertIn("Status final: displayed", out.stdout)

        time.sleep(0.3)
        receiver.terminate()
        shown = receiver.communicate(timeout=5)[0]
        self.assertIn("Olá do README", shown)
        files = sorted(p.suffix for p in inbox.iterdir())
        self.assertEqual(files, [".html", ".png"])
        saved = next(inbox.glob("*.html")).read_text(encoding="utf-8")
        self.assertIn("<h1>Oi</h1>", saved); self.assertNotIn("roubar", saved); self.assertIn("Content-Security-Policy", saved)

        revoke = self.run_module("app.client", "self-revoke", "-c", str(tmp / "rx.json"))
        self.assertEqual(revoke.returncode, 0)


if __name__ == "__main__":
    unittest.main()
