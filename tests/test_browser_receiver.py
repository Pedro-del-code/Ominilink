"""Receptor web testado em Chromium real (Playwright). Ignorado automaticamente se o navegador não existir."""
import re
import unittest
from unittest import mock

from app.capabilities.profiles import python_sender
from app.client import ApiError, OmniLinkClient
from tests.helpers import ENROLL_KEY, ServerThread, image_bytes, make_settings, noise_png

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = expect = None


def _browser_available() -> bool:
    if sync_playwright is None:
        return False
    try:
        with sync_playwright() as pw:
            pw.chromium.launch().close()
        return True
    except Exception:
        return False


BROWSER_OK = _browser_available()


@unittest.skipUnless(BROWSER_OK, "Playwright/Chromium indisponível neste ambiente")
class BrowserCase(unittest.TestCase):
    server_overrides: dict = {}
    expect_blocked = False       # testes de defesa em profundidade esperam erros de console por bloqueios (CSP/sandbox)

    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.server = ServerThread(make_settings(sse_keepalive_seconds=1, **self.server_overrides)).__enter__()
        self.addCleanup(self.server.__exit__)
        self.ctx = self.browser.new_context(viewport={"width": 1000, "height": 800})
        self.addCleanup(self.ctx.close)
        self.page = self.ctx.new_page()
        self.problems, self.requests, self.failed, self.responses = [], [], {}, []
        self.page.on("pageerror", lambda e: self.problems.append(str(e)))
        self.page.on("console", lambda m: self.problems.append(m.text) if m.type == "error" and not self.expect_blocked else None)
        self.page.on("request", lambda r: self.requests.append(r.url))
        self.page.on("requestfailed", lambda r: self.failed.__setitem__(r.url, r.failure))
        self.page.on("response", lambda r: self.responses.append(r.url))
        self.sender = OmniLinkClient(self.server.url)
        self.sender.register(ENROLL_KEY, python_sender())

    def assertNoBrowserProblems(self):
        self.page.wait_for_timeout(100)          # entrega eventos pendentes do Playwright
        self.assertEqual(self.problems, [])

    def poll(self, predicate, timeout=6.0, message="condição não atingida a tempo"):
        """Espera ativa que mantém o laço de eventos do Playwright em funcionamento (time.sleep não o faria)."""
        waited = 0.0
        while waited < timeout:
            if predicate():
                return
            self.page.wait_for_timeout(100)
            waited += 0.1
        self.fail(message)

    def connect(self, remember=False, name=None):
        page = self.page
        page.goto(self.server.url + "/receiver")
        if name:
            page.fill("#device-name", name)
        page.fill("#enroll-key", ENROLL_KEY)
        if remember:
            page.check("#remember")
        page.click("#setup-form button[type=submit]")
        self.wait_connected()
        return page.inner_text("#device-id").split(" ")[0]

    def wait_connected(self):
        # `expect` usa o mundo isolado do Playwright; wait_for_function usaria eval, bloqueado pela CSP da página
        expect(self.page.locator("#status-text")).to_have_text(re.compile(r"^conectado"), timeout=8000)

    def deliver(self, fn, decide="approve"):
        """Tenta enviar; se faltar autorização, responde ao cartão de consentimento na página e tenta de novo."""
        try:
            return fn()
        except ApiError as exc:
            self.assertEqual(exc.code, "NOT_AUTHORIZED")
            self.page.wait_for_selector(".grant button.primary", timeout=6000)
            self.page.click(".grant button.primary" if decide == "approve" else ".grant button.ghost")
            self.page.wait_for_selector(".grant", state="detached", timeout=5000)
            if decide != "approve":
                return None
            return fn()

    def status(self, reply):
        return self.sender.message_status(reply["message_id"], wait=6)["status"]


class ContentTests(BrowserCase):
    def test_register_connect_and_receive_text_image_and_html(self):
        rid = self.connect()
        self.assertRegex(rid, r"^dev_[0-9a-f]{16}$")
        caps = self.sender.capabilities(rid)
        self.assertEqual((caps["platform"], caps["codecs"], caps["transports"]), ("browser", ["json-b64"], ["sse", "http-longpoll"]))

        text = "Olá! <b>não é negrito</b> — ação, coração 🙂"
        reply = self.deliver(lambda: self.sender.send_text(rid, text))
        self.page.wait_for_selector(".msg-text")
        self.assertEqual(self.page.inner_text(".msg-text"), text)                 # texto literal: nada é interpretado como HTML
        self.assertEqual(self.page.locator(".msg-text b").count(), 0)
        self.assertEqual(self.status(reply), "displayed")

        reply = self.deliver(lambda: self.sender.send_image(rid, noise_png((300, 200)), filename="ruido.png"))
        self.poll(lambda: self.page.evaluate("(() => { const i = document.querySelector('.item img'); return i ? i.naturalWidth : 0; })()") > 0)
        self.assertEqual(self.page.evaluate("document.querySelector('.item img').naturalWidth"), 300)
        self.assertEqual(self.status(reply), "displayed")

        reply = self.deliver(lambda: self.sender.send_html(rid, "<h2>Relatório</h2><ul><li>um</li><li>dois</li></ul><script>window.__pwn = 1</script>"))
        frame = self.page.frame_locator("iframe.html-frame")
        frame.locator("h2").wait_for(timeout=6000)
        self.assertEqual(frame.locator("li").count(), 2)
        self.assertEqual(self.page.get_attribute("iframe.html-frame", "sandbox"), "")   # atributo presente e vazio = sandbox total
        self.assertEqual(self.status(reply), "displayed")
        self.assertIsNone(self.page.evaluate("window.__pwn"))

        self.assertEqual(self.page.locator("#history li").count(), 3)
        self.page.locator("#history li").last.click()                              # histórico: reabre o item mais antigo
        self.assertEqual(self.page.inner_text(".msg-text"), text)
        self.assertNoBrowserProblems()

    def test_untrusted_fields_are_never_interpreted_as_markup(self):
        evil = OmniLinkClient(self.server.url)
        evil.register(ENROLL_KEY, python_sender("<img src=x onerror=window.__pwn=1>"))   # nome de remetente hostil
        rid = self.connect()
        self.deliver(lambda: evil.send_text(rid, "<script>window.__pwn=2</script>", filename="<svg onload=window.__pwn=3>.txt"))
        self.page.wait_for_selector(".msg-text")
        self.assertIn("<script>window.__pwn=2</script>", self.page.inner_text(".msg-text"))
        self.assertIsNone(self.page.evaluate("window.__pwn"))
        self.assertNoBrowserProblems()

    def test_consent_prompt_shows_sender_and_kind_and_deny_is_respected(self):
        rid = self.connect()
        with self.assertRaises(ApiError):
            self.sender.send_text(rid, "posso?")
        self.page.wait_for_selector(".grant")
        card = self.page.inner_text(".grant p")
        self.assertIn("python-sender", card); self.assertIn(self.sender.device_id, card); self.assertIn("texto", card)
        self.page.click(".grant button.ghost")                                      # Negar
        self.page.wait_for_selector(".grant", state="detached")
        with self.assertRaises(ApiError) as ctx:
            self.sender.send_text(rid, "e agora?")
        self.assertEqual(ctx.exception.code, "NOT_AUTHORIZED")
        self.assertEqual(self.page.locator(".msg-text").count(), 0)
        self.assertNoBrowserProblems()


class SessionTests(BrowserCase):
    def test_wrong_enrollment_key_is_reported_in_the_form(self):
        self.page.goto(self.server.url + "/receiver")
        self.page.fill("#enroll-key", "chave-errada-errada")
        self.page.click("#setup-form button[type=submit]")
        expect(self.page.locator("#setup-error")).to_have_text(re.compile(r"inválida"), timeout=5000)
        self.assertTrue(self.page.is_visible("#setup"))
        self.assertTrue(self.page.is_hidden("#inbox"))

    def test_remembered_credentials_survive_a_reload_without_the_enrollment_key(self):
        rid = self.connect(remember=True)
        self.page.reload()
        self.wait_connected()
        self.assertTrue(self.page.is_hidden("#setup"))
        self.assertEqual(self.page.inner_text("#device-id").split(" ")[0], rid)
        stored = self.page.evaluate("localStorage.getItem('omnilink.receiver.v1')")
        self.assertNotIn(ENROLL_KEY, stored)                                       # a chave de registro nunca é guardada
        self.assertEqual(self.page.input_value("#enroll-key"), "")

    def test_without_remember_the_credential_does_not_outlive_the_session(self):
        self.connect(remember=False)
        self.assertIsNone(self.page.evaluate("localStorage.getItem('omnilink.receiver.v1')"))
        self.assertIsNotNone(self.page.evaluate("sessionStorage.getItem('omnilink.receiver.v1')"))

    def test_revoke_button_revokes_on_the_server_and_returns_to_setup(self):
        rid = self.connect(remember=True)
        self.page.on("dialog", lambda d: d.accept())
        self.page.click("#forget")
        self.page.wait_for_selector("#setup", state="visible")
        self.assertIsNone(self.page.evaluate("localStorage.getItem('omnilink.receiver.v1')"))
        with self.assertRaises(ApiError) as ctx:
            self.sender.send_text(rid, "alguém?")
        self.assertEqual(ctx.exception.code, "RECEIVER_UNKNOWN")

    def test_server_side_revocation_sends_the_page_back_to_setup(self):
        rid = self.connect(remember=True)
        OmniLinkClient(self.server.url)._request("DELETE", f"/v1/admin/devices/{rid}", headers={"X-Enrollment-Key": ENROLL_KEY})
        self.page.wait_for_selector("#setup", state="visible", timeout=8000)
        self.assertIsNone(self.page.evaluate("localStorage.getItem('omnilink.receiver.v1')"))


class ResilienceTests(BrowserCase):
    server_overrides = {"sse_max_seconds": 2}

    def test_page_reconnects_when_the_stream_ends_and_keeps_receiving(self):
        rid = self.connect()
        self.sender_grant(rid)
        streams = lambda: sum(1 for u in self.requests if u.endswith("/v1/stream"))
        self.poll(lambda: streams() >= 3, timeout=10, message=f"não reconectou: {streams()} conexões")   # o servidor encerra o fluxo a cada 2 s
        self.wait_connected()
        reply = self.sender.send_text(rid, "depois da reconexão")
        self.page.wait_for_selector(".msg-text")
        self.assertEqual(self.status(reply), "displayed")
        self.assertNoBrowserProblems()

    def sender_grant(self, rid):
        with self.assertRaises(ApiError):
            self.sender.send_text(rid, "pedido")
        self.page.wait_for_selector(".grant button.primary")
        self.page.click(".grant button.primary")
        self.page.wait_for_selector(".grant", state="detached")


class CapabilityNegotiationInTheBrowserTests(BrowserCase):
    def test_browser_without_streaming_support_registers_and_works_over_long_polling(self):
        self.page.add_init_script("delete window.ReadableStream;")                 # simula navegador antigo (ex.: TV)
        rid = self.connect()
        self.assertEqual(self.sender.capabilities(rid)["transports"], ["http-longpoll"])     # anuncia só o que suporta
        self.assertIn("http-longpoll", self.page.inner_text("#status-text"))
        reply = self.deliver(lambda: self.sender.send_text(rid, "via long-poll"))
        self.assertEqual(reply["negotiation"]["selection"]["transport"], "http-longpoll")
        self.page.wait_for_selector(".msg-text")
        self.assertEqual(self.page.inner_text(".msg-text"), "via long-poll")
        self.assertEqual(self.status(reply), "displayed")
        self.assertFalse(any(u.endswith("/v1/stream") for u in self.requests))
        self.assertNoBrowserProblems()


class DefenseInDepthTests(BrowserCase):
    """Mesmo que o servidor falhasse em sanitizar, o navegador isola o HTML (sandbox + CSP herdada)."""

    expect_blocked = True

    HOSTILE = ("<p id='ok'>visível</p>"
               "<script>document.body.setAttribute('data-pwn','script')</script>"
               "<img src='https://evil.invalid/pixel.png' onerror=\"document.body.setAttribute('data-pwn','img')\">"
               "<meta http-equiv='refresh' content='0;url=https://evil.invalid/refresh'>"
               "<form action='https://evil.invalid/form' method='post'><button id='send'>enviar</button></form>"
               "<a id='link' href='https://evil.invalid/link' target='_blank'>link</a>"
               "<iframe src='https://evil.invalid/nested'></iframe>")

    def test_unsanitized_hostile_html_cannot_execute_navigate_or_load_resources(self):
        rid = self.connect()
        passthrough = lambda src, max_bytes=None: (src, False)               # desliga a sanitização do servidor (só neste teste)
        with mock.patch("app.content.engine.sanitize_html", passthrough):
            reply = self.deliver(lambda: self.sender.send_html(rid, self.HOSTILE))
        self.assertIn("<script>", self.page.get_attribute("iframe.html-frame", "srcdoc"))   # o HTML hostil de fato chegou
        frame = self.page.frame_locator("iframe.html-frame")
        frame.locator("#ok").wait_for(timeout=6000)
        frame.locator("#send").click(timeout=3000)                            # tenta enviar o formulário
        frame.locator("#link").click(timeout=3000)                            # tenta navegar pelo link
        self.page.wait_for_timeout(1200)                                      # tempo para refresh/requisições acontecerem
        self.assertIsNone(frame.locator("body").get_attribute("data-pwn"))    # nenhum script executou
        evil = [u for u in self.requests if "evil.invalid" in u]
        self.assertTrue(all(self.failed.get(u) == "csp" for u in evil), (evil, self.failed))   # só tentativas, todas barradas pela CSP
        self.assertEqual([u for u in self.responses if "evil.invalid" in u], [])                # nenhuma resposta: nada saiu da máquina
        self.assertTrue(self.page.url.endswith("/receiver"))
        self.assertEqual(frame.locator("#ok").inner_text(), "visível")        # o quadro não foi redirecionado
        self.assertEqual(self.status(reply), "displayed")


if __name__ == "__main__":
    unittest.main()
