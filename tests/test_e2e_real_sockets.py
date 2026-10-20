"""Ponta a ponta com servidor HTTP real e clientes reais (requests), em loopback."""
import base64
import os
import requests
import tempfile
import threading
import time
import unittest

from app.capabilities.profiles import (browser_receiver, constrained_receiver, legacy_receiver, python_receiver,
                                       python_sender)
from app.client import ApiError, OmniLinkClient, ReconnectLimitExceeded, RetryPolicy
from app.content import inspect_image
from app.coordinator.store import SqlStore
from app.receiver import CollectorPresenter, GrantPolicy, ReceiverRunner, ReceiverService
from tests.helpers import ENROLL_KEY, ServerThread, image_bytes, make_settings, noise_png


def wait_for(predicate, timeout=6.0, interval=0.05, message="condição não atingida a tempo"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError(message)


class RunningReceiver:
    def __init__(self, url, caps, transport, codec="json-b64", policy=None, retry=None, key=ENROLL_KEY, fresh_connections=False):
        session = requests.Session()
        if fresh_connections:  # sem keep-alive: depois de uma queda do servidor, novas conexões realmente falham
            session.headers["Connection"] = "close"
        self.client = OmniLinkClient(url, retry=retry or RetryPolicy(max_attempts=30, base_delay=0.05, max_delay=0.3, jitter=0),
                                     session=session)
        self.client.register(key, caps)
        self.presenter, self.events, self.error = CollectorPresenter(), [], None
        self.service = ReceiverService(self.client.device_id, caps, self.presenter, policy or GrantPolicy(auto_accept_all=True))
        self.stop = threading.Event()
        self.runner = ReceiverRunner(self.client, self.service, transport=transport, codec=codec, stop=self.stop,
                                     on_event=lambda kind, payload: self.events.append(kind))
        if transport == "http-longpoll":
            self.runner.transport.wait_seconds = 1
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        try:
            self.runner.run()
        except BaseException as exc:  # registrado para as asserções
            self.error = exc

    def start(self):
        self.thread.start()
        wait_for(lambda: self.runner.connections >= 1 or self.error, message="receptor não conectou")
        return self

    @property
    def id(self):
        return self.client.device_id

    def close(self):
        self.stop.set()
        self.thread.join(timeout=4)


class E2ECase(unittest.TestCase):
    def setUp(self):
        self.server = ServerThread(make_settings(sse_keepalive_seconds=1, redeliver_after_seconds=1))
        self.server.__enter__()
        self.addCleanup(self.server.__exit__)
        self.url, self.receivers = self.server.url, []
        self.sender = OmniLinkClient(self.url)
        self.sender.register(ENROLL_KEY, python_sender())

    def receiver(self, caps, transport, codec="json-b64", **kw):
        r = RunningReceiver(self.url, caps, transport, codec, **kw).start()
        self.receivers.append(r)
        self.addCleanup(r.close)
        return r

    def deliver(self, fn, tries=60):
        """Executa um envio; na primeira vez o receptor ainda precisa autorizar (consentimento explícito)."""
        for _ in range(tries):
            try:
                return fn()
            except ApiError as exc:
                if exc.code != "NOT_AUTHORIZED":
                    raise
                time.sleep(0.1)
        raise AssertionError("o receptor não autorizou a tempo")

    def final_status(self, reply, wait=6):
        return self.sender.message_status(reply["message_id"], wait=wait)


class DeliveryTests(E2ECase):
    def test_text_over_sse_with_delivery_confirmation(self):
        r = self.receiver(python_receiver(), "sse")
        reply = self.deliver(lambda: self.sender.send_text(r.id, "Olá via SSE ✓"))
        status = self.final_status(reply)
        self.assertEqual((status["status"], status["delivered_via"]), ("displayed", "sse"))
        self.assertEqual([m.data.decode() for m in r.presenter.messages], ["Olá via SSE ✓"])
        self.assertIn("grant.request", r.events)                       # autorização explícita aconteceu

    def test_image_over_longpoll_uses_the_binary_codec(self):
        r = self.receiver(python_receiver(), "http-longpoll", "binary-frame")
        png = noise_png()
        reply = self.deliver(lambda: self.sender.send_image(r.id, png, filename="ruido.png"))
        self.assertEqual(reply["negotiation"]["selection"]["codec"], "binary-frame")
        status = self.final_status(reply)
        self.assertEqual((status["status"], status["delivered_via"]), ("displayed", "http-longpoll"))
        (msg,) = r.presenter.messages
        self.assertEqual((msg.data, msg.filename), (png, "ruido.png"))

    def test_negotiation_differs_per_receiver_for_the_same_content(self):
        a = self.receiver(browser_receiver(), "sse")                       # só JSON
        b = self.receiver(python_receiver(), "http-longpoll", "binary-frame")
        png = noise_png()
        plan_a = self.deliver(lambda: self.sender.send_image(a.id, png))["negotiation"]["selection"]
        plan_b = self.deliver(lambda: self.sender.send_image(b.id, png))["negotiation"]["selection"]
        self.assertEqual((plan_a["transport"], plan_a["codec"]), ("sse", "json-b64"))
        self.assertEqual((plan_b["transport"], plan_b["codec"]), ("http-longpoll", "binary-frame"))
        wait_for(lambda: len(a.presenter.messages) == len(b.presenter.messages) == 1)
        self.assertEqual(a.presenter.messages[0].data, b.presenter.messages[0].data)

    def test_html_is_sanitized_end_to_end(self):
        r = self.receiver(python_receiver(), "sse")
        reply = self.deliver(lambda: self.sender.send_html(r.id, "<h1>Oi</h1><script>steal()</script><img src=x onerror=steal()>"))
        self.assertEqual(self.final_status(reply)["status"], "displayed")
        self.assertEqual(r.presenter.messages[0].data, b"<h1>Oi</h1>")

    def test_content_is_adapted_to_a_constrained_device(self):
        r = self.receiver(constrained_receiver(), "http-longpoll")
        reply = self.deliver(lambda: self.sender.send_image(r.id, noise_png((500, 400))))
        self.assertEqual(self.final_status(reply)["status"], "displayed")
        (msg,) = r.presenter.messages
        info = inspect_image(msg.data, max_bytes=60_000, max_pixels=10**6)
        self.assertEqual(info.mime, "image/jpeg"); self.assertLessEqual((info.width, info.height), (160, 120))
        self.assertTrue(any(a.startswith("fit_image") for a in msg.adapted))
        html_reply = self.deliver(lambda: self.sender.send_html(r.id, "<h2>Título</h2><p>texto</p>"))
        self.assertEqual(self.final_status(html_reply)["status"], "displayed")
        self.assertEqual((r.presenter.messages[1].kind, r.presenter.messages[1].adapted), ("text", ["html_to_text"]))

    def test_incompatible_device_is_explained_and_nothing_is_sent(self):
        legacy = OmniLinkClient(self.url); legacy.register(ENROLL_KEY, legacy_receiver())
        legacy.grant(self.sender.device_id, ["text"])
        with self.assertRaises(ApiError) as ctx:
            self.sender.send_text(legacy.device_id, "alguém aí?")
        self.assertEqual(ctx.exception.status, 422)
        codes = {d["code"] for d in ctx.exception.negotiation["diagnostics"]}
        self.assertTrue({"PROTOCOL_VERSION_MISMATCH", "NO_COMMON_TRANSPORT"} <= codes)
        self.assertTrue(ctx.exception.negotiation["alternatives"])
        self.assertEqual(legacy.fetch_inbox(wait=0)["items"], [])

    def test_receiver_can_refuse_a_sender(self):
        r = self.receiver(python_receiver(), "sse", policy=GrantPolicy(ask=lambda req: "deny"))
        with self.assertRaises(ApiError) as first:
            self.sender.send_text(r.id, "posso?")
        wait_for(lambda: r.client.grants()["requests"] == [] and "grant.request" in r.events)
        with self.assertRaises(ApiError) as second:
            self.sender.send_text(r.id, "e agora?")
        self.assertEqual((first.exception.code, second.exception.code), ("NOT_AUTHORIZED", "NOT_AUTHORIZED"))
        self.assertEqual(r.presenter.messages, [])


class RecoveryTests(E2ECase):
    def test_offline_receiver_gets_queued_messages_after_it_connects(self):
        later = OmniLinkClient(self.url); later.register(ENROLL_KEY, python_receiver())
        later.grant(self.sender.device_id, ["text"])
        reply = self.sender.send_text(later.device_id, "guardada na fila")
        self.assertIn("RECEIVER_OFFLINE", [d["code"] for d in reply["diagnostics"]])
        self.assertEqual(self.sender.message_status(reply["message_id"])["status"], "queued")
        presenter = CollectorPresenter()
        service = ReceiverService(later.device_id, python_receiver(), presenter, GrantPolicy())
        stop = threading.Event(); runner = ReceiverRunner(later, service, transport="sse", stop=stop)
        thread = threading.Thread(target=runner.run, daemon=True); thread.start()
        self.addCleanup(lambda: (stop.set(), thread.join(4)))
        self.assertEqual(self.final_status(reply)["status"], "displayed")
        self.assertEqual(presenter.messages[0].data, b"guardada na fila")

    def test_lost_ack_leads_to_redelivery_without_duplicate_presentation(self):
        r = self.receiver(python_receiver(), "sse")
        real_ack, calls = r.client.ack, []

        def flaky_ack(*a, **kw):
            calls.append(a)
            if len(calls) == 1:
                raise ConnectionError("a confirmação se perdeu")
            return real_ack(*a, **kw)

        r.client.ack = flaky_ack
        reply = self.deliver(lambda: self.sender.send_text(r.id, "uma vez só"))
        self.assertEqual(self.final_status(reply, wait=10)["status"], "displayed")
        self.assertEqual(len(r.presenter.messages), 1)
        self.assertGreaterEqual(len(calls), 2)
        self.assertGreaterEqual(self.sender.message_status(reply["message_id"])["attempts"], 2)

    def test_revoked_device_stops_receiving_and_the_runner_exits_with_an_auth_error(self):
        r = self.receiver(python_receiver(), "sse")
        admin = OmniLinkClient(self.url)
        resp = admin._request("DELETE", f"/v1/admin/devices/{r.id}", headers={"X-Enrollment-Key": ENROLL_KEY})
        self.assertEqual(resp.status_code, 200)
        wait_for(lambda: r.error is not None, message="o laço de recepção não encerrou")
        self.assertIsInstance(r.error, ApiError)
        self.assertEqual(r.error.code, "DEVICE_REVOKED")

    def test_receiver_reconnects_after_a_server_restart_with_persistent_state(self):
        """Simula o spin-down/reinício do Render: o estado está no banco e o receptor reconecta sozinho."""
        with tempfile.TemporaryDirectory() as tmp:
            url_db = "sqlite:///" + os.path.join(tmp, "omnilink.db")
            settings = make_settings(sse_keepalive_seconds=1, sse_max_seconds=2)
            first = ServerThread(settings, SqlStore(url_db)).__enter__()
            port = first.port
            sender = OmniLinkClient(first.url); sender.register(ENROLL_KEY, python_sender())
            r = RunningReceiver(first.url, python_receiver(), "sse", fresh_connections=True).start()
            self.addCleanup(r.close)
            r.client.grant(sender.device_id, ["text"])
            first.__exit__()                                              # servidor "cai" (o fluxo SSE em curso termina em até 2 s)
            time.sleep(3.0)                                               # janela de indisponibilidade real: conexões recusadas
            second = ServerThread(settings, SqlStore(url_db), port=port).__enter__()   # volta no mesmo endereço, mesmo banco
            self.addCleanup(second.__exit__)
            wait_for(lambda: "reconnecting" in r.events and r.runner.connections >= 2, timeout=10,
                     message=f"não reconectou: eventos={r.events[-5:]} erro={r.error}")
            reply = sender.send_text(r.id, "depois do reinício")           # credenciais e autorização sobreviveram
            self.assertEqual(sender.message_status(reply["message_id"], wait=6)["status"], "displayed")
            self.assertEqual(r.presenter.messages[-1].data, "depois do reinício".encode())

    def test_reconnection_attempts_are_bounded_when_the_server_never_returns(self):
        server = ServerThread(make_settings(sse_keepalive_seconds=1, sse_max_seconds=2)).__enter__()
        r = RunningReceiver(server.url, python_receiver(), "sse", fresh_connections=True,
                            retry=RetryPolicy(max_attempts=3, base_delay=0.05, max_delay=0.1, jitter=0)).start()
        self.addCleanup(r.close)
        server.__exit__()
        wait_for(lambda: r.error is not None, timeout=15, message="não desistiu")
        self.assertIsInstance(r.error, ReconnectLimitExceeded)
        self.assertEqual(r.error.attempts, 3)
        self.assertEqual(r.events.count("reconnecting"), 3)


if __name__ == "__main__":
    unittest.main()
