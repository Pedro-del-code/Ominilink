#!/usr/bin/env python3
"""Demonstração local do OmniLink: servidor Flask + clientes reais em 127.0.0.1, passo a passo.

    python examples/demo_local.py

Tudo roda em um único processo (servidor em thread, receptores em threads), mas a comunicação é HTTP real.
"""
from __future__ import annotations

import base64
import dataclasses
import io
import logging
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

from app.api import create_app  # noqa: E402
from app.capabilities.profiles import (browser_receiver, constrained_receiver, legacy_receiver,  # noqa: E402
                                       python_receiver, python_sender)
from app.client import ApiError, OmniLinkClient, RetryPolicy  # noqa: E402
from app.config import Settings  # noqa: E402
from app.diagnostics import explain  # noqa: E402
from app.receiver import CollectorPresenter, GrantPolicy, ReceiverRunner, ReceiverService  # noqa: E402

KEY = "chave-da-demonstracao-123456"
logging.getLogger("werkzeug").setLevel(logging.ERROR)


class Receiver:
    """Dispositivo receptor: cliente + laço de recepção em thread + apresentador que apenas coleta."""

    def __init__(self, url, caps, transport, codec, policy=None):
        self.client = OmniLinkClient(url, retry=RetryPolicy(max_attempts=20, base_delay=0.05, max_delay=0.3, jitter=0))
        self.reg = self.client.register(KEY, caps)
        self.caps, self.transport, self.codec = caps, transport, codec
        self.presenter, self.error, self.stop = CollectorPresenter(), None, threading.Event()
        self.service = ReceiverService(self.client.device_id, caps, self.presenter, policy or GrantPolicy(auto_accept_all=True))
        self.runner = ReceiverRunner(self.client, self.service, transport=transport, codec=codec, stop=self.stop)
        if transport == "http-longpoll":
            self.runner.transport.wait_seconds = 1
        self.thread = threading.Thread(target=self._run, daemon=True)

    @property
    def id(self):
        return self.client.device_id

    def _run(self):
        try:
            self.runner.run()
        except BaseException as exc:
            self.error = exc

    def start(self):
        self.thread.start()
        deadline = time.monotonic() + 5
        while self.runner.connections < 1 and time.monotonic() < deadline:
            time.sleep(0.05)
        return self


def title(n, text):
    print(f"\n[{n}] {text}\n" + "-" * (len(text) + 5))


def send_until_authorized(fn, note):
    try:
        return fn()
    except ApiError as exc:
        if exc.code != "NOT_AUTHORIZED":
            raise
        print(f"    {note}: {exc.diagnostic['code']} (pedido {exc.diagnostic['detail']['request_id']}) — o receptor foi notificado")
    for _ in range(50):
        time.sleep(0.1)
        try:
            return fn()
        except ApiError as exc:
            if exc.code != "NOT_AUTHORIZED":
                raise
    raise SystemExit("o receptor não autorizou a tempo")


def describe_plan(plan):
    ops = ", ".join(a["op"] for a in plan["adaptations"]) or "sem adaptação"
    return f"{plan['transport']} + {plan['codec']} ({ops})"


def main() -> int:
    settings = dataclasses.replace(
        Settings.from_env({"OMNILINK_ENV": "development", "OMNILINK_ENROLLMENT_KEY": KEY, "OMNILINK_LOG_LEVEL": "CRITICAL"}),
        sse_keepalive_seconds=1, redeliver_after_seconds=2)
    server = make_server("127.0.0.1", 0, create_app(settings), threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"OmniLink — demonstração local (coordenador em {url}, armazenamento em memória)")

    title(1, "Registro: cada dispositivo anuncia suas próprias capacidades")
    sender = OmniLinkClient(url); sender.register(KEY, python_sender())
    notebook = Receiver(url, python_receiver("notebook"), "sse", "json-b64", GrantPolicy()).start()
    browser = Receiver(url, browser_receiver("navegador-tv"), "sse", "json-b64").start()
    painel = Receiver(url, constrained_receiver("painel-simples"), "http-longpoll", "json-b64").start()
    bulk = Receiver(url, python_receiver("estacao-de-trabalho"), "http-longpoll", "binary-frame").start()
    legacy = OmniLinkClient(url); legacy.register(KEY, legacy_receiver("equipamento-antigo"))
    for d in sender.devices():
        print(f"    {d['id']}  {d['name']:<20} {'online ' if d['online'] else 'offline'} transportes={','.join(d['transports']) or '-':<22} codecs={','.join(d['codecs']) or '-'}")

    title(2, "Negociação (simulação, nada é enviado): a MESMA imagem de ~920 KB para cada receptor")
    noise = Image.frombytes("RGB", (640, 480), os.urandom(640 * 480 * 3)); buf = io.BytesIO(); noise.save(buf, "PNG")
    big_png = buf.getvalue()
    desc = dict(size=len(big_png), width=640, height=480, mime="image/png")
    for name, dev_id in (("notebook", notebook.id), ("navegador-tv", browser.id), ("estacao-de-trabalho", bulk.id), ("painel-simples", painel.id)):
        neg = sender.negotiate(dev_id, "image", **desc)
        print(f"    {name:<20} → {describe_plan(neg['selection'])}")
    neg = sender.negotiate(legacy.device_id, "image", **desc)
    print("    equipamento-antigo   → INCOMPATÍVEL")
    print("        " + explain(neg["diagnostics"], neg["alternatives"]).replace("\n", "\n        "))

    title(3, "Consentimento explícito: sem autorização, nada chega ao receptor")
    try:
        sender.send_text(notebook.id, "olá!")
    except ApiError as exc:
        print(f"    envio recusado: {exc.code} — {exc.diagnostic['suggestion']}")
    pending = notebook.client.grants()["requests"][0]
    print(f"    o notebook vê o pedido de '{pending['sender_name']}' para: {', '.join(pending['kinds'])}; conteúdo pendente no inbox: "
          f"{[i['type'] for i in notebook.client.fetch_inbox(wait=0)['items'] if i['type'] == 'message']}")
    notebook.client.decide_grant(pending["id"], "approve")
    reply = sender.send_text(notebook.id, "Olá, notebook! ✓")
    print(f"    após a aprovação: aceito ({reply['message_id']}), status final = {sender.message_status(reply['message_id'], wait=5)['status']}")

    title(4, "Texto, imagem e HTML entregues, com confirmação de recebimento")
    reply = send_until_authorized(lambda: sender.send_image(bulk.id, big_png, filename="ruido.png"), "imagem para a estação")
    st = sender.message_status(reply["message_id"], wait=5)
    print(f"    imagem 640x480 → estação: plano {describe_plan(reply['negotiation']['selection'])}; entregue via {st['delivered_via']}; status {st['status']}")
    print(f"        bytes idênticos aos enviados: {bulk.presenter.messages[-1].data == big_png}")
    hostile = "<h1>Relatório</h1><p onclick='roubar()'>Itens: <b>3</b></p><script>roubar()</script><img src=x onerror=roubar()>"
    reply = send_until_authorized(lambda: sender.send_html(browser.id, hostile), "HTML para o navegador")
    st = sender.message_status(reply["message_id"], wait=5)
    print(f"    HTML hostil → navegador: status {st['status']}; recebido: {browser.presenter.messages[-1].data.decode()}")

    title(5, "Adaptação ao receptor: o conteúdo é transformado, nunca descartado em silêncio")
    reply = send_until_authorized(lambda: sender.send_image(painel.id, big_png), "imagem para o painel")
    st = sender.message_status(reply["message_id"], wait=5)
    got = painel.presenter.messages[-1]
    print(f"    imagem 920 KB → painel (JPEG ≤160x120, ≤60 KB): {len(got.data)} bytes, {got.mime}; status {st['status']}")
    print(f"        operações: {got.adapted}")
    reply = send_until_authorized(lambda: sender.send_html(painel.id, "<h2>Aviso</h2><p>Reunião às <b>10h</b>.</p>"), "HTML para o painel")
    sender.message_status(reply["message_id"], wait=5)
    print(f"    HTML → painel (só texto): {painel.presenter.messages[-1].data.decode()!r} {painel.presenter.messages[-1].adapted}")

    title(6, "Fila, reentrega e recuperação")
    late = OmniLinkClient(url); late.register(KEY, python_receiver("tablet-que-estava-offline"))
    late.grant(sender.device_id, ["text"])
    reply = sender.send_text(late.device_id, "guardada na fila enquanto você estava fora")
    print(f"    receptor offline: aceito com aviso {[d['code'] for d in reply['diagnostics']]}; status = {sender.message_status(reply['message_id'])['status']}")
    tablet = Receiver.__new__(Receiver)
    tablet.client, tablet.presenter, tablet.error, tablet.stop = late, CollectorPresenter(), None, threading.Event()
    tablet.service = ReceiverService(late.device_id, python_receiver(), tablet.presenter, GrantPolicy())
    real_ack, tries = late.ack, []

    def lossy_ack(*a, **kw):                        # a primeira confirmação "se perde" na rede
        tries.append(a)
        if len(tries) == 1:
            raise ConnectionError("rede caiu")
        return real_ack(*a, **kw)

    late.ack = lossy_ack
    tablet.runner = ReceiverRunner(late, tablet.service, transport="sse", stop=tablet.stop)
    threading.Thread(target=tablet.runner.run, daemon=True).start()
    st = sender.message_status(reply["message_id"], wait=10)
    print(f"    tablet conectou: status {st['status']} após {st['attempts']} entregas; apresentou {len(tablet.presenter.messages)} vez(es) "
          f"(a reentrega foi deduplicada)")

    title(7, "Revogação: acesso encerrado imediatamente")
    admin = OmniLinkClient(url)
    admin._request("DELETE", f"/v1/admin/devices/{browser.id}", headers={"X-Enrollment-Key": KEY})
    try:
        sender.send_text(browser.id, "ainda aí?")
    except ApiError as exc:
        print(f"    envio ao dispositivo revogado: {exc.code}")
    deadline = time.monotonic() + 5
    while browser.error is None and time.monotonic() < deadline:
        time.sleep(0.1)
    print(f"    o receptor revogado encerrou seu laço com: {getattr(browser.error, 'code', browser.error)}")
    print("\nDemonstração concluída.")
    for r in (notebook, painel, bulk):
        r.stop.set()
    tablet.stop.set()
    server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
