import os
import tempfile
import threading
import unittest
from pathlib import Path

import requests

from app.capabilities.profiles import browser_receiver, constrained_receiver, python_receiver
from app.client import ApiError, OmniLinkClient, ReconnectLimitExceeded, RetryPolicy
from app.codec import Message, new_message_id
from app.receiver import CollectorPresenter, ConsolePresenter, GrantPolicy, ReceiverRunner, ReceiverService
from app.receiver.presenters import safe_console_text
from app.transport import ReceiverTransport, available_transports, create_transport, register_transport
from tests.helpers import DEV_A, DEV_B, image_bytes


class RetryPolicyTests(unittest.TestCase):
    def test_exponential_growth_is_capped(self):
        policy = RetryPolicy(max_attempts=10, base_delay=0.5, max_delay=4.0, jitter=0.0)
        self.assertEqual([policy.delay(n) for n in range(1, 7)], [0.5, 1.0, 2.0, 4.0, 4.0, 4.0])

    def test_jitter_stays_within_bounds(self):
        policy = RetryPolicy(base_delay=1.0, max_delay=1.0, jitter=0.2)
        self.assertAlmostEqual(policy.delay(1, rng=lambda: 0.0), 0.8)
        self.assertAlmostEqual(policy.delay(1, rng=lambda: 1.0), 1.2)
        self.assertTrue(all(0.8 <= policy.delay(1) <= 1.2 for _ in range(100)))


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self._body, self.headers, self.text = status, body if body is not None else {"ok": True}, headers or {}, ""
        self.closed = False

    def json(self): return self._body
    def close(self): self.closed = True


class FakeSession:
    def __init__(self, outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def request(self, method, url, **kw):
        self.calls.append((method, url))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class HttpRetryTests(unittest.TestCase):
    def client(self, outcomes, attempts=4):
        self.sleeps = []
        self.session = FakeSession(outcomes)
        return OmniLinkClient("http://x", "dev_0123456789abcdef." + "a" * 43, session=self.session,
                              retry=RetryPolicy(max_attempts=attempts, base_delay=0.1, max_delay=1, jitter=0), sleep=self.sleeps.append)

    def test_idempotent_calls_retry_connection_errors_with_backoff(self):
        client = self.client([requests.ConnectionError(), requests.Timeout(), FakeResponse(200, {"ok": True, "devices": [1]})])
        self.assertEqual(client.devices(), [1])
        self.assertEqual(self.sleeps, [0.1, 0.2])
        self.assertEqual(len(self.session.calls), 3)

    def test_server_starting_up_is_retried(self):                       # ex.: instância do Render acordando (502/503)
        client = self.client([FakeResponse(503), FakeResponse(502), FakeResponse(200, {"ok": True, "devices": []})])
        self.assertEqual(client.devices(), [])

    def test_gives_up_after_the_attempt_limit(self):
        client = self.client([requests.ConnectionError()] * 10, attempts=3)
        with self.assertRaises(requests.ConnectionError):
            client.devices()
        self.assertEqual(len(self.session.calls), 3)
        self.assertEqual(len(self.sleeps), 2)

    def test_registration_is_never_retried_automatically(self):
        client = self.client([requests.ConnectionError(), FakeResponse(201, {"token": "x"})])
        with self.assertRaises(requests.ConnectionError):
            client.register("chave", python_receiver())
        self.assertEqual(len(self.session.calls), 1)

    def test_client_errors_are_not_retried_and_keep_the_diagnosis(self):
        body = {"ok": False, "error": {"code": "NOT_AUTHORIZED", "message": "m", "detail": {"request_id": "grq_1"}}}
        client = self.client([FakeResponse(403, body)])
        with self.assertRaises(ApiError) as ctx:
            client.send_text("dev_" + "1" * 16, "oi")
        self.assertEqual((ctx.exception.status, ctx.exception.code), (403, "NOT_AUTHORIZED"))
        self.assertEqual(len(self.session.calls), 1)

    def test_sends_are_retried_safely_with_a_stable_idempotency_key(self):
        sent = []

        class Recorder(FakeSession):
            def request(self, method, url, **kw):
                sent.append(kw.get("json"))
                return super().request(method, url, **kw)

        client = self.client([requests.ConnectionError(), FakeResponse(202, {"ok": True, "message_id": "m"})])
        client.session = Recorder([requests.ConnectionError(), FakeResponse(202, {"ok": True, "message_id": "m"})])
        client.send_text("dev_" + "1" * 16, "oi")
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0]["client_id"], sent[1]["client_id"])      # mesmo client_id: o servidor deduplica

    def test_rate_limit_header_is_exposed(self):
        client = self.client([FakeResponse(429, {"error": {"code": "RATE_LIMITED", "message": "m"}}, {"Retry-After": "7"})])
        with self.assertRaises(ApiError) as ctx:
            client.devices()
        self.assertEqual(ctx.exception.retry_after, 7.0)


class ScriptedTransport(ReceiverTransport):
    """Transporte de teste: cada conexão executa um roteiro (lista de eventos ou exceções)."""
    name = "scripted"

    def __init__(self, scripts, on_exhausted=None):
        super().__init__(client=None)
        self.scripts, self.connections = list(scripts), 0
        self.on_exhausted = on_exhausted

    def events(self, stop):
        self.connections += 1
        if not self.scripts:
            if self.on_exhausted:
                self.on_exhausted()
            return
        for step in self.scripts.pop(0):
            if isinstance(step, BaseException):
                raise step
            yield step


class FakeApiClient:
    def __init__(self, fail_acks=0):
        self.retry = RetryPolicy(max_attempts=3, base_delay=0.1, max_delay=1.0, jitter=0.0)
        self.acks, self.decisions, self.fail_acks = [], [], fail_acks

    def ack(self, message_id, status, reason=None):
        if self.fail_acks:
            self.fail_acks -= 1
            raise requests.ConnectionError("rede caiu")
        self.acks.append((message_id, status, reason))

    def decide_grant(self, request_id, decision, kinds=None):
        self.decisions.append((request_id, decision))


def text_message(body=b"oi", **kw):
    return Message(new_message_id(), DEV_A, kw.pop("recipient", DEV_B), "text", "text/plain", body, **kw)


class RunnerCase(unittest.TestCase):
    def runner(self, scripts, policy=None, client=None, caps=None, **kw):
        self.client = client or FakeApiClient()
        self.presenter = CollectorPresenter()
        service = ReceiverService(DEV_B, caps or python_receiver(), self.presenter, policy or GrantPolicy())
        self.sleeps, self.stop = [], threading.Event()
        runner = ReceiverRunner(self.client, service, transport="sse", stop=self.stop, sleep=self.sleeps.append, **kw)
        runner.transport = ScriptedTransport(scripts, on_exhausted=self.stop.set)
        return runner


class ReconnectionTests(RunnerCase):
    def test_gives_up_after_a_limited_number_of_consecutive_failures(self):
        runner = self.runner([[ConnectionError("x")]] * 20)
        with self.assertRaises(ReconnectLimitExceeded) as ctx:
            runner.run()
        self.assertEqual(ctx.exception.attempts, 3)
        self.assertEqual(ctx.exception.code, "RECONNECT_LIMIT_EXCEEDED")
        self.assertEqual(runner.transport.connections, 4)                # 1 tentativa + 3 reconexões
        self.assertEqual(self.sleeps, [0.1, 0.2, 0.4])                    # recuo exponencial

    def test_a_successful_connection_resets_the_failure_counter(self):
        script = [[ConnectionError()], [ConnectionError()], [("ready", {}), ConnectionError()], [ConnectionError()], [ConnectionError()],
                  [ConnectionError()], [ConnectionError()]]
        runner = self.runner(script)
        with self.assertRaises(ReconnectLimitExceeded):
            runner.run()
        self.assertEqual(runner.transport.connections, 6)                 # sem o reset teriam sido apenas 4
        self.assertEqual(runner.connections, 1)

    def test_normal_stream_end_reconnects_without_penalty(self):
        runner = self.runner([[("ready", {}), ("bye", {})]] * 6)
        runner.run()                                                      # termina porque o roteiro acaba e `stop` é acionado
        self.assertEqual(runner.transport.connections, 7)
        self.assertTrue(all(d == 0.2 for d in self.sleeps))

    def test_auth_failures_are_fatal_and_not_retried(self):
        for status in (401, 403):
            runner = self.runner([[ApiError(status, {"error": {"code": "AUTH_INVALID", "message": "m"}})]] * 5)
            with self.assertRaises(ApiError):
                runner.run()
            self.assertEqual((runner.transport.connections, self.sleeps), (1, []))

    def test_server_errors_are_retried(self):
        runner = self.runner([[ApiError(500, {})], [ApiError(503, {})], [("ready", {})]])
        runner.run()
        self.assertEqual(self.sleeps[:2], [0.1, 0.2])

    def test_revoked_event_is_fatal(self):
        runner = self.runner([[("ready", {}), ("revoked", {"code": "DEVICE_REVOKED"})]])
        with self.assertRaises(ApiError) as ctx:
            runner.run()
        self.assertEqual(ctx.exception.code, "DEVICE_REVOKED")

    def test_reconnecting_events_are_reported(self):
        seen = []
        runner = self.runner([[ConnectionError()], [("ready", {})]], on_event=lambda k, p: seen.append(k))
        runner.run()
        self.assertEqual(seen[0], "reconnecting")


class EventHandlingTests(RunnerCase):
    def test_message_is_presented_and_acknowledged(self):
        msg = text_message(b"ola")
        self.runner([[("ready", {}), ("message", msg)]]).run()
        self.assertEqual([m.data for m in self.presenter.messages], [b"ola"])
        self.assertEqual(self.client.acks, [(msg.id, "displayed", None)])

    def test_lost_ack_is_tolerated_and_redelivery_is_deduplicated(self):
        msg = text_message()
        self.runner([[("ready", {}), ("message", msg), ("message", msg)]], client=FakeApiClient(fail_acks=1)).run()
        self.assertEqual(len(self.presenter.messages), 1)                # apresentou uma vez só
        self.assertEqual(self.client.acks, [(msg.id, "displayed", None)])  # a segunda confirmação passou

    def test_invalid_frames_are_rejected_with_a_reason(self):
        self.runner([[("ready", {}), ("invalid", {"id": "msg_" + "a" * 24, "code": "INTEGRITY_FAILED"}), ("invalid", {"id": None, "code": "X"})]]).run()
        self.assertEqual(self.client.acks, [("msg_" + "a" * 24, "rejected", "INTEGRITY_FAILED")])

    def test_grant_policies(self):
        request = {"id": "grq_" + "1" * 16, "from": DEV_A, "from_name": "s", "kinds": ["text"]}
        cases = [(GrantPolicy(auto_accept_all=True), [(request["id"], "approve")]),
                 (GrantPolicy(allowed_senders=frozenset({DEV_A})), [(request["id"], "approve")]),
                 (GrantPolicy(allowed_senders=frozenset({DEV_B})), []),
                 (GrantPolicy(), []),
                 (GrantPolicy(ask=lambda r: "deny"), [(request["id"], "deny")]),
                 (GrantPolicy(ask=lambda r: None), [])]
        for policy, expected in cases:
            runner = self.runner([[("ready", {}), ("grant.request", request)]], policy=policy)
            runner.run()
            self.assertEqual(self.client.decisions, expected)

    def test_receiver_does_not_trust_the_relay(self):
        script = [("ready", {}),
                  ("message", text_message(recipient="dev_" + "9" * 16)),                                   # destinatário errado
                  ("message", Message(new_message_id(), DEV_A, DEV_B, "html", "text/html", b"<p>ok</p><script>x()</script>")),
                  ("message", Message(new_message_id(), DEV_A, DEV_B, "image", "image/png", b"\x89PNG\r\n\x1a\nlixo"))]    # imagem corrompida
        runner = self.runner([script])
        runner.run()
        self.assertEqual([a[1] for a in self.client.acks], ["rejected", "displayed", "rejected"])
        self.assertEqual(self.client.acks[0][2], "RECIPIENT_MISMATCH")
        self.assertEqual(self.presenter.messages[0].data, b"<p>ok</p>")                                     # HTML re-sanitizado
        self.assertEqual(len(self.presenter.messages), 1)

    def test_capability_limits_are_enforced_by_the_receiver_itself(self):
        small = constrained_receiver()
        big_text = text_message(b"x" * 2000)
        image = Message(new_message_id(), DEV_A, DEV_B, "image", "image/png", image_bytes())
        html = Message(new_message_id(), DEV_A, DEV_B, "html", "text/html", b"<p>x</p>")
        self.runner([[("ready", {}), ("message", big_text), ("message", image), ("message", html)]], caps=small).run()
        self.assertEqual([a[1:] for a in self.client.acks],
                         [("rejected", "SIZE_LIMIT_EXCEEDED"), ("rejected", "CONTENT_UNSUPPORTED"), ("rejected", "CONTENT_UNSUPPORTED")])

    def test_presenter_failures_do_not_break_the_loop(self):
        class Broken:
            def present(self, msg): raise RuntimeError("tela quebrada")
        service = ReceiverService(DEV_B, python_receiver(), Broken())
        self.assertEqual(service.handle(text_message()).status, "rejected")
        self.assertTrue(service.handle(text_message()).reason.startswith("PRESENTATION_FAILED"))


class PresenterTests(unittest.TestCase):
    def test_files_are_named_from_the_message_id_not_from_the_sender(self):
        with tempfile.TemporaryDirectory() as tmp:
            lines = []
            presenter = ConsolePresenter(Path(tmp) / "inbox", out=lines.append)
            evil = Message(new_message_id(), DEV_A, DEV_B, "image", "image/png", image_bytes(), filename="../../etc/cron.d/x")
            presenter.present(evil)
            files = os.listdir(Path(tmp) / "inbox")
            self.assertEqual(files, [f"{evil.id}.png"])
            self.assertFalse((Path(tmp) / "etc").exists())
            html = Message(new_message_id(), DEV_A, DEV_B, "html", "text/html", b"<p>ola</p>")
            presenter.present(html)
            saved = (Path(tmp) / "inbox" / f"{html.id}.html").read_text()
            self.assertIn("Content-Security-Policy", saved); self.assertIn("<p>ola</p>", saved)

    def test_terminal_escape_sequences_are_stripped(self):
        self.assertEqual(safe_console_text("a\x1b[2J\x1b]0;titulo\x07b\tc\nd"), "a[2J]0;titulob\tc\nd")
        lines = []
        ConsolePresenter(out=lines.append).present(text_message("sem\x1b[31m cor".encode()))
        self.assertNotIn("\x1b", "".join(lines))


class TransportRegistryTests(unittest.TestCase):
    def test_builtin_transports_and_plugin_registration(self):
        self.assertTrue({"sse", "http-longpoll"} <= set(available_transports()))
        with self.assertRaises(ValueError):
            create_transport("pombo-correio", None)
        register_transport("scripted", lambda client, codec, max_bytes: ScriptedTransport([]))
        self.assertEqual(create_transport("scripted", None).name, "scripted")


if __name__ == "__main__":
    unittest.main()
