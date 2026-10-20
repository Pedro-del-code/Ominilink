#!/usr/bin/env python3
"""Verificação pós-implantação: percorre o fluxo completo contra qualquer coordenador (local ou Render).

    export OMNILINK_ENROLLMENT_KEY=...            # valor de Environment no painel do Render
    python examples/smoke_remote.py --url https://omnilink-coordinator.onrender.com

Cria dois dispositivos temporários, verifica autenticação, consentimento, negociação, entrega (JSON e binário),
confirmação e sanitização, e SEMPRE os revoga ao final. Sai com código 0 (tudo certo) ou 1 (alguma falha).
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests  # noqa: E402
from PIL import Image  # noqa: E402

from app.capabilities.profiles import browser_receiver, python_receiver, python_sender  # noqa: E402
from app.client import ApiError, OmniLinkClient, RetryPolicy  # noqa: E402
from app.codec import get_codec  # noqa: E402


class Checks:
    def __init__(self):
        self.failures, self.count = [], 0

    def ok(self, label: str, condition: bool, detail: str = "") -> bool:
        self.count += 1
        print(f"  {'OK  ' if condition else 'FALHA'}  {label}" + (f" — {detail}" if detail else ""))
        if not condition:
            self.failures.append(label)
        return condition


def wait_for_health(url: str, timeout: float) -> dict | None:
    """Aguarda o serviço responder (instâncias gratuitas do Render levam cerca de 1 minuto para acordar)."""
    deadline, attempt = time.monotonic() + timeout, 0
    while time.monotonic() < deadline:
        attempt += 1
        try:
            resp = requests.get(url + "/health", timeout=15)
            if resp.status_code == 200:
                return resp.json()
            print(f"  … /health respondeu {resp.status_code}; aguardando (tentativa {attempt})")
        except requests.RequestException as exc:
            print(f"  … sem resposta ({type(exc).__name__}); aguardando (tentativa {attempt})")
        time.sleep(min(10, 1.5 * attempt))
    return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=os.environ.get("OMNILINK_SERVER_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--enrollment-key", default=os.environ.get("OMNILINK_ENROLLMENT_KEY"))
    parser.add_argument("--health-timeout", type=float, default=120)
    args = parser.parse_args(argv)
    if not args.enrollment_key:
        print("Defina OMNILINK_ENROLLMENT_KEY (ou use --enrollment-key).")
        return 1
    url, check, created = args.url.rstrip("/"), Checks(), []
    print(f"Verificando {url}")

    health = wait_for_health(url, args.health_timeout)
    if not check.ok("GET /health responde 200", health is not None):
        return 1
    check.ok("armazenamento", True, f"{health['store']} ({'persistente' if health['persistent'] else 'VOLÁTIL: dados se perdem ao reiniciar'})")

    retry = RetryPolicy(max_attempts=4, base_delay=1.0, max_delay=8.0)
    try:
        wrong = OmniLinkClient(url, retry=retry)
        try:
            wrong.register("chave-incorreta-incorreta", python_sender())
            check.ok("registro com chave errada é recusado", False)
        except ApiError as exc:
            check.ok("registro com chave errada é recusado", exc.status == 401, exc.code)
        check.ok("rota protegida sem token é recusada", requests.get(url + "/v1/devices", timeout=15).status_code == 401)

        sender, receiver = OmniLinkClient(url, retry=retry), OmniLinkClient(url, retry=retry)
        sender.register(args.enrollment_key, python_sender("smoke-emissor")); created.append(sender)
        receiver.register(args.enrollment_key, python_receiver("smoke-receptor")); created.append(receiver)
        check.ok("dois dispositivos registrados", True, f"{sender.device_id}, {receiver.device_id}")
        check.ok("receptor aparece na descoberta", receiver.device_id in [d["id"] for d in sender.devices()])

        try:
            sender.send_text(receiver.device_id, "sem autorização")
            check.ok("envio sem autorização é recusado", False)
        except ApiError as exc:
            check.ok("envio sem autorização é recusado", exc.code == "NOT_AUTHORIZED", exc.code)
        check.ok("nenhum conteúdo chegou antes da aprovação",
                 [i["type"] for i in receiver.fetch_inbox(wait=0)["items"]] in ([], ["grant.request"]))
        receiver.grant(sender.device_id, ["text", "html", "image"])

        neg = sender.negotiate(receiver.device_id, "image", size=900_000, width=640, height=480, mime="image/png")
        check.ok("negociação (simulação)", neg["compatible"], f"{neg['selection']['transport']} + {neg['selection']['codec']}")

        reply = sender.send_text(receiver.device_id, "olá do teste de fumaça ✓")
        item = receiver.fetch_inbox(wait=10)["items"][0]
        check.ok("texto entregue via long-poll", item.get("body") == "olá do teste de fumaça ✓")
        receiver.ack(item["id"], "displayed")
        check.ok("confirmação de recebimento chega ao emissor", sender.message_status(reply["message_id"], wait=10)["status"] == "displayed")

        reply = sender.send_html(receiver.device_id, "<p>ok</p><script>alert(1)</script>")
        item = receiver.fetch_inbox(wait=10)["items"][0]
        check.ok("HTML chega sanitizado", item.get("body") == "<p>ok</p>", repr(item.get("body")))
        receiver.ack(item["id"], "displayed")

        buf = io.BytesIO(); Image.frombytes("RGB", (200, 150), os.urandom(200 * 150 * 3)).save(buf, "PNG")
        reply = sender.send_image(receiver.device_id, buf.getvalue(), filename="teste.png")
        item = receiver.fetch_inbox(wait=10, codec="binary-frame")["items"][0]
        frame = receiver.fetch_frame(item["id"])
        msg = get_codec("binary-frame").decode(frame, max_bytes=2_000_000)
        check.ok("imagem entregue por frame binário, bytes idênticos", msg.data == buf.getvalue(), f"{len(frame)} bytes no fio")
        receiver.ack(item["id"], "displayed")
        check.ok("status final da imagem", sender.message_status(reply["message_id"], wait=10)["status"] == "displayed")
    except (ApiError, requests.RequestException) as exc:
        check.ok("fluxo completo sem exceções", False, f"{type(exc).__name__}: {exc}")
    finally:
        for client in created:
            try:
                client.revoke_self()
            except (ApiError, requests.RequestException):
                pass
        if created:
            print(f"  limpeza: {len(created)} dispositivo(s) temporário(s) revogado(s)")
    print(f"\nSMOKE TEST: {'OK' if not check.failures else 'FALHOU'} ({check.count - len(check.failures)}/{check.count} verificações)")
    return 0 if not check.failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
