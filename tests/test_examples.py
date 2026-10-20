"""Os exemplos entregues devem funcionar: a demonstração local e a verificação pós-implantação."""
import contextlib
import importlib.util
import io
import unittest
from pathlib import Path

from tests.helpers import ENROLL_KEY, ServerThread, make_settings

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def load(name):
    spec = importlib.util.spec_from_file_location(name, EXAMPLES / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ExampleTests(unittest.TestCase):
    def test_smoke_remote_passes_against_a_running_coordinator(self):
        with ServerThread(make_settings(sse_keepalive_seconds=1)) as server:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = load("smoke_remote").main(["--url", server.url, "--enrollment-key", ENROLL_KEY, "--health-timeout", "10"])
            text = out.getvalue()
            self.assertEqual(code, 0, text)
            self.assertIn("SMOKE TEST: OK", text)
            self.assertNotIn("FALHA", text)
            self.assertEqual(server.coordinator.list_devices(), [])        # dispositivos temporários revogados
            self.assertNotIn(ENROLL_KEY, text)

    def test_smoke_remote_reports_failure_with_the_wrong_key(self):
        with ServerThread(make_settings()) as server:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = load("smoke_remote").main(["--url", server.url, "--enrollment-key", "chave-errada-errada", "--health-timeout", "5"])
            self.assertEqual(code, 1)
            self.assertIn("SMOKE TEST: FALHOU", out.getvalue())

    def test_smoke_remote_gives_up_when_nothing_is_listening(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = load("smoke_remote").main(["--url", "http://127.0.0.1:9", "--enrollment-key", ENROLL_KEY, "--health-timeout", "2"])
        self.assertEqual(code, 1)

    def test_local_demo_runs_to_completion(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = load("demo_local").main()
        text = out.getvalue()
        self.assertEqual(code, 0)
        for marker in ("NOT_AUTHORIZED", "binary-frame", "INCOMPATÍVEL", "html_to_text", "RECEIVER_OFFLINE", "DEVICE_REVOKED",
                       "Demonstração concluída"):
            self.assertIn(marker, text)


if __name__ == "__main__":
    unittest.main()
